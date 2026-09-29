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

## How it works

The simulator (Next.js app) is also the fleet's **registry**. Any machine on
the same network can start any number of robots with `join.py`; each robot
registers with the simulator, which names it first-come-first-served
(`robot0`, `robot1`, ...), so names never clash across machines. The simulator
shows every registered robot, and robots look each other up in the registry,
so any robot can message any other, on any machine.

Works across macOS, Windows and Linux. Each machine needs Docker and Python 3;
the machine hosting the simulator also needs Node.js.

## Run the simulation

**1. Start the simulator** on one machine (the "registry machine"):

```bash
cd simulator
npm install      # first time only
npm run dev
```

Open http://localhost:3000. Note this machine's LAN IP:

```bash
ipconfig getifaddr en0    # macOS
ipconfig                  # Windows (IPv4 Address)
hostname -I               # Linux
```

**2. Start robots on any machine**, including the registry machine
(`<registry-ip>` is the IP from step 1):

```bash
python join.py --registry <registry-ip>:3000 --count 20
```

Run it again to add more; run it on as many machines as you like. Each run
builds the `lastmile` image locally (so Apple Silicon and Intel/AMD machines
both work) and picks free host ports starting at 8000.

In the simulator you can set status/battery for every robot. On/Off works for
robots on the registry machine; other machines' robots show as `remote` and
are powered from their own machine (`python join.py --stop`).

## Stop

```bash
python join.py --stop        # on each machine: remove its robots
# Ctrl+C in the simulator terminal to stop the web app
```

## Poke the robots directly (optional)

Each robot's API is at `<machine-ip>:<port>` (shown in the simulator):

```bash
curl localhost:8000/state

# have that robot message robot3, wherever robot3 runs
curl -X POST localhost:8000/send \
  -H 'Content-Type: application/json' \
  -d '{"to":"robot3","body":{"msg":"hello"}}'

curl localhost:8000/peers    # every other robot and its address
```

`delivered:true` means the message reached robot3's inbox. A `503` means the
target is `dead`/`no_response`; a connection error means it is powered off.

## Troubleshooting

- *Robots never appear in the simulator:* check the robot machine can reach
  the registry: `curl <registry-ip>:3000/api/registry`.
- *Connection refused / timeout between machines:* allow the ports through the
  firewall (the simulator's 3000 and the robots' 8000+). Windows: add an
  inbound Windows Defender Firewall rule; Linux: `sudo ufw allow 3000/tcp` and
  `sudo ufw allow 8000:8100/tcp`. Guest and campus Wi-Fi often block
  device-to-device traffic; use a home router or phone hotspot.
- *A machine's IP changed:* run `python join.py --stop`, then join again.
- *Simulator restarted:* running robots re-register within a few seconds and
  keep their names.
