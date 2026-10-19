"""Start robots on this machine and join them to the fleet.

    python join.py --registry 192.168.1.132:3000 --count 20
    python join.py --registry 192.168.1.132:3000 --count 5     # add 5 more
    python join.py --registry 192.168.1.132:3000 --at "Engineering Technology; Vivian Engineering Center"
    python join.py --stop                                       # remove this machine's robots

The registry is the simulator (`npm run dev`) on whichever machine hosts it.
Robots get their names from it first-come-first-served, so any number of
machines can join without naming conflicts. Works on macOS, Windows and Linux;
only needs Python 3 and Docker.
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import urllib.request

IMAGE = "lastmile"
LABEL = "lastmile.robot"
ROBOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "robot")
ROBOT_AUTH_TOKEN = os.getenv("ROBOT_AUTH_TOKEN", "")


def docker(*args, capture=False):
    result = subprocess.run(
        ["docker", *args],
        check=True,
        universal_newlines=True,  # same as text=True, which needs Python 3.7+ (Jetson Nano has 3.6)
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout if capture else None


def lan_ip_towards(registry_host):
    """The local IP this machine uses to reach the registry (no traffic is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((registry_host, 9))
        return s.getsockname()[0]
    finally:
        s.close()


def used_ports():
    out = docker("ps", "-a", "--filter", f"label={LABEL}",
                 "--format", '{{.Label "lastmile.port"}}', capture=True)
    return {int(p) for p in out.split() if p.isdigit()}


def port_is_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def free_ports(start, count):
    taken = used_ports()
    ports, port = [], start
    while len(ports) < count:
        if port > 65535:
            sys.exit("ran out of ports")
        if port not in taken and port_is_free(port):
            ports.append(port)
        port += 1
    return ports


def check_registry(registry):
    """Fail early if this machine can't reach the simulator's registry."""
    url = f"http://{registry}/api/registry"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            r.read()
    except OSError as e:
        sys.exit(
            f"Can't reach the registry at {url}\n  ({e})\n"
            "Is the simulator (npm run dev) running there, and are both machines on a\n"
            "network that allows device-to-device traffic? Campus/guest Wi-Fi often\n"
            "blocks it; try a phone hotspot or Tailscale."
        )


def parse_spots(text):
    """'Engineering Technology; 25,10; ...' -> [(x, y), ...] on the 0-100 campus grid."""
    buildings = None
    spots = []
    for item in filter(None, (part.strip() for part in text.split(";"))):
        try:
            x, y = (float(n) for n in item.split(","))
        except ValueError:
            if buildings is None:
                path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orchestrator", "campus_buildings.json")
                with open(path) as f:
                    buildings = {name.lower(): spot for name, spot in json.load(f).items()}
            matches = [n for n in buildings if item.lower() == n or item.lower() in n]
            exact = [n for n in matches if n == item.lower()]
            if len(exact or matches) != 1:
                sys.exit(f"--at: {item!r} is not x,y and matches {len(matches)} buildings; "
                         f"names: {', '.join(buildings)}")
            spot = buildings[(exact or matches)[0]]
            x, y = spot["x"], spot["y"]
        if not (0 <= x <= 100 and 0 <= y <= 100):
            sys.exit(f"--at: {item!r} is outside the 0-100 map")
        spots.append((x, y))
    return spots


def start(args):
    spots = parse_spots(args.at) if args.at else []
    count = args.count if args.count is not None else (len(spots) or 5)
    if spots and len(spots) != count:
        sys.exit(f"--at lists {len(spots)} spots but --count is {count}")
    check_registry(args.registry)
    registry_host = args.registry.rsplit(":", 1)[0]
    host_ip = args.host_ip or lan_ip_towards(registry_host)

    print(f"Building {IMAGE} image...", flush=True)
    docker("build", "-q", "-t", IMAGE, ROBOT_DIR, capture=True)

    for index, port in enumerate(free_ports(args.port_start, count)):
        run_args = [
            "run", "-d", "--restart", "unless-stopped",
            "--name", f"lastmile-{port}",
            "-p", f"{port}:8000",
            "--label", LABEL,
            "--label", f"lastmile.host={host_ip}",
            "--label", f"lastmile.port={port}",
            "-e", f"REGISTRY={args.registry}",
            "-e", f"HOST_IP={host_ip}",
            "-e", f"HOST_PORT={port}",
        ]
        if ROBOT_AUTH_TOKEN:
            run_args += ["-e", f"ROBOT_AUTH_TOKEN={ROBOT_AUTH_TOKEN}"]
        if spots:
            run_args += ["-e", f"START_X={spots[index][0]}", "-e", f"START_Y={spots[index][1]}"]
        if args.motor_driver:
            run_args += ["-e", f"MOTOR_DRIVER={args.motor_driver}"]
        # Drive calibration (see robot/app.py), passed through when set in the shell.
        for name in ("DRIVE_SPEED", "TURN_SPEED", "MOTOR_POWER", "MAX_MOTOR_POWER", "MAX_DRIVE_METERS", "START_SPACE", "START_DOOR"):
            if os.getenv(name):
                run_args += ["-e", f"{name}={os.getenv(name)}"]
        run_args += [IMAGE]
        docker(*run_args, capture=True)
        print(f"started robot on {host_ip}:{port} (container lastmile-{port})")

    print(f"\n{count} robot(s) joining via registry {args.registry}. "
          f"Names appear in the simulator within a few seconds.")


def stop():
    ids = docker("ps", "-aq", "--filter", f"label={LABEL}", capture=True).split()
    if ids:
        docker("rm", "-f", *ids, capture=True)
    print(f"removed {len(ids)} robot container(s) from this machine")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--registry", help="simulator address, e.g. 192.168.1.132:3000")
    p.add_argument("--count", type=int, help="robots to start (default 5, or one per --at spot)")
    p.add_argument("--at", help='where each robot starts on the campus map, ";"-separated building names or x,y '
                                '(e.g. "Engineering Technology; 25,10"); default: random')
    p.add_argument("--host-ip", help="this machine's LAN IP (auto-detected if omitted)")
    p.add_argument("--port-start", type=int, default=8000, help="first host port to try (default 8000)")
    p.add_argument("--motor-driver", help="drive the real car, e.g. jetracer (needs robot/motor_bridge.py running; default: map only)")
    p.add_argument("--stop", action="store_true", help="remove all robots on this machine")
    args = p.parse_args()

    if args.stop:
        stop()
    elif args.registry:
        start(args)
    else:
        p.error("--registry is required (or use --stop)")


if __name__ == "__main__":
    main()
