# Robot Swarm Simulator

A local, non-deployed simulation environment for testing LLM task decomposition and multi-robot coordination logic before running it on real Jetson hardware. Each simulated robot runs as an isolated Docker container, so the system can be scaled to N robots without needing N physical devices.

## Goal

Simulate a fleet of robots on a shared map, where each robot:
- Runs as its own Docker container (independent process/state)
- Can communicate with other robot containers over the network
- Has a controllable runtime state (battery level, alive/dead, unresponsive, on/off)

This lets us test coordination, failure handling, and task hand-off logic (e.g. the DHT-based orchestration from [[dht-frl-paper]]) against a swarm of "robots" without physical fleet access.

## Architecture

```
┌────────────┐      HTTP/WS       ┌──────────────┐
│  Next.js   │ ───────────────── │   FastAPI    │
│  Frontend  │ <───────────────── │ Orchestrator │
└────────────┘                    └──────┬───────┘
                                          │ Docker SDK
                                          │ (spawn/control containers)
                     ┌────────────────────┼────────────────────┐
                     ▼                    ▼                    ▼
              ┌────────────┐      ┌────────────┐      ┌────────────┐
              │ robot-01   │◄────►│ robot-02   │◄────►│ robot-N    │
              │ container  │      │ container  │      │ container  │
              └────────────┘      └────────────┘      └────────────┘
                     └── all on shared Docker network ──┘
```

- **Frontend (Next.js):** Map view + per-robot controls (toggle power, drain battery, force unresponsive/dead, view position/status).
- **Orchestrator (FastAPI):** Source of truth for the map and robot registry. Uses the Docker SDK to spawn/stop/inspect robot containers and relays control commands from the frontend.
- **Robot containers:** Each is a small FastAPI (or similar) service exposing `/state`, `/heartbeat`, and message endpoints. Robots discover and talk to each other directly over a shared Docker bridge network (not just through the orchestrator), so inter-robot messaging (e.g. task hand-off) can be tested realistically.
- **Network:** A dedicated Docker bridge network (`robot-net`) so containers can reach each other by container name/hostname.

## Robot states (controllable)

| State        | Effect                                      |
|--------------|----------------------------------------------|
| `on` / `off` | Container running vs. stopped                |
| `battery`    | 0–100, drains over time or set manually      |
| `alive`      | Normal operation                              |
| `dead`       | Container exists but stops processing/responding |
| `no_response`| Process alive but drops/ignores all messages (simulates network/hardware fault) |

## Tech stack

- **Frontend:** Next.js (map UI CSS Tailwind u, control panel)
- **Backend/Orchestrator:** FastAPI + Docker SDK for Python
- **Robots:** Lightweight FastAPI containers, one per robot
- **Networking:** Docker bridge network, containers communicate via HTTP or WebSocket
- **Container mgmt:** Docker Compose (base fleet) + Docker SDK (dynamic spawn/kill from orchestrator)

## Scope

In scope: robot container simulation, inter-robot networking, state control, basic map UI.
Out of scope (for now): deployment, real Jetson integration, persistence/database, auth.