"""LLM task gateway for the LastMile fleet.

Gemini plans through one narrow tool (assign_move_task). This service validates
the request, selects a healthy robot from the registry, and executes the task.
The Gemini key never leaves this service.
"""
import os
import math
import re
import time
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException
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
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
TASK_TIMEOUT_SECONDS = float(os.getenv("TASK_TIMEOUT_SECONDS", "15"))
MAX_TASK_RETRIES = int(os.getenv("MAX_TASK_RETRIES", "1"))
KNOWN_LOCATIONS = {
    "north entrance": {"x": 90.0, "y": 90.0},
    "east entrance": {"x": 90.0, "y": 50.0},
    "west entrance": {"x": 10.0, "y": 50.0},
    "south entrance": {"x": 50.0, "y": 10.0},
    "warehouse": {"x": 20.0, "y": 80.0},
    "main door": {"x": 50.0, "y": 10.0},
}
LOCATION_ALIASES = {
    "north gate": "north entrance",
    "east gate": "east entrance",
    "west gate": "west entrance",
    "south gate": "south entrance",
    "front door": "main door",
}
tasks: dict[str, dict] = {}


class ChatRequest(BaseModel):
    message: str


class DirectTaskRequest(BaseModel):
    type: str
    payload: dict
    minimum_battery: float = 20
    robot_id: str | None = None


class CancelTaskRequest(BaseModel):
    reason: str = "cancelled by user"


def now():
    return datetime.now(timezone.utc).isoformat()


def robot_headers():
    return {"Authorization": f"Bearer {ROBOT_AUTH_TOKEN}"} if ROBOT_AUTH_TOKEN else {}


def robot_url(robot):
    host = LOCAL_ROBOT_HOST if LOCAL_HOST_IP and robot.get("host") == LOCAL_HOST_IP else robot["host"]
    return f"http://{host}:{robot['port']}"


async def fleet_snapshot():
    async with httpx.AsyncClient(timeout=3) as client:
        response = await client.get(REGISTRY_URL)
        response.raise_for_status()
        robots = response.json().get("robots", [])
        result = []
        for robot in robots:
            try:
                state_response = await client.get(
                    f"{robot_url(robot)}/state",
                    headers=robot_headers(),
                )
                robot_state = state_response.json()
                result.append({**robot, "state": robot_state, "reachable": True})
            except (httpx.HTTPError, ValueError):
                result.append({**robot, "state": None, "reachable": False})
        return result


def resolve_destination(destination: str):
    normalized = destination.strip().lower()
    normalized = LOCATION_ALIASES.get(normalized, normalized)
    if normalized in KNOWN_LOCATIONS:
        return {"name": normalized, **KNOWN_LOCATIONS[normalized]}
    coordinates = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", normalized)
    if coordinates:
        return {"name": destination, "x": float(coordinates.group(1)), "y": float(coordinates.group(2))}
    raise HTTPException(
        status_code=400,
        detail=f"unknown destination '{destination}'; known locations: {', '.join(KNOWN_LOCATIONS)}",
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
):
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
                else:
                    handoff_reason = (
                        f"{robot_id} battery is below the required "
                        f"{minimum_battery:g}%"
                    )
            candidates = available
    else:
        candidates = available

    if not candidates:
        if handoff_reason:
            raise HTTPException(
                status_code=409,
                detail=f"{handoff_reason}; no other suitable robot is available",
            )
        raise HTTPException(status_code=409, detail="no suitable robot is available")

    def selection_key(item):
        position = item["state"].get("position") or {"x": 0, "y": 0}
        distance = math.hypot(
            float(position.get("x", 0)) - target_x,
            float(position.get("y", 0)) - target_y,
        )
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
):
    if not package_id.strip():
        raise HTTPException(status_code=400, detail="package_id is required")
    location = resolve_destination(destination)
    payload = {
        "package_id": package_id,
        "destination": location,
    }
    task = await assign_task(
        "deliver_package",
        payload,
        location["x"],
        location["y"],
        minimum_battery,
        robot_id,
        excluded_robot_ids,
        retry_count,
    )
    task["destination"] = location
    return task


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
                "destination": {"type": "string", "description": "Known location name or x,y coordinates."},
                "minimum_battery": {"type": "number", "description": "Minimum battery percentage."},
                "robot_id": {"type": "string", "description": "Optional preferred robot ID."},
            },
            "required": ["package_id", "destination"],
        },
    )
    return [types.Tool(function_declarations=[move_tool, delivery_tool])]


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
        )
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
            unavailable = unavailable or not robot["state"].get("accepting_tasks", False)

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
    except (httpx.HTTPError, ValueError):
        return await reassign_or_fail(task, "robot status check failed")
    return task


async def reassign_or_fail(task: dict, reason: str):
    if task.get("retry_count", 0) >= task.get("max_retries", MAX_TASK_RETRIES):
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
            replacement = await assign_delivery_task(
                task["payload"]["package_id"],
                destination.get("name") or f"{destination['x']},{destination['y']}",
                task.get("minimum_battery", 20),
                excluded_robot_ids={old_robot},
                retry_count=task.get("retry_count", 0) + 1,
            )
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
    fleet_context = [
        {
            "name": robot["name"],
            "reachable": robot["reachable"],
            "state": robot["state"],
        }
        for robot in fleet
    ]
    prompt = (
        "You control a simulated robot fleet. Use assign_move_task for a simple move "
        "and assign_delivery_task when the user mentions delivering a package. Never "
        "invent robot IDs and never issue HTTP or shell "
        "commands. If the requested robot is unavailable, hand the task to another "
        "available robot and explain the handoff. If the request is unrelated, "
        "explain that only move and package-delivery tasks are currently supported. "
        "Known locations include north entrance, east entrance, west entrance, "
        "south entrance, warehouse, and main door. The map uses "
        "x and y from 0 to 100; interpret top "
        "right as approximately (90, 90), top left as (10, 90), bottom right "
        "as (90, 10), and bottom left as (10, 10).\n\n"
        f"Fleet snapshot:\n{fleet_context}\n\nUser request: {req.message}"
    )
    client = genai.Client(api_key=GEMINI_API_KEY)
    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(tools=gemini_tools()),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Gemini request failed ({type(exc).__name__}): {exc}",
        )
    calls = getattr(response, "function_calls", None) or []
    if not calls:
        return {"message": response.text, "executed": None}

    call = calls[0]
    if call.name not in ("assign_move_task", "assign_delivery_task"):
        raise HTTPException(status_code=400, detail=f"unsupported Gemini tool: {call.name}")
    args = dict(call.args or {})
    try:
        if call.name == "assign_delivery_task":
            result = await assign_delivery_task(
                str(args.get("package_id", "")),
                str(args.get("destination", "")),
                float(args.get("minimum_battery", 20)),
                args.get("robot_id"),
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
                f"{result['payload']['package_id']} to {result['destination']['name']}."
                if result["type"] == "deliver_package"
                else f"Assigned {result['robot_id']} to move to ({result['payload']['x']}, {result['payload']['y']})."
            )
        ),
        "executed": result,
        "gemini_tool": {"name": call.name, "arguments": args},
    }
