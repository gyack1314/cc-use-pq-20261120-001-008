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
        print(canonical({"status": "通过", "synthetic": True, "exposure": exposure["total"],
                         "study_estimate": study["estimate"], "portfolio": plan["selected"],
                         "corrected_zone_days": len(changed["affected_zone_days"]), "archive_verified": True}))


if __name__ == "__main__":
    run()
