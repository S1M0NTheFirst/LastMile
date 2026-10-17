"""Meter-based floor navigation shared by the robot API and deterministic tests.

u points up the main hallway; v points along the right branch. Heading zero
faces +v. Door sides are relative to walking away from the junction.
"""
import copy
import json
import math
from pathlib import Path

FLOOR = json.loads(Path(__file__).with_name("floor4.json").read_text())
CENTER_U = (FLOOR["geometry"]["junctionNear"] + FLOOR["geometry"]["junctionFar"]) / 2
DOORS = {d["id"]: d for d in FLOOR["doors"]}
DOOR_SURFACE_OFFSET = 0.06
ELEVATOR = FLOOR["elevator"]
PICKUP_SECONDS = 3.0


def in_stem(u):
    """The main hallway (stem of the T) runs from the elevator up to the junction."""
    return u < FLOOR["geometry"]["junctionNear"] - 0.05


def hallway_point(u, v):
    """Snap a pose onto the hallway centerline the robot drives along."""
    return (u, 0.0) if in_stem(u) else (CENTER_U, v)


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

    def place_at_elevator(self):
        """Put the robot where it arrives on the floor, facing up the hallway."""
        self.queue = []
        self.command = (0.0, 0.0)
        self.pose.update(u=ELEVATOR["u"], v=ELEVATOR["v"], heading=math.pi / 2, space="floor4")
        self.mission = {"phase": "at_elevator", "door_id": None}

    def _travel(self, at, to):
        """Queue centerline legs between two hallway points, via the junction when
        one end is in the main hallway and the other in the cross hallway."""
        if in_stem(at[0]) and in_stem(to[0]):
            self._move(to[0], 0.0, "travelling")
        elif in_stem(at[0]) or in_stem(to[0]):
            self._move(CENTER_U, 0.0, "travelling")
            self._move(to[0], to[1], "travelling")
        else:
            self._move(CENTER_U, to[1], "travelling")
        return to

    def start(self, door_id):
        return self.start_route([{"door_id": door_id}])

    def start_route(self, stops):
        """Drive through stops in order. A stop is {"door_id": ...} (a door, or
        "junction") or {"u": ..., "v": ...} (a hallway point, e.g. a pickup)."""
        if not stops:
            raise ValueError("A route needs at least one stop")
        targets = [None if s.get("door_id") in (None, "junction") else door_target(s["door_id"]) for s in stops]
        self.queue = []
        at = hallway_point(self.pose["u"], self.pose["v"])
        if not in_stem(self.pose["u"]):
            # A robot at a door first backs out along its approach line.
            self._move(CENTER_U, self.pose["v"], "returning_to_centerline")
        for stop, target in zip(stops, targets):
            if target:
                at = self._travel(at, (CENTER_U, target["v"]))
                # Keep the stop observable by the dashboard's one-second state poll.
                self.queue.append({"kind": "pause", "remaining": 1.5, "phase": "stopped_at_door"})
                self.queue.append({"kind": "turn", "heading": target["heading"], "phase": "turning_to_door"})
                self._move(target["u"], target["v"], "approaching_door")
                if stop is not stops[-1]:
                    self._move(CENTER_U, target["v"], "returning_to_centerline")
            elif stop.get("door_id") == "junction":
                at = self._travel(at, (CENTER_U, 0.0))
            else:
                at = self._travel(at, hallway_point(float(stop["u"]), float(stop["v"])))
                self.queue.append({"kind": "pause", "remaining": float(stop.get("pause", PICKUP_SECONDS)),
                                   "phase": stop.get("phase", "picking_up")})
        last = stops[-1]
        door_id = last.get("door_id")
        self.mission = {"door_id": door_id, "phase": self.queue[0]["phase"] if self.queue else "idle", "distance_m": 0.0}
        if targets[-1]:
            self.mission["centerline_distance_m"] = DOORS[door_id]["steps"] * FLOOR["stepMeters"]
        return self.mission

    def route_seconds(self, stops, dt=0.1):
        """How long the route would take from the current pose, by simulating it."""
        trial = copy.deepcopy(self)
        trial.start_route(stops)
        seconds = 0.0
        while trial.queue and seconds < 3600:
            trial.tick(dt)
            seconds += dt
        return seconds

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
                door_id = self.mission["door_id"]
                self.mission["phase"] = "at_junction" if door_id == "junction" else "door_touched" if door_id else "arrived"
