# LastMile — Robot Swarm Simulator

A local simulation environment for testing multi-robot coordination logic before
running it on real hardware. Each robot runs as its own Docker container on a
shared network, and a Next.js web app (the orchestrator + UI) lets you watch the
fleet on a map and control each robot's power, battery, and failure state.

See [Simulator.md](Simulator.md) for the full design.

## Requirements

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (running)
- [Node.js](https://nodejs.org/) 18+ and npm

## Clone

```bash
git clone https://github.com/S1M0NTheFirst/LastMile.git
cd LastMile
```

## Run the simulation

**1. Start the 5-robot fleet** (builds one `lastmile` image, runs `robot1`–`robot5`):

```bash
docker compose up --build -d
```

**2. Start the web app** in a second terminal:

```bash
cd simulator
npm install      # first time only
npm run dev
```

**3. Open the simulator:** http://localhost:3000

You'll see a map with a dot per robot and a control panel on the right to toggle
power, set battery, and force `alive` / `no_response` / `dead` states.

## Stop

```bash
# Ctrl+C in the simulator terminal to stop the web app
docker compose down          # stop and remove the robot containers
```

## Poke the robots directly (optional)

Each robot publishes its API on ports `8001`–`8005` (robot1 → 8001, etc.):

```bash
# view a robot's state
curl localhost:8001/state

# have robot1 message robot3 (delivered to robot3's inbox)
curl -X POST localhost:8001/send \
  -H 'Content-Type: application/json' \
  -d '{"to":"robot3","body":{"msg":"hello"}}'

# check robot3 received it
curl localhost:8003/state
```

`delivered:true` means the robots can reach each other over the shared network.
A `503` means the target is `dead`/`no_response`; a name-resolution error means
it is powered off.

