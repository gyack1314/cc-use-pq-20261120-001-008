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


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Application(Path(self.temp.name) / "city.sqlite3")
        self.addCleanup(self.app.ledger.close)
        self.bundle = example("city.json")
        self.app.ingest(self.bundle)
        self.query = example("exposure.json")

    def edit(self, kind, identity, **changes):
        row = next(copy.deepcopy(item["data"]) for item in self.bundle["records"]
                   if item["kind"] == kind and all(item["data"].get(key) == value for key, value in identity.items()))
        row.update(changes)
        return {"kind": kind, "data": row}

    def ingest_edit(self, *records, name="correction", source=None):
        return self.app.ingest({"id": name, "source": source or self.bundle["source"], "records": list(records)})

    def assert_error(self, code, call):
        with self.assertRaises(DomainError) as error:
            call()
        self.assertEqual(error.exception.code, code)

    def test_replayed_package_has_no_new_watermark(self):
        first = self.app.inspect()
        result = self.app.ingest(self.bundle)
        self.assertTrue(result["replayed"])
        self.assertEqual(result["seq"], 1)
        self.assertEqual(first, self.app.inspect())

    def test_batch_identifier_conflict_rolls_back(self):
        changed = copy.deepcopy(self.bundle)
        changed["records"][0]["data"]["name"] = "改变后的名称"
        self.assert_error("idempotency", lambda: self.app.ingest(changed))
        self.assertEqual(self.app.inspect()["watermark"], 1)
        self.assertEqual(self.app.ledger.snapshot()["zone"]["river"]["name"], "河畔区")

    def test_new_objects_must_start_at_one(self):
        row = {"kind": "zone", "data": {"id": "new", "name": "新区", "revision": 2}}
        self.assert_error("revision", lambda: self.ingest_edit(row))
        self.assertNotIn("new", self.app.ledger.snapshot()["zone"])

    def test_revision_conflict_not_last_writer_wins(self):
        row = self.edit("zone", {"id": "river"}, name="别名")
        self.assert_error("revision_conflict", lambda: self.ingest_edit(row))
        self.assertEqual(len(self.app.history("zone", "river")), 1)

    def test_foreign_source_cannot_revise_reading(self):
        row = self.edit("reading", {"station": "river-s", "metric": "heat", "day": "2026-07-01"}, revision=2, value="36")
        self.assert_error("source", lambda: self.ingest_edit(row, source="另一来源"))
        self.assertEqual(self.app.inspect()["watermark"], 1)

    def test_observation_revision_retains_historical_analysis(self):
        before = self.app.analyze(self.query)
        row = self.edit("reading", {"station": "river-s", "metric": "heat", "day": "2026-07-01"}, revision=2, value="28")
        self.ingest_edit(row)
        current = self.app.analyze(self.query)
        historical = self.app.analyze(self.query, seq=1)
        self.assertEqual(historical, before)
        self.assertEqual(current["total"]["known_hazard_population_days"], 700)
        self.assertEqual(current["total"]["known_exposed_person_days"], 640)
        history = self.app.history("reading", "river-s:heat:2026-07-01")
        self.assertEqual([row["revision"] for row in history], [1, 2])
        self.assertEqual(history[1]["source"], self.bundle["source"])

    def test_missing_is_not_zero(self):
        row = self.edit("reading", {"station": "river-s", "metric": "heat", "day": "2026-07-01"}, revision=2, quality="missing", value=None)
        self.ingest_edit(row)
        result = self.app.analyze(self.query)
        self.assertEqual(result["status"], "incomplete")
        self.assertIsNone(result["total"]["hazard_fraction"])
        self.assertEqual(result["total"]["unknown_zone_days"], 1)
        target = next(row for row in result["daily"] if row["zone"] == "river" and row["day"] == "2026-07-01")
        self.assertIsNone(target["exposed"])
        self.assertEqual(target["reason"], ["observation_gap"])

    def test_suspect_value_is_excluded(self):
        row = self.edit("reading", {"station": "hill-s", "metric": "heat", "day": "2026-07-02"}, revision=2, quality="suspect")
        self.ingest_edit(row)
        diagnostic = self.app.diagnose(self.query)
        hill = next(row for row in diagnostic["zones"] if row["zone"] == "hill")
        self.assertEqual(hill["observations"]["complete"], 3)
        self.assertEqual(hill["observations"]["problems"][0]["issues"][0]["reason"], "suspect")

    def test_partial_station_weight_is_not_renormalized_into_coverage(self):
        station = self.edit("station", {"id": "river-s"}, revision=2, weight="0.6")
        second = self.edit("station", {"id": "river-s"}, id="river-s2", weight="0.4")
        self.ingest_edit(station, second)
        result = self.app.analyze(self.query)
        self.assertEqual(result["total"]["unknown_zone_days"], 4)
        target = next(row for row in result["daily"] if row["zone"] == "river")
        self.assertEqual(target["weather"]["coverage"], "0.600000")
        relaxed = self.app.analyze({**self.query, "minimum_coverage": "0.5"})
        self.assertEqual(relaxed["status"], "qualified")
        self.assertEqual(relaxed["total"]["known_exposed_person_days"], 740)

    def test_overweight_station_is_atomic_failure(self):
        zone = {"kind": "zone", "data": {"id": "new", "name": "新区", "revision": 1}}
        station = self.edit("station", {"id": "river-s"}, id="river-s2", weight="0.1")
        self.assert_error("station_weight", lambda: self.ingest_edit(zone, station))
        self.assertNotIn("new", self.app.ledger.snapshot()["zone"])
        self.assertEqual(self.app.inspect()["watermark"], 1)

    def test_unit_conversion_and_low_direction(self):
        row = self.edit("reading", {"station": "river-s", "metric": "heat", "day": "2026-07-01"}, revision=2, unit="F", value="89.6")
        self.ingest_edit(row)
        result = self.app.analyze(self.query)
        self.assertEqual(result["total"]["known_weighted_burden"], "2860.000000")
        water = self.app.analyze({**self.query, "metric": "water"})
        self.assertEqual(water["total"]["known_exposed_person_days"], 0)
        self.assertEqual(water["daily"][0]["weather"]["value"], "100.000000")
        self.assertIsNone(water["total"]["protection_fraction"])

    def test_bad_unit_or_range_does_not_create_batch(self):
        for changes, code in [({"unit": "kWh"}, "unit"), ({"value": "300"}, "reading_range")]:
            row = self.edit("reading", {"station": "river-s", "metric": "heat", "day": "2026-07-01"}, revision=2, **changes)
            self.assert_error(code, lambda: self.ingest_edit(row))
            self.assertEqual(self.app.inspect()["watermark"], 1)

    def test_invalid_numbers_and_dates_rejected(self):
        for value in [True, "NaN", "Infinity", "1e100", "not-a-number"]:
            row = self.edit("reading", {"station": "river-s", "metric": "heat", "day": "2026-07-01"}, revision=2, value=value)
            self.assert_error("number", lambda: self.ingest_edit(row))
        for value in ["2026-7-01", "2026-02-30", "20260701"]:
            row = self.edit("reading", {"station": "river-s", "metric": "heat", "day": "2026-07-01"}, day=value)
            self.assert_error("date", lambda: self.ingest_edit(row))

    def test_missing_value_contract_and_extra_fields(self):
        row = self.edit("reading", {"station": "river-s", "metric": "heat", "day": "2026-07-01"}, quality="missing")
        self.assert_error("missing", lambda: self.ingest_edit(row))
        row = self.edit("zone", {"id": "river"}, revision=2, unregistered="x")
        self.assert_error("shape", lambda: self.ingest_edit(row))

    def test_duplicate_objects_in_batch_rejected(self):
        row = self.edit("zone", {"id": "river"}, revision=2, name="新名称")
        self.assert_error("duplicate", lambda: self.ingest_edit(row, row))
        self.assertEqual(self.app.inspect()["watermark"], 1)

    def test_station_scope_validates_existing_observations(self):
        station = self.edit("station", {"id": "river-s"}, revision=2, start="2026-07-03")
        self.assert_error("station_scope", lambda: self.ingest_edit(station))
        self.assertEqual(self.app.ledger.snapshot()["station"]["river-s"]["start"], "2026-01-01")

    def test_csv_roundtrip_preserves_arrays_unicode_and_null(self):
        for kind in ["zone", "metric", "station", "population", "action", "reading"]:
            rows = [item["data"] for item in self.bundle["records"] if item["kind"] == kind]
            rendered = encode_csv(rows, kind)
            decoded = decode_csv("\ufeff" + rendered, kind)
            self.assertEqual(len(decoded), len(rows))
            rerendered = encode_csv([item["data"] for item in decoded], kind)
            self.assertEqual(rendered, rerendered)
        row = self.edit("reading", {"station": "river-s", "metric": "heat", "day": "2026-07-01"}, quality="missing", value=None)
        parsed = decode_csv(encode_csv([row["data"]], "reading"), "reading")
        self.assertIsNone(parsed[0]["data"]["value"])

    def test_csv_header_duplicate_and_malformed_array(self):
        self.assert_error("header", lambda: decode_csv("id,id\na,b\n", "zone"))
        self.assert_error("header", lambda: decode_csv("", "zone"))
        self.assert_error("csv", lambda: decode_csv("id,revision,name\na,1\n", "zone"))
        self.assert_error("csv", lambda: decode_csv("id,revision,name\na,-1,名称\n", "zone"))
        duplicate = "id,revision,name\na,1,名称\na,1,名称\n"
        self.assert_error("duplicate", lambda: decode_csv(duplicate, "zone"))

    def test_csv_snapshot_can_be_loaded_into_new_ledger(self):
        directory = Path(self.temp.name) / "csv"
        exported = self.app.export_csv(directory)
        files = {item["kind"]: item["path"] for item in exported["files"]}
        with Application(Path(self.temp.name) / "restored.sqlite3") as restored:
            restored.ingest_csv("csv-restored", "本地合成城市资料", files)
            self.assertEqual(restored.analyze(self.query)["total"], self.app.analyze(self.query)["total"])
            self.assertEqual(restored.compare(example("study.json"))["estimate"], "-2.500000")
