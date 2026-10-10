#!/usr/bin/env python3
"""JetRacer node for the LastMile simulator, with optional keyboard driving.

Registers with the simulator's registry and answers /state, so the car shows up
in the front end as a `remote` robot. Standard library only, so it runs on the
Python 3.6 that ships with the JetPack 4.5 JetRacer image.

    python3 jetracer_node.py --registry 192.168.1.50:3000            # show up only
    python3 jetracer_node.py --registry 192.168.1.50:3000 --drive    # + keyboard driving

With --drive, open http://<car-ip>:<port>/ in a browser on your laptop and
drive with W/A/S/D or the arrow keys. Stop any Jupyter notebook that also uses
the car first, and close the camera notebooks (they hold the camera/motors).
"""
import argparse
import json
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

state = {
    "id": None,            # assigned by the registry (robot0, robot1, ...)
    "type": "car",         # extra field; the UI ignores it for now
    "power": "on",
    "status": "alive",     # alive | dead | no_response
    "battery": 100.0,      # 0-100
    "position": {"x": 50.0, "y": 50.0},
    "drive": {"steering": 0.0, "throttle": 0.0},
    "inbox": [],
}

car = None                 # NvidiaRacecar, only created with --drive
last_cmd = 0.0             # time of the last /drive command (watchdog)
MAX_THROTTLE = 0.5        # keyboard "full throttle" maps to this
STEER_SIGN = 1.0           # flip to -1 (--steer-sign -1) if left/right are reversed
WATCHDOG_S = 0.4           # no command for this long => stop the car

PAGE = r"""<!doctype html>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>JetRacer keys</title>
<style>
body{font-family:sans-serif;background:#0f1115;color:#eee;text-align:center;padding-top:40px}
kbd{background:#333;padding:4px 10px;border-radius:4px}
#s{font-size:1.4em;margin:24px}
</style>
<h2>JetRacer keyboard control</h2>
<p><kbd>W</kbd>/<kbd>&uarr;</kbd> forward &nbsp; <kbd>S</kbd>/<kbd>&darr;</kbd> reverse &nbsp;
<kbd>A</kbd>/<kbd>&larr;</kbd> left &nbsp; <kbd>D</kbd>/<kbd>&rarr;</kbd> right &nbsp;
<kbd>Space</kbd> stop</p>
<p>Keep this tab focused. The car stops if you switch tabs or close this page.</p>
<div id="s">stopped</div>
<script>
const keys = {w:'f', arrowup:'f', s:'b', arrowdown:'b', a:'l', arrowleft:'l', d:'r', arrowright:'r'};
const down = new Set();
function cmd() {
  return {
    throttle: (down.has('f') ? 1 : 0) - (down.has('b') ? 1 : 0),
    steering: (down.has('l') ? 1 : 0) - (down.has('r') ? 1 : 0)
  };
}
addEventListener('keydown', e => {
  const k = e.key.toLowerCase();
  if (k === ' ') { down.clear(); e.preventDefault(); return; }
  if (keys[k]) { down.add(keys[k]); e.preventDefault(); }
});
addEventListener('keyup', e => { const k = keys[e.key.toLowerCase()]; if (k) down.delete(k); });
addEventListener('blur', () => down.clear());
setInterval(() => {
  const c = cmd();
  fetch('/drive', {method: 'POST', body: JSON.stringify(c)})
    .then(r => r.json())
    .then(j => { document.getElementById('s').textContent =
        j.error ? j.error : 'throttle ' + j.throttle.toFixed(2) + '  steering ' + j.steering.toFixed(2); })
    .catch(() => { document.getElementById('s').textContent = 'no connection to car'; });
}, 100);
</script>
"""


def clamp(v, lo=-1.0, hi=1.0):
    return max(lo, min(hi, float(v)))


def apply_drive(steering, throttle):
    """Send a command to the motors. A dead / no_response car refuses to move."""
    if car is None:
        return
    if state["status"] != "alive":
        steering = throttle = 0.0
    steering = clamp(steering) * STEER_SIGN
    throttle = clamp(throttle) * MAX_THROTTLE
    car.steering = steering
    car.throttle = throttle
    state["drive"] = {"steering": steering, "throttle": throttle}


def watchdog_loop():
    """If commands stop arriving (tab closed, WiFi drop), stop the car."""
    while True:
        time.sleep(0.1)
        if car is not None and time.time() - last_cmd > WATCHDOG_S:
            if state["drive"]["throttle"] != 0.0 or state["drive"]["steering"] != 0.0:
                apply_drive(0.0, 0.0)


def detect_ip(registry):
    """LAN IP of the interface that can reach the registry."""
    host = registry.split(":")[0]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 1))
        return s.getsockname()[0]
    finally:
        s.close()


def register_loop(registry, host_ip, port):
    """Registering doubles as the heartbeat; the simulator drops us after 10s of silence."""
    failing = False
    while True:
        try:
            body = json.dumps({"host": host_ip, "port": port, "name": state["id"]}).encode()
            req = urllib.request.Request(
                "http://%s/api/registry" % registry,
                data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                name = json.loads(r.read().decode())["name"]
            if name != state["id"]:
                print("registered with %s as %s" % (registry, name), flush=True)
            state["id"] = name
            failing = False
        except Exception as e:
            if not failing:
                print("can't register with %s: %r; retrying" % (registry, e), flush=True)
            failing = True
        time.sleep(3 if state["id"] else 1)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj, ctype="application/json"):
        data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json_body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n).decode() or "{}")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/state":
            self._send(200, state)
        elif self.path == "/heartbeat":
            if state["status"] != "alive":
                self._send(503, {"detail": "%s not responsive" % state["id"]})
            else:
                self._send(200, {"id": state["id"], "ts": time.time(), "battery": state["battery"]})
        else:
            self._send(404, {"detail": "not found"})

    def do_POST(self):
        global last_cmd
        body = self._json_body()
        if self.path == "/drive":
            if car is None:
                return self._send(400, {"error": "start the node with --drive"})
            last_cmd = time.time()
            apply_drive(body.get("steering", 0.0), body.get("throttle", 0.0))
            self._send(200, state["drive"])
        elif self.path == "/control":
            status = body.get("status")
            if status is not None:
                if status not in ("alive", "dead", "no_response"):
                    return self._send(400, {"detail": "invalid status"})
                state["status"] = status
                if status != "alive":
                    apply_drive(0.0, 0.0)
            if body.get("battery") is not None:
                state["battery"] = max(0.0, min(100.0, float(body["battery"])))
            self._send(200, state)
        elif self.path == "/message":
            if state["status"] != "alive":
                return self._send(503, {"detail": "%s dropped message" % state["id"]})
            state["inbox"].append({"from": body.get("sender"), "body": body.get("body"), "ts": time.time()})
            self._send(200, {"accepted": True})
        else:
            self._send(404, {"detail": "not found"})

    def log_message(self, *args):  # keep the console quiet (UI polls every 2s)
        pass


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    global car, MAX_THROTTLE, STEER_SIGN
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", required=True, help="simulator address, e.g. 192.168.1.50:3000")
    ap.add_argument("--host-ip", help="this car's IP as seen by the simulator (auto-detected)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--x", type=float, default=50.0)
    ap.add_argument("--y", type=float, default=50.0)
    ap.add_argument("--drive", action="store_true", help="enable keyboard driving (needs the jetracer package)")
    ap.add_argument("--max-throttle", type=float, default=MAX_THROTTLE, help="top speed, 0 to 1 (default 0.25)")
    ap.add_argument("--steering-offset", type=float, help="trim so the car drives straight (the wiki's example uses 0.18)")
    ap.add_argument("--steer-sign", type=float, default=1.0, choices=[1.0, -1.0], help="use -1 if left/right are reversed")
    args = ap.parse_args()

    MAX_THROTTLE = clamp(args.max_throttle, 0.0, 1.0)
    STEER_SIGN = args.steer_sign

    if args.drive:
        from jetracer.nvidia_racecar import NvidiaRacecar
        car = NvidiaRacecar()
        if args.steering_offset is not None:
            car.steering_offset = args.steering_offset
        car.steering = 0.0
        car.throttle = 0.0
        threading.Thread(target=watchdog_loop, daemon=True).start()

    host_ip = args.host_ip or detect_ip(args.registry)
    state["position"] = {"x": args.x, "y": args.y}
    print("car at %s:%d, registry %s" % (host_ip, args.port, args.registry), flush=True)
    if car is not None:
        print("keyboard control: http://%s:%d/  (max throttle %.2f)" % (host_ip, args.port, MAX_THROTTLE), flush=True)

    threading.Thread(target=register_loop, args=(args.registry, host_ip, args.port), daemon=True).start()
    try:
        ThreadedServer(("0.0.0.0", args.port), Handler).serve_forever()
    finally:
        if car is not None:
            car.throttle = 0.0
            car.steering = 0.0


if __name__ == "__main__":
    main()
