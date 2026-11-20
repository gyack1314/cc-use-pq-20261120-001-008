import copy
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from climate_ledger.__main__ import main
from climate_ledger.application import Application
from climate_ledger.contracts import DomainError
from climate_ledger.publication import Publication


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def example(name):
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


class AnalysisPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "city.sqlite3"
        self.app = Application(self.database)
        self.addCleanup(self.app.ledger.close)
        self.bundle = example("city.json")
        self.query = example("exposure.json")
        self.app.ingest(self.bundle)

    def edit(self, kind, selector, **changes):
        record = copy.deepcopy(next(item for item in self.bundle["records"] if item["kind"] == kind
                                    and all(item["data"].get(key) == value for key, value in selector.items())))
        record["data"].update(changes)
        return record

    def change(self, *rows, name="correction"):
        return self.app.ingest({"id": name, "source": self.bundle["source"], "records": list(rows)})

    def assert_error(self, code, call):
        with self.assertRaises(DomainError) as error:
            call()
        self.assertEqual(error.exception.code, code)

    def reviewed(self, identity="draft", scope="heat", query=None):
        draft = self.app.draft(identity, scope, query or self.query, "分析员")
        self.app.review(identity, draft["digest"], "审核员")
        return draft

    def test_weather_matched_study_observes_both_phases(self):
        result = self.app.compare(example("study.json"))
        self.assertEqual(result["status"], "qualified")
        self.assertEqual(result["estimate"], "-2.500000")
        self.assertEqual(len(result["phases"]["before"]["pairs"]), 2)
        self.assertEqual(result["phases"]["after"]["mean_difference"], "-2.500000")
        self.assertEqual(result["excluded"], [])
        self.assertIn("不等同因果证明", result["interpretation"])

    def test_control_activation_excludes_contaminated_days_and_blocks_release(self):
        control = self.edit("action", {"id": "shade-river"}, id="hill-action", revision=1,
                            zone="hill", status="active", facility="hill-hall")
        self.change(control)
        result = self.app.compare(example("study.json"))
        self.assertEqual(result["status"], "insufficient")
        self.assertIsNone(result["estimate"])
        self.assertEqual(len([r for r in result["excluded"] if r["reason"] == "control_contaminated"]), 2)
        draft = self.app.draft("study", "impact", example("study.json"), "分析员", kind="study")
        self.assertFalse(draft["content"]["assessment"]["publishable"])
        self.assert_error("unpublishable", lambda: self.app.review("study", draft["digest"], "审核员"))

    def test_weather_tolerance_does_not_replace_missing_controls(self):
        row = self.edit("reading", {"station": "hill-s", "metric": "heat", "day": "2026-07-03"}, revision=2, value="42")
        self.change(row)
        result = self.app.compare(example("study.json"))
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(len(result["phases"]["after"]["pairs"]), 1)
        self.assertEqual(result["phases"]["after"]["unmatched"][0]["reason"], "no_weather_match")
        relaxed = self.app.compare({**example("study.json"), "tolerance": "10"})
        self.assertEqual(relaxed["status"], "qualified")

    def test_control_day_cannot_be_reused_for_second_treated_zone(self):
        rows = []
        for item in self.bundle["records"]:
            data = item["data"]
            if item["kind"] == "zone" and data["id"] == "river":
                rows.append({"kind": "zone", "data": {"id": "river2", "revision": 1, "name": "河畔二区"}})
            elif item["kind"] in ("station", "population", "action") and data["zone"] == "river" and data.get("status", "active") == "active":
                clone = copy.deepcopy(item)
                clone["data"]["id"] += "2"
                clone["data"]["zone"] = "river2"
                if item["kind"] == "action":
                    clone["data"]["facility"] += "2"
                rows.append(clone)
            elif item["kind"] == "reading" and data["station"] == "river-s":
                clone = copy.deepcopy(item)
                clone["data"]["station"] = "river-s2"
                rows.append(clone)
        self.change(*rows)
        result = self.app.compare({**example("study.json"), "treated": ["river", "river2"]})
        for phase in result["phases"].values():
            identities = [(row["control"], row["day"]) for row in phase["pairs"]]
            self.assertEqual(len(identities), len(set(identities)))
            self.assertEqual(len(phase["unmatched"]), 2)

    def test_bad_study_cohorts_and_periods_rejected(self):
        study = example("study.json")
        self.assert_error("cohort", lambda: self.app.compare({**study, "controls": ["river"]}))
        self.assert_error("period", lambda: self.app.compare({**study, "before": study["after"]}))
        self.assert_error("pairs", lambda: self.app.compare({**study, "minimum_pairs": 0}))

    def test_portfolio_compares_joint_protection_without_modifying_facts(self):
        before = self.app.inspect()
        result = self.app.plan(example("portfolio.json"))
        self.assertEqual(result["selected"], ["shared-river"])
        self.assertEqual(result["cost_cents"], 9000)
        self.assertEqual(result["additional_protected_person_days"], 140)
        self.assertEqual(result["avoided_weighted_burden"], "700.000000")
        self.assertIn(["shade-river", "shared-river"], result["conflicts"])
        self.assertEqual(before, self.app.inspect())
        self.assertEqual(result["status"], "hypothetical")

    def test_portfolio_respects_budget_existing_facilities_and_missing_data(self):
        spec = example("portfolio.json")
        no_budget = self.app.plan({**spec, "budget_cents": 0})
        self.assertEqual(no_budget["selected"], [])
        self.assertEqual(len(no_budget["excluded"]), 3)
        self.change(self.edit("action", {"id": "shade-river"}, revision=2, facility="river-library"))
        result = self.app.plan(spec)
        self.assertTrue(any(r["action"] == "shade-river" and r["reason"] == "facility_committed" for r in result["excluded"]))
        self.change(self.edit("reading", {"station": "hill-s", "metric": "heat", "day": "2026-07-03"},
                              revision=2, quality="missing", value=None), name="missing")
        self.assert_error("incomplete", lambda: self.app.plan(spec))

    def test_joint_effect_is_smaller_than_sum_when_population_overlaps(self):
        spec = example("portfolio.json")
        proposals = [r for r in spec["proposals"] if r["action"] != "shared-river"]
        result = self.app.plan({**spec, "proposals": proposals, "budget_cents": 14000})
        self.assertEqual(result["selected"], ["hub-river"])
        summed = sum(int(row["additional_protected"]) for row in result["standalone"])
        self.assertGreater(summed, result["additional_protected_person_days"])
        self.assertEqual(result["projected"]["known_protected_person_days"], 200)

    def test_equity_missing_weather_bounds_do_not_become_zero(self):
        self.change(self.edit("reading", {"station": "river-s", "metric": "heat", "day": "2026-07-01"},
                              revision=2, quality="missing", value=None))
        result = self.app.equity(self.query)
        adult = next(r for r in result["groups"] if r["zone"] == "river" and r["group"] == "adult")
        self.assertFalse(adult["complete"])
        self.assertEqual(adult["unknown_population_days"], 80)
        self.assertGreater(float(adult["upper_burden"]), float(adult["lower_burden"]))
        self.assertEqual(len(result["observation_priorities"]), 1)
        self.assertTrue(any(r["relation"] == "undetermined" for r in result["contrasts"]))

    def test_equity_population_unknown_cannot_support_rank(self):
        self.change(self.edit("population", {"id": "river-senior"}, revision=2, end="2026-07-02"))
        result = self.app.equity(self.query)
        self.assertEqual(result["status"], "population_unknown")
        self.assertEqual(len(result["population_gaps"]), 2)
        self.assertTrue(all(r["relation"] == "undetermined" for r in result["contrasts"]))

    def test_reconciliation_explains_same_day_changes_not_time_trend(self):
        self.change(self.edit("population", {"id": "river-senior"}, revision=2, count=40))
        result = self.app.reconcile(self.query, 1, 2)
        self.assertTrue(result["comparable"])
        self.assertEqual(result["fact_changes"][0]["kind"], "population")
        self.assertEqual(len(result["affected_zone_days"]), 4)
        after = next(r for r in result["affected_zone_days"] if r["day"] == "2026-07-03")
        self.assertIn("actions", after["changes"])
        self.assertEqual(after["changes"]["weighted_burden"]["after"], "500.000000")
        self.assertEqual(result["total_changes"]["known_population_days"]["known_delta"], "80.000000")

    def test_episode_boundary_records_unknown_instead_of_observed_end(self):
        self.change(self.edit("reading", {"station": "river-s", "metric": "heat", "day": "2026-07-03"},
                              revision=2, value=None, quality="missing"))
        episodes = self.app.analyze(self.query)["episodes"]
        river = [r for r in episodes if r["zone"] == "river"]
        self.assertEqual(len(river), 2)
        self.assertEqual(river[0]["right_boundary"], "unknown")
        self.assertEqual(river[0]["days"], 2)
        self.assertEqual(river[1]["right_boundary"], "window")

    def test_author_cannot_self_review_and_digest_is_bound(self):
        draft = self.app.draft("draft", "heat", self.query, "分析员")
        self.assert_error("reviewer", lambda: self.app.review("draft", draft["digest"], "分析员"))
        self.assert_error("digest", lambda: self.app.review("draft", "wrong", "审核员"))
        self.assert_error("review", lambda: self.app.publish("draft", "release"))
        self.assertIsNone(self.app.publication.head("heat"))

    def test_reviewed_release_is_immutable_after_correction(self):
        self.reviewed()
        released = self.app.publish("draft", "release")
        self.change(self.edit("action", {"id": "cool-river"}, revision=2, capacity=50))
        old = self.app.publication.release("release")
        self.assertEqual(old["digest"], released["digest"])
        self.assertEqual(old["content"], released["content"])
        self.assertFalse(old["current_evidence"])
        self.assertEqual(old["content"]["document"]["analysis"]["total"]["known_protected_person_days"], 60)
        self.assertEqual(self.app.analyze(self.query)["total"]["known_protected_person_days"], 100)

    def test_correction_between_review_and_publish_blocks_stale_draft(self):
        self.reviewed()
        self.change(self.edit("population", {"id": "river-senior"}, revision=2, count=30))
        self.assert_error("stale", lambda: self.app.publish("draft", "release"))
        self.assertIsNone(self.app.publication.head("heat"))
        replacement = self.reviewed("replacement")
        release = self.app.publish("replacement", "release")
        self.assertEqual(release["content"]["draft_digest"], replacement["digest"])

    def test_concurrent_head_advance_is_not_silently_overwritten(self):
        self.reviewed("a")
        self.reviewed("b")
        self.app.publish("a", "first")
        self.assert_error("head_conflict", lambda: self.app.publish("b", "second"))
        self.assertEqual(self.app.publication.head("heat"), "first")
        self.app.publish("b", "second", "first")
        self.assertEqual(self.app.publication.head("heat"), "second")
        self.assertFalse(self.app.publication.release("first")["is_head"])
        self.assertEqual(self.app.publication.release("second")["content"]["supersedes"], "first")

    def test_release_retry_returns_original_and_rejects_reused_identity(self):
        self.reviewed()
        first = self.app.publish("draft", "release")
        retry = self.app.publish("draft", "release")
        self.assertEqual(first, retry)
        self.reviewed("other")
        self.assert_error("release_conflict", lambda: self.app.publish("other", "release"))
        self.assert_error("released", lambda: self.app.review("draft", first["content"]["draft_digest"], "另一审核员"))

    def test_export_digest_and_interrupted_replace_preserve_existing_file(self):
        self.reviewed()
        self.app.publish("draft", "release")
        destination = Path(self.temp.name) / "published.json"
        self.app.publication.export("release", destination)
        self.assertTrue(Publication.verify_export(destination)["valid"])
        original = destination.read_bytes()
        with patch("climate_ledger.publication.os.replace", side_effect=OSError("合成磁盘故障")):
            with self.assertRaises(OSError):
                self.app.publication.export("release", destination)
        self.assertEqual(original, destination.read_bytes())
        self.assertEqual(list(destination.parent.glob(".release-*")), [])
        changed = json.loads(original)
        changed["content"]["reviewer"] = "篡改"
        destination.write_text(json.dumps(changed), encoding="utf-8")
        self.assert_error("digest", lambda: Publication.verify_export(destination))

    def test_backup_reopen_and_semantic_audit(self):
        self.reviewed()
        self.app.publish("draft", "release")
        self.change(self.edit("population", {"id": "river-senior"}, revision=2, count=25))
        audit = self.app.audit()
        self.assertEqual(audit["status"], "verified")
        self.assertEqual(audit["batches"], 2)
        self.assertEqual(audit["releases"], 1)
        backup = Path(self.temp.name) / "backup.sqlite3"
        self.app.backup(backup)
        with Application(backup) as restored:
            self.assertEqual(restored.audit(), audit)
            self.assertEqual(restored.analyze(self.query), self.app.analyze(self.query))
            self.assertEqual(restored.publication.release("release"), self.app.publication.release("release"))
        self.assert_error("backup", lambda: self.app.backup(backup))
        self.assert_error("backup", lambda: self.app.backup(self.database))

    def test_second_connection_sees_only_committed_batches(self):
        with Application(self.database) as other:
            self.assertEqual(other.inspect()["watermark"], 1)
            self.change(self.edit("zone", {"id": "river"}, revision=2, name="河畔新版"))
            self.assertEqual(other.inspect()["watermark"], 2)
            self.assertEqual(other.ledger.snapshot()["zone"]["river"]["name"], "河畔新版")

    def test_cli_json_and_domain_failure_exit_code(self):
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["--database", str(self.database), "analyze", str(EXAMPLES / "exposure.json")])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["total"]["known_exposed_person_days"], 740)
        error = io.StringIO()
        with redirect_stderr(error):
            status = main(["--database", str(self.database), "history", "unknown", "anything"])
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(error.getvalue())["error"], "kind")

    def test_empty_snapshot_and_unknown_version_cannot_invent_analysis(self):
        with Application(":memory:") as empty:
            self.assert_error("metric", lambda: empty.analyze(self.query))
        self.assert_error("snapshot", lambda: self.app.analyze(self.query, seq=100))
        self.assert_error("period", lambda: self.app.analyze({**self.query, "end": "2026-06-01"}))
