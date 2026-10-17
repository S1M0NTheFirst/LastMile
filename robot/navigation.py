"""Meter-based floor navigation shared by the robot API and deterministic tests.

u points up the main hallway; v points along the right branch. Heading zero
faces +v. Door sides are relative to walking away from the junction.
"""
import json
import math
from pathlib import Path

FLOOR = json.loads(Path(__file__).with_name("floor4.json").read_text())
CENTER_U = (FLOOR["geometry"]["junctionNear"] + FLOOR["geometry"]["junctionFar"]) / 2
DOORS = {d["id"]: d for d in FLOOR["doors"]}
DOOR_SURFACE_OFFSET = 0.06


def door_target(door_id):
    door = DOORS[door_id]
    if not door["branch"] or not door["side"]:
        raise ValueError("This door needs its hallway branch and wall side confirmed")
    v = door["steps"] * FLOOR["stepMeters"] * (-1 if door["branch"] == "left" else 1)
    far = (door["branch"] == "left") == (door["side"] == "right")
    surface_u = FLOOR["geometry"]["junctionFar" if far else "junctionNear"] + (-1 if far else 1) * DOOR_SURFACE_OFFSET
    heading = math.pi / 2 if far else -math.pi / 2
    contact_u = surface_u - math.sin(heading) * FLOOR["robot"]["length"] / 2
    return {"u": contact_u, "v": v, "heading": heading, "surface_u": surface_u}


def angle_difference(target, current):
    return math.atan2(math.sin(target - current), math.cos(target - current))


APPROACH_SPEED = 0.18


class Navigator:
    """Integrates the route by time. speed (m/s), turn_speed (rad/s) and power
    (throttle) default to the simulated floor robot; a real car passes its
    calibration so the map pose tracks the motors. After each tick, command
    holds the (throttle, steering) the car should be driving for that tick.
    """

    def __init__(self, speed=None, turn_speed=None, power=1.0):
        self.pose = {"u": CENTER_U, "v": 0.0, "heading": 0.0, "space": "floor4"}
        self.mission = {"phase": "idle", "door_id": None}
        self.queue = []
        self.speed = speed or FLOOR["robot"]["speed"]
        self.turn_speed = turn_speed or FLOOR["robot"]["turnSpeed"]
        self.power = power
        self.command = (0.0, 0.0)

    def _turn_command(self, diff):
        # Car-like (Ackermann) turn: creep forward at full lock; +heading is left.
        self.command = (self.power, -1.0 if diff > 0 else 1.0)

    def _move(self, u, v, phase):
        self.queue.append({"kind": "move", "u": u, "v": v, "phase": phase})

    def start(self, door_id):
        target = None if door_id == "junction" else door_target(door_id)
        self.queue = []
        # A robot at a door first backs out along its approach line.
        self._move(CENTER_U, self.pose["v"], "returning_to_centerline")
        self._move(CENTER_U, target["v"] if target else 0.0, "travelling")
        if target:
            # Keep the stop observable by the dashboard's one-second state poll.
            self.queue.append({"kind": "pause", "remaining": 1.5, "phase": "stopped_at_door"})
            self.queue.append({"kind": "turn", "heading": target["heading"], "phase": "turning_to_door"})
            self._move(target["u"], target["v"], "approaching_door")
        self.mission = {"door_id": door_id, "phase": self.queue[0]["phase"], "distance_m": 0.0}
        if target:
            self.mission["centerline_distance_m"] = DOORS[door_id]["steps"] * FLOOR["stepMeters"]
        return self.mission

    def cancel(self):
        self.queue = []
        self.command = (0.0, 0.0)
        self.mission["phase"] = "cancelled"

    def tick(self, dt):
        self.command = (0.0, 0.0)
        if not self.queue:
            return
        step = self.queue[0]
        self.mission["phase"] = step["phase"]
        if step["kind"] == "pause":
            step["remaining"] -= dt
            done = step["remaining"] <= 0
        elif step["kind"] == "turn":
            diff = angle_difference(step["heading"], self.pose["heading"])
            turn = min(abs(diff), self.turn_speed * dt)
            self._turn_command(diff)
            self.pose["heading"] += math.copysign(turn, diff)
            done = abs(diff) <= turn + 1e-9
        else:
            du, dv = step["u"] - self.pose["u"], step["v"] - self.pose["v"]
            distance = math.hypot(du, dv)
            heading = math.atan2(du, dv) if distance > 1e-9 else self.pose["heading"]
            # Preserve orientation when backing away from a door; turn before hallway travel.
            diff = angle_difference(heading, self.pose["heading"])
            if step["phase"] == "travelling" and abs(diff) > 0.01 and distance > 1e-9:
                self.pose["heading"] += math.copysign(min(abs(diff), self.turn_speed * dt), diff)
                self._turn_command(diff)
                self.mission["phase"] = "turning_to_hallway"
                return
            # Approach slowly; throttle scales with speed like the drive executor.
            scale = APPROACH_SPEED / FLOOR["robot"]["speed"] if step["phase"] == "approaching_door" else 1.0
            speed = self.speed * scale
            travel = min(distance, speed * dt)
            if distance > 1e-9:
                # Backing out of a door keeps the door heading, so it is reverse.
                reverse = -1.0 if step["phase"] == "returning_to_centerline" else 1.0
                self.command = (reverse * self.power * scale, 0.0)
                self.pose["u"] += du / distance * travel
                self.pose["v"] += dv / distance * travel
            self.mission["distance_m"] += travel
            done = distance <= travel + 1e-9
        if done:
            self.queue.pop(0)
            if not self.queue:
                self.command = (0.0, 0.0)
                self.mission["phase"] = "at_junction" if self.mission["door_id"] == "junction" else "door_touched"
