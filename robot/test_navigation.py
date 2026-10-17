import math
import unittest

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


if __name__ == "__main__":
    unittest.main()
