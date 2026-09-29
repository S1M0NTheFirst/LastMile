"""Single robot node.

One image, run as any number of containers on any number of machines. Each
robot registers with a central registry (the simulator), which assigns its
name first-come-first-served (robot0, robot1, ...) and tells robots where
every other robot is reachable, so any robot can message any other.
"""
import os
import asyncio
import random
import time

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# Registry address, e.g. "192.168.1.132:3000" (the machine running the simulator).
REGISTRY = os.getenv("REGISTRY", "")
# Where other machines can reach this robot: the host's LAN IP + published port.
HOST_IP = os.getenv("HOST_IP", "")
HOST_PORT = int(os.getenv("HOST_PORT", "0"))
# Battery drains this many points per tick (1 tick/sec).
DRAIN_RATE = float(os.getenv("DRAIN_RATE", "0.1"))

app = FastAPI(title="Robot")

state = {
    "id": None,             # assigned by the registry
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


async def _register_loop():
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


async def _registry_robots() -> list[dict]:
    async with httpx.AsyncClient(timeout=3.0) as client:
        r = await client.get(f"http://{REGISTRY}/api/registry")
        return r.json()["robots"]


@app.on_event("startup")
async def _startup():
    if not (REGISTRY and HOST_IP and HOST_PORT):
        raise RuntimeError("REGISTRY, HOST_IP and HOST_PORT must be set (use join.py)")
    asyncio.create_task(_battery_loop())
    asyncio.create_task(_register_loop())


# ---- observability ----------------------------------------------------------

@app.get("/state")
def get_state():
    return state


@app.get("/heartbeat")
def heartbeat():
    if not _responsive():
        raise HTTPException(status_code=503, detail=f"{state['id']} not responsive")
    return {"id": state["id"], "ts": time.time(), "battery": state["battery"]}


# ---- inter-robot messaging --------------------------------------------------

class Message(BaseModel):
    sender: str
    body: dict


@app.post("/message")
def receive(msg: Message):
    if not _responsive():
        raise HTTPException(status_code=503, detail=f"{state['id']} dropped message")
    entry = {"from": msg.sender, "body": msg.body, "ts": time.time()}
    state["inbox"].append(entry)
    return {"accepted": True}


class SendRequest(BaseModel):
    to: str            # robot name, e.g. "robot23"
    body: dict


@app.post("/send")
async def send(req: SendRequest):
    if not _responsive():
        raise HTTPException(status_code=503, detail=f"{state['id']} not responsive")
    try:
        robots = await _registry_robots()
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
        robots = await _registry_robots()
    except (httpx.HTTPError, KeyError, ValueError) as e:
        raise HTTPException(status_code=502, detail=f"registry unreachable: {e}")
    return {"peers": [r for r in robots if r["name"] != state["id"]]}


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
