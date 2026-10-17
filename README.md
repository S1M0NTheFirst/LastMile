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

## Verify the measured-floor update

The floor and the robot services share `robot/floor4.json`: 1 step = 0.50 m,
1 tile = 0.25 m. The reference point is the hallway junction near room 418.
Left/right wall sides are viewed walking **away from that junction**. Step
counts are provisionally treated as hallway centerline distances; the final
approach to a door is additional. Room dimensions and door widths are not surveyed.

After pulling/editing this update, restart the dashboard and recreate existing
robots so their containers include the navigation service. From the project root:

```bash
# Removes this project's existing robot containers on this machine.
python3 join.py --stop
cd simulator
npm install
npm run dev
```

In a second Terminal, with Docker Desktop running:

```bash
cd "/Users/ayushimistry/Desktop/AI UI prjct/LastMile"
python3 join.py --registry "$(ipconfig getifaddr en0):3000" --count 3
```

If `en0` has no address, replace `$(ipconfig getifaddr en0)` with your active
network IP. On macOS the dashboard also checks Docker Desktop's user socket,
so local power controls do not require enabling the system-wide Docker socket.

1. Open http://localhost:3000 and choose **OVERVIEW**. Zoom out with **−**
   until it stops. The ground and perimeter should stay visible without fading
   into fog. The external campus map may take time to download; the measured
   floor works immediately even if that download fails.
2. Choose **ECS 4F**. Door labels and the green **418 · JUNCTION · 0 m** marker
   should appear. Use **ROOM LABELS** and **ROBOT LABELS** to toggle them.
3. In **Measured room navigation**, select a robot and **416 · door A**.
   Confirm **6 steps · 3.00 m · 12 tiles**, branch **left**, wall **right**.
   Select **RETURN TO JUNCTION** first if you want every test to start at zero.
4. Select **GO TO DOOR**. The camera follows the robot. Watch the phase change:
   travelling → stopped at door → turning to door → approaching door → door touched.
   The robot stops on the centerline before turning, then slowly brings its front
   to the closed door. This is simulated geometric contact, not a physical sensor.
5. While following, drag horizontally through a full circle, drag vertically
   to adjust elevation, and scroll to zoom. Your orbit angle should stay stable
   as the robot turns. **RESET BEHIND ROBOT** restores the default follow view.
   Hallway surfaces that block the camera fade so the robot remains visible.
6. Repeat with **408** (right/right, 5.00 m) and **402** (right/left, 7.25 m)
   to verify opposite door-facing directions. Check **405 · door 2** at 30.00 m
   and **413** at 27.75 m to verify both extended hallway ends.
7. Start a long route, then **CANCEL ROUTE**. The pose should stop changing.
   You can also inject **FAIL ROBOT** from its details: navigation pauses until
   recovery. A robot with zero battery cannot navigate; charge and recover it.
8. Select either bathroom: its distance remains listed, but **GO TO DOOR** is
   disabled because branch and wall side still need confirmation. Both 416
   entries are preserved. The close 412/414 and 407/405 door pairs have
   explicitly provisional widths. **All measured entrances** lists all 22 entries.

Automated checks (from the project root):

```bash
python3 -m unittest discover -s robot -p 'test_*.py' -v
cd simulator
npm run build
```

The navigation tests cover all 20 placed doors, unit conversion, heading and
front-to-door contact, returning from a door, cancellation, and rejecting
unplaced or unknown destinations. Measured navigation is independent of the
unfinished LLM task console.
