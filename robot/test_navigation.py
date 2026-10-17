import asyncio
import math
import time
import unittest
from unittest import mock

from navigation import CENTER_U, DOORS, FLOOR, Navigator, angle_difference, door_target


class NavigationTests(unittest.TestCase):
    def complete(self, navigator):
        phases = set()
        for _ in range(3000):
            if not navigator.queue:
                return phases
            navigator.tick(0.05)
            phases.add(navigator.mission["phase"])
        self.fail("Navigation did not finish within 150 simulated seconds")

    def test_measurements_and_entries(self):
        self.assertEqual(len(DOORS), 22)
        self.assertEqual(FLOOR["stepMeters"], 2 * FLOOR["tileMeters"])
        self.assertEqual(door_target("405-2")["v"], 30)
        self.assertEqual(door_target("413")["v"], -27.75)
        self.assertEqual(door_target("416-a")["v"], -3)
        self.assertEqual(door_target("416-b")["v"], -10.25)

    def test_all_placed_doors_stop_turn_and_contact_without_crossing_wall(self):
        for door_id, door in DOORS.items():
            if not door["branch"]:
                continue
            with self.subTest(door=door_id):
                nav = Navigator()
                nav.start(door_id)
                phases = self.complete(nav)
                target = door_target(door_id)
                self.assertIn("stopped_at_door", phases)
                self.assertIn("turning_to_door", phases)
                self.assertIn("approaching_door", phases)
                self.assertEqual(nav.mission["phase"], "door_touched")
                self.assertAlmostEqual(nav.pose["u"], target["u"])
                self.assertAlmostEqual(nav.pose["v"], target["v"])
                self.assertAlmostEqual(angle_difference(target["heading"], nav.pose["heading"]), 0)
                front_u = nav.pose["u"] + math.sin(nav.pose["heading"]) * FLOOR["robot"]["length"] / 2
                self.assertAlmostEqual(front_u, target["surface_u"])
                self.assertGreater(nav.pose["u"], FLOOR["geometry"]["junctionNear"])
                self.assertLess(nav.pose["u"], FLOOR["geometry"]["junctionFar"])

    def test_opposite_branches_and_walls_produce_correct_headings(self):
        for door_id, sign in [("416-a", 1), ("411-1", -1), ("402", 1), ("408", -1)]:
            self.assertEqual(door_target(door_id)["heading"], sign * math.pi / 2)

    def test_unplaced_and_unknown_doors_cannot_start(self):
        nav = Navigator()
        for door_id in ["bathroom-male", "bathroom-female"]:
            with self.assertRaises(ValueError):
                nav.start(door_id)
        with self.assertRaises(KeyError):
            nav.start("unknown")
        self.assertFalse(nav.queue)

    def test_return_from_door_retracts_before_hallway_travel(self):
        nav = Navigator()
        nav.start("408")
        self.complete(nav)
        start_v = nav.pose["v"]
        nav.start("416-a")
        while nav.queue and nav.queue[0]["phase"] == "returning_to_centerline":
            nav.tick(0.05)
            self.assertAlmostEqual(nav.pose["v"], start_v)
        self.assertAlmostEqual(nav.pose["u"], CENTER_U)
        self.complete(nav)
        nav.start("junction")
        self.complete(nav)
        self.assertAlmostEqual(nav.pose["u"], CENTER_U)
        self.assertAlmostEqual(nav.pose["v"], 0)
        self.assertEqual(nav.mission["phase"], "at_junction")

    def test_cancel_stops_motion_and_allows_another_destination(self):
        nav = Navigator()
        nav.start("405-2")
        for _ in range(20):
            nav.tick(0.05)
        nav.cancel()
        before = dict(nav.pose)
        nav.tick(5)
        self.assertEqual(nav.pose, before)
        self.assertEqual(nav.mission["phase"], "cancelled")
        nav.start("408")
        self.complete(nav)
        self.assertEqual(nav.mission["phase"], "door_touched")

    def test_sim_navigator_defaults_match_floor_speeds(self):
        nav = Navigator()
        self.assertEqual(nav.speed, FLOOR["robot"]["speed"])
        self.assertEqual(nav.turn_speed, FLOOR["robot"]["turnSpeed"])
        # Same route timing as before: 407-1 finishes and touches the door.
        nav.start("407-1")
        self.complete(nav)
        self.assertEqual(nav.mission["phase"], "door_touched")
        self.assertEqual(nav.command, (0.0, 0.0))

    def test_elevator_route_drives_forward_up_hallway_then_to_door(self):
        nav = Navigator(power=0.3)
        nav.place_at_elevator()
        nav.start("407-1")
        nav.tick(0.05)
        self.assertEqual(nav.mission["phase"], "travelling")
        self.assertGreater(nav.command[0], 0)  # leaves the elevator forward, not in reverse
        phases = self.complete(nav)
        self.assertIn("turning_to_hallway", phases)
        self.assertNotIn("returning_to_centerline", phases)
        target = door_target("407-1")
        self.assertAlmostEqual(nav.pose["u"], target["u"])
        self.assertAlmostEqual(nav.pose["v"], target["v"])

    def test_handoff_route_stops_at_pickup_point_before_door(self):
        nav = Navigator()
        nav.place_at_elevator()
        nav.start_route([{"u": 7.5, "v": 3.1}, {"door_id": "407-1"}])
        while nav.mission["phase"] != "picking_up":
            nav.tick(0.05)
        self.assertAlmostEqual(nav.pose["u"], CENTER_U)  # snapped onto the centerline
        self.assertAlmostEqual(nav.pose["v"], 3.1)
        self.complete(nav)
        self.assertEqual(nav.mission["phase"], "door_touched")
        self.assertGreater(nav.route_seconds([{"door_id": "junction"}]), 0)

    def test_reversing_out_of_door_uses_negative_throttle(self):
        nav = Navigator(power=0.3)
        nav.start("408")
        self.complete(nav)
        nav.start("407-1")
        nav.tick(0.05)
        self.assertEqual(nav.mission["phase"], "returning_to_centerline")
        self.assertLess(nav.command[0], 0)
        self.assertEqual(nav.command[1], 0.0)


class RealCarNavigationTests(unittest.TestCase):
    """navigation_loop drives the motors when a real driver is active."""

    def setUp(self):
        import app
        self.app = app
        patches = [
            mock.patch.object(app.motors, "DRIVER", "jetracer"),
            mock.patch.object(app.motors, "set_drive"),
            mock.patch.dict(app.state, {"status": "alive", "battery": 100.0}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.set_drive = app.motors.set_drive
        self.nav = app.make_navigator()
        p = mock.patch.object(app, "navigator", self.nav)
        p.start()
        self.addCleanup(p.stop)

    def run_loop(self, seconds, until=None):
        async def main():
            loop = asyncio.create_task(self.app.navigation_loop())
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline and not (until and until()):
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.12)  # let the loop send its stop
            loop.cancel()
        asyncio.run(main())

    def test_407_command_sequence(self):
        # Speed up the car's calibration so the route runs quickly in the test.
        self.nav.speed *= 40
        self.nav.turn_speed *= 40
        self.nav.start("407-1")
        self.run_loop(20, until=lambda: not self.nav.queue)
        self.assertEqual(self.nav.mission["phase"], "door_touched")
        calls = [c.args for c in self.set_drive.call_args_list]
        sequence = [calls[0]]
        for c in calls[1:]:
            if c != sequence[-1]:
                sequence.append(c)
        power = self.app.MOTOR_POWER
        slow = power * 0.18 / FLOOR["robot"]["speed"]
        expected = [(0.0, 0.0), (power, 0.0), (0.0, 0.0), (power, 1.0), (slow, 0.0), (0, 0)]
        # Zero-length back-out at the junction, hallway travel (+v from the junction), pause, right turn (heading -pi/2), slow approach, stop.
        self.assertEqual(door_target("407-1")["heading"], -math.pi / 2)
        self.assertEqual(len(sequence), len(expected), sequence)
        for got, want in zip(sequence, expected):
            self.assertAlmostEqual(got[0], want[0])
            self.assertAlmostEqual(got[1], want[1])

    def test_cancel_stops_motors_immediately(self):
        from fastapi.testclient import TestClient
        self.nav.start("407-1")
        self.nav.tick(0.05)
        self.set_drive.reset_mock()
        TestClient(self.app.app).post("/navigate", json={"cancel": True})
        self.set_drive.assert_called_with(0, 0)
        self.assertEqual(self.nav.mission["phase"], "cancelled")

    def test_dead_battery_stops_motors(self):
        self.nav.start("407-1")
        self.app.state["battery"] = 0
        self.run_loop(0.2)
        self.set_drive.assert_called_with(0, 0)
        self.assertNotIn(mock.call(self.app.MOTOR_POWER, 0.0), self.set_drive.call_args_list)

    def test_sim_driver_never_commands_motors(self):
        with mock.patch.object(self.app.motors, "DRIVER", "sim"):
            self.nav.start("407-1")
            self.run_loop(0.3)
        self.set_drive.assert_not_called()


if __name__ == "__main__":
    unittest.main()
