"""Single robot node.

One image, run as N containers (robot1, robot2, ...). Each container is an
isolated robot with controllable runtime state. Robots on the same machine
talk over the shared Docker bridge network by container name; robots on other
machines are reached at that machine's LAN IP + published port.
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
# Comma-separated peers. A bare name is a container on this machine's bridge
# network ("robot2" -> robot2:8000); "name=host:port" is a robot on another
# machine, e.g. "robot6=192.168.1.50:8006".
def _parse_peers(raw: str) -> dict[str, str]:
    peers = {}
    for entry in (p.strip() for p in raw.split(",")):
        if not entry:
            continue
        name, _, addr = entry.partition("=")
        peers[name.strip()] = addr.strip() or f"{name.strip()}:8000"
    return peers


PEERS = _parse_peers(os.getenv("PEERS", ""))
# Battery drains this many points per tick (1 tick/sec).
DRAIN_RATE = float(os.getenv("DRAIN_RATE", "0.1"))

app = FastAPI(title=f"Robot {ROBOT_ID}")

state = {
    "id": ROBOT_ID,
    "power": "on",          # on | off  (off => container stopped, handled by orchestrator)
    "status": "alive",      # alive | dead | no_response
    "battery": 100.0,       # 0-100
    "position": {"x": round(random.uniform(0, 100), 1),
                 "y": round(random.uniform(0, 100), 1)},
    "inbox": [],            # messages received from peers
}


def _responsive() -> bool:
    """dead / no_response robots drop everything except control endpoints."""
    return state["status"] == "alive"


async def _battery_loop():
    while True:
        await asyncio.sleep(1)
        if state["status"] != "dead" and state["battery"] > 0:
            state["battery"] = max(0.0, state["battery"] - DRAIN_RATE)
            if state["battery"] == 0:
                state["status"] = "dead"


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_battery_loop())


# ---- observability ----------------------------------------------------------

@app.get("/state")
def get_state():
    return state


@app.get("/heartbeat")
def heartbeat():
    if not _responsive():
        raise HTTPException(status_code=503, detail=f"{ROBOT_ID} not responsive")
    return {"id": ROBOT_ID, "ts": time.time(), "battery": state["battery"]}


# ---- inter-robot messaging --------------------------------------------------

class Message(BaseModel):
    sender: str
    body: dict


@app.post("/message")
def receive(msg: Message):
    if not _responsive():
        raise HTTPException(status_code=503, detail=f"{ROBOT_ID} dropped message")
    entry = {"from": msg.sender, "body": msg.body, "ts": time.time()}
    state["inbox"].append(entry)
    return {"accepted": True}


class SendRequest(BaseModel):
    to: str            # peer name from PEERS, or a raw host[:port]
    body: dict


@app.post("/send")
async def send(req: SendRequest):
    if not _responsive():
        raise HTTPException(status_code=503, detail=f"{ROBOT_ID} not responsive")
    addr = PEERS.get(req.to, req.to)
    if ":" not in addr:
        addr = f"{addr}:8000"
    url = f"http://{addr}/message"
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
def control(ctrl: Control):
    if ctrl.status is not None:
        if ctrl.status not in ("alive", "dead", "no_response"):
            raise HTTPException(status_code=400, detail="invalid status")
        state["status"] = ctrl.status
    if ctrl.battery is not None:
        state["battery"] = max(0.0, min(100.0, ctrl.battery))
    return state
