import copy
import json
import tempfile
from pathlib import Path

from climate_ledger.application import Application
from climate_ledger.contracts import DomainError, canonical
from climate_ledger.publication import Publication


def run():
    examples = Path(__file__).parent / "examples"
    load = lambda name: json.loads((examples / name).read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        with Application(root / "city.sqlite3") as app:
            bundle = load("city.json")
            assert app.ingest(bundle)["seq"] == 1
            assert app.ingest(bundle)["replayed"]
            exposure = app.analyze(load("exposure.json"))
            assert exposure["total"]["known_exposed_person_days"] == 740
            study = app.compare(load("study.json"))
            assert study["estimate"] == "-2.500000"
            plan = app.plan(load("portfolio.json"))
            assert plan["selected"] == ["shared-river"]
            assert app.priorities(load("exposure.json"))["priorities"][0]["zone"] == "hill"
            draft = app.draft("july", "heat-july", load("exposure.json"), "分析员")
            app.review("july", draft["digest"], "审核员")
            release = app.publish("july", "july-v1")
            app.publication.export("july-v1", root / "july.json")
            assert Publication.verify_export(root / "july.json")["valid"]
            correction = copy.deepcopy(next(row for row in bundle["records"] if row["kind"] == "population"
                                             and row["data"]["id"] == "river-senior"))
            correction["data"].update(revision=2, count=40)
            app.ingest({"id": "population-corrected", "source": bundle["source"], "records": [correction]})
            assert app.analyze(load("exposure.json"), seq=1) == exposure
            assert app.publication.release("july-v1")["digest"] == release["digest"]
            assert not app.publication.release("july-v1")["current_evidence"]
            changed = app.reconcile(load("exposure.json"), 1, 2)
            assert len(changed["affected_zone_days"]) == 4
            assert app.audit()["status"] == "verified"
            app.backup(root / "backup.sqlite3")
        with Application(root / "backup.sqlite3") as restored:
            assert restored.audit()["batches"] == 2
            assert restored.publication.release("july-v1")["digest"] == release["digest"]

        # 并行测量口径：旧专题保持原定义，新口径与有证据转换独立可查
        with Application(root / "calibration.sqlite3") as calibrated:
            calibrated.ingest(bundle)
            calibrated.ingest(load("calibrations.json"))
            legacy = calibrated.analyze(load("exposure.json"))
            assert legacy["daily"][0]["weather"]["calibration"] == "legacy.heat"
            assert legacy["total"]["known_exposed_person_days"] == 740
            fixed14 = calibrated.analyze(load("exposure_fixed14.json"))
            assert fixed14["status"] == "qualified"
            august = fixed14["daily"][0]
            assert august["weather"]["calibration"] == "heat-fixed14-v1"
            assert august["weather"]["hazardous"] is True
            # 旧口径承诺不参与新口径分配，只有显式重新确认的行动生效
            assert [item["action"] for item in august["actions"]] == ["cool-river-14"]
            # 不允许转换时，同名温度列不能配对
            silent = calibrated.analyze({**load("exposure_max.json"), "conversions": []})
            assert silent["total"]["usable_zone_days"] == 0
            assert silent["daily"][0]["weather"]["issues"][0]["reason"] == "calibration_mismatch"
            max_view = calibrated.analyze(load("exposure_max.json"))
            converted_day = next(row for row in max_view["daily"]
                                 if row["zone"] == "river" and row["day"] == "2026-07-03")
            assert converted_day["weather"]["value"] == "35.000000"
            assert converted_day["weather"]["evidence"][0]["conversion"]["id"] == "conv-legacy-max"
            study = calibrated.compare(load("study_max.json"))
            assert study["status"] == "qualified"
            assert all(pair["treated_weather"]["calibration"] == "heat-max-v1"
                       for phase in study["phases"].values() for pair in phase["pairs"])
            # 收窄转换支持范围：原始读数不变，跨期研究失去资格，未使用该转换的专题数值不变
            narrowed = copy.deepcopy(next(row for row in load("calibrations.json")["records"]
                                           if row["kind"] == "conversion"
                                           and row["data"]["id"] == "conv-legacy-max"))
            narrowed["data"].update(revision=2, minimum="20", maximum="34")
            calibrated.ingest({"id": "conversion-narrowed", "source": bundle["source"],
                               "records": [narrowed]})
            assert calibrated.compare(load("study_max.json"))["status"] == "insufficient"
            assert calibrated.history("reading", "river-s:heat:2026-07-03")[0]["payload"]["value"] == "35"
            assert calibrated.analyze(load("exposure_fixed14.json"))["status"] == "qualified"
            assert calibrated.audit()["status"] == "verified"
        print(canonical({"status": "通过", "synthetic": True, "exposure": exposure["total"],
                         "study_estimate": study["estimate"], "portfolio": plan["selected"],
                         "corrected_zone_days": len(changed["affected_zone_days"]),
                         "parallel_calibrations": True, "archive_verified": True}))


if __name__ == "__main__":
    run()
