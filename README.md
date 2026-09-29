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

## Run across multiple machines (same network)

Each machine runs its own robots with `docker compose`. Robots on the same
machine reach each other by container name; robots on the other machine are
reached at that machine's **LAN IP + published port**. Works across macOS,
Windows and Linux.

| Machine | Compose file                   | Robots        | Ports        |
|---------|--------------------------------|---------------|--------------|
| A       | `docker-compose.yml`           | robot1–robot5 | 8001–8005    |
| B       | `docker-compose.machine-b.yml` | robot6–robot10| 8006–8010    |

**1. Find each machine's LAN IP** (both must be on the same Wi-Fi/LAN):

```bash
ipconfig getifaddr en0    # macOS
ipconfig                  # Windows (IPv4 Address)
hostname -I               # Linux
```

**2. On each machine, create `.env`** from `.env.example` with the *other*
machine's IP:

```bash
cp .env.example .env
# machine A: set MACHINE_B_IP=<B's IP>
# machine B: set MACHINE_A_IP=<A's IP>
```

**3. Start the robots:**

```bash
docker compose up --build -d                                  # machine A
docker compose -f docker-compose.machine-b.yml up --build -d  # machine B
```

Each machine builds its own `lastmile` image, so different CPU architectures
(Apple Silicon vs. Intel/AMD) just work — no registry needed.

**4. Show B's robots in the simulator** (on the machine running `npm run dev`):

```bash
cp simulator/.env.local.example simulator/.env.local
# edit the IPs to machine B's IP, then restart `npm run dev`
```

Remote robots appear alongside local ones. Their status/battery controls work;
On/Off is disabled because only the machine running a container can stop it.

**5. Verify cross-machine messaging** (from machine A):

```bash
curl <B's IP>:8006/state                  # A can reach B
curl -X POST localhost:8001/send \
  -H 'Content-Type: application/json' \
  -d '{"to":"robot8","body":{"msg":"hello from machine A"}}'
curl <B's IP>:8008/state                  # message is in robot8's inbox
```

**Troubleshooting**

- *Connection refused / timeout between machines:* allow the ports through the
  firewall (Windows Defender Firewall inbound rule for 8001–8010; Linux
  `sudo ufw allow 8001:8010/tcp`). Guest and campus Wi-Fi often block
  device-to-device traffic — use a home router or phone hotspot.
- *Robots stopped reaching each other after a reconnect:* LAN IPs can change.
  Update `.env` and run `docker compose up -d` again (and `.env.local` for the
  simulator), or reserve the IPs in your router.
