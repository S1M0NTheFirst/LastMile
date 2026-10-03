# LastMile — Robot Swarm Simulator

A local simulation environment for testing multi-robot coordination logic before
running it on real hardware. Each robot runs as its own Docker container, on
one machine or spread across many, and a Next.js web app (the orchestrator + UI)
lets you watch the fleet on a map and control each robot's power, battery, and
failure state.

See [Simulator.md](Simulator.md) for the full design.

## Requirements

Every machine that runs robots:

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (running), or Docker Engine on Linux
- [Python](https://www.python.org/) 3.8+ (`python3` on macOS/Linux, `python` on Windows)

The one machine that runs the simulator also needs:

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
python3 join.py --registry <registry-ip>:3000 --count 20
```

Run it again to add more; run it on as many machines as you like. Each run
builds the `lastmile` image locally (so Apple Silicon and Intel/AMD machines
both work) and picks free host ports starting at 8000.

In the simulator you can set status/battery for every robot. On/Off works for
robots on the registry machine; other machines' robots show as `remote` and
are powered from their own machine (`python3 join.py --stop`).

## Add another machine

Repeat this on every machine you want to add (any OS, any number of machines).
The simulator must already be running on the registry machine.

**1. Get on the same network** as the registry machine (same Wi-Fi/LAN, or
both on Tailscale; see Troubleshooting if your Wi-Fi blocks it).

**2. Get the code:**

```bash
git clone https://github.com/S1M0NTheFirst/LastMile.git
cd LastMile
```

(or `git pull` if you already have it)

**3. Start Docker**, then join with as many robots as you want:

```bash
python3 join.py --registry <registry-ip>:3000 --count 10
```

`join.py` first checks it can reach the registry and stops with a clear error
if it can't. The new robots appear in the simulator within a few seconds,
named after the last existing robot (e.g. `robot20`, `robot21`, ...) and marked
`remote`.

**4. Add more later** by running `join.py` again on the same machine; it picks
free ports automatically. Check a robot registered with
`docker logs lastmile-<port>` (it prints `registered with ... as robotN`).

**5. Leave the fleet:** `python3 join.py --stop` removes this machine's robots.
They stay listed as offline in the simulator until the simulator restarts.

## Stop

```bash
python3 join.py --stop       # on each machine: remove its robots
# Ctrl+C in the simulator terminal to stop the web app
# (restarting the simulator clears the robot list; names start at robot0 again)
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
- *Campus/guest Wi-Fi blocks machines from reaching each other:* install
  [Tailscale](https://tailscale.com/) on every machine, log in to the same
  account, and use the registry machine's Tailscale IP (`tailscale ip -4`)
  as `<registry-ip>`.
- *A robot never shows up:* `docker logs lastmile-<port>` on its machine shows
  why it can't register.
- *A machine's IP changed:* run `python3 join.py --stop`, then join again.
- *Simulator restarted:* running robots re-register within a few seconds and
  keep their names.
