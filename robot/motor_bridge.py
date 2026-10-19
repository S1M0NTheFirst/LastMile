"""Hardware bridge for a JetRacer. Runs on the Jetson itself, not in Docker.

The robot container can't reach the car's I2C boards or the CSI camera, so
with MOTOR_DRIVER=jetracer it talks to this server over HTTP instead:

    ROBOT_AUTH_TOKEN=lastmile-local-secret python3 motor_bridge.py

    POST /drive        {"throttle": -1..1, "steering": -1..1}
    POST /trim         {"offset": -0.1}, moves "straight" to fix wheels that rest off-center
    GET  /battery      {"voltage": 11.8, "percent": 78}, from the INA219 chip
    GET  /camera.jpg   the latest camera frame
    GET  /camera.mjpg  the camera as a continuous video stream (what the dashboard shows)

Uses only the standard library plus `jetracer` (and smbus / OpenCV, which the
Waveshare image ships, for the battery and camera), and works with the Jetson's
stock Python 3.6. For safety the car stops whenever no command arrives for half
a second (the robot re-sends its command ten times a second while driving), so
a crashed container or a dropped network connection never leaves it running.

If the wheels rest off-center, try values with POST /trim until they point
straight, then start the bridge with STEERING_OFFSET=<that value>:

    curl -X POST -H "Authorization: Bearer $ROBOT_AUTH_TOKEN" \\
      -d '{"offset": -0.1}' http://localhost:8090/trim

If the wheels steer the wrong way, start it with FLIP_STEERING=1; if they
spin backward on a forward command, add FLIP_THROTTLE=1; if the picture is
upside down, add FLIP_CAMERA=1.

Only one program can use the camera at a time: close any Jupyter notebook that
opened it, and if it still fails run `sudo systemctl restart nvargus-daemon`.
"""
import json
import os
import struct
import subprocess
import sys
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
HERE = os.path.dirname(os.path.abspath(__file__))
CAMERA_STREAM_FPS = int(os.getenv("CAMERA_STREAM_FPS", "12"))
CAMERA_IDLE_SECONDS = 10  # release the camera this long after the last request

car = NvidiaRacecar()
# The jetracer library scales throttle by throttle_gain (0.8 by default), so
# throttle 1 is not the motor's top speed; THROTTLE_GAIN=1 unlocks it.
if os.getenv("THROTTLE_GAIN"):
    car.throttle_gain = float(os.getenv("THROTTLE_GAIN"))
lock = threading.Lock()
last_command = 0.0
moving = False


def refresh_steering():
    """Re-send the current steering to the servo.

    The library only moves the servo when `car.steering` changes, so after
    changing steering_offset the servo would not move until the next turn.
    """
    current = car.steering
    car.steering = current + 1e-6
    car.steering = current


def set_trim(offset):
    """Set where "straight" is (the library's steering_offset) and move the servo there."""
    limit = max(0.0, 1.0 - abs(car.steering_gain))  # beyond this, full lock would exceed the servo range
    if abs(offset) > limit:
        raise ValueError("offset must be between -{0:g} and {0:g}".format(limit))
    with lock:
        car.steering_offset = offset
        refresh_steering()


if os.getenv("STEERING_OFFSET"):
    car.steering_offset = float(os.getenv("STEERING_OFFSET"))


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
    """Latest camera frame, read from a helper process (camera_worker.py).

    The helper starts on the first request and is stopped CAMERA_IDLE_SECONDS
    after the last one, so the Nano's CPU (and the camera for other programs) is
    free whenever nobody is watching. If the helper crashes, the next request
    starts a new one and this bridge keeps running.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.frame = None
        self.error = None
        self.proc = None
        self.last_request = 0.0

    def jpeg(self):
        with self.lock:
            self.last_request = time.time()
            if self.proc is None:
                self.error = None
                self.frame = None
                self.proc = subprocess.Popen(
                    [sys.executable, os.path.join(HERE, "camera_worker.py")],
                    stdout=subprocess.PIPE,
                )
                threading.Thread(target=self.read_frames, args=(self.proc,), daemon=True).start()
        deadline = time.time() + 10  # Argus takes a few seconds to start
        while time.time() < deadline:
            with self.lock:
                frame, error = self.frame, self.error
            if frame is not None:
                return frame
            if error:
                raise RuntimeError(error)
            time.sleep(0.05)
        raise RuntimeError("camera gave no frame within 10 s")

    def read_frames(self, proc):
        error = None
        try:
            while True:
                header = proc.stdout.read(4)
                if len(header) < 4:
                    break
                size = struct.unpack(">I", header)[0]
                data = proc.stdout.read(size)
                if len(data) < size:
                    break  # the helper died mid-frame
                with self.lock:
                    self.frame = data
                    if time.time() - self.last_request > CAMERA_IDLE_SECONDS:
                        return  # nobody is watching any more
        finally:
            proc.stdout.close()  # tells the helper to release the camera and exit
            try:
                code = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                code = proc.wait()
            if code == 3:
                error = ("could not open the camera; close any notebook using it, or run "
                         "`sudo systemctl restart nvargus-daemon`")
            elif code not in (0, None):
                error = "camera helper stopped (exit code {})".format(code)
            with self.lock:
                if self.proc is proc:
                    self.proc = None
                    self.frame = None
                    if error and not self.idle_stop():
                        self.error = error
                        print("camera: {}".format(error), flush=True)

    def idle_stop(self):
        return time.time() - self.last_request > CAMERA_IDLE_SECONDS


camera = Camera()


class Handler(BaseHTTPRequestHandler):
    def authorized(self):
        if TOKEN and self.headers.get("Authorization") != "Bearer " + TOKEN:
            self.reply(401, {"error": "invalid robot token"})
            return False
        return True

    def stream_camera(self):
        """Send frames one after another until the viewer disconnects."""
        try:
            data = camera.jpeg()  # fails before any headers go out if the camera is broken
        except Exception as exc:
            return self.reply(503, {"error": "{}: {}".format(type(exc).__name__, exc)})
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                self.wfile.write(
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n"
                )
                self.wfile.flush()
                time.sleep(1.0 / CAMERA_STREAM_FPS)
                data = camera.jpeg()
        except (OSError, RuntimeError):
            pass  # viewer left, or the camera stopped

    def do_GET(self):
        if self.path not in ("/battery", "/camera.jpg", "/camera.mjpg"):
            return self.reply(404, {"error": "not found"})
        if not self.authorized():
            return
        if self.path == "/camera.mjpg":
            return self.stream_camera()
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
        if self.path not in ("/drive", "/trim"):
            return self.reply(404, {"error": "not found"})
        if not self.authorized():
            return
        if self.path == "/trim":
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
                set_trim(float(body["offset"]))
            except (ValueError, KeyError, TypeError) as exc:
                return self.reply(400, {"error": 'send {"offset": a small number such as -0.1}: ' + str(exc)})
            return self.reply(200, {"steering_offset": car.steering_offset})
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
    refresh_steering()  # put the wheels at "straight" now, with the offset applied
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
