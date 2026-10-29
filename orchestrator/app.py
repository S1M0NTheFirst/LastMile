"""LLM task gateway for the LastMile fleet.

Gemini plans through narrow tools (assign_move_task, drive_robot, ...). This
service validates the request, selects a healthy robot from the registry, and
executes the task. It also runs camera autopilots: Gemini looks at a real car's
camera, picks one small move, and repeats until the goal is reached. The Gemini
key never leaves this service.
"""
import asyncio
import json
import os
import math
import re
import socket
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

try:
    from google import genai
    from google.genai import types
except ImportError:  # makes /health useful before dependencies are installed
    genai = None
    types = None


app = FastAPI(title="LastMile Orchestrator")
REGISTRY_URL = os.getenv("REGISTRY_URL", "http://localhost:3000/api/registry").rstrip("/")
ROBOT_AUTH_TOKEN = os.getenv("ROBOT_AUTH_TOKEN", "")
LOCAL_HOST_IP = os.getenv("LOCAL_HOST_IP", "")
LOCAL_ROBOT_HOST = os.getenv("LOCAL_ROBOT_HOST", "127.0.0.1")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_TIMEOUT_SECONDS = float(os.getenv("GEMINI_TIMEOUT_SECONDS", "45"))
# When the main model hasn't answered after GEMINI_HEDGE_SECONDS (or fails outright), the same
# request also goes to the next model here, and the first answer wins. Google's models are
# sometimes overloaded for tens of seconds at a time.
GEMINI_FALLBACK_MODELS = [m.strip() for m in os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.1-flash-lite,gemini-3.5-flash,gemini-3-flash-preview,gemini-3.5-flash-lite").split(",") if m.strip()]
GEMINI_HEDGE_SECONDS = float(os.getenv("GEMINI_HEDGE_SECONDS", "4"))
TASK_TIMEOUT_SECONDS = float(os.getenv("TASK_TIMEOUT_SECONDS", "15"))
MAX_TASK_RETRIES = int(os.getenv("MAX_TASK_RETRIES", "1"))
AUTOPILOT_MAX_STEPS = int(os.getenv("AUTOPILOT_MAX_STEPS", "20"))
# Largest single autopilot move, so the car re-checks the camera often.
AUTOPILOT_MAX_METERS = float(os.getenv("AUTOPILOT_MAX_METERS", "2.0"))
AUTOPILOT_MAX_DEGREES = float(os.getenv("AUTOPILOT_MAX_DEGREES", "90"))
FINAL_TASK_STATUSES = ("completed", "failed", "cancelled", "reassigned")
KNOWN_LOCATIONS = {
    "north entrance": {"x": 90.0, "y": 90.0},
    "east entrance": {"x": 90.0, "y": 50.0},
    "west entrance": {"x": 10.0, "y": 50.0},
    "south entrance": {"x": 50.0, "y": 10.0},
    "warehouse": {"x": 20.0, "y": 80.0},
    "main door": {"x": 50.0, "y": 10.0},
    # The building with the measured 4th floor; the dashboard centers on it.
    "ecs": {"x": 50.0, "y": 50.0},
}
# Every named OpenStreetMap building on the dashboard's campus map, at its OSM
# centroid on the 0-100 grid (ECS at 50,50, 4 m per unit, +y north). Built from
# the same Overpass data the dashboard draws, so names match the map labels.
CAMPUS_BUILDINGS = json.loads((Path(__file__).resolve().parent / "campus_buildings.json").read_text())
for building_name, spot in CAMPUS_BUILDINGS.items():
    KNOWN_LOCATIONS.setdefault(building_name.lower(), {"x": spot["x"], "y": spot["y"]})
METERS_PER_UNIT = 2.0  # the dashboard's 0-100 grid spans 200 m
FLOOR4_ENTRY_METERS = float(os.getenv("FLOOR4_ENTRY_METERS", "60"))  # extra cost for a robot outside to reach the 4th floor


def load_floor4():
    """The measured 4th floor shared with the robots (robot/floor4.json). In
    Docker, mount it and point FLOOR4_PATH at it."""
    path = Path(os.getenv("FLOOR4_PATH", Path(__file__).resolve().parent.parent / "robot" / "floor4.json"))
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


FLOOR4 = load_floor4()
FLOOR4_GEOMETRY = (FLOOR4 or {}).get("geometry", {"junctionNear": 7.09, "junctionFar": 8.75})
FLOOR4_CENTER_U = (FLOOR4_GEOMETRY["junctionNear"] + FLOOR4_GEOMETRY["junctionFar"]) / 2
FLOOR4_ELEVATOR = (FLOOR4 or {}).get("elevator", {"u": 5.0, "v": 0})
# Room number -> the door a delivery uses (the first placed entry, e.g. 407 -> 407-1).
ROOM_DOORS = {}
for door in (FLOOR4 or {}).get("doors", []):
    if door.get("branch") and door.get("side"):
        ROOM_DOORS.setdefault(door["room"].lower(), door["id"])
LOCATION_ALIASES = {
    "north gate": "north entrance",
    "east gate": "east entrance",
    "west gate": "west entrance",
    "south gate": "south entrance",
    "front door": "main door",
    "building a": "college of professional and continuing education",
    "cpace": "college of professional and continuing education",
    "vec": "vivian engineering center",
    "rec center": "student recreation and wellness center",
    "srwc": "student recreation and wellness center",
    "ecs engineering & computer science": "ecs",
}
tasks: dict[str, dict] = {}
# Robot name -> its last /state that answered, so a killed robot's spot is known.
last_seen: dict[str, dict] = {}
autopilots: dict[str, dict] = {}  # robot_id -> latest mission
autopilot_workers: dict[str, asyncio.Task] = {}


class ChatRequest(BaseModel):
    message: str


class DirectTaskRequest(BaseModel):
    type: str
    payload: dict
    minimum_battery: float = 20
    robot_id: str | None = None


class CancelTaskRequest(BaseModel):
    reason: str = "cancelled by user"


class AutopilotRequest(BaseModel):
    robot_id: str
    goal: str
    max_steps: int = AUTOPILOT_MAX_STEPS


def now():
    return datetime.now(timezone.utc).isoformat()


# Models that just failed are skipped for a while: a model whose free daily quota is used up
# (429) stays unusable for hours, an overloaded one (503) usually recovers within seconds.
model_cooldown: dict[str, float] = {}


def cool_down(model: str, error: Exception):
    text = str(error)
    if "429" in text or "RESOURCE_EXHAUSTED" in text:
        seconds = 1800
    elif "404" in text or "NOT_FOUND" in text:
        seconds = 3600
    elif "503" in text or "UNAVAILABLE" in text or "500" in text:
        seconds = 20
    else:
        return
    model_cooldown[model] = time.time() + seconds


async def generate(contents, config):
    """Ask Gemini, racing backup models when the main one is slow or failing.

    Returns (response, model that answered). Raises asyncio.TimeoutError after
    GEMINI_TIMEOUT_SECONDS, or RuntimeError if every model failed.
    """
    client = genai.Client(api_key=GEMINI_API_KEY)
    models = [GEMINI_MODEL] + [m for m in GEMINI_FALLBACK_MODELS if m != GEMINI_MODEL]
    usable = [m for m in models if model_cooldown.get(m, 0) <= time.time()]
    models = usable or models  # if every model is cooling down, try them all anyway
    pending: dict[asyncio.Task, str] = {}
    errors: list[str] = []
    started = time.time()
    next_model = 0

    def launch():
        nonlocal next_model
        model = models[next_model]
        next_model += 1
        pending[asyncio.ensure_future(client.aio.models.generate_content(
            model=model, contents=contents, config=config))] = model

    launch()
    try:
        while True:
            remaining = started + GEMINI_TIMEOUT_SECONDS - time.time()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            more = next_model < len(models)
            done, _ = await asyncio.wait(
                pending, timeout=min(remaining, GEMINI_HEDGE_SECONDS) if more else remaining,
                return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                model = pending.pop(task)
                if task.exception() is None:
                    print(f"gemini: {model} answered in {time.time() - started:.1f} s", flush=True)
                    return task.result(), model
                cool_down(model, task.exception())
                errors.append(f"{model}: {type(task.exception()).__name__}: {str(task.exception())[:150]}")
                print(f"gemini: {errors[-1]}", flush=True)
            if more and (not done or not pending):  # slow, or everything so far failed
                launch()
            elif not pending:
                raise RuntimeError("; ".join(errors))
    finally:
        for task in pending:
            task.cancel()


def robot_headers():
    return {"Authorization": f"Bearer {ROBOT_AUTH_TOKEN}"} if ROBOT_AUTH_TOKEN else {}


_local_hosts: dict[str, bool] = {}


def is_this_machine(host: str) -> bool:
    """True when host is one of this machine's own addresses, whatever network it is on now.

    Robots started here register with the Mac's LAN IP, which changes with the Wi-Fi and can
    be unreachable from the Mac itself; those are reached on LOCAL_ROBOT_HOST instead.
    """
    if host not in _local_hosts:
        local = bool(LOCAL_HOST_IP) and host == LOCAL_HOST_IP
        if not local:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                    probe.connect((host, 9))  # no traffic: only picks the source address
                    local = probe.getsockname()[0] == host
            except OSError:
                pass
        _local_hosts[host] = local
    return _local_hosts[host]


def robot_url(robot):
    host = LOCAL_ROBOT_HOST if is_this_machine(robot["host"]) else robot["host"]
    return f"http://{host}:{robot['port']}"


ROBOT_STATE_TIMEOUT = float(os.getenv("ROBOT_STATE_TIMEOUT", "1.5"))


_snapshot: dict = {"at": 0.0, "task": None}
SNAPSHOT_REUSE_SECONDS = 0.3


async def fleet_snapshot():
    """The fleet, shared between callers: the dashboard, the task watcher and every
    active task all ask at once, so asks within a fraction of a second reuse one lookup."""
    task = _snapshot["task"]
    if task is None or (task.done() and time.time() - _snapshot["at"] > SNAPSHOT_REUSE_SECONDS):
        task = _snapshot["task"] = asyncio.ensure_future(_fleet_snapshot())
        _snapshot["at"] = time.time()
    return await asyncio.shield(task)


async def _fleet_snapshot():
    """Every registered robot with its state, asked in parallel so one unreachable
    robot costs one short timeout instead of delaying everything."""
    async with httpx.AsyncClient(timeout=3) as client:
        response = await client.get(REGISTRY_URL)
        response.raise_for_status()
        robots = response.json().get("robots", [])

        async def look(robot):
            try:
                state_response = await client.get(
                    f"{robot_url(robot)}/state",
                    headers=robot_headers(),
                    timeout=ROBOT_STATE_TIMEOUT,
                )
                robot_state = state_response.json()
                last_seen[robot["name"]] = {"state": robot_state, "at": now()}
                return {**robot, "state": robot_state, "reachable": True}
            except (httpx.HTTPError, ValueError):
                return {**robot, "state": None, "reachable": False,
                        "last_seen": last_seen.get(robot["name"])}

        return list(await asyncio.gather(*(look(robot) for robot in robots)))


def resolve_destination(destination: str):
    normalized = destination.strip().lower()
    normalized = LOCATION_ALIASES.get(normalized, normalized)
    if normalized in KNOWN_LOCATIONS:
        return {"name": normalized, **KNOWN_LOCATIONS[normalized]}
    # 4th-floor rooms: "407", "room 407", "ECS 407", "ECS-407", or a door id like "407-2".
    room = re.fullmatch(r"(?:ecs[\s-]*)?(?:room\s*)?([\w-]+)", normalized)
    if room:
        key = room.group(1)
        door_id = ROOM_DOORS.get(key) or next(
            (d["id"] for d in (FLOOR4 or {}).get("doors", []) if d["id"].lower() == key and d.get("branch")),
            None,
        )
        if door_id:
            return {"name": f"room {door_id.split('-')[0]}", **KNOWN_LOCATIONS["ecs"],
                    "space": "floor4", "door_id": door_id}
    # "room 405 door 1", "405 door a", "405-a": a door written with a number or letter.
    room_door = re.fullmatch(r"(?:ecs[\s-]*)?(?:room\s*)?(\d+[a-z]?)[\s-]*(?:door|entry|entrance)?[\s-]*([a-z]|\d)", normalized)
    if room_door:
        number, entry = room_door.groups()
        swapped = {"a": "1", "b": "2", "c": "3", "1": "a", "2": "b", "3": "c"}.get(entry)
        door_id = next(
            (d["id"] for d in (FLOOR4 or {}).get("doors", [])
             if d.get("branch") and d["id"].lower() in (f"{number}-{entry}", f"{number}-{swapped}")),
            None,
        )
        if door_id:
            return {"name": f"room {door_id.split('-')[0]}", **KNOWN_LOCATIONS["ecs"],
                    "space": "floor4", "door_id": door_id}
    # A partial building name the LLM shortened, e.g. "engineering technology building".
    partial = [n for n in KNOWN_LOCATIONS if len(normalized) >= 4 and (normalized in n or n in normalized)]
    if len(partial) == 1:
        return {"name": partial[0], **KNOWN_LOCATIONS[partial[0]]}
    coordinates = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", normalized)
    if coordinates:
        return {"name": destination, "x": float(coordinates.group(1)), "y": float(coordinates.group(2))}
    raise HTTPException(
        status_code=400,
        detail=(
            f"unknown destination '{destination}'; known locations: {', '.join(KNOWN_LOCATIONS)}"
            + (f", or a 4th-floor room in ECS ({', '.join(sorted(ROOM_DOORS))})" if ROOM_DOORS else "")
        ),
    )


def floor4_distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Meters along the T-shaped hallway between two (u, v) points: the main
    hallway runs up to the junction, the cross hallway runs along v."""
    def snap(u, v):
        return ("stem", u, 0.0) if u < FLOOR4_GEOMETRY["junctionNear"] - 0.05 else ("branch", FLOOR4_CENTER_U, v)
    (sa, ua, va), (sb, ub, vb) = snap(*a), snap(*b)
    if sa == sb == "stem":
        return abs(ua - ub)
    if sa == sb == "branch":
        return abs(va - vb)
    return abs(ua - ub) + abs(va - vb)


def travel_distance(robot_state: dict, target: dict) -> float:
    """Approximate meters for a robot to reach a target on the campus grid or
    on the 4th floor (reached through the ECS elevator)."""
    position = robot_state.get("position") or {"x": 0, "y": 0}
    pose = robot_state.get("pose") or {}
    elevator = (FLOOR4_ELEVATOR["u"], FLOOR4_ELEVATOR["v"])
    on_floor = robot_state.get("space") == "floor4" and "u" in pose
    here = (pose.get("u"), pose.get("v")) if on_floor else None
    building = KNOWN_LOCATIONS["ecs"]

    def campus(a, b):
        return math.hypot(float(a["x"]) - float(b["x"]), float(a["y"]) - float(b["y"])) * METERS_PER_UNIT

    if target.get("space") == "floor4":
        spot = (float(target["u"]), float(target["v"]))
        if on_floor:
            return floor4_distance(here, spot)
        # A robot outside still has to get into the building and wait for the elevator, so a
        # robot already on this floor (such as the car parked at room 411) is preferred.
        return campus(position, building) + FLOOR4_ENTRY_METERS + floor4_distance(elevator, spot)
    if on_floor:
        return floor4_distance(here, elevator) + campus(building, target)
    return campus(position, target)


def describe_floor4_spot(u: float, v: float) -> str:
    if u < FLOOR4_GEOMETRY["junctionNear"] - 0.05:
        return f"{abs(u - FLOOR4_ELEVATOR['u']):.1f} m from the elevator in the main hallway"
    side = "right" if v >= 0 else "left"
    return f"{abs(v):.1f} m down the {side} hallway"


def stuck_on_floor4(robot_state: dict, task_type: str, target: dict) -> bool:
    """A real car can't leave the 4th floor, so it never takes a package from outside."""
    return (
        task_type == "deliver_package"
        and robot_state.get("driver", "sim") != "sim"
        and robot_state.get("space") == "floor4"
        and target.get("space") != "floor4"
    )


async def assign_task(
    task_type: str,
    payload: dict,
    target_x: float,
    target_y: float,
    minimum_battery: float = 20,
    robot_id: str | None = None,
    excluded_robot_ids: set[str] | None = None,
    retry_count: int = 0,
    allow_handoff: bool = True,
    target: dict | None = None,
):
    # The nearest robot is chosen by travel_distance to target (defaults to the x,y point).
    target = target or {"x": target_x, "y": target_y}
    if not (0 <= target_x <= 100 and 0 <= target_y <= 100):
        raise HTTPException(status_code=400, detail="x and y must be between 0 and 100")

    robots = await fleet_snapshot()
    excluded_robot_ids = excluded_robot_ids or set()
    busy_robot_ids = {
        task["robot_id"]
        for task in tasks.values()
        if task.get("status") in ("sending", "acknowledged", "running", "reassigning")
    }
    available = [
        robot for robot in robots
        if robot["reachable"]
        and robot["state"]
        and robot["state"].get("status") == "alive"
        and robot["state"].get("accepting_tasks") is True
        and float(robot["state"].get("battery", 0)) >= minimum_battery
        and robot["name"] not in excluded_robot_ids
        and robot["name"] not in busy_robot_ids
        and not stuck_on_floor4(robot["state"], task_type, target)
    ]
    requested_robot = (
        next((robot for robot in robots if robot["name"] == robot_id), None)
        if robot_id else None
    )
    handoff_reason = None
    if robot_id:
        candidates = [robot for robot in available if robot["name"] == robot_id]
        if not candidates:
            if not requested_robot:
                handoff_reason = f"{robot_id} is not registered"
            elif not requested_robot["reachable"] or not requested_robot["state"]:
                handoff_reason = f"{robot_id} is unreachable"
            else:
                requested_state = requested_robot["state"]
                if requested_state.get("status") != "alive":
                    handoff_reason = f"{robot_id} is {requested_state.get('status')}"
                elif not requested_state.get("accepting_tasks"):
                    handoff_reason = f"{robot_id} is not accepting tasks"
                elif robot_id in busy_robot_ids:
                    handoff_reason = f"{robot_id} is busy with another task"
                else:
                    handoff_reason = (
                        f"{robot_id} battery is below the required "
                        f"{minimum_battery:g}%"
                    )
            candidates = available if allow_handoff else []
    else:
        candidates = available

    if not candidates:
        if handoff_reason:
            fallback = "no other suitable robot is available" if allow_handoff else "this task is not handed to another robot"
            raise HTTPException(status_code=409, detail=f"{handoff_reason}; {fallback}")
        raise HTTPException(status_code=409, detail="no suitable robot is available")

    def selection_key(item):
        distance = travel_distance(item["state"], target)
        # Nearest robot wins; battery breaks ties in favor of the healthier one.
        return distance, -float(item["state"].get("battery", 0))

    robot = min(candidates, key=selection_key)
    task_id = f"task-{uuid.uuid4().hex[:10]}"
    task = {
        "task_id": task_id,
        "robot_id": robot["name"],
        "type": task_type,
        "payload": payload,
        "status": "sending",
        "created_at": now(),
        "deadline_epoch": time.time() + TASK_TIMEOUT_SECONDS,
        "timeout_seconds": TASK_TIMEOUT_SECONDS,
        "minimum_battery": minimum_battery,
        "retry_count": retry_count,
        "max_retries": MAX_TASK_RETRIES,
    }
    if robot_id:
        task["requested_robot_id"] = robot_id
    if handoff_reason and robot["name"] != robot_id:
        task["handoff_from"] = robot_id
        task["handoff_reason"] = handoff_reason
    tasks[task_id] = task
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(
                f"{robot_url(robot)}/tasks",
                headers={**robot_headers(), "Content-Type": "application/json"},
                json={"task_id": task_id, "type": task_type, "payload": payload},
            )
            if response.status_code >= 400:
                task["status"] = "failed"
                task["error"] = response.text
                raise HTTPException(status_code=502, detail=f"robot rejected task: {response.text}")
            task.update(response.json())
            task["status"] = "acknowledged"
            task["deadline_epoch"] += float(task.get("estimated_seconds", 0))
            return task
    except httpx.HTTPError as exc:
        task["status"] = "failed"
        task["error"] = str(exc)
        raise HTTPException(status_code=502, detail=f"robot unreachable: {exc}")


async def assign_move_task(
    x: float,
    y: float,
    minimum_battery: float = 20,
    robot_id: str | None = None,
    excluded_robot_ids: set[str] | None = None,
    retry_count: int = 0,
):
    return await assign_task(
        "move_to",
        {"x": x, "y": y},
        x,
        y,
        minimum_battery,
        robot_id,
        excluded_robot_ids,
        retry_count,
    )


async def assign_delivery_task(
    package_id: str,
    destination: str,
    minimum_battery: float = 20,
    robot_id: str | None = None,
    excluded_robot_ids: set[str] | None = None,
    retry_count: int = 0,
    pickup: str | dict | None = None,
):
    """pickup is a location name (where the package waits), or a resolved spot
    such as {"space": "floor4", "u": ..., "v": ...} where a robot died with it."""
    if not package_id.strip():
        raise HTTPException(status_code=400, detail="package_id is required")
    location = resolve_destination(destination)
    payload = {
        "package_id": package_id,
        "destination": location,
    }
    if isinstance(pickup, str) and pickup.strip():
        pickup = {**resolve_destination(pickup), "space": "campus"}
        if pickup.get("door_id"):
            raise HTTPException(status_code=400, detail="pickup must be a campus location such as college of professional and continuing education")
    if pickup:
        payload["pickup"] = pickup
    # The robot nearest the package (pickup spot, else the destination) takes it.
    task = await assign_task(
        "deliver_package",
        payload,
        location["x"],
        location["y"],
        minimum_battery,
        robot_id,
        excluded_robot_ids,
        retry_count,
        target=pickup or location,
    )
    task["destination"] = location
    return task


async def assign_drive_task(robot_id: str | None, steps, minimum_battery: float = 20):
    """Simple motion (straight runs, turns) for one named robot; never handed off."""
    if not robot_id:
        raise HTTPException(status_code=400, detail="drive needs a robot_id")
    if not isinstance(steps, list) or not steps:
        raise HTTPException(status_code=400, detail="drive needs at least one step")
    payload = {"steps": []}
    for s in steps:
        s = dict(s)
        step = {"action": str(s.get("action", "")).lower(), "amount": float(s.get("amount", 0))}
        if s.get("speed"):
            step["speed"] = float(s["speed"])  # the robot validates and caps it
        payload["steps"].append(step)
    # The robot validates the steps. The 50,50 target only ranks candidates,
    # and without handoff the only candidate is robot_id.
    return await assign_task("drive", payload, 50, 50, minimum_battery, robot_id, allow_handoff=False)


def describe_steps(steps: list[dict]) -> str:
    return ", ".join(
        f"{s['action']} {s['amount']:g}{' m' if s['action'] in ('forward', 'backward') else '°'}"
        + (f" at speed {s['speed']:g}" if s.get("speed") else "")
        for s in steps
    )


SPEED_PARAMETER = {
    "type": "number",
    "description": (
        "Optional throttle from 0.1 to 1: 1 is full speed, 0.3 is normal. Only set it "
        "when the user asks for a speed (fast, full speed, slowly)."
    ),
}


def gemini_tools():
    move_tool = types.FunctionDeclaration(
        name="assign_move_task",
        description=(
            "Assign one robot to move to an x,y position on the 0 to 100 map. "
            "If robot_id is unavailable, hand the task to another available robot."
        ),
        parameters={
            "type": "object",
            "properties": {
                "x": {"type": "number", "description": "Target x coordinate from 0 to 100."},
                "y": {"type": "number", "description": "Target y coordinate from 0 to 100."},
                "minimum_battery": {"type": "number", "description": "Minimum battery percentage."},
                "robot_id": {"type": "string", "description": "Optional preferred robot ID, such as robot1."},
            },
            "required": ["x", "y"],
        },
    )
    delivery_tool = types.FunctionDeclaration(
        name="assign_delivery_task",
        description=(
            "Deliver a package to a named map location. Choose the nearest available "
            "robot, or hand off to another robot if the requested robot is unavailable."
        ),
        parameters={
            "type": "object",
            "properties": {
                "package_id": {"type": "string", "description": "Package identifier."},
                "destination": {
                    "type": "string",
                    "description": "Known location name, a 4th-floor ECS room such as 407, or x,y coordinates.",
                },
                "pickup": {
                    "type": "string",
                    "description": "Optional known location where the package is picked up first, such as college of professional and continuing education.",
                },
                "minimum_battery": {"type": "number", "description": "Minimum battery percentage."},
                "robot_id": {"type": "string", "description": "Optional preferred robot ID."},
            },
            "required": ["package_id", "destination"],
        },
    )
    drive_tool = types.FunctionDeclaration(
        name="drive_robot",
        description=(
            "Drive one named robot with simple motion commands, run in order: "
            "forward/backward move straight in meters, left/right turn in place in degrees. "
            "Use for straight runs and turns, not for going to a map location."
        ),
        parameters={
            "type": "object",
            "properties": {
                "robot_id": {"type": "string", "description": "Robot to drive, such as robot3."},
                "steps": {
                    "type": "array",
                    "description": "Motion commands, executed in order.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["forward", "backward", "left", "right"]},
                            "amount": {
                                "type": "number",
                                "description": "Meters for forward/backward, degrees for left/right (max 360).",
                            },
                            "speed": SPEED_PARAMETER,
                        },
                        "required": ["action", "amount"],
                    },
                },
            },
            "required": ["robot_id", "steps"],
        },
    )
    autopilot_tool = types.FunctionDeclaration(
        name="start_autopilot",
        description=(
            "Let one named robot with a camera drive itself toward a visual goal, looking "
            "through its camera after every small move (for example 'drive until you see "
            "the door', 'find the red box', 'follow the hallway and stop at the wall')."
        ),
        parameters={
            "type": "object",
            "properties": {
                "robot_id": {"type": "string", "description": "Robot to drive, such as robot3."},
                "goal": {"type": "string", "description": "What the robot should do, in plain words."},
            },
            "required": ["robot_id", "goal"],
        },
    )
    return [types.Tool(function_declarations=[move_tool, delivery_tool, drive_tool, autopilot_tool])]


TASK_WATCH_SECONDS = float(os.getenv("TASK_WATCH_SECONDS", "0.5"))


async def watch_tasks():
    """Refresh active tasks so a dead robot's task is handed off even when no
    dashboard is polling."""
    while True:
        await asyncio.sleep(TASK_WATCH_SECONDS)
        for task_id, task in list(tasks.items()):
            if task.get("status") in ("acknowledged", "running"):
                try:
                    await get_task(task_id)
                except Exception as exc:  # keep watching the other tasks
                    print(f"task watch failed for {task_id}: {exc!r}", flush=True)


@app.on_event("startup")
async def start_task_watch():
    asyncio.create_task(watch_tasks())


@app.get("/health")
def health():
    return {
        "ok": True,
        "gemini_configured": bool(GEMINI_API_KEY),
        "registry_url": REGISTRY_URL,
    }


@app.get("/fleet")
async def fleet():
    try:
        return {"robots": await fleet_snapshot()}
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"registry unavailable: {exc}")


@app.post("/tasks")
async def direct_task(req: DirectTaskRequest):
    if req.type == "move_to":
        return await assign_move_task(
            float(req.payload.get("x", -1)),
            float(req.payload.get("y", -1)),
            req.minimum_battery,
            req.robot_id,
        )
    if req.type == "deliver_package":
        destination = req.payload.get("destination")
        if isinstance(destination, dict):
            destination = destination.get("name") or f"{destination.get('x')},{destination.get('y')}"
        return await assign_delivery_task(
            req.payload.get("package_id", ""),
            destination or "",
            req.minimum_battery,
            req.robot_id,
            pickup=req.payload.get("pickup"),
        )
    if req.type == "drive":
        return await assign_drive_task(req.robot_id, req.payload.get("steps"), req.minimum_battery)
    raise HTTPException(status_code=400, detail="unsupported task type")


@app.get("/tasks/{task_id}")
async def get_task(task_id: str):
    task = tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task["status"] in ("completed", "failed", "cancelled", "reassigned"):
        return task

    # Refresh the task from the robot so the dashboard sees running/completed
    # rather than only the initial acknowledgement. This is also where the
    # simulator detects a dead robot or an expired task.
    try:
        robots = await fleet_snapshot()
        robot = next((r for r in robots if r["name"] == task["robot_id"]), None)
        expired = time.time() > task.get("deadline_epoch", float("inf"))
        unavailable = not robot or not robot["reachable"] or not robot["state"]
        if robot and robot["state"]:
            unavailable = unavailable or robot["state"].get("status") != "alive"
            # A robot clears accepting_tasks while it drives a floor route, so
            # only a low-battery return counts as giving the task up.
            unavailable = unavailable or robot["state"].get("mode") == "returning_to_charge"

        if expired or unavailable:
            reason = "task timed out" if expired else "assigned robot became unavailable"
            return await reassign_or_fail(task, reason)

        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(
                f"{robot_url(robot)}/tasks/{task_id}",
                headers=robot_headers(),
            )
            if response.status_code == 200:
                task.update(response.json())
                # The robot itself gave the task up because it failed or ran low on battery
                # (it may say so before our fleet check sees it): hand the task on.
                if task["status"] == "failed" and str(task.get("error", "")).startswith("robot became"):
                    return await reassign_or_fail(task, task["error"])
    except (httpx.HTTPError, ValueError):
        return await reassign_or_fail(task, "robot status check failed")
    return task


def handoff_pickup(task: dict, old_robot: str) -> tuple[dict | None, str]:
    """Where the replacement collects the package: where the old robot was last
    seen if it had the package, otherwise the original pickup."""
    original = task["payload"].get("pickup")
    seen = (last_seen.get(old_robot) or {}).get("state") or {}
    had_package = not original or original.get("space") == "floor4" or bool(task.get("picked_up_at"))
    if not had_package or not seen:
        return original, "before picking the package up" if not had_package else "at an unknown spot"
    pose = seen.get("pose") or {}
    if seen.get("space") == "floor4" and "u" in pose:
        u, v = float(pose["u"]), float(pose["v"])
        return {"space": "floor4", "u": round(u, 2), "v": round(v, 2)}, f"on the ECS 4th floor, {describe_floor4_spot(u, v)}"
    position = seen.get("position") or {}
    return ({"space": "campus", "x": position.get("x"), "y": position.get("y"), "name": f"{old_robot}'s last position"},
            f"on campus at ({position.get('x')}, {position.get('y')})")


async def reassign_or_fail(task: dict, reason: str):
    # A drive is meant for one physical robot, so it is never moved to another.
    if task["type"] == "drive" or task.get("retry_count", 0) >= task.get("max_retries", MAX_TASK_RETRIES):
        task["status"] = "failed"
        task["error"] = reason
        task["failed_at"] = now()
        return task

    old_robot = task["robot_id"]
    task["status"] = "reassigning"
    task["error"] = reason
    try:
        if task["type"] == "deliver_package":
            destination = task["payload"]["destination"]
            pickup, note = handoff_pickup(task, old_robot)
            replacement = await assign_delivery_task(
                task["payload"]["package_id"],
                destination.get("door_id") or destination.get("name") or f"{destination['x']},{destination['y']}",
                task.get("minimum_battery", 20),
                excluded_robot_ids={old_robot},
                retry_count=task.get("retry_count", 0) + 1,
                pickup=pickup,
            )
            task["handoff_note"] = (
                f"{old_robot} stopped ({reason}) {note}; {replacement['robot_id']} is picking up "
                f"package {task['payload']['package_id']} there and delivering it to {destination.get('name')}."
            )
            replacement["handoff_from"] = old_robot
            replacement["handoff_reason"] = reason
            replacement["handoff_note"] = task["handoff_note"]
        else:
            replacement = await assign_move_task(
                task["payload"]["x"],
                task["payload"]["y"],
                task.get("minimum_battery", 20),
                excluded_robot_ids={old_robot},
                retry_count=task.get("retry_count", 0) + 1,
            )
        task["status"] = "reassigned"
        task["replacement_task_id"] = replacement["task_id"]
        task["replacement_robot_id"] = replacement["robot_id"]
        task["reassigned_at"] = now()
    except HTTPException as exc:
        task["status"] = "failed"
        task["error"] = f"{reason}; reassignment failed: {exc.detail}"
        task["failed_at"] = now()
    return task


@app.get("/tasks")
async def list_tasks():
    result = []
    for task_id in list(tasks):
        result.append(await get_task(task_id))
    return {"tasks": result}


@app.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, req: CancelTaskRequest):
    task = tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task["status"] in ("completed", "failed", "cancelled", "reassigned"):
        return task

    try:
        robots = await fleet_snapshot()
        robot = next((r for r in robots if r["name"] == task["robot_id"]), None)
        if robot:
            async with httpx.AsyncClient(timeout=3) as client:
                await client.post(
                    f"{robot_url(robot)}/tasks/{task_id}/cancel",
                    headers={**robot_headers(), "Content-Type": "application/json"},
                    json={"reason": req.reason},
                )
    except httpx.HTTPError:
        task["error"] = "robot was unreachable during cancellation"
    task["status"] = "cancelled"
    task["error"] = req.reason
    task["cancelled_at"] = now()
    return task


@app.post("/chat")
async def chat(req: ChatRequest):
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=503, detail="GEMINI_API_KEY is not configured")
    if genai is None:
        raise HTTPException(status_code=503, detail="google-genai is not installed")

    try:
        fleet = await fleet_snapshot()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"fleet unavailable: {exc}")
    keep = ("status", "mode", "accepting_tasks", "battery", "space", "position", "camera")
    fleet_context = [
        {
            "name": robot["name"],
            "reachable": robot["reachable"],
            "state": {k: robot["state"].get(k) for k in keep} if robot["state"] else None,
        }
        for robot in fleet
    ]
    prompt = (
        "You control a simulated robot fleet. Use assign_move_task for a simple move "
        "and assign_delivery_task when the user mentions delivering a package. Use "
        "drive_robot when the user asks a robot to drive straight, back up, or turn "
        "(for example 'robot3 go forward 1 meter then turn left'); a turn with no "
        "angle means 90 degrees. drive_robot needs a named robot: if the user does not "
        "say which robot to drive, ask instead of picking one. Use start_autopilot "
        "when the user wants a robot to react to what its camera sees (find, look for, "
        "until you see, follow, avoid); only robots whose state has camera true can do "
        "that. Only pass robot_id when the user names that robot; otherwise leave it "
        "out so the nearest robot is chosen. Never "
        "invent robot IDs and never issue HTTP or shell "
        "commands. If the requested robot is unavailable, hand the task to another "
        "available robot and explain the handoff. If the request is unrelated, "
        "explain that only move, drive, and package-delivery tasks are currently supported. "
        "Known locations include north entrance, east entrance, west entrance, "
        f"south entrance, warehouse, main door, and these campus buildings: {', '.join(CAMPUS_BUILDINGS)} "
        "(cpace = College of Professional and Continuing Education, vec = Vivian Engineering Center). "
        "Any of them can be a pickup or a destination. Rooms on the ECS "
        "4th floor (for example 407) are delivery destinations: pass the room number as "
        "destination (a specific door as 405-1; door A of a room is its first door), and pass where the package comes from (for example cpace) as "
        "pickup. If a robot dies mid-delivery the system hands the package to the robot "
        "closest to where it died; a car parked on the 4th floor (for example at room 411) is "
        "closest to a failure there, so you do not need to choose it yourself. The map uses "
        "x and y from 0 to 100; interpret top "
        "right as approximately (90, 90), top left as (10, 90), bottom right "
        "as (90, 10), and bottom left as (10, 10).\n\n"
        f"Fleet snapshot:\n{fleet_context}\n\nUser request: {req.message}"
    )
    try:
        # Async, so a slow Gemini answer doesn't freeze fleet polling and task watching.
        response, _ = await generate(prompt, types.GenerateContentConfig(tools=gemini_tools()))
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail=f"Gemini did not answer within {GEMINI_TIMEOUT_SECONDS:g} s; try again")
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Gemini request failed ({type(exc).__name__}): {exc}",
        )
    calls = getattr(response, "function_calls", None) or []
    if not calls:
        return {"message": response.text, "executed": None}

    call = calls[0]
    if call.name not in ("assign_move_task", "assign_delivery_task", "drive_robot", "start_autopilot"):
        raise HTTPException(status_code=400, detail=f"unsupported Gemini tool: {call.name}")
    args = dict(call.args or {})
    # Gemini may name a robot nobody asked for; "pick a robot" means the nearest one.
    if call.name in ("assign_move_task", "assign_delivery_task") and str(args.get("robot_id", "")).lower() not in req.message.lower():
        args.pop("robot_id", None)
    if call.name == "start_autopilot":
        mission = await start_autopilot(
            AutopilotRequest(robot_id=str(args.get("robot_id", "")), goal=str(args.get("goal", "")))
        )
        return {
            "message": f"{mission['robot_id']} autopilot started: {mission['goal']}",
            "executed": None,
            "autopilot": mission,
            "gemini_tool": {"name": call.name, "arguments": args},
        }
    try:
        if call.name == "drive_robot":
            result = await assign_drive_task(args.get("robot_id"), args.get("steps"))
        elif call.name == "assign_delivery_task":
            result = await assign_delivery_task(
                str(args.get("package_id", "")),
                str(args.get("destination", "")),
                float(args.get("minimum_battery", 20)),
                args.get("robot_id"),
                pickup=args.get("pickup"),
            )
        else:
            result = await assign_move_task(
                float(args.get("x", -1)),
                float(args.get("y", -1)),
                float(args.get("minimum_battery", 20)),
                args.get("robot_id"),
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Task execution failed ({type(exc).__name__}): {exc}",
        )
    return {
        "message": (
            f"{result['handoff_reason']}; assigned {result['robot_id']} instead."
            if result.get("handoff_reason")
            else (
                f"Assigned {result['robot_id']} to deliver package "
                f"{result['payload']['package_id']}"
                + (f" from {result['payload']['pickup']['name']}" if result["payload"].get("pickup", {}).get("name") else "")
                + f" to {result['destination']['name']}."
                if result["type"] == "deliver_package"
                else f"Driving {result['robot_id']}: {describe_steps(result['payload']['steps'])}."
                if result["type"] == "drive"
                else f"Assigned {result['robot_id']} to move to ({result['payload']['x']}, {result['payload']['y']})."
            )
        ),
        "executed": result,
        "gemini_tool": {"name": call.name, "arguments": args},
    }


# ---- camera + autopilot -----------------------------------------------------

async def find_robot(robot_id: str) -> dict:
    async with httpx.AsyncClient(timeout=3) as client:
        response = await client.get(REGISTRY_URL)
        response.raise_for_status()
    robot = next((r for r in response.json().get("robots", []) if r["name"] == robot_id), None)
    if not robot:
        raise HTTPException(status_code=404, detail=f"{robot_id} is not registered")
    return robot


async def camera_frame(robot_id: str) -> bytes:
    try:
        robot = await find_robot(robot_id)
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(f"{robot_url(robot)}/camera", headers=robot_headers())
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"{robot_id} camera unreachable: {exc}")
    if response.status_code != 200:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise HTTPException(status_code=response.status_code, detail=detail)
    return response.content


@app.get("/robots/{robot_id}/camera")
async def robot_camera(robot_id: str):
    return Response(await camera_frame(robot_id), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/robots/{robot_id}/camera.mjpg")
async def robot_camera_stream(robot_id: str):
    """Live camera video, relayed from the robot without buffering."""
    robot = await find_robot(robot_id)
    client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=10.0))
    try:
        response = await client.send(
            client.build_request("GET", f"{robot_url(robot)}/camera.mjpg", headers=robot_headers()),
            stream=True,
        )
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"{robot_id} camera unreachable: {exc}")
    if response.status_code != 200:
        body = (await response.aread()).decode(errors="replace")
        await response.aclose()
        await client.aclose()
        raise HTTPException(status_code=response.status_code, detail=body)

    async def chunks():
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        except httpx.HTTPError:
            pass
        finally:
            await response.aclose()
            await client.aclose()

    return StreamingResponse(
        chunks(), media_type=response.headers["content-type"], headers={"Cache-Control": "no-store"}
    )


def autopilot_tools():
    move_tool = types.FunctionDeclaration(
        name="move",
        description="Make one small move, then you will see a new camera image.",
        parameters={
            "type": "object",
            "properties": {
                "observation": {"type": "string", "description": "What the camera shows, in one short sentence."},
                "action": {"type": "string", "enum": ["forward", "backward", "left", "right"]},
                "amount": {
                    "type": "number",
                    "description": (
                        f"Meters for forward/backward (max {AUTOPILOT_MAX_METERS:g}), "
                        f"degrees for left/right (max {AUTOPILOT_MAX_DEGREES:g})."
                    ),
                },
                "speed": SPEED_PARAMETER,
                "reason": {"type": "string", "description": "Why this move gets closer to the goal."},
            },
            "required": ["observation", "action", "amount", "reason"],
        },
    )
    finish_tool = types.FunctionDeclaration(
        name="finish",
        description="Stop driving because the goal is reached, or it cannot be reached.",
        parameters={
            "type": "object",
            "properties": {
                "observation": {"type": "string", "description": "What the camera shows, in one short sentence."},
                "success": {"type": "boolean", "description": "True if the goal was reached."},
                "summary": {"type": "string", "description": "One sentence for the user."},
            },
            "required": ["observation", "success", "summary"],
        },
    )
    return [types.Tool(function_declarations=[move_tool, finish_tool])]


def autopilot_prompt(mission: dict, step: int) -> str:
    history = "\n".join(
        f"{e['step']}. saw: {e['observation']} -> {e['action']} {e['amount']:g}"
        + (f" at speed {e['speed']:g}" if e.get("speed") else "")
        for e in mission["log"] if e.get("action")
    ) or "none yet"
    return (
        "You drive a small robot car (a JetRacer) using the attached image from its "
        f"front camera. Goal: {mission['goal']}\n\n"
        "Each turn, call move for one small step, or finish when the goal is reached "
        "or clearly impossible. The car steers like a car: left/right drive forward "
        "along an arc at full lock, so a turn needs free space ahead; back up first if "
        "something is close in front. Never drive forward into an obstacle closer than "
        "about half a meter. Size each move by how much free space you see: short moves "
        "(0.2 to 0.5 m, 15 to 45 degrees) near obstacles or when unsure, longer straight "
        f"moves (up to {AUTOPILOT_MAX_METERS:g} m) when the path ahead is clearly open. "
        "If the goal asks for a speed (full speed, fast, slowly), set speed on every "
        "move to match: 1 is full speed, 0.3 is normal. The image is all you know "
        "about the world.\n\n"
        f"This is step {step} of at most {mission['max_steps']}.\n"
        f"Moves so far:\n{history}"
    )


async def wait_for_drive(task_id: str) -> dict:
    while True:
        task = await get_task(task_id)
        if task["status"] in FINAL_TASK_STATUSES:
            return task
        await asyncio.sleep(0.3)


async def run_autopilot(mission: dict):
    config = types.GenerateContentConfig(
        tools=autopilot_tools(),
        tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="ANY")),
    )
    try:
        for step in range(1, mission["max_steps"] + 1):
            mission["step"] = step
            mission["phase"] = "looking"
            frame = await camera_frame(mission["robot_id"])
            mission["phase"] = "thinking"
            response, _ = await generate(
                [types.Part.from_bytes(data=frame, mime_type="image/jpeg"), autopilot_prompt(mission, step)],
                config,
            )
            calls = getattr(response, "function_calls", None) or []
            if not calls:
                raise RuntimeError(f"Gemini did not choose a move: {response.text}")
            args = dict(calls[0].args or {})
            entry = {"step": step, "observation": str(args.get("observation", "")), "ts": time.time()}
            mission["log"].append(entry)

            if calls[0].name == "finish":
                entry["summary"] = str(args.get("summary", ""))
                mission["status"] = "succeeded" if args.get("success") else "gave_up"
                mission["summary"] = entry["summary"]
                return

            action = str(args.get("action", "")).lower()
            limit = AUTOPILOT_MAX_METERS if action in ("forward", "backward") else AUTOPILOT_MAX_DEGREES
            amount = round(min(max(float(args.get("amount", 0)), 0.05), limit), 2)
            entry.update(action=action, amount=amount, reason=str(args.get("reason", "")))
            move = {"action": action, "amount": amount}
            if args.get("speed"):
                move["speed"] = entry["speed"] = round(min(max(float(args["speed"]), 0.1), 1.0), 2)
            mission["phase"] = "driving"
            task = await assign_drive_task(mission["robot_id"], [move])
            mission["task_id"] = task["task_id"]
            task = await wait_for_drive(task["task_id"])
            if task["status"] != "completed":
                raise RuntimeError(f"drive {task['status']}: {task.get('error', '')}")
        mission["status"] = "gave_up"
        mission["summary"] = f"stopped after {mission['max_steps']} steps without finishing"
    except asyncio.CancelledError:
        mission["status"] = "stopped"
        raise
    except HTTPException as exc:
        mission["status"] = "failed"
        mission["summary"] = str(exc.detail)
    except Exception as exc:
        mission["status"] = "failed"
        mission["summary"] = f"{type(exc).__name__}: {exc}"
    finally:
        mission["phase"] = None
        mission["finished_at"] = now()
        autopilot_workers.pop(mission["robot_id"], None)


@app.post("/autopilot")
async def start_autopilot(req: AutopilotRequest):
    if not GEMINI_API_KEY or genai is None:
        raise HTTPException(status_code=503, detail="autopilot needs GEMINI_API_KEY and google-genai")
    goal = req.goal.strip()
    if not goal:
        raise HTTPException(status_code=400, detail="autopilot needs a goal")
    if req.robot_id in autopilot_workers:
        raise HTTPException(status_code=409, detail=f"{req.robot_id} autopilot is already running")
    await camera_frame(req.robot_id)  # fail now, not mid-mission, if there is no camera
    mission = {
        "robot_id": req.robot_id,
        "goal": goal,
        "max_steps": max(1, min(req.max_steps, 100)),
        "status": "running",
        "phase": None,
        "step": 0,
        "task_id": None,
        "summary": None,
        "log": [],
        "started_at": now(),
        "finished_at": None,
    }
    autopilots[req.robot_id] = mission
    autopilot_workers[req.robot_id] = asyncio.create_task(run_autopilot(mission))
    return mission


@app.get("/autopilot")
def list_autopilots():
    return {"missions": list(autopilots.values())}


@app.get("/autopilot/{robot_id}")
def get_autopilot(robot_id: str):
    mission = autopilots.get(robot_id)
    if not mission:
        raise HTTPException(status_code=404, detail=f"{robot_id} has no autopilot mission")
    return mission


@app.post("/autopilot/{robot_id}/stop")
async def stop_autopilot(robot_id: str):
    mission = get_autopilot(robot_id)
    worker = autopilot_workers.get(robot_id)
    if worker:
        worker.cancel()
    # Stop the car now rather than at the end of its current move.
    if mission["task_id"] in tasks:
        await cancel_task(mission["task_id"], CancelTaskRequest(reason="autopilot stopped"))
    if mission["status"] == "running":
        mission["status"] = "stopped"
    return mission
