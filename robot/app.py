"""Single robot node.

One image, run as any number of containers on any number of machines. Each
robot registers with a central registry (the simulator), which assigns its
name first-come-first-served (robot0, robot1, ...) and tells robots where
every other robot is reachable, so any robot can message any other.
"""
import os
import asyncio
import math
import random
import time
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

import motors

# Registry address, e.g. "192.168.1.132:3000" (the machine running the simulator).
REGISTRY = os.getenv("REGISTRY", "")
# Where other machines can reach this robot: the host's LAN IP + published port.
HOST_IP = os.getenv("HOST_IP", "")
HOST_PORT = int(os.getenv("HOST_PORT", "0"))
# Battery drains this many points per tick (1 tick/sec).
DRAIN_RATE = float(os.getenv("DRAIN_RATE", "0.1"))
LOW_BATTERY_THRESHOLD = float(os.getenv("LOW_BATTERY_THRESHOLD", "15"))
# Drive calibration: how fast the car goes at MOTOR_POWER throttle. Measure
# these on the real car so "forward 1 m" and "left 90" come out right.
DRIVE_SPEED = float(os.getenv("DRIVE_SPEED", "0.3"))   # meters per second
TURN_SPEED = float(os.getenv("TURN_SPEED", "45"))      # degrees per second at full steering lock
MOTOR_POWER = float(os.getenv("MOTOR_POWER", "0.3"))   # throttle 0-1 used for drive steps
# Largest amount one drive step may ask for: degrees for turns, and meters for
# straight runs only when MAX_DRIVE_METERS is set (no distance cap by default).
MAX_DRIVE_METERS = float(os.getenv("MAX_DRIVE_METERS", "inf"))
DRIVE_LIMITS = {"forward": MAX_DRIVE_METERS, "backward": MAX_DRIVE_METERS, "left": 360.0, "right": 360.0}
METERS_PER_UNIT = 2.0  # the dashboard's 0-100 grid spans 200 m

app = FastAPI(title="Robot")

state = {
    "id": None,             # assigned by the registry
    "power": "on",          # on | off  (off => container stopped, handled by orchestrator)
    "status": "alive",      # alive | dead | no_response
    "mode": "available",    # available | returning_to_charge | unavailable
    "accepting_tasks": True,
    "battery": 100.0,       # 0-100
    "position": {"x": round(random.uniform(0, 100), 1),
                 "y": round(random.uniform(0, 100), 1)},
    "heading": 90.0,        # degrees, counter-clockwise from east; 90 = facing north (+y)
    "inbox": [],            # messages received from peers
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


async def battery_loop():
    global low_battery_notified
    while True:
        await asyncio.sleep(1)
        if state["status"] != "dead" and state["battery"] > 0:
            state["battery"] = max(0.0, state["battery"] - DRAIN_RATE)
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
    """Validate a drive payload: {"steps": [{"action": "forward", "amount": 1.5}, ...]}."""
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
        clean.append({"action": action, "amount": amount})
    return clean


def drive_seconds(steps: list[dict]) -> float:
    return sum(
        s["amount"] / (DRIVE_SPEED if s["action"] in ("forward", "backward") else TURN_SPEED)
        for s in steps
    )


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
            straight = step["action"] in ("forward", "backward")
            throttle = -MOTOR_POWER if step["action"] == "backward" else MOTOR_POWER
            steering = {"left": -1.0, "right": 1.0}.get(step["action"], 0.0)
            remaining = step["amount"] / (DRIVE_SPEED if straight else TURN_SPEED)
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
                state["heading"] = (state["heading"] - steering * TURN_SPEED * dt) % 360
                d = math.copysign(DRIVE_SPEED * dt, throttle) / METERS_PER_UNIT
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
        elif task["type"] in ("move_to", "deliver_package"):
            if task["type"] == "deliver_package":
                destination = task["payload"]["destination"]
                x = float(destination["x"])
                y = float(destination["y"])
            else:
                x = float(task["payload"]["x"])
                y = float(task["payload"]["y"])
            if not (0 <= x <= 100 and 0 <= y <= 100):
                raise ValueError("x and y must be between 0 and 100")

            start = dict(state["position"])
            steps = 10
            for step in range(1, steps + 1):
                await asyncio.sleep(0.25)
                if task["status"] == "cancelled":
                    return
                state["position"] = {
                    "x": round(start["x"] + (x - start["x"]) * step / steps, 1),
                    "y": round(start["y"] + (y - start["y"]) * step / steps, 1),
                }
                task["progress"] = round(step / steps, 2)

            task["result"] = {"final_position": state["position"]}
            if task["type"] == "deliver_package":
                task["result"]["delivered"] = True
                task["result"]["package_id"] = task["payload"]["package_id"]
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
    return state
