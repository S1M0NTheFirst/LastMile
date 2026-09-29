"""Start robots on this machine and join them to the fleet.

    python join.py --registry 192.168.1.132:3000 --count 20
    python join.py --registry 192.168.1.132:3000 --count 5     # add 5 more
    python join.py --stop                                       # remove this machine's robots

The registry is the simulator (`npm run dev`) on whichever machine hosts it.
Robots get their names from it first-come-first-served, so any number of
machines can join without naming conflicts. Works on macOS, Windows and Linux;
only needs Python 3 and Docker.
"""
import argparse
import os
import socket
import subprocess
import sys

IMAGE = "lastmile"
LABEL = "lastmile.robot"
ROBOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "robot")


def docker(*args, capture=False):
    result = subprocess.run(
        ["docker", *args],
        check=True,
        text=True,
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


def start(args):
    registry_host = args.registry.rsplit(":", 1)[0]
    host_ip = args.host_ip or lan_ip_towards(registry_host)

    print(f"Building {IMAGE} image...", flush=True)
    docker("build", "-q", "-t", IMAGE, ROBOT_DIR, capture=True)

    for port in free_ports(args.port_start, args.count):
        docker(
            "run", "-d", "--restart", "unless-stopped",
            "--name", f"lastmile-{port}",
            "-p", f"{port}:8000",
            "--label", LABEL,
            "--label", f"lastmile.host={host_ip}",
            "--label", f"lastmile.port={port}",
            "-e", f"REGISTRY={args.registry}",
            "-e", f"HOST_IP={host_ip}",
            "-e", f"HOST_PORT={port}",
            IMAGE,
            capture=True,
        )
        print(f"started robot on {host_ip}:{port}")

    print(f"\n{args.count} robot(s) joining via registry {args.registry}. "
          f"Names appear in the simulator within a few seconds.")


def stop():
    ids = docker("ps", "-aq", "--filter", f"label={LABEL}", capture=True).split()
    if ids:
        docker("rm", "-f", *ids, capture=True)
    print(f"removed {len(ids)} robot container(s) from this machine")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--registry", help="simulator address, e.g. 192.168.1.132:3000")
    p.add_argument("--count", type=int, default=5, help="robots to start (default 5)")
    p.add_argument("--host-ip", help="this machine's LAN IP (auto-detected if omitted)")
    p.add_argument("--port-start", type=int, default=8000, help="first host port to try (default 8000)")
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
