"""Car output for a physical robot (a JetRacer on a Jetson Nano).

The robot calls set_drive(throttle, steering): throttle from -1 (full reverse)
to 1 (full forward), steering from -1 (full left) to 1 (full right);
set_drive(0, 0) stops. MOTOR_DRIVER picks the backend:

- "sim" (default) drives nothing, so the robot only moves on the dashboard map.
- "jetracer" sends each command to robot/motor_bridge.py, which runs on the
  Jetson itself (outside Docker) and drives the car.

To support other hardware, add a branch below and list its name in DRIVERS.
"""
import os

import httpx

DRIVER = os.getenv("MOTOR_DRIVER", "sim")
DRIVERS = ("sim", "jetracer")
# The container reaches the bridge through the Jetson's LAN address.
BRIDGE_URL = os.getenv("MOTOR_BRIDGE_URL") or f"http://{os.getenv('HOST_IP', '127.0.0.1')}:8090"


def check_driver():
    """Fail at startup rather than mid-drive if MOTOR_DRIVER is unknown."""
    if DRIVER not in DRIVERS:
        raise RuntimeError(
            f"MOTOR_DRIVER={DRIVER!r} is not implemented in robot/motors.py "
            f"(available: {', '.join(DRIVERS)})"
        )


def set_drive(throttle: float, steering: float):
    if DRIVER == "sim":
        return
    token = os.getenv("ROBOT_AUTH_TOKEN", "")
    try:
        response = httpx.post(
            f"{BRIDGE_URL}/drive",
            json={"throttle": throttle, "steering": steering},
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=0.5,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"motor bridge at {BRIDGE_URL} failed ({exc!r}); is motor_bridge.py running on the Jetson?"
        ) from exc


async def bridge_get(path: str, timeout: float = 6.0) -> httpx.Response:
    """GET /battery or /camera.jpg from the bridge; RuntimeError when unavailable."""
    if DRIVER == "sim":
        raise RuntimeError("this robot has no hardware (MOTOR_DRIVER=sim)")
    token = os.getenv("ROBOT_AUTH_TOKEN", "")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                f"{BRIDGE_URL}{path}",
                headers={"Authorization": f"Bearer {token}"} if token else {},
            )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"motor bridge at {BRIDGE_URL} unreachable ({exc!r})") from exc
    if response.status_code != 200:
        try:
            detail = response.json().get("error", response.text)
        except ValueError:
            detail = response.text
        raise RuntimeError(f"motor bridge {path}: {detail}")
    return response
