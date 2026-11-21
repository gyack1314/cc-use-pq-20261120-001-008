import copy
import json
import tempfile
import unittest
from pathlib import Path

from climate_ledger.application import Application
from climate_ledger.contracts import DomainError
from climate_ledger.formats import decode_csv, encode_csv


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def example(name):
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def calibration(id="heat-max-v1", **changes):
    record = {
        "id": id, "revision": 1, "name": "日最高气温显式口径", "metric": "heat",
        "concept": "daily-maximum-air-temperature", "concept_version": "1",
        "window": {"kind": "day"}, "effective_start": "2026-01-01", "effective_end": "2026-12-31",
        "dimension": "temperature", "unit": "C", "minimum": "-50", "maximum": "60",
        "threshold": "35", "direction": "high"}
    record.update(changes)
    return {"kind": "calibration", "data": record}


def fixed14(id="heat-fixed14-v1", **changes):
    record = {
        "id": id, "revision": 1, "name": "14时固定时刻气温", "metric": "heat",
        "concept": "fixed-clock-air-temperature", "concept_version": "1",
        "window": {"kind": "fixed_clock", "anchor": "14:00"},
        "effective_start": "2026-08-01", "effective_end": "2026-12-31",
        "dimension": "temperature", "unit": "C", "minimum": "-50", "maximum": "60",
        "threshold": "33", "direction": "high"}
    record.update(changes)
    return {"kind": "calibration", "data": record}


def conversion(id="conv-legacy-max", **changes):
    record = {
        "id": id, "revision": 1, "name": "旧最高温到显式最高温恒等转换",
        "source": "legacy.heat", "target": "heat-max-v1",
        "formula": {"kind": "scale", "a": "1"},
        "minimum": "20", "maximum": "45", "start": "2026-01-01", "end": "2026-10-31",
        "reversible": True}
    record.update(changes)
    return {"kind": "conversion", "data": record}


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Application(Path(self.temp.name) / "city.sqlite3")
        self.addCleanup(self.app.ledger.close)
        self.app.ingest(example("city.json"))
        self.counter = 0
        self.legacy_query = {"metric": "heat", "zones": ["river", "hill"],
                             "start": "2026-07-01", "end": "2026-07-04", "minimum_coverage": "0.8"}

    def ingest(self, *records, name=None, source="本地合成城市资料"):
        batch_id = name or f"batch-{id(self)}-{self.counter}"
        self.counter += 1
        return self.app.ingest({"id": batch_id, "source": source, "records": list(records)})

    def assert_error(self, code, call):
        with self.assertRaises(DomainError) as error:
            call()
        self.assertEqual(error.exception.code, code)

    def install_calibrations(self):
        return self.ingest(calibration(), fixed14(), conversion(),
                           conversion(id="conv-14to-max", name="14时温到最高温经验关系",
                                      source="heat-fixed14-v1", target="heat-max-v1",
                                      formula={"kind": "affine", "a": "1", "b": "2"},
                                      minimum="20", maximum="40",
                                      start="2026-08-01", end="2026-10-31", reversible=False))

    def reconfirm_max_action(self):
        self.ingest({"kind": "action", "data": {
            "id": "cool-river-max", "revision": 1, "name": "显式口径重新确认", "zone": "river",
            "metric": "heat", "start": "2026-07-03", "end": "2026-07-04", "status": "active",
            "capacity": 30, "groups": ["senior", "adult"], "priority": 10,
            "facility": "river-library-max", "calibration": "heat-max-v1"}})

    def test_legacy_observations_keep_original_definition_without_drift(self):
        before = self.app.analyze(self.legacy_query)
        self.install_calibrations()
        after = self.app.analyze(self.legacy_query)
        self.assertEqual(after["watermark"], 2)
        self.assertEqual({k: v for k, v in after.items() if k != "watermark"},
                         {k: v for k, v in before.items() if k != "watermark"})
        weather = after["daily"][0]["weather"]
        self.assertEqual(weather["calibration"], "legacy.heat")
        self.assertTrue(weather["calibration_legacy"])
        self.assertIsNone(weather["calibration_revision"])
        self.assertEqual(weather["window"], {"kind": "unspecified", "legacy": True})
        self.assertEqual(after["total"]["known_exposed_person_days"], 740)

    def test_calibration_declares_stable_concept_window_threshold_and_scope(self):
        self.install_calibrations()
        result = self.app.analyze({**self.legacy_query, "calibration": "heat-max-v1"})
        descriptor = next(c for c in result["calibration_registry"]["calibrations"]
                          if c["id"] == "heat-max-v1")
        self.assertEqual(descriptor["concept"], "daily-maximum-air-temperature")
        self.assertEqual(descriptor["concept_version"], "1")
        self.assertEqual(descriptor["window"], {"kind": "day"})
        self.assertEqual(descriptor["threshold"], "35")
        self.assertEqual(descriptor["effective_start"], "2026-01-01")
        self.assertEqual(descriptor["effective_end"], "2026-12-31")

    def test_different_measurement_concepts_are_incompatible_by_default(self):
        self.install_calibrations()
        result = self.app.analyze({**self.legacy_query, "calibration": "heat-max-v1"})
        self.assertEqual(result["total"]["usable_zone_days"], 0)
        issue = result["daily"][0]["weather"]["issues"][0]
        self.assertEqual(issue["reason"], "calibration_mismatch")
        self.assertEqual(issue["binding"], "legacy.heat")
        self.assertEqual(issue["calibration"], "heat-max-v1")

    def test_same_definition_different_units_remain_physically_consistent(self):
        self.install_calibrations()
        reading = {"kind": "reading", "data": {
            "station": "river-s", "metric": "heat", "day": "2026-07-05", "revision": 1,
            "value": "95", "unit": "F", "quality": "valid", "calibration": "heat-max-v1"}}
        self.ingest(reading)
        result = self.app.analyze({"metric": "heat", "zones": ["river"], "start": "2026-07-05",
                                   "end": "2026-07-05", "minimum_coverage": "0.8",
                                   "calibration": "heat-max-v1"})
        weather = result["daily"][0]["weather"]
        self.assertEqual(weather["value"], "35.000000")
        self.assertTrue(weather["hazardous"])
        self.assertNotIn("conversion", weather["evidence"][0])

    def test_physical_unit_conversion_precedes_declared_calibration_conversion(self):
        self.install_calibrations()
        # 95F is a legacy reading: first 95F -> 35C (physical), then identity conversion range checked on 35C
        self.ingest({"kind": "reading", "data": {
            "station": "river-s", "metric": "heat", "day": "2026-07-10", "revision": 1,
            "value": "95", "unit": "F", "quality": "valid"}})
        result = self.app.analyze(
            {**self.legacy_query, "zones": ["river"], "start": "2026-07-10", "end": "2026-07-10",
             "calibration": "heat-max-v1", "conversions": ["conv-legacy-max"]})
        weather = result["daily"][0]["weather"]
        self.assertEqual(weather["value"], "35.000000")
        self.assertTrue(weather["hazardous"])
        self.assertEqual(weather["evidence"][0]["conversion"]["id"], "conv-legacy-max")

    def test_close_values_under_different_windows_are_not_paired(self):
        self.install_calibrations()
        result = self.app.compare({
            "metric": "heat", "treated": ["river"], "controls": ["hill"],
            "before": ["2026-07-01", "2026-07-02"], "after": ["2026-07-03", "2026-07-04"],
            "tolerance": "2", "minimum_pairs": 1, "calibration": "heat-fixed14-v1"})
        self.assertEqual(result["status"], "insufficient")
        self.assertIsNone(result["estimate"])
        self.assertTrue(all(e["reason"] == "incomparable_calibration" for e in result["excluded"]))
        detail = result["excluded"][0]["detail"]
        self.assertEqual(detail["binding"], "legacy.heat")

    def test_declared_conversion_with_evidence_makes_legacy_comparable(self):
        self.install_calibrations()
        query = {**self.legacy_query, "calibration": "heat-max-v1",
                 "conversions": ["conv-legacy-max"]}
        result = self.app.analyze(query)
        self.assertEqual(result["status"], "qualified")
        target_day = next(row for row in result["daily"]
                          if row["zone"] == "river" and row["day"] == "2026-07-03")
        self.assertEqual(target_day["weather"]["value"], "35.000000")
        self.assertTrue(target_day["weather"]["hazardous"])
        used = target_day["weather"]["evidence"][0]["conversion"]
        self.assertEqual(used["id"], "conv-legacy-max")
        self.assertTrue(used["reversible"])
        self.assertEqual(result["calibration_registry"]["conversions"][0]["id"], "conv-legacy-max")

    def august_stream(self, value1="33", value2=None):
        records = [
            {"kind": "station", "data": {"id": "river-s", "revision": 2, "zone": "river",
                                         "start": "2026-01-01", "end": "2026-07-31",
                                         "weight": "1", "metrics": ["heat", "water"]}},
            {"kind": "station", "data": {"id": "river-s14", "revision": 1, "zone": "river",
                                         "start": "2026-08-01", "end": "2026-12-31",
                                         "weight": "1", "metrics": ["heat"]}},
            {"kind": "reading", "data": {"station": "river-s14", "metric": "heat",
                                         "day": "2026-08-01", "revision": 1, "value": value1,
                                         "unit": "C", "quality": "valid",
                                         "calibration": "heat-fixed14-v1"}}]
        if value2 is not None:
            records.append({"kind": "reading", "data": {
                "station": "river-s14", "metric": "heat", "day": "2026-08-02", "revision": 1,
                "value": value2, "unit": "C", "quality": "valid",
                "calibration": "heat-fixed14-v1"}})
        return records

    def test_conversion_outside_declared_range_remains_incomparable(self):
        self.install_calibrations()
        # narrow the empirical conversion so the second August day falls outside its dates
        self.ingest(conversion(id="conv-14to-max", revision=2,
                               source="heat-fixed14-v1", target="heat-max-v1",
                               formula={"kind": "affine", "a": "1", "b": "2"},
                               minimum="20", maximum="40", start="2026-08-01",
                               end="2026-08-01", reversible=False))
        self.ingest(*self.august_stream(value2="45"))
        result = self.app.analyze(
            {"metric": "heat", "zones": ["river"], "start": "2026-08-01", "end": "2026-08-02",
             "minimum_coverage": "0.8", "calibration": "heat-max-v1",
             "conversions": ["conv-14to-max"]})
        day = next(row for row in result["daily"] if row["day"] == "2026-08-02")
        self.assertFalse(day["usable"])
        self.assertEqual(day["weather"]["issues"][0]["reason"], "outside_period")

    def test_converted_result_beyond_target_range_is_not_emitted(self):
        self.install_calibrations()
        # disjoint date window avoids contradicting the identity conversion; 32C*10 exceeds the target range
        self.ingest(conversion(id="conv-amplify", source="legacy.heat", target="heat-max-v1",
                               formula={"kind": "affine", "a": "10", "b": "0"},
                               minimum="0", maximum="100", start="2026-11-01", end="2026-12-31",
                               reversible=True))
        result = self.app.analyze(
            {**self.legacy_query, "zones": ["river"], "start": "2026-07-01", "end": "2026-07-01",
             "calibration": "heat-max-v1", "conversions": ["conv-amplify"]})
        weather = result["daily"][0]["weather"]
        self.assertFalse(result["daily"][0]["usable"])
        self.assertEqual(weather["issues"][0]["reason"], "outside_period")
        # within the window the amplified 320C would exceed the target range: no number is emitted
        self.ingest({"kind": "reading", "data": {
            "station": "river-s", "metric": "heat", "day": "2026-11-15", "revision": 1,
            "value": "32", "unit": "C", "quality": "valid"}})
        later = self.app.analyze(
            {**self.legacy_query, "zones": ["river"], "start": "2026-11-15", "end": "2026-11-15",
             "calibration": "heat-max-v1", "conversions": ["conv-amplify"]})
        self.assertEqual(later["daily"][0]["weather"]["issues"][0]["reason"], "converted_out_of_range")

    def test_no_automatic_path_search_when_allow_list_is_silent(self):
        self.install_calibrations()
        result = self.app.analyze({**self.legacy_query, "calibration": "heat-max-v1"})
        self.assertEqual(result["total"]["usable_zone_days"], 0)

    def test_unknown_or_contradictory_conversion_paths_rejected(self):
        self.install_calibrations()
        self.assert_error("conversion", lambda: self.app.analyze(
            {**self.legacy_query, "calibration": "heat-max-v1", "conversions": ["missing-conv"]}))
        contradictory = conversion(id="conv-other", name="另一条矛盾恒等关系",
                                   formula={"kind": "affine", "a": "1", "b": "1"},
                                   minimum="20", maximum="40", start="2026-02-01", end="2026-09-30",
                                   reversible=False)
        self.assert_error("conversion_conflict", lambda: self.ingest(contradictory))
        self.assertEqual(self.app.inspect()["watermark"], 2)

    def test_metric_semantics_cannot_be_revised_into_a_new_definition(self):
        row = next(copy.deepcopy(item) for item in example("city.json")["records"]
                   if item["kind"] == "metric" and item["data"]["id"] == "heat")
        row["data"].update(revision=2, threshold="33")
        self.assert_error("metric_redefinition", lambda: self.ingest(row))
        self.assertEqual(self.app.ledger.snapshot()["metric"]["heat"]["threshold"], "30")

    def test_reading_calibration_binding_is_immutable_across_revisions(self):
        self.install_calibrations()
        original = {"kind": "reading", "data": {
            "station": "river-s", "metric": "heat", "day": "2026-07-01", "revision": 2,
            "value": "32", "unit": "C", "quality": "valid", "calibration": "heat-max-v1"}}
        self.assert_error("calibration_rebind", lambda: self.ingest(original))
        history = self.app.history("reading", "river-s:heat:2026-07-01")
        self.assertEqual([row["revision"] for row in history], [1])

    def test_observation_outside_calibration_scope_rejected_in_bundle(self):
        # a new reading identity under a calibration whose scope does not cover the day
        row = {"kind": "reading", "data": {
            "station": "river-s", "metric": "heat", "day": "2026-07-15", "revision": 1,
            "value": "32", "unit": "C", "quality": "valid", "calibration": "heat-fixed14-v1"}}
        self.assert_error("calibration_scope",
                          lambda: self.ingest(fixed14(), row))
        self.assertNotIn("heat-fixed14-v1", self.app.ledger.snapshot()["calibration"])

    def test_duplicate_calibration_definition_rejected(self):
        self.install_calibrations()
        self.assert_error("calibration_duplicate", lambda: self.ingest(
            calibration(id="heat-max-dup", name="重复定义")))
    def test_calibration_dimension_and_range_must_agree_with_metric(self):
        bad = calibration(id="bad-dim", dimension="water", unit="L",
                          minimum="0", maximum="1000", threshold="100")
        self.assert_error("dimension", lambda: self.ingest(bad))
        wide = calibration(id="bad-range", minimum="-60")
        self.assert_error("range", lambda: self.ingest(wide))

    def test_window_anchor_requires_valid_clock_time(self):
        for anchor in ["14", "24:00", "09:5", "ab:cd"]:
            self.assert_error("window", lambda anchor=anchor: self.ingest(
                fixed14(id="heat-bad-" + anchor.replace(":", ""), window={"kind": "fixed_clock", "anchor": anchor})))

    def test_formula_shapes_and_reversibility_flag_validated(self):
        self.assert_error("formula", lambda: self.ingest(
            conversion(id="bad-scale", formula={"kind": "scale", "a": "0"})))
        self.assert_error("formula", lambda: self.ingest(
            conversion(id="bad-affine", formula={"kind": "affine", "a": "1"})))
        bad_flag = conversion(id="bad-flag")
        bad_flag["data"]["reversible"] = "yes"
        self.assert_error("field", lambda: self.ingest(bad_flag))
        legacy = conversion(id="legacy-id")
        legacy["data"]["id"] = "legacy.illegal"
        self.assert_error("identity", lambda: self.ingest(legacy))

    def test_action_commitments_are_isolated_by_calibration(self):
        self.install_calibrations()
        self.ingest(
            *self.august_stream(),
            {"kind": "action", "data": {
                "id": "legacy-summer", "revision": 1, "name": "旧口径夏季承诺", "zone": "river",
                "metric": "heat", "start": "2026-08-01", "end": "2026-08-02", "status": "active",
                "capacity": 30, "groups": ["senior", "adult"], "priority": 10,
                "facility": "river-library-summer"}},
            {"kind": "action", "data": {
                "id": "fixed14-space", "revision": 1, "name": "14时口径行动", "zone": "river",
                "metric": "heat", "start": "2026-08-01", "end": "2026-08-02", "status": "active",
                "capacity": 25, "groups": ["senior", "adult"], "priority": 10,
                "facility": "river-hub-14", "calibration": "heat-fixed14-v1"}})
        result = self.app.analyze(
            {"metric": "heat", "zones": ["river"], "start": "2026-08-01", "end": "2026-08-01",
             "minimum_coverage": "0.8", "calibration": "heat-fixed14-v1"})
        day = result["daily"][0]
        self.assertEqual([action["action"] for action in day["actions"]], ["fixed14-space"])
        self.assertEqual(day["protected"], 25)
        # the legacy commitment remains on the books and still serves legacy analysis binding
        self.assertEqual(self.app.ledger.snapshot()["action"]["legacy-summer"]["status"], "active")

    def test_legacy_commitment_is_not_withdrawn_when_new_threshold_not_exceeded(self):
        # fixed14 reading 32C stays below the 33C threshold; legacy action must not be silently retired
        self.install_calibrations()
        self.ingest(
            *self.august_stream(value1="32"),
            {"kind": "action", "data": {
                "id": "legacy-summer", "revision": 1, "name": "旧口径承诺", "zone": "river",
                "metric": "heat", "start": "2026-08-01", "end": "2026-08-02", "status": "active",
                "capacity": 30, "groups": ["senior", "adult"], "priority": 10,
                "facility": "river-library-summer"}})
        result = self.app.analyze(
            {"metric": "heat", "zones": ["river"], "start": "2026-08-01", "end": "2026-08-01",
             "minimum_coverage": "0.8", "calibration": "heat-fixed14-v1"})
        day = result["daily"][0]
        self.assertFalse(day["hazardous"])
        self.assertEqual(day["actions"], [])
        self.assertEqual(self.app.ledger.snapshot()["action"]["legacy-summer"]["status"], "active")

    def test_only_explicitly_reconfirmed_actions_switch_definition(self):
        self.install_calibrations()
        self.ingest(
            {"kind": "action", "data": {
                "id": "cool-river-max", "revision": 1, "name": "显式口径重新确认",
                "zone": "river", "metric": "heat", "start": "2026-07-03", "end": "2026-07-04",
                "status": "active", "capacity": 30, "groups": ["senior", "adult"], "priority": 10,
                "facility": "river-library-max", "calibration": "heat-max-v1"}})
        result = self.app.analyze(
            {**self.legacy_query, "zones": ["river"], "calibration": "heat-max-v1",
             "conversions": ["conv-legacy-max"]})
        day = next(row for row in result["daily"] if row["day"] == "2026-07-03")
        self.assertEqual([action["action"] for action in day["actions"]], ["cool-river-max"])
        legacy = self.app.analyze({**self.legacy_query, "zones": ["river"]})
        legacy_day = next(row for row in legacy["daily"] if row["day"] == "2026-07-03")
        self.assertEqual([action["action"] for action in legacy_day["actions"]], ["cool-river"])

    def test_action_calibration_rebind_blocked_and_scope_checked(self):
        self.install_calibrations()
        # changing the binding of an existing action is blocked even when the target is in scope
        row = next(copy.deepcopy(item) for item in example("city.json")["records"]
                   if item["kind"] == "action" and item["data"]["id"] == "cool-river")
        row["data"].update(revision=2, calibration="heat-max-v1")
        self.assert_error("calibration_rebind", lambda: self.ingest(row))
        outside = {"kind": "action", "data": {
            "id": "out-of-scope", "revision": 1, "name": "超范围行动", "zone": "river",
            "metric": "heat", "start": "2026-07-01", "end": "2026-07-31", "status": "planned",
            "capacity": 10, "groups": ["senior", "adult"], "priority": 1,
            "facility": "x", "calibration": "heat-fixed14-v1"}}
        self.assert_error("calibration_scope", lambda: self.ingest(outside))

    def test_study_uses_one_common_calibration_and_records_exclusions(self):
        self.install_calibrations()
        self.reconfirm_max_action()
        result = self.app.compare({
            "metric": "heat", "treated": ["river"], "controls": ["hill"],
            "before": ["2026-07-01", "2026-07-02"], "after": ["2026-07-03", "2026-07-04"],
            "tolerance": "1", "minimum_pairs": 2, "calibration": "heat-max-v1",
            "conversions": ["conv-legacy-max"]})
        self.assertEqual(result["status"], "qualified")
        self.assertEqual(len(result["phases"]["before"]["pairs"]), 2)
        for phase in result["phases"].values():
            for pair in phase["pairs"]:
                self.assertEqual(pair["treated_weather"]["calibration"], "heat-max-v1")
                self.assertEqual(pair["control_weather"]["calibration"], "heat-max-v1")

    def test_narrowing_conversion_range_disqualifies_study_without_touching_readings(self):
        self.install_calibrations()
        self.reconfirm_max_action()
        spec = {"metric": "heat", "treated": ["river"], "controls": ["hill"],
                "before": ["2026-07-01", "2026-07-02"], "after": ["2026-07-03", "2026-07-04"],
                "tolerance": "1", "minimum_pairs": 2, "calibration": "heat-max-v1",
                "conversions": ["conv-legacy-max"]}
        self.assertEqual(self.app.compare(spec)["status"], "qualified")
        self.ingest(conversion(revision=2, minimum="20", maximum="34"))
        narrowed = self.app.compare(spec)
        self.assertEqual(narrowed["status"], "insufficient")
        self.assertTrue(all(e["reason"] == "incomparable_calibration" for e in narrowed["excluded"]))
        self.assertEqual(narrowed["excluded"][0]["detail"]["reason"], "out_of_range")
        history = self.app.history("reading", "river-s:heat:2026-07-03")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["payload"]["value"], "35")
        # drafts built on the older interpretation must be re-judged against the current watermark
        draft = self.app.draft("stale", "heat", spec, "分析员", kind="study")
        self.assertFalse(draft["content"]["assessment"]["publishable"])

    def test_unrelated_topics_are_not_declared_changed(self):
        self.install_calibrations()
        water_query = {"metric": "water", "zones": ["river"],
                       "start": "2026-07-01", "end": "2026-07-04"}
        water_before = self.app.analyze(water_query)
        self.ingest(conversion(revision=2, minimum="20", maximum="34"))
        water_after = self.app.analyze(water_query)
        self.assertEqual({k: v for k, v in water_after.items() if k != "watermark"},
                         {k: v for k, v in water_before.items() if k != "watermark"})

    def test_bundle_association_check_rejects_whole_package(self):
        orphan = calibration(id="orphan", metric="missing-metric")
        zone = {"kind": "zone", "data": {"id": "never-loaded", "revision": 1, "name": "不入账区"}}
        self.assert_error("reference", lambda: self.ingest(orphan, zone, name="broken-bundle"))
        self.assertNotIn("never-loaded", self.app.ledger.snapshot()["zone"])
        self.assertNotIn("orphan", self.app.ledger.snapshot()["calibration"])
        self.assertEqual(self.app.inspect()["watermark"], 1)

    def test_reconciliation_reports_calibration_basis_and_revisions(self):
        self.install_calibrations()
        before = self.app.analyze({**self.legacy_query, "calibration": "heat-max-v1",
                                   "conversions": ["conv-legacy-max"]})
        self.ingest(conversion(revision=2, minimum="20", maximum="34"))
        report = self.app.reconcile(
            {**self.legacy_query, "calibration": "heat-max-v1",
             "conversions": ["conv-legacy-max"]}, 2, 3)
        changed = next(item for item in report["affected_zone_days"] if item["day"] == "2026-07-03")
        self.assertEqual(changed["before_evidence"]["calibration"], "heat-max-v1")
        self.assertIn("conversion",
                      changed["before_evidence"]["readings"][0])
        self.assertEqual(changed["changes"]["usable"], {"before": True, "after": False})

    def test_csv_roundtrip_for_calibration_and_conversion(self):
        self.install_calibrations()
        snapshot = self.app.ledger.snapshot()
        for kind in ("calibration", "conversion"):
            rows = [row for row in snapshot[kind].values()]
            rendered = encode_csv(rows, kind)
            decoded = decode_csv(rendered, kind)
            self.assertEqual(len(decoded), len(rows))
            self.assertEqual(encode_csv([item["data"] for item in decoded], kind), rendered)
        reading_rows = [row for row in snapshot["reading"].values()
                        if row["station"] == "river-s" and row["day"] == "2026-07-01"]
        rendered = encode_csv(reading_rows, "reading")
        self.assertNotIn("calibration", rendered.splitlines()[1])
        self.assertNotIn("calibration", decode_csv(rendered, "reading")[0]["data"])

    def test_published_release_records_definition_conversion_and_commitment(self):
        self.install_calibrations()
        spec = {**self.legacy_query, "calibration": "heat-max-v1",
                "conversions": ["conv-legacy-max"]}
        draft = self.app.draft("cal-draft", "heat-max", spec, "分析员")
        self.app.review("cal-draft", draft["digest"], "审核员")
        release = self.app.publish("cal-draft", "cal-release")
        document = release["content"]["document"]["analysis"]
        registry = document["calibration_registry"]
        self.assertEqual({c["id"] for c in registry["calibrations"]},
                         {"legacy.heat", "heat-max-v1"})
        self.assertEqual(registry["conversions"][0]["id"], "conv-legacy-max")
        day = next(row for row in document["daily"]
                   if row["zone"] == "river" and row["day"] == "2026-07-03")
        self.assertEqual(day["weather"]["evidence"][0]["conversion"]["id"], "conv-legacy-max")
        self.assertEqual(day["actions"], [])
        # subsequent calibration work cannot mutate the released document
        self.ingest(conversion(revision=2, minimum="20", maximum="34"))
        self.assertEqual(self.app.publication.release("cal-release")["digest"], release["digest"])
        self.assertEqual(self.app.audit()["status"], "verified")


if __name__ == "__main__":
    unittest.main()
