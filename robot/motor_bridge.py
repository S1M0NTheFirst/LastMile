"""Motor bridge for a JetRacer. Runs on the Jetson itself, not in Docker.

The robot container can't use the jetracer library or its I2C servo board, so
with MOTOR_DRIVER=jetracer it sends drive commands here over HTTP instead:

    ROBOT_AUTH_TOKEN=lastmile-local-secret python3 motor_bridge.py

Uses only the standard library plus `jetracer`, and works with the Jetson's
stock Python 3.6. For safety the car stops whenever no command arrives for half
a second (the robot re-sends its command ten times a second while driving), so
a crashed container or a dropped network connection never leaves it running.

If the wheels steer the wrong way, start it with FLIP_STEERING=1; if they
spin backward on a forward command, add FLIP_THROTTLE=1.
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

car = NvidiaRacecar()
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


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/drive":
            return self.reply(404, {"error": "not found"})
        if TOKEN and self.headers.get("Authorization") != "Bearer " + TOKEN:
            return self.reply(401, {"error": "invalid robot token"})
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
    print("motor bridge listening on port {}".format(PORT), flush=True)
    try:
        Server(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        car.throttle = 0.0
        car.steering = 0.0
