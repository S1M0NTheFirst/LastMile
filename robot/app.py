"""Single robot node.

One image, run as N containers (robot1, robot2, ...). Each container is an
isolated robot with controllable runtime state. Robots talk to each other
directly over the shared Docker bridge network by container name.
"""
import os
import asyncio
import random
import time

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# Identity comes from the environment so every container shares one image.
ROBOT_ID = os.getenv("ROBOT_ID", "robot0")
PORT = int(os.getenv("PORT", "8000"))
# Comma-separated list of peer hostnames, e.g. "robot1,robot2,robot3".
PEERS = [p.strip() for p in os.getenv("PEERS", "").split(",") if p.strip()]
# Optional cross-machine peer endpoints. Format:
#   robot4=http://100.x.y.z:8004,robot5=http://100.x.y.z:8005
PEER_URLS = {}
for item in os.getenv("PEER_URLS", "").split(","):
    if "=" in item:
        name, url = item.split("=", 1)
        PEER_URLS[name.strip()] = url.strip().rstrip("/")
# Battery drains this many points per tick (1 tick/sec).
DRAIN_RATE = float(os.getenv("DRAIN_RATE", "0.1"))
LOW_BATTERY_THRESHOLD = float(os.getenv("LOW_BATTERY_THRESHOLD", "15"))

app = FastAPI(title=f"Robot {ROBOT_ID}")

state = {
    "id": ROBOT_ID,
    "power": "on",          # on | off  (off => container stopped, handled by orchestrator)
    "status": "alive",      # alive | dead | no_response
    "mode": "available",    # available | returning_to_charge | unavailable
    "accepting_tasks": True,
    "battery": 100.0,       # 0-100
    "position": {"x": round(random.uniform(0, 100), 1),
                 "y": round(random.uniform(0, 100), 1)},
    "inbox": [],            # messages received from peers
}
low_battery_notified = False
peer_health = {peer: None for peer in PEERS}


def responsive() -> bool:
    """dead / no_response robots drop everything except control endpoints."""
    return state["status"] == "alive"


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
                        "message": f"{ROBOT_ID} battery is low",
                    },
                )
            if state["battery"] == 0:
                await notify_peers(
                    "battery_dead",
                    {
                        "battery": 0,
                        "action": "unavailable",
                        "message": f"{ROBOT_ID} battery is dead",
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
            for peer in PEERS:
                peer_base = PEER_URLS.get(peer, f"http://{peer}:8000")
                is_healthy = True
                try:
                    response = await client.get(f"{peer_base}/heartbeat")
                    is_healthy = response.status_code == 200
                except httpx.HTTPError:
                    is_healthy = False

                previous = peer_health[peer]
                peer_health[peer] = is_healthy

                if previous is True and not is_healthy:
                    await notify_peers(
                        "peer_unresponsive",
                        {
                            "peer": peer,
                            "action": "remove_from_task_assignment",
                            "message": f"{peer} is no longer responding",
                        },
                    )


@app.on_event("startup")
async def startup():
    asyncio.create_task(battery_loop())
    asyncio.create_task(monitor_peers())


@app.on_event("shutdown")
async def shutdown():
    await notify_peers(
        "robot_offline",
        {
            "action": "powered_off",
            "message": f"{ROBOT_ID} is shutting down",
        },
    )


# ---- observability ----------------------------------------------------------

@app.get("/state")
def get_state():
    return state


@app.get("/heartbeat")
def heartbeat():
    if not responsive():
        raise HTTPException(status_code=503, detail=f"{ROBOT_ID} not responsive")
    return {"id": ROBOT_ID, "ts": time.time(), "battery": state["battery"]}


# ---- inter-robot messaging --------------------------------------------------

class Message(BaseModel):
    sender: str
    body: dict


@app.post("/message")
def receive(msg: Message):
    if not responsive():
        raise HTTPException(status_code=503, detail=f"{ROBOT_ID} dropped message")
    entry = {"from": msg.sender, "body": msg.body, "ts": time.time()}
    state["inbox"].append(entry)
    return {"accepted": True}


async def notify_peers(event: str, details: dict):
    """Send an automatic event to every configured peer."""
    body = {"event": event, "robot": ROBOT_ID, **details}
    async with httpx.AsyncClient(timeout=3.0) as client:
        for peer in PEERS:
            peer_base = PEER_URLS.get(peer, f"http://{peer}:8000")
            try:
                await client.post(
                    f"{peer_base}/message",
                    json={"sender": ROBOT_ID, "body": body},
                )
            except httpx.HTTPError:
                # A peer may be powered off or unreachable; the local robot
                # continues operating and can retry through later policies.
                pass


class SendRequest(BaseModel):
    to: str            # peer hostname (container name)
    body: dict


@app.post("/send")
async def send(req: SendRequest):
    if not responsive():
        raise HTTPException(status_code=503, detail=f"{ROBOT_ID} not responsive")
    peer_base = PEER_URLS.get(req.to, f"http://{req.to}:8000")
    url = f"{peer_base}/message"
    async with httpx.AsyncClient(timeout=3.0) as client:
        try:
            r = await client.post(url, json={"sender": ROBOT_ID, "body": req.body})
            return {"delivered": r.status_code == 200, "peer_status": r.status_code}
        except httpx.HTTPError as e:
            return {"delivered": False, "error": str(e)}


@app.get("/peers")
def peers():
    return {"peers": PEERS}


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
                    "message": f"{ROBOT_ID} changed status to {ctrl.status}",
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
