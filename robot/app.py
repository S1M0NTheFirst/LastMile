"""Single robot node.

One image, run as any number of containers on any number of machines. Each
robot registers with a central registry (the simulator), which assigns its
name first-come-first-served (robot0, robot1, ...) and tells robots where
every other robot is reachable, so any robot can message any other.
"""
import os
import asyncio
import copy
import math
import random
import time
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Header, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from navigation import DOORS, ELEVATOR, FLOOR, Navigator

import motors

# Registry address, e.g. "192.168.1.132:3000" (the machine running the simulator).
REGISTRY = os.getenv("REGISTRY", "")
# Where other machines can reach this robot: the host's LAN IP + published port.
HOST_IP = os.getenv("HOST_IP", "")
HOST_PORT = int(os.getenv("HOST_PORT", "0"))
# Simulated robots drain this many battery points per tick (1 tick/sec); a
# real car (MOTOR_DRIVER=jetracer) reports its measured battery instead.
DRAIN_RATE = float(os.getenv("DRAIN_RATE", "0.1"))
LOW_BATTERY_THRESHOLD = float(os.getenv("LOW_BATTERY_THRESHOLD", "15"))
# Drive calibration: how fast the car goes at MOTOR_POWER throttle. Measure
# these on the real car so "forward 1 m" and "left 90" come out right.
DRIVE_SPEED = float(os.getenv("DRIVE_SPEED", "0.3"))   # meters per second
TURN_SPEED = float(os.getenv("TURN_SPEED", "45"))      # degrees per second at full steering lock
MOTOR_POWER = float(os.getenv("MOTOR_POWER", "0.3"))   # throttle 0-1 used for drive steps
# A step may ask for its own throttle ("speed", 0-1, 1 = full speed), capped
# here. DRIVE_SPEED and TURN_SPEED are measured at MOTOR_POWER and assumed to
# scale with throttle, so distances are only approximate at other speeds.
MAX_MOTOR_POWER = float(os.getenv("MAX_MOTOR_POWER", "1.0"))
# Largest amount one drive step may ask for: degrees for turns, and meters for
# straight runs only when MAX_DRIVE_METERS is set (no distance cap by default).
MAX_DRIVE_METERS = float(os.getenv("MAX_DRIVE_METERS", "inf"))
DRIVE_LIMITS = {"forward": MAX_DRIVE_METERS, "backward": MAX_DRIVE_METERS, "left": 360.0, "right": 360.0}
METERS_PER_UNIT = 2.0  # the dashboard's 0-100 grid spans 200 m
# Where the measured 4th floor's building (ECS) sits on the 0-100 campus grid;
# the dashboard centers the campus map on it.
FLOOR4_BUILDING = {"x": 50.0, "y": 50.0}
# Simulated robots start outside on the campus; a real car starts on the 4th
# floor at the elevator, since that is the only map it can drive.
# START_X/START_Y put a simulated robot at a chosen campus spot (default: random);
# START_DOOR (e.g. 411-1) starts a robot on the 4th floor at that door instead of the elevator.
START_DOOR = os.getenv("START_DOOR", "")
START_SPACE = os.getenv("START_SPACE", "campus" if motors.DRIVER == "sim" and not START_DOOR else "floor4")
# Visual-only campus travel: how fast a simulated robot crosses the map, in meters per
# second. Slow on purpose so the dashboard can show it following the roads.
CAMPUS_SPEED = float(os.getenv("CAMPUS_SPEED", "3"))

app = FastAPI(title="Robot")


def make_navigator() -> Navigator:
    """Simulated robots use the floor's own speeds; a real car integrates the
    route at its drive calibration (step_rate) so map timing matches the motors."""
    if motors.DRIVER == "sim":
        return Navigator()
    return Navigator(
        speed=step_rate({"action": "forward", "speed": MOTOR_POWER}),
        turn_speed=math.radians(step_rate({"action": "left", "speed": MOTOR_POWER})),
        power=MOTOR_POWER,
    )


def stop_motors():
    try:
        motors.set_drive(0, 0)
    except RuntimeError:
        pass  # bridge unreachable: its watchdog stops the car


state = {
    "id": None,             # assigned by the registry
    "power": "on",          # on | off  (off => container stopped, handled by orchestrator)
    "status": "alive",      # alive | dead | no_response
    "mode": "available",    # available | returning_to_charge | unavailable
    "accepting_tasks": True,
    "battery": 100.0,       # 0-100
    "battery_voltage": None,  # volts, real cars only
    "camera": motors.DRIVER != "sim",  # real cars stream /camera
    "driver": motors.DRIVER,        # sim | jetracer (a real car can't leave its floor)
    "position": {"x": round(float(os.getenv("START_X") or random.uniform(0, 100)), 1),
                 "y": round(float(os.getenv("START_Y") or random.uniform(0, 100)), 1)},
    "heading": 90.0,        # degrees, counter-clockwise from east; 90 = facing north (+y)
    "inbox": [],            # messages received from peers
    "space": START_SPACE,   # campus (0-100 grid, visual only) | floor4 (measured meters)
    "leg": None,            # campus trip in progress: {"from", "to", "progress"}; lets the dashboard follow roads
    "pose": None,           # authoritative floor-local meters and heading
    "navigation": None,
    "floor_version": "measured-v1",
}
tasks: dict[str, dict] = {}
task_workers: dict[str, asyncio.Task] = {}
low_battery_notified = False
# peer name -> last known healthy (True/False/None); None means not probed yet
peer_health: dict[str, bool | None] = {}


def responsive() -> bool:
    """dead / no_response robots drop everything except control endpoints."""
    return state["status"] == "alive"


async def registry_robots() -> list[dict]:
    async with httpx.AsyncClient(timeout=3.0) as client:
        r = await client.get(f"http://{REGISTRY}/api/registry")
        return r.json()["robots"]


async def notify_peers(event: str, details: dict):
    """Send an automatic event to every other registered robot."""
    if not state["id"]:
        return
    body = {"event": event, "robot": state["id"], **details}
    try:
        robots = await registry_robots()
    except (httpx.HTTPError, KeyError, ValueError):
        return
    async with httpx.AsyncClient(timeout=3.0) as client:
        for peer in robots:
            if peer["name"] == state["id"]:
                continue
            try:
                await client.post(
                    f"http://{peer['host']}:{peer['port']}/message",
                    json={"sender": state["id"], "body": body},
                )
            except httpx.HTTPError:
                # A peer may be powered off or unreachable; the local robot
                # continues operating and can retry through later policies.
                pass


async def read_real_battery():
    """Update the battery from the car's sensor; keeps the last value on failure."""
    try:
        reading = (await motors.bridge_get("/battery", timeout=2.0)).json()
    except (RuntimeError, ValueError) as exc:
        print(f"battery read failed: {exc}", flush=True)
        return
    state["battery"] = float(reading["percent"])
    state["battery_voltage"] = reading["voltage"]


async def battery_loop():
    global low_battery_notified
    while True:
        await asyncio.sleep(1)
        if state["status"] != "dead" and state["battery"] > 0:
            if motors.DRIVER == "sim":
                state["battery"] = max(0.0, state["battery"] - DRAIN_RATE)
            else:
                await read_real_battery()
                # Voltage sags while driving, so take tasks again once a
                # rested battery reads comfortably above the threshold.
                if low_battery_notified and state["battery"] > LOW_BATTERY_THRESHOLD + 5:
                    low_battery_notified = False
                    if state["mode"] == "returning_to_charge":
                        state["mode"] = "available"
                        state["accepting_tasks"] = True
            if (
                not low_battery_notified
                and 0 < state["battery"] < LOW_BATTERY_THRESHOLD
            ):
                low_battery_notified = True
                state["mode"] = "returning_to_charge"
                state["accepting_tasks"] = False
                await notify_peers(
                    "battery_low",
                    {
                        "battery": state["battery"],
                        "action": "returning_to_charge",
                        "message": f"{state['id']} battery is low",
                    },
                )
            if state["battery"] == 0:
                await notify_peers(
                    "battery_dead",
                    {
                        "battery": 0,
                        "action": "unavailable",
                        "message": f"{state['id']} battery is dead",
                    },
                )
                state["status"] = "dead"
                state["mode"] = "unavailable"
                state["accepting_tasks"] = False


async def monitor_peers():
    """Detect peers that stop responding to heartbeat requests."""
    async with httpx.AsyncClient(timeout=2.0) as client:
        while True:
            await asyncio.sleep(3)
            if not state["id"]:
                continue
            try:
                robots = await registry_robots()
            except (httpx.HTTPError, KeyError, ValueError):
                continue

            known = {r["name"] for r in robots if r["name"] != state["id"]}
            for name in list(peer_health):
                if name not in known:
                    peer_health.pop(name, None)

            for peer in robots:
                name = peer["name"]
                if name == state["id"]:
                    continue
                peer_base = f"http://{peer['host']}:{peer['port']}"
                is_healthy = True
                try:
                    response = await client.get(f"{peer_base}/heartbeat")
                    is_healthy = response.status_code == 200
                except httpx.HTTPError:
                    is_healthy = False

                previous = peer_health.get(name)
                peer_health[name] = is_healthy

                if previous is True and not is_healthy:
                    await notify_peers(
                        "peer_unresponsive",
                        {
                            "peer": name,
                            "action": "remove_from_task_assignment",
                            "message": f"{name} is no longer responding",
                        },
                    )


async def navigation_loop():
    previous = time.monotonic()
    was_navigating = False
    while True:
        await asyncio.sleep(0.05)
        now = time.monotonic()
        dt, previous = min(now - previous, 0.15), now
        can_drive = responsive() and state["battery"] > 0
        if can_drive:
            navigator.tick(dt)
        state["navigation"] = navigator.mission
        navigating = bool(navigator.queue)
        if motors.DRIVER != "sim" and (navigating or was_navigating):
            # Re-sent every tick: the motor bridge stops the car when commands stop.
            if can_drive and navigating:
                try:
                    motors.set_drive(*navigator.command)
                except RuntimeError as exc:
                    print(f"navigation stopped: {exc}", flush=True)
                    navigator.cancel()
                    stop_motors()
            else:
                stop_motors()
        # Only take over mode/position while a floor route is active, so the
        # battery and drive logic keep control the rest of the time.
        if navigating:
            state["mode"] = navigator.mission["phase"]
            state["accepting_tasks"] = False
            # On the campus map a robot inside the building sits at the building.
            state["position"] = dict(FLOOR4_BUILDING)
        elif was_navigating and responsive() and not low_battery_notified:
            state["mode"] = "available"
            state["accepting_tasks"] = True
        was_navigating = navigating


async def register_loop():
    """Register until we get a name, then keep re-registering as a heartbeat.

    Sending our current name lets us keep it if the registry restarts.
    """
    failing = False
    async with httpx.AsyncClient(timeout=3.0) as client:
        while True:
            try:
                r = await client.post(
                    f"http://{REGISTRY}/api/registry",
                    json={"host": HOST_IP, "port": HOST_PORT, "name": state["id"]},
                )
                r.raise_for_status()
                name = r.json()["name"]
                if name != state["id"]:
                    print(f"registered with {REGISTRY} as {name}", flush=True)
                state["id"] = name
                failing = False
            except (httpx.HTTPError, KeyError, ValueError) as e:
                # Keep retrying, but only log when registration starts failing
                # so `docker logs` shows why a robot never appears.
                if not failing:
                    print(f"can't register with {REGISTRY}: {e!r}; retrying", flush=True)
                failing = True
            await asyncio.sleep(3 if state["id"] else 1)


@app.on_event("startup")
async def startup():
    if not (REGISTRY and HOST_IP and HOST_PORT):
        raise RuntimeError("REGISTRY, HOST_IP and HOST_PORT must be set (use join.py)")
    motors.check_driver()
    asyncio.create_task(battery_loop())
    asyncio.create_task(register_loop())
    asyncio.create_task(monitor_peers())
    asyncio.create_task(navigation_loop())


@app.on_event("shutdown")
async def shutdown():
    await notify_peers(
        "robot_offline",
        {
            "action": "powered_off",
            "message": f"{state['id']} is shutting down",
        },
    )


# ---- observability ----------------------------------------------------------

@app.get("/state")
def get_state():
    return state


@app.get("/camera")
async def camera(authorization: str | None = Header(default=None)):
    """Latest JPEG from the car's camera (real cars only)."""
    require_robot_token(authorization)
    if not state["camera"]:
        raise HTTPException(status_code=404, detail=f"{state['id']} has no camera")
    try:
        frame = await motors.bridge_get("/camera.jpg")
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return Response(frame.content, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/camera.mjpg")
async def camera_stream(authorization: str | None = Header(default=None)):
    """Live camera video (real cars only), relayed from the bridge."""
    require_robot_token(authorization)
    if not state["camera"]:
        raise HTTPException(status_code=404, detail=f"{state['id']} has no camera")
    try:
        content_type, chunks = await motors.bridge_stream("/camera.mjpg")
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return StreamingResponse(chunks, media_type=content_type, headers={"Cache-Control": "no-store"})


@app.get("/heartbeat")
def heartbeat():
    if not responsive():
        raise HTTPException(status_code=503, detail=f"{state['id']} not responsive")
    return {"id": state["id"], "ts": time.time(), "battery": state["battery"]}


# ---- inter-robot messaging --------------------------------------------------

class Message(BaseModel):
    sender: str
    body: dict


@app.post("/message")
def receive(msg: Message):
    if not responsive():
        raise HTTPException(status_code=503, detail=f"{state['id']} dropped message")
    entry = {"from": msg.sender, "body": msg.body, "ts": time.time()}
    state["inbox"].append(entry)
    return {"accepted": True}


class SendRequest(BaseModel):
    to: str            # robot name, e.g. "robot23"
    body: dict


@app.post("/send")
async def send(req: SendRequest):
    if not responsive():
        raise HTTPException(status_code=503, detail=f"{state['id']} not responsive")
    try:
        robots = await registry_robots()
    except (httpx.HTTPError, KeyError, ValueError) as e:
        return {"delivered": False, "error": f"registry unreachable: {e}"}
    peer = next((r for r in robots if r["name"] == req.to), None)
    if not peer:
        return {"delivered": False, "error": f"unknown robot {req.to}"}
    url = f"http://{peer['host']}:{peer['port']}/message"
    async with httpx.AsyncClient(timeout=3.0) as client:
        try:
            r = await client.post(url, json={"sender": state["id"], "body": req.body})
            return {"delivered": r.status_code == 200, "peer_status": r.status_code}
        except httpx.HTTPError as e:
            return {"delivered": False, "error": str(e)}


@app.get("/peers")
async def peers():
    try:
        robots = await registry_robots()
    except (httpx.HTTPError, KeyError, ValueError) as e:
        raise HTTPException(status_code=502, detail=f"registry unreachable: {e}")
    return {"peers": [r for r in robots if r["name"] != state["id"]]}


# ---- task execution --------------------------------------------------------

class TaskRequest(BaseModel):
    task_id: str
    type: str
    payload: dict = {}


class TaskCancel(BaseModel):
    reason: str = "cancelled by orchestrator"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_robot_token(authorization: str | None):
    """Optional shared-secret protection for cross-machine task traffic."""
    expected = os.getenv("ROBOT_AUTH_TOKEN", "")
    if expected and authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="invalid robot token")


def parse_drive_steps(payload: dict) -> list[dict]:
    """Validate a drive payload: {"steps": [{"action": "forward", "amount": 1.5, "speed": 0.5}, ...]}.

    speed is optional and defaults to MOTOR_POWER.
    """
    steps = payload.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 10:
        raise ValueError("drive requires 1 to 10 steps")
    clean = []
    for step in steps:
        action = str(step.get("action", "")).lower()
        if action not in DRIVE_LIMITS:
            raise ValueError(f"unknown drive action {action!r}; use {', '.join(DRIVE_LIMITS)}")
        amount = float(step.get("amount", 0))
        if not 0 < amount <= DRIVE_LIMITS[action]:
            cap = "" if math.isinf(DRIVE_LIMITS[action]) else f" and at most {DRIVE_LIMITS[action]:g}"
            raise ValueError(f"{action} amount must be above 0{cap}")
        speed = float(step.get("speed") or MOTOR_POWER)
        if not 0 < speed <= 1:
            raise ValueError("speed must be above 0 and at most 1 (1 = full speed)")
        clean.append({"action": action, "amount": amount, "speed": min(speed, MAX_MOTOR_POWER)})
    return clean


def step_rate(step: dict) -> float:
    """Meters (or degrees) per second for one step, scaled from the calibration."""
    base = DRIVE_SPEED if step["action"] in ("forward", "backward") else TURN_SPEED
    return base * step["speed"] / MOTOR_POWER


navigator = make_navigator()
if START_SPACE == "floor4":
    if START_DOOR:
        if START_DOOR not in DOORS or not DOORS[START_DOOR]["branch"]:
            raise RuntimeError(f"START_DOOR {START_DOOR!r} is not a placed door; use an id such as 411-1")
        navigator.place_at_door(START_DOOR)
    else:
        navigator.place_at_elevator()
    state["position"] = dict(FLOOR4_BUILDING)
else:
    navigator.pose["space"] = "campus"
state["pose"] = navigator.pose
state["navigation"] = navigator.mission


def enter_floor4():
    """Arrive on the 4th floor by the elevator (the campus leg ends at the building)."""
    navigator.place_at_elevator()
    state["space"] = "floor4"
    state["position"] = dict(FLOOR4_BUILDING)
    state["navigation"] = navigator.mission


def leaves_floor4(payload: dict) -> bool:
    """A simulated robot on the 4th floor rides the elevator down before a campus pickup or drop-off."""
    pickup = payload.get("pickup") or {}
    return state["space"] == "floor4" and (pickup.get("space") == "campus" or not payload["destination"].get("door_id"))


ELEVATOR_STOP = [{"u": ELEVATOR["u"], "v": ELEVATOR["v"], "pause": 0.5, "phase": "to_elevator"}]


def leave_floor4():
    """Ride the elevator down: the robot is back at the building on the campus map."""
    navigator.pose["space"] = "campus"
    state["space"] = "campus"
    state["position"] = dict(FLOOR4_BUILDING)
    state["navigation"] = navigator.mission


def delivery_stops(payload: dict) -> list[dict]:
    """Floor-4 stops for a delivery: an optional handoff pickup, then the door."""
    stops = []
    pickup = payload.get("pickup") or {}
    if pickup.get("space") == "floor4":
        stops.append({"u": pickup["u"], "v": pickup["v"], "phase": "picking_up"})
    stops.append({"door_id": payload["destination"]["door_id"]})
    return stops


def campus_seconds(a: dict, b: dict) -> float:
    """Time to cross the campus map between two points on the 0-100 grid."""
    meters = math.hypot(float(a["x"]) - float(b["x"]), float(a["y"]) - float(b["y"])) * METERS_PER_UNIT
    return max(2.0, meters / CAMPUS_SPEED)


def delivery_seconds(payload: dict) -> float:
    """Rough duration so the orchestrator's timeout fits the whole delivery."""
    seconds = 0.0
    pickup = payload.get("pickup") or {}
    destination = payload["destination"]
    leaving = leaves_floor4(payload)
    if leaving:
        seconds += copy.deepcopy(navigator).route_seconds(ELEVATOR_STOP)
    if state["space"] == "campus" or leaving:
        here = FLOOR4_BUILDING if leaving else state["position"]
        for stop in ([pickup] if pickup.get("space") == "campus" else []) + [
            destination if not destination.get("door_id") else FLOOR4_BUILDING
        ]:
            seconds += campus_seconds(here, stop)
            here = stop
    if destination.get("door_id"):
        trial = copy.deepcopy(navigator)
        if state["space"] == "campus" or leaving:
            trial.place_at_elevator()
        seconds += trial.route_seconds(delivery_stops(payload))
    return seconds


async def slide_to(task: dict, x: float, y: float):
    """Campus travel is visual only: glide across the 0-100 grid at CAMPUS_SPEED.

    state["leg"] tells the dashboard where the trip started and ended and how far
    along it is, so it can draw the robot following the roads instead of this
    straight line.
    """
    start = dict(state["position"])
    seconds = campus_seconds(start, {"x": x, "y": y})
    steps = max(1, round(seconds / 0.25))
    state["leg"] = {"from": start, "to": {"x": x, "y": y}, "progress": 0.0}
    try:
        for step in range(1, steps + 1):
            await asyncio.sleep(seconds / steps)
            if state["status"] != "alive":
                raise RuntimeError(f"robot became {state['status']} on the way")
            state["position"] = {
                "x": round(start["x"] + (x - start["x"]) * step / steps, 2),
                "y": round(start["y"] + (y - start["y"]) * step / steps, 2),
            }
            state["leg"]["progress"] = round(step / steps, 3)
    finally:
        state["leg"] = None


async def drive_floor_route(stops: list[dict], task: dict | None = None):
    """Run a floor-4 route to its end, tracking the phase on the task."""
    if navigator.queue:
        raise RuntimeError("robot is already on a floor route")
    navigator.start_route(stops)
    try:
        while navigator.queue:
            await asyncio.sleep(0.1)
            if state["status"] != "alive":
                raise RuntimeError(f"robot became {state['status']} on floor 4")
            if task is not None:
                if navigator.mission["phase"] == "picking_up" and "picked_up_at" not in task:
                    task["picked_up_at"] = now()
                task["floor_phase"] = navigator.mission["phase"]
    finally:
        if navigator.queue:  # cancelled or failed mid-route
            navigator.cancel()
            if motors.DRIVER != "sim":
                stop_motors()


async def run_delivery(task: dict) -> dict:
    """Campus legs (visual) to the pickup and the building, then the measured
    floor-4 route: elevator, optional handoff pickup point, destination door."""
    payload = task["payload"]
    destination = payload["destination"]
    pickup = payload.get("pickup") or {}
    if leaves_floor4(payload):
        task["phase"] = "leaving_floor4"
        await drive_floor_route(ELEVATOR_STOP)
        leave_floor4()
    if state["space"] == "campus" and pickup.get("space") == "campus":
        task["phase"] = "to_pickup"
        await slide_to(task, float(pickup["x"]), float(pickup["y"]))
        task["picked_up_at"] = now()
    if not destination.get("door_id"):
        task["phase"] = "to_destination"
        await slide_to(task, float(destination["x"]), float(destination["y"]))
        return {"final_position": state["position"]}
    if state["space"] == "campus":
        task["phase"] = "to_building"
        await slide_to(task, FLOOR4_BUILDING["x"], FLOOR4_BUILDING["y"])
        enter_floor4()
    if navigator.queue:
        raise RuntimeError("robot is already on a floor route")
    task["phase"] = "on_floor4"
    await drive_floor_route(delivery_stops(payload), task)
    return {"final_pose": dict(navigator.pose), "door_id": destination["door_id"]}


def drive_seconds(steps: list[dict]) -> float:
    return sum(s["amount"] / step_rate(s) for s in steps)


async def run_drive(task: dict) -> dict:
    """Run drive steps in order on the car, mirroring the motion on the map.

    The robot drives like a car (JetRacer): a turn is driving forward with the
    wheels at full lock until the heading has changed by the requested angle,
    so on the map it traces an arc rather than spinning in place.
    """
    steps = task["payload"]["steps"]
    total = drive_seconds(steps)
    elapsed = 0.0
    try:
        for step in steps:
            throttle = -step["speed"] if step["action"] == "backward" else step["speed"]
            steering = {"left": -1.0, "right": 1.0}.get(step["action"], 0.0)
            scale = step["speed"] / MOTOR_POWER  # map motion speeds up with the throttle
            remaining = step["amount"] / step_rate(step)
            while remaining > 1e-6:  # float leftovers would add an extra ~0 s tick
                if state["status"] != "alive":
                    raise RuntimeError(f"robot became {state['status']} mid-drive")
                # Re-sent every tick: the motor bridge stops the car when
                # commands stop arriving.
                motors.set_drive(throttle, steering)
                dt = min(0.1, remaining)
                await asyncio.sleep(dt)
                remaining -= dt
                elapsed += dt
                state["heading"] = (state["heading"] - steering * TURN_SPEED * scale * dt) % 360
                if state["space"] == "campus":  # a robot on the 4th floor stays inside the building
                    d = math.copysign(DRIVE_SPEED * scale * dt, throttle) / METERS_PER_UNIT
                    h = math.radians(state["heading"])
                    pos = state["position"]
                    state["position"] = {
                        "x": round(min(100.0, max(0.0, pos["x"] + d * math.cos(h))), 3),
                        "y": round(min(100.0, max(0.0, pos["y"] + d * math.sin(h))), 3),
                    }
                task["progress"] = round(min(elapsed / total, 1.0), 2)
    finally:
        # Always stop the car, including on cancel or error.
        try:
            motors.set_drive(0, 0)
        except RuntimeError:
            pass  # bridge unreachable: its watchdog stops the car; keep the original error
    return {"final_position": state["position"], "heading": round(state["heading"])}


async def run_task(task_id: str):
    task = tasks[task_id]
    task["status"] = "running"
    task["started_at"] = now()
    try:
        if task["type"] == "drive":
            task["result"] = await run_drive(task)
        elif task["type"] == "deliver_package":
            task["result"] = await run_delivery(task)
            task["result"]["delivered"] = True
            task["result"]["package_id"] = task["payload"]["package_id"]
        elif task["type"] == "move_to":
            x = float(task["payload"]["x"])
            y = float(task["payload"]["y"])
            if not (0 <= x <= 100 and 0 <= y <= 100):
                raise ValueError("x and y must be between 0 and 100")

            if state["space"] == "floor4" and motors.DRIVER == "sim":
                task["phase"] = "leaving_floor4"
                await drive_floor_route(ELEVATOR_STOP)
                leave_floor4()
            await slide_to(task, x, y)
            task["progress"] = 1.0

            task["result"] = {"final_position": state["position"]}
        elif task["type"] == "report_status":
            task["result"] = {"state": dict(state)}
        else:
            raise ValueError(f"unsupported task type: {task['type']}")

        task["status"] = "completed"
        task["completed_at"] = now()
    except asyncio.CancelledError:
        task["status"] = "cancelled"
        task["error"] = "worker cancelled"
        raise
    except Exception as exc:
        task["status"] = "failed"
        task["error"] = str(exc)
        task["completed_at"] = now()
    finally:
        task_workers.pop(task_id, None)


@app.post("/tasks")
async def create_task(
    req: TaskRequest,
    authorization: str | None = Header(default=None),
):
    require_robot_token(authorization)
    if not state["id"]:
        raise HTTPException(status_code=503, detail="robot is not registered")
    if state["status"] != "alive" or not state["accepting_tasks"]:
        raise HTTPException(status_code=409, detail="robot is not accepting tasks")
    if any(
        worker and not worker.done()
        for worker in task_workers.values()
    ):
        raise HTTPException(status_code=409, detail="robot already has an active task")
    if req.task_id in tasks:
        return tasks[req.task_id]
    if req.type not in ("move_to", "deliver_package", "report_status", "drive"):
        raise HTTPException(status_code=400, detail="unsupported task type")
    if req.type == "drive":
        try:
            req.payload = {"steps": parse_drive_steps(req.payload)}
        except (ValueError, TypeError, AttributeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    if req.type == "move_to" and not {"x", "y"}.issubset(req.payload):
        raise HTTPException(status_code=400, detail="move_to requires x and y")
    if req.type == "deliver_package":
        destination = req.payload.get("destination", {})
        if not req.payload.get("package_id") or not {"x", "y"}.issubset(destination):
            raise HTTPException(
                status_code=400,
                detail="deliver_package requires package_id and destination x,y",
            )
        door_id = destination.get("door_id")
        if door_id and not (door_id in DOORS and DOORS[door_id]["branch"]):
            raise HTTPException(status_code=400, detail=f"unknown or unplaced door {door_id}")
        if motors.DRIVER != "sim" and leaves_floor4(req.payload):
            raise HTTPException(status_code=400, detail="this car stays on the 4th floor; it can't collect a package from outside")
        pickup = req.payload.get("pickup") or {}
        if pickup and not ({"u", "v"} if pickup.get("space") == "floor4" else {"x", "y"}).issubset(pickup):
            raise HTTPException(status_code=400, detail="pickup needs u,v on floor4 or x,y on campus")

    task = {
        "task_id": req.task_id,
        "robot_id": state["id"],
        "type": req.type,
        "payload": req.payload,
        "status": "acknowledged",
        "progress": 0.0,
        "created_at": now(),
    }
    if req.type == "drive":
        # Lets the orchestrator extend its timeout for long drives.
        task["estimated_seconds"] = round(drive_seconds(req.payload["steps"]), 1)
    if req.type == "deliver_package":
        task["estimated_seconds"] = round(delivery_seconds(req.payload), 1)
    if req.type == "move_to":
        leaving = state["space"] == "floor4" and motors.DRIVER == "sim"
        task["estimated_seconds"] = round(
            campus_seconds(FLOOR4_BUILDING if leaving else state["position"], req.payload)
            + (copy.deepcopy(navigator).route_seconds(ELEVATOR_STOP) if leaving else 0), 1)
    tasks[req.task_id] = task
    task_workers[req.task_id] = asyncio.create_task(run_task(req.task_id))
    return task


@app.get("/tasks/{task_id}")
def get_task(task_id: str, authorization: str | None = Header(default=None)):
    require_robot_token(authorization)
    task = tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    return task


@app.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    req: TaskCancel | None = None,
    authorization: str | None = Header(default=None),
):
    require_robot_token(authorization)
    task = tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task["status"] in ("completed", "failed", "cancelled"):
        return task
    task["status"] = "cancelled"
    task["error"] = (req.reason if req else "cancelled by orchestrator")
    worker = task_workers.get(task_id)
    if worker:
        worker.cancel()
    return task


# ---- control (used by orchestrator / frontend) ------------------------------

class Control(BaseModel):
    status: str | None = None    # alive | dead | no_response
    battery: float | None = None


@app.post("/control")
async def control(ctrl: Control):
    global low_battery_notified
    if ctrl.status is not None:
        if ctrl.status not in ("alive", "dead", "no_response"):
            raise HTTPException(status_code=400, detail="invalid status")
        if ctrl.status != "alive" and state["status"] == "alive":
            await notify_peers(
                "robot_status",
                {
                    "status": ctrl.status,
                    "action": "unavailable",
                    "message": f"{state['id']} changed status to {ctrl.status}",
                },
            )
        state["status"] = ctrl.status
        if ctrl.status != "alive":
            state["mode"] = "unavailable"
            state["accepting_tasks"] = False
    if ctrl.battery is not None:
        state["battery"] = max(0.0, min(100.0, ctrl.battery))
        if state["battery"] > LOW_BATTERY_THRESHOLD:
            low_battery_notified = False
            if state["status"] == "alive":
                state["mode"] = "available"
                state["accepting_tasks"] = True
        if state["battery"] == 0:
            state["status"] = "dead"
    return state


class NavigationRequest(BaseModel):
    door_id: str | None = None
    cancel: bool = False


@app.post("/navigate")
async def navigate(req: NavigationRequest):
    if req.cancel:
        navigator.cancel()
        if motors.DRIVER != "sim":
            stop_motors()
    else:
        if not responsive() or state["battery"] <= 0:
            raise HTTPException(status_code=409, detail="Recover and charge the robot before navigating")
        if navigator.queue:
            raise HTTPException(status_code=409, detail="Cancel the current route before choosing another door")
        if not req.door_id:
            raise HTTPException(status_code=400, detail="door_id is required")
        if state["space"] != "floor4":
            enter_floor4()  # a campus robot sent to a door takes the elevator up first
        try:
            navigator.start(req.door_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown door")
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error))
    state["navigation"] = navigator.mission
    return state
