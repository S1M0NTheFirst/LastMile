# Running LastMile

How to start the dashboard, the Gemini orchestrator, the Jetson car and the simulated Docker robots.

The Mac is at `192.168.137.23` and the car is at `192.168.137.106`. The Mac's IP changes when its Wi-Fi
changes, so the commands below read it with `ipconfig getifaddr en0` instead of typing it. If the car's
IP changes, replace `192.168.137.106` everywhere. Both machines must be on the same network.

Check the Mac's IP and that the car answers:

```bash
ipconfig getifaddr en0; ping -c1 192.168.137.106
```

## 1. Mac: dashboard (Terminal 1)

The dashboard is also the registry that names the robots.

```bash
cd ~/Documents/GitHub/LastMile/simulator
npm run dev
```

## 2. Mac: orchestrator (Terminal 2)

Turns what you type into robot commands, using Gemini. Type your own key in place of `<your-key>`.

```bash
cd ~/Documents/GitHub/LastMile/orchestrator
export REGISTRY_URL=http://localhost:3000/api/registry
export LOCAL_HOST_IP=$(ipconfig getifaddr en0)
export LOCAL_ROBOT_HOST=127.0.0.1
export ROBOT_AUTH_TOKEN=lastmile-local-secret
export GEMINI_MODEL=gemini-3.1-flash-lite
export GEMINI_API_KEY=<your-key>
uvicorn app:app --host 0.0.0.0 --port 8012
```

A free Gemini key is slow and limited to about 20 requests per day per model. Turning on billing for the key
gives fast, steady answers.

## 3. Mac: copy the code to the car (Terminal 3)

Repeat whenever `robot/` or `join.py` changes.

```bash
cd ~/Documents/GitHub/LastMile
scp -r robot join.py jetson@192.168.137.106:LastMile/
```

## 4. Jetson: motor bridge

Runs on the car itself and gives the robot its motors, camera and battery. Without it the car cannot
drive and the camera stays blank.

```bash
ssh jetson@192.168.137.106
```

Then, one command at a time on the Jetson:

```bash
pkill -f motor_bridge.py; pkill -f camera_worker.py; cd ~/LastMile/robot
```

```bash
THROTTLE_GAIN=1 ROBOT_AUTH_TOKEN=lastmile-local-secret FLIP_THROTTLE=1 FLIP_STEERING=1 setsid nohup python3 motor_bridge.py > ~/bridge.log 2>&1 < /dev/null &
```

```bash
sleep 20; tail -5 ~/bridge.log
```

The log should end with `motor bridge listening on port 8090`. Then type `exit`.
If the wheels rest off-center, add `STEERING_OFFSET=<value>` to the command (see the top of `robot/motor_bridge.py`).
If the camera fails, run `sudo systemctl restart nvargus-daemon` before starting the bridge, never while it runs.

## 5. Mac: connect the car at room 411 (Terminal 3)

Put the car by the door of room 411, facing the door. It stays parked there on the 4th-floor map, and a
robot that fails on the 4th floor is taken over by the closest robot, which is the car. Start the car
**before** the Docker robots so it is `robot0`.

```bash
MAC=$(ipconfig getifaddr en0); ssh jetson@192.168.137.106 "cd ~/LastMile && python3 join.py --stop && START_DOOR=411-1 ROBOT_AUTH_TOKEN=lastmile-local-secret MOTOR_POWER=0.5 DRIVE_SPEED=0.6 python3 join.py --registry $MAC:3000 --host-ip 192.168.137.106 --count 1 --motor-driver jetracer"
```

The car appears on the dashboard within about 10 seconds. It builds a Docker image the first time, which takes a minute.

## 6. Mac: simulated Docker robots (Terminal 3)

Docker Desktop must be running. This removes old robots and starts 8 new ones at random places:

```bash
cd ~/Documents/GitHub/LastMile
IP=$(ipconfig getifaddr en0); ROBOT_AUTH_TOKEN=lastmile-local-secret python3 join.py --stop; ROBOT_AUTH_TOKEN=lastmile-local-secret python3 join.py --registry $IP:3000 --host-ip $IP --count 8
```

To choose where they start, use `--at` with building names or `x,y` points (0-100) instead of `--count`:

```bash
IP=$(ipconfig getifaddr en0); ROBOT_AUTH_TOKEN=lastmile-local-secret python3 join.py --stop; ROBOT_AUTH_TOKEN=lastmile-local-secret python3 join.py --registry $IP:3000 --host-ip $IP --at "Engineering Technology; Vivian Engineering Center; College of Professional and Continuing Education; Student Recreation and Wellness Center; University Police"
```

Docker robots cross the campus at 3 m/s along the map's roads. Set `CAMPUS_SPEED=<m/s>` before the command to
change that, and `DRAIN_RATE=<percent per second>` to change the battery (default 0.02, about 83 minutes).
After the Wi-Fi changes, run this step again, because robots keep the IP they were started with.

## 7. Use it

Open http://localhost:3000. Click a robot for its battery, camera and the FAIL / DRAIN / STOP buttons.
In the task console:

- `Deliver package pkg1 from Human Services and Design to ECS room 405 door 1`
- `robot0 forward 1 m, turn left` (use the car's name from the dashboard)
- `robot0 drive until you see a door and stop`

The nearest Docker robot takes the delivery and rides to the 4th floor. Press **FAIL ROBOT** on it while it
drives in the hallway. The orchestrator hands the package to the closest robot to where it stopped, which is
the car at room 411. **The car really drives** the route from the map (it backs out of the doorway, drives to
the failed robot's spot, picks up, delivers to door 405-1, then drives back to 411), so clear its path and be
ready to lift it.

## Stop

```bash
cd ~/Documents/GitHub/LastMile
python3 join.py --stop
ssh jetson@192.168.137.106 'cd ~/LastMile && python3 join.py --stop; pkill -f motor_bridge.py'
```

Then press Ctrl+C in the dashboard and orchestrator terminals.
