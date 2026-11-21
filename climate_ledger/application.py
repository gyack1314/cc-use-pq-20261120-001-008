from .adaptation import Adaptation, normalize_action
from .analysis.exposure import Exposure
from .analysis.study import Study
from .contracts import KINDS, identifier, integer, require, shape, text
from .ledger import Ledger
from .observatory.calibration import Calibrations, normalize_calibration, normalize_conversion
from .observatory.catalog import Catalog, normalize_metric, normalize_station, normalize_zone
from .observatory.readings import Readings, normalize_reading
from .publication import Publication
from .residents import Residents, normalize_population


NORMALIZERS = {"zone": normalize_zone, "metric": normalize_metric,
               "calibration": normalize_calibration, "conversion": normalize_conversion,
               "station": normalize_station,
               "population": normalize_population, "action": normalize_action,
               "reading": normalize_reading}

METRIC_SEMANTIC_FIELDS = ("dimension", "unit", "threshold", "direction", "minimum", "maximum")


class Application:
    def __init__(self, path):
        self.ledger = Ledger(path)
        self.publication = Publication(self.ledger)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.ledger.close()

    @staticmethod
    def validate(snapshot):
        Catalog(snapshot).validate()
        Calibrations(snapshot).validate()
        Readings(snapshot).validate()
        Residents(snapshot).validate()
        Adaptation(snapshot).validate()

    def ingest(self, bundle):
        shape(bundle, ("id", "source", "records"))
        identifier(bundle["id"], "batch.id")
        text(bundle["source"], "source")
        require(isinstance(bundle["records"], list) and 0 < len(bundle["records"]) <= 100000,
                "batch", "入账记录数量需在1至100000之间")
        normalized = []
        for item in bundle["records"]:
            shape(item, ("kind", "data"))
            require(item["kind"] in KINDS, "kind", "未知资料类型")
            record = NORMALIZERS[item["kind"]](item["data"])
            if item["kind"] == "metric":
                previous = self.ledger.snapshot()["metric"].get(record["id"])
                if previous is not None and any(record[field] != previous[field]
                                                 for field in METRIC_SEMANTIC_FIELDS):
                    require(False, "metric_redefinition",
                            "指标语义字段不能修订；测量定义变化应注册为新口径", metric=record["id"])
            if item["kind"] == "reading":
                key = ":".join((record["station"], record["metric"], record["day"]))
                previous = self.ledger.snapshot()["reading"].get(key)
                if previous is not None and previous.get("calibration") != record.get("calibration"):
                    require(False, "calibration_rebind",
                            "观测不能在修订中改变测量口径；新定义应使用新观测或新口径", reading=key)
            if item["kind"] == "action":
                previous = self.ledger.snapshot()["action"].get(record["id"])
                if previous is not None and previous.get("calibration") != record.get("calibration"):
                    require(False, "calibration_rebind",
                            "行动承诺不能在修订中改变测量口径；重新确认应登记新行动", action=record["id"])
            normalized.append((item["kind"], record))
        with self.ledger.transaction(write=True):
            return self.ledger.append(bundle, normalized, self.validate)

    def analyze(self, spec, *, seq=None):
        with self.ledger.transaction():
            watermark = self.ledger.watermark() if seq is None else integer(seq, "seq")
            analysis = Exposure(self.ledger.snapshot(watermark)).analyze(spec)
            return {"watermark": watermark, **analysis}

    def compare(self, spec, *, seq=None):
        with self.ledger.transaction():
            watermark = self.ledger.watermark() if seq is None else integer(seq, "seq")
            analysis = Study(self.ledger.snapshot(watermark)).compare(spec)
            return {"watermark": watermark, **analysis}

    def draft(self, draft_id, scope, spec, author, *, kind="exposure"):
        require(kind in ("exposure", "study"), "kind", "草稿分析类型无效")
        with self.ledger.transaction(write=True):
            snapshot = self.ledger.snapshot()
            analysis = Exposure(snapshot).analyze(spec) if kind == "exposure" else Study(snapshot).compare(spec)
            return self.publication.create(draft_id, scope, analysis, author)

    def review(self, draft_id, digest, reviewer):
        with self.ledger.transaction(write=True):
            return self.publication.review(draft_id, digest, reviewer)

    def publish(self, draft_id, release_id, expected_head=None):
        with self.ledger.transaction(write=True):
            return self.publication.publish(draft_id, release_id, expected_head)

    def inspect(self):
        with self.ledger.transaction():
            snapshot = self.ledger.snapshot()
            self.validate(snapshot)
            return {**self.ledger.integrity(), "objects": {kind: len(rows) for kind, rows in snapshot.items()},
                    "batches": self.ledger.provenance(self.ledger.watermark()),
                    "releases": self.publication.list_releases()}

    def diagnose(self, spec):
        from .contracts import query_spec
        spec = query_spec(spec)
        with self.ledger.transaction():
            snapshot = self.ledger.snapshot()
            readings = Readings(snapshot, spec["calibration"])
            residents = Residents(snapshot)
            adaptation = Adaptation(snapshot, spec["calibration"])
            return {"watermark": self.ledger.watermark(), "zones": [
                {"zone": zone, "observations": readings.diagnostics(zone, spec["metric"], spec["start"], spec["end"]),
                 "residents": residents.period_coverage(zone, spec["start"], spec["end"]),
                 "actions": adaptation.utilization(zone, spec["metric"], spec["start"], spec["end"], readings, spec["minimum_coverage"])}
                for zone in spec["zones"]]}

    def history(self, kind, identity):
        with self.ledger.transaction():
            return self.ledger.history(kind, identity)

    def plan(self, spec, *, seq=None):
        from .analysis.portfolio import Portfolio
        with self.ledger.transaction():
            watermark = self.ledger.watermark() if seq is None else integer(seq, "seq")
            result = Portfolio(self.ledger.snapshot(watermark)).plan(spec)
            return {"watermark": watermark, **result}

    def reconcile(self, spec, before_seq, after_seq):
        from .analysis.reconciliation import Reconciliation
        with self.ledger.transaction():
            return Reconciliation(self.ledger).compare(spec, before_seq, after_seq)

    def ingest_csv(self, batch_id, source, files):
        from .formats import bundle_from_csv
        return self.ingest(bundle_from_csv(batch_id, source, files))

    def export_csv(self, directory, *, seq=None):
        from .formats import snapshot_csv
        with self.ledger.transaction():
            watermark = self.ledger.watermark() if seq is None else integer(seq, "seq")
            return {"watermark": watermark, "files": snapshot_csv(self.ledger.snapshot(watermark), directory)}

    def equity(self, spec, *, seq=None):
        from .analysis.equity import Equity
        with self.ledger.transaction():
            watermark = self.ledger.watermark() if seq is None else integer(seq, "seq")
            return {"watermark": watermark, **Equity(self.ledger.snapshot(watermark)).analyze(spec)}

    def audit(self):
        from .audit import Audit
        with self.ledger.transaction():
            return Audit(self.ledger).verify(self.validate)

    def backup(self, destination):
        from .audit import Audit
        return Audit(self.ledger).backup(destination)

    def priorities(self, spec, *, seq=None):
        with self.ledger.transaction():
            watermark = self.ledger.watermark() if seq is None else integer(seq, "seq")
            return {"watermark": watermark, **Exposure(self.ledger.snapshot(watermark)).priorities(spec)}
