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

## Run across multiple machines (Docker Swarm)

`docker compose` only networks containers on one machine. To spread robots
across multiple computers and still have them reach each other by name, join
the machines into a **Docker Swarm** and deploy on its **overlay network**
(an overlay network is routed between hosts, unlike the local `bridge`
network `docker compose` uses).

**1. Pick one machine as the manager and initialize the swarm:**

```bash
# on machine A — use the IP the other machine can reach it at
docker swarm init --advertise-addr <machine-A-ip>
```

This prints a `docker swarm join --token ...` command.

**2. Join the other machine(s) as workers:**

```bash
# on machine B, paste the command machine A printed
docker swarm join --token <token> <machine-A-ip>:2377
```

Confirm both are in the cluster (run on the manager):

```bash
docker node ls
```

**3. Build the robot image on every machine** (swarm doesn't build images,
it only runs them — every node needs `lastmile` available locally, or you
push it to a registry all nodes can pull from):

```bash
docker build -t lastmile ./robot
```

**4. Deploy the fleet as a stack** (from the manager only, even after more
machines join later):

```bash
docker stack deploy -c docker-stack.yml lastmile
```

`docker-stack.yml` defines **10 robots**, pinned by placement constraint:
`robot1`–`robot5` to the manager node, `robot6`–`robot10` to a worker node.
If you deploy before machine B has joined, `robot6`–`10` sit at `0/1`
("pending", no node satisfies their constraint yet) — as soon as machine B
joins as a worker, Swarm automatically schedules them there. No redeploy
needed.

**5. Verify:**

```bash
docker service ls                     # robot1-5 show 1/1, robot6-10 too once B has joined
curl localhost:8001/state             # works from either machine

# prove cross-machine messaging: pick two robots and check they can talk
curl -X POST localhost:8001/send \
  -H 'Content-Type: application/json' \
  -d '{"to":"robot8","body":{"msg":"hello from another host"}}'
curl localhost:8008/state             # message should be in the inbox
```

### Frontend: does it show all 10?

Yes — `simulator/` auto-detects swarm mode and switches from listing local
containers to querying Swarm's service API, which is cluster-wide (the
manager knows about every service and where it's scheduled, even if the
container itself is running on machine B). Run `npm run dev` on the manager
machine and open http://localhost:3000 — you'll see all 10 robots, correctly
reflecting which are up vs. still pending a node. Naming stays simple no
matter which physical machine each one lands on: always `robot1`…`robot10`.

**Tear down:**

```bash
docker stack rm lastmile              # remove the fleet
docker swarm leave --force            # on a worker, to leave the cluster
docker swarm leave --force            # on the manager, to disband it (last)
```

### Why this works

Docker Swarm gives every service a place in a cluster-wide DNS, backed by an
overlay network (VXLAN tunnels between hosts). A container on machine B can
resolve `robot1` and reach it exactly like `robot-net` in the single-machine
setup, even though the container actually runs on machine A. Ports you
publish (`8001:8000` etc.) are also reachable from **any** node's IP, not
just the one running that container, via Swarm's routing mesh.

