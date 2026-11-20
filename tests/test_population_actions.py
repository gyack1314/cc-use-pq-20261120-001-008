import copy
import json
import tempfile
import unittest
from pathlib import Path

from climate_ledger.application import Application
from climate_ledger.contracts import DomainError


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


class PopulationActionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Application(Path(self.temp.name) / "city.sqlite3")
        self.addCleanup(self.app.ledger.close)
        self.bundle = json.loads((EXAMPLES / "city.json").read_text(encoding="utf-8"))
        self.query = json.loads((EXAMPLES / "exposure.json").read_text(encoding="utf-8"))
        self.app.ingest(self.bundle)

    def row(self, kind, identity, **changes):
        result = copy.deepcopy(next(r for r in self.bundle["records"] if r["kind"] == kind and r["data"].get("id") == identity))
        result["data"].update(changes)
        return result

    def change(self, *records, name="changes"):
        return self.app.ingest({"id": name, "source": self.bundle["source"], "records": list(records)})

    def assert_error(self, code, call):
        with self.assertRaises(DomainError) as error:
            call()
        self.assertEqual(error.exception.code, code)

    def test_group_priority_capacity_and_no_duplicate_people(self):
        result = self.app.analyze(self.query)
        row = next(r for r in result["daily"] if r["zone"] == "river" and r["day"] == "2026-07-03")
        self.assertEqual(row["protected"], 30)
        self.assertEqual(row["exposed"], 70)
        senior = next(g for g in row["groups"] if g["group"] == "senior")
        adult = next(g for g in row["groups"] if g["group"] == "adult")
        self.assertEqual(senior["protected"], 20)
        self.assertEqual(senior["exposed"], 0)
        self.assertEqual(adult["protected"], 10)
        self.assertEqual(row["weighted_burden"], "350.000000")
        self.assertEqual(result["total"]["known_protected_person_days"], 60)
        self.assertEqual(result["total"]["known_weighted_burden"], "2860.000000")

    def test_two_actions_share_remaining_eligible_population(self):
        self.change(self.row("action", "hub-river", revision=2, status="active"))
        result = self.app.analyze(self.query)
        row = next(r for r in result["daily"] if r["zone"] == "river" and r["day"] == "2026-07-03")
        self.assertEqual(row["protected"], 100)
        self.assertEqual(row["exposed"], 0)
        self.assertEqual(sum(a["used"] for a in row["actions"]), 100)
        self.assertEqual(sum(a["unused"] for a in row["actions"]), 10)
        self.assertEqual(row["actions"][0]["action"], "cool-river")

    def test_population_correction_changes_both_allocation_and_burden(self):
        draft = self.app.draft("first", "heat", self.query, "分析员")
        self.app.review("first", draft["digest"], "审核员")
        self.change(self.row("population", "river-senior", revision=2, count=40))
        row = next(r for r in self.app.analyze(self.query)["daily"] if r["zone"] == "river" and r["day"] == "2026-07-03")
        self.assertEqual(row["population"]["population"], 120)
        senior = next(g for g in row["groups"] if g["group"] == "senior")
        adult = next(g for g in row["groups"] if g["group"] == "adult")
        self.assertEqual(senior["protected"], 30)
        self.assertEqual(adult["protected"], 0)
        self.assertEqual(row["weighted_burden"], "500.000000")
        self.assert_error("stale", lambda: self.app.publish("first", "release"))
        self.assertEqual(self.app.analyze(self.query, seq=1)["total"]["known_weighted_burden"], "2860.000000")

    def test_population_overlap_does_not_partially_enter(self):
        row = self.row("population", "river-senior", id="overlap", revision=1, start="2026-07-01", end="2026-07-31")
        self.assert_error("population_overlap", lambda: self.change(row))
        self.assertEqual(len(self.app.ledger.snapshot()["population"]), 4)

    def test_zero_population_is_known_and_cannot_receive_capacity(self):
        self.change(self.row("population", "river-senior", revision=2, count=0),
                    self.row("population", "river-adult", revision=2, count=0))
        result = self.app.analyze({**self.query, "zones": ["river"]})
        self.assertEqual(result["status"], "qualified")
        self.assertEqual(result["total"]["known_population_days"], 0)
        self.assertEqual(result["total"]["known_protected_person_days"], 0)
        self.assertIsNone(result["total"]["hazard_fraction"])
        draft = self.app.draft("empty", "heat", {**self.query, "zones": ["river"]}, "分析员")
        self.assertFalse(draft["content"]["assessment"]["publishable"])

    def test_population_gap_blocks_complete_summary(self):
        self.change(self.row("population", "river-senior", revision=2, end="2026-07-02"))
        result = self.app.analyze(self.query)
        self.assertEqual(result["total"]["unknown_zone_days"], 2)
        self.assertEqual(result["total"]["known_protected_person_days"], 0)
        self.assertIsNone(result["total"]["protection_fraction"])
        diagnostics = self.app.diagnose(self.query)
        river = next(r for r in diagnostics["zones"] if r["zone"] == "river")
        self.assertEqual(river["residents"]["complete_days"], 2)
        self.assertEqual(river["residents"]["missing"][0]["groups"], ["senior"])

    def test_active_facility_conflict_rejected(self):
        row = self.row("action", "shade-river", revision=2, status="active", facility="river-library")
        self.assert_error("facility_conflict", lambda: self.change(row))
        self.assertEqual(self.app.ledger.snapshot()["action"]["shade-river"]["status"], "planned")

    def test_adjacent_inclusive_intervals_cannot_share_end_date(self):
        closed = self.row("action", "cool-river", revision=2, end="2026-07-03")
        next_action = self.row("action", "shade-river", revision=2, start="2026-07-03", status="active", facility="river-library")
        self.assert_error("facility_conflict", lambda: self.change(closed, next_action))
        next_action["data"]["start"] = "2026-07-04"
        self.change(closed, next_action)
        daily = self.app.analyze(self.query)["daily"]
        row = next(r for r in daily if r["zone"] == "river" and r["day"] == "2026-07-04")
        self.assertEqual(row["actions"][0]["action"], "shade-river")

    def test_suspension_removes_protection_but_preserves_old_view(self):
        self.change(self.row("action", "cool-river", revision=2, status="suspended"))
        current = self.app.analyze(self.query)
        old = self.app.analyze(self.query, seq=1)
        self.assertEqual(current["total"]["known_protected_person_days"], 0)
        self.assertEqual(old["total"]["known_protected_person_days"], 60)
        study = json.loads((EXAMPLES / "study.json").read_text(encoding="utf-8"))
        compared = self.app.compare(study)
        self.assertEqual(compared["status"], "insufficient")
        self.assertTrue(any(r["reason"] == "action_not_active" for r in compared["excluded"]))

    def test_unknown_group_and_zone_rejected(self):
        self.assert_error("reference", lambda: self.change(self.row("action", "cool-river", revision=2, groups=["unknown"])))
        self.assert_error("reference", lambda: self.change(self.row("population", "river-adult", revision=2, zone="unknown")))
        self.assert_error("zone", lambda: self.app.analyze({**self.query, "zones": ["unknown"]}))

    def test_not_hazardous_never_consumes_protection(self):
        query = {**self.query, "metric": "water"}
        result = self.app.analyze(query)
        self.assertEqual(result["total"]["known_exposed_person_days"], 0)
        self.assertTrue(all(r["actions"] == [] for r in result["daily"]))
        self.assertEqual(result["episodes"], [])
