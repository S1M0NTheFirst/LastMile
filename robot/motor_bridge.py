"""Hardware bridge for a JetRacer. Runs on the Jetson itself, not in Docker.

The robot container can't reach the car's I2C boards or the CSI camera, so
with MOTOR_DRIVER=jetracer it talks to this server over HTTP instead:

    ROBOT_AUTH_TOKEN=lastmile-local-secret python3 motor_bridge.py

    POST /drive        {"throttle": -1..1, "steering": -1..1}
    GET  /battery      {"voltage": 11.8, "percent": 78}, from the INA219 chip
    GET  /camera.jpg   the latest camera frame

Uses only the standard library plus `jetracer` (and smbus / OpenCV, which the
Waveshare image ships, for the battery and camera), and works with the Jetson's
stock Python 3.6. For safety the car stops whenever no command arrives for half
a second (the robot re-sends its command ten times a second while driving), so
a crashed container or a dropped network connection never leaves it running.

If the wheels steer the wrong way, start it with FLIP_STEERING=1; if they
spin backward on a forward command, add FLIP_THROTTLE=1; if the picture is
upside down, add FLIP_CAMERA=1.

Only one program can use the camera at a time: close any Jupyter notebook that
opened it, and if it still fails run `sudo systemctl restart nvargus-daemon`.
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

from jetracer.nvidia_racecar import NvidiaRacecar

PORT = int(os.getenv("MOTOR_BRIDGE_PORT", "8090"))
TOKEN = os.getenv("ROBOT_AUTH_TOKEN", "")
STEERING_SIGN = -1.0 if os.getenv("FLIP_STEERING") else 1.0
THROTTLE_SIGN = -1.0 if os.getenv("FLIP_THROTTLE") else 1.0
WATCHDOG_SECONDS = 0.5
# Waveshare JetRacer battery monitor: INA219 at 0x41 on I2C bus 1, three 18650
# cells in series (12.6 V full, about 9 V empty).
INA219_BUS = int(os.getenv("INA219_BUS", "1"))
INA219_ADDR = int(os.getenv("INA219_ADDR", "0x41"), 16)
BATTERY_FULL_V = float(os.getenv("BATTERY_FULL_V", "12.6"))
BATTERY_EMPTY_V = float(os.getenv("BATTERY_EMPTY_V", "9.0"))
CAMERA_WIDTH, CAMERA_HEIGHT = 640, 360
CAMERA_FLIP = 2 if os.getenv("FLIP_CAMERA") else 0  # nvvidconv flip-method 2 = rotate 180

car = NvidiaRacecar()
# The jetracer library scales throttle by throttle_gain (0.8 by default), so
# throttle 1 is not the motor's top speed; THROTTLE_GAIN=1 unlocks it.
if os.getenv("THROTTLE_GAIN"):
    car.throttle_gain = float(os.getenv("THROTTLE_GAIN"))
lock = threading.Lock()
last_command = 0.0
moving = False


def set_drive(throttle, steering):
    global last_command, moving
    with lock:
        car.steering = STEERING_SIGN * steering
        car.throttle = THROTTLE_SIGN * throttle
        last_command = time.time()
        moving = bool(throttle)


def watchdog():
    while True:
        time.sleep(0.1)
        with lock:
            stale = moving and time.time() - last_command > WATCHDOG_SECONDS
        if stale:
            set_drive(0.0, 0.0)
            print("watchdog: no command for {}s, car stopped".format(WATCHDOG_SECONDS), flush=True)


def read_battery():
    """Battery voltage from the INA219 bus-voltage register, as volts and percent."""
    try:
        import smbus
    except ImportError:
        import smbus2 as smbus
    bus = smbus.SMBus(INA219_BUS)
    try:
        msb, lsb = bus.read_i2c_block_data(INA219_ADDR, 0x02, 2)
    finally:
        bus.close()
    voltage = ((msb << 8 | lsb) >> 3) * 0.004  # register holds 4 mV steps above bit 3
    percent = (voltage - BATTERY_EMPTY_V) / (BATTERY_FULL_V - BATTERY_EMPTY_V) * 100
    return {"voltage": round(voltage, 2), "percent": round(max(0.0, min(100.0, percent)), 1)}


class Camera:
    """Keeps reading the CSI camera in the background so a frame is always fresh.

    Opened on the first request, so the bridge also runs without a camera.
    """
    PIPELINE = (
        "nvarguscamerasrc ! video/x-raw(memory:NVMM), width=1280, height=720, framerate=30/1 ! "
        "nvvidconv flip-method={flip} ! video/x-raw, width={w}, height={h}, format=BGRx ! "
        "videoconvert ! video/x-raw, format=BGR ! appsink drop=true max-buffers=1"
    )

    def __init__(self):
        self.lock = threading.Lock()
        self.frame = None
        self.error = None
        self.started = False

    def jpeg(self):
        with self.lock:
            if not self.started:
                self.started = True
                self.error = None
                threading.Thread(target=self.run, daemon=True).start()
        deadline = time.time() + 5  # the camera takes a few seconds to start
        while time.time() < deadline:
            with self.lock:
                frame, error = self.frame, self.error
            if error:
                raise RuntimeError(error)
            if frame is not None:
                import cv2
                ok, data = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if not ok:
                    raise RuntimeError("could not encode the camera frame")
                return data.tobytes()
            time.sleep(0.05)
        raise RuntimeError("camera gave no frame within 5 s")

    def run(self):
        try:
            import cv2
            cap = cv2.VideoCapture(
                self.PIPELINE.format(flip=CAMERA_FLIP, w=CAMERA_WIDTH, h=CAMERA_HEIGHT),
                cv2.CAP_GSTREAMER,
            )
            if not cap.isOpened():
                raise RuntimeError(
                    "could not open the camera; close any notebook using it, or run "
                    "`sudo systemctl restart nvargus-daemon`"
                )
            while True:
                ok, frame = cap.read()
                if not ok:
                    raise RuntimeError("camera stopped delivering frames")
                with self.lock:
                    self.frame = frame
        except Exception as exc:
            print("camera: {}".format(exc), flush=True)
            with self.lock:
                self.error = str(exc)
                self.frame = None
                self.started = False  # the next request tries again


camera = Camera()


class Handler(BaseHTTPRequestHandler):
    def authorized(self):
        if TOKEN and self.headers.get("Authorization") != "Bearer " + TOKEN:
            self.reply(401, {"error": "invalid robot token"})
            return False
        return True

    def do_GET(self):
        if self.path not in ("/battery", "/camera.jpg"):
            return self.reply(404, {"error": "not found"})
        if not self.authorized():
            return
        try:
            if self.path == "/battery":
                return self.reply(200, read_battery())
            data = camera.jpeg()
        except Exception as exc:
            return self.reply(503, {"error": "{}: {}".format(type(exc).__name__, exc)})
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path != "/drive":
            return self.reply(404, {"error": "not found"})
        if not self.authorized():
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
            throttle = max(-1.0, min(1.0, float(body["throttle"])))
            steering = max(-1.0, min(1.0, float(body.get("steering", 0))))
        except (ValueError, KeyError, TypeError):
            return self.reply(400, {"error": 'send {"throttle": -1..1, "steering": -1..1}'})
        set_drive(throttle, steering)
        self.reply(200, {"throttle": throttle, "steering": steering})

    def reply(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # the robot sends ten commands a second while driving


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    set_drive(0.0, 0.0)
    threading.Thread(target=watchdog, daemon=True).start()
    try:
        print("battery: {voltage} V ({percent}%)".format(**read_battery()), flush=True)
    except Exception as exc:
        print("battery: not readable ({}); /battery will report errors".format(exc), flush=True)
    print("motor bridge listening on port {}".format(PORT), flush=True)
    try:
        Server(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        car.throttle = 0.0
        car.steering = 0.0
