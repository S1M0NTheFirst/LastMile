# Running LastMile

How to start the dashboard, the Gemini orchestrator and the Jetson car.
The Mac is at `192.168.137.28` and the car is at `192.168.137.244`.
If the network changes, check the Mac's IP with `ipconfig getifaddr en0` and use the new one everywhere.

## 1. Mac: dashboard

The dashboard is also the registry that names the robots.

```bash
cd simulator
npm run dev
```

## 2. Mac: orchestrator

Turns what you type into robot commands, using Gemini.

```bash
cd orchestrator
export REGISTRY_URL=http://localhost:3000/api/registry
export LOCAL_HOST_IP=192.168.137.28
export LOCAL_ROBOT_HOST=127.0.0.1
export ROBOT_AUTH_TOKEN=lastmile-local-secret
export GEMINI_MODEL=gemini-3.5-flash-lite
export GEMINI_API_KEY=<your-key>
uvicorn app:app --host 0.0.0.0 --port 8012
```

## 3. Jetson: copy the code

Run from the project folder on the Mac. Repeat whenever `robot/` or `join.py` changes.

```bash
scp -r robot join.py jetson@192.168.137.244:LastMile/
```

## 4. Jetson: motor bridge

Runs on the car itself and gives the robot access to the motors, camera and battery.

```bash
ssh jetson@192.168.137.244
sudo systemctl restart nvargus-daemon
cd ~/LastMile/robot
pkill -f motor_bridge.py
THROTTLE_GAIN=1 ROBOT_AUTH_TOKEN=lastmile-local-secret FLIP_THROTTLE=1 FLIP_STEERING=1 nohup python3 motor_bridge.py > ~/bridge.log 2>&1 &
sleep 10
cat ~/bridge.log
```

The log should end with `motor bridge listening on port 8090`. Then type `exit`.

## 5. Mac: connect the car to the dashboard

```bash
ssh jetson@192.168.137.244 'cd ~/LastMile && python3 join.py --stop && ROBOT_AUTH_TOKEN=lastmile-local-secret MOTOR_POWER=0.5 DRIVE_SPEED=0.6 python3 join.py --registry 192.168.137.28:3000 --host-ip 192.168.137.244 --count 1 --motor-driver jetracer'
```

The car appears on the dashboard within about 10 seconds. Robots get names in the order
they join, so start the car before any simulated robots to make it `robot0`.

## 6. Optional: simulated robots

```bash
ROBOT_AUTH_TOKEN=lastmile-local-secret python3 join.py --registry 192.168.137.28:3000 --host-ip 192.168.137.28 --count 3
```

## 7. Use it

Open http://localhost:3000 and click the car for its battery, live camera and autopilot box.
In the task console, use the car's name from the dashboard:

- `robot0 forward 1 m, turn left`
- `robot0 drive until you see a door and stop`

## Stop

```bash
python3 join.py --stop
ssh jetson@192.168.137.244 'cd ~/LastMile && python3 join.py --stop; pkill -f motor_bridge.py'
```

Then press Ctrl+C in the dashboard and orchestrator terminals.
