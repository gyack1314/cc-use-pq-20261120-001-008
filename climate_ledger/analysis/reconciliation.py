from decimal import Decimal

from ..contracts import KINDS, decimal_text, fingerprint, query_spec, require
from .exposure import Exposure


class Reconciliation:
    def __init__(self, ledger):
        self.ledger = ledger

    def compare(self, spec, before_seq, after_seq):
        require(before_seq <= after_seq, "snapshot", "核对快照顺序错误")
        spec = query_spec(spec)
        before = self.ledger.snapshot(before_seq)
        after = self.ledger.snapshot(after_seq)
        first = Exposure(before).analyze(spec)
        second = Exposure(after).analyze(spec)
        changed = []
        for kind in KINDS:
            identities = sorted(set(before[kind]) | set(after[kind]))
            for identity in identities:
                old, new = before[kind].get(identity), after[kind].get(identity)
                if old == new:
                    continue
                history = self.ledger.history(kind, identity)
                changed.append({"kind": kind, "identity": identity,
                                "before_revision": old["revision"] if old else None,
                                "after_revision": new["revision"] if new else None,
                                "editions": [{"seq": row["seq"], "revision": row["revision"],
                                              "batch": row["batch"], "source": row["source"]}
                                             for row in history if before_seq < row["seq"] <= after_seq]})
        daily_before = {(r["zone"], r["day"]): r for r in first["daily"]}
        daily_after = {(r["zone"], r["day"]): r for r in second["daily"]}
        impacts = []
        for key in sorted(daily_before):
            old, new = daily_before[key], daily_after[key]
            difference = self.daily_difference(old, new)
            if difference:
                impacts.append({"zone": key[0], "day": key[1], "changes": difference,
                                "before_evidence": self.evidence(old), "after_evidence": self.evidence(new)})
        return {"query": spec, "before_seq": before_seq, "after_seq": after_seq,
                "comparable": first["status"] == second["status"] == "qualified",
                "before_status": first["status"], "after_status": second["status"],
                "total_changes": self.total_difference(first["total"], second["total"]),
                "fact_changes": changed, "affected_zone_days": impacts,
                "before_digest": fingerprint(first), "after_digest": fingerprint(second),
                "interpretation": "这是资料与口径变化的结果核对，不将其解释为真实时间趋势"}

    @staticmethod
    def daily_difference(old, new):
        result = {}
        for field in ("usable", "hazardous", "protected", "exposed", "weighted_burden", "reason"):
            if old[field] != new[field]:
                result[field] = {"before": old[field], "after": new[field]}
        for field in ("value", "coverage", "metric_revision"):
            if old["weather"][field] != new["weather"][field]:
                result["weather." + field] = {"before": old["weather"][field], "after": new["weather"][field]}
        if old["population"] != new["population"]:
            result["population"] = {"before": old["population"], "after": new["population"]}
        if old["actions"] != new["actions"]:
            result["actions"] = {"before": old["actions"], "after": new["actions"]}
        return result

    @staticmethod
    def total_difference(old, new):
        result = {}
        for field in ("known_population_days", "known_hazard_population_days", "known_protected_person_days",
                      "known_exposed_person_days", "known_weighted_burden", "unknown_zone_days"):
            result[field] = {"before": old[field], "after": new[field],
                             "known_delta": decimal_text(Decimal(new[field]) - Decimal(old[field]))}
        for field in ("hazard_fraction", "protection_fraction"):
            result[field] = {"before": old[field], "after": new[field],
                             "delta": decimal_text(Decimal(new[field]) - Decimal(old[field]))
                             if old[field] is not None and new[field] is not None else None}
        return result

    @staticmethod
    def evidence(row):
        return {"readings": row["weather"]["evidence"], "metric_revision": row["weather"]["metric_revision"],
                "population": [{"id": group["population"], "revision": group["revision"]}
                               for group in row["population"]["groups"]],
                "actions": [{"id": action["action"], "revision": action["revision"]} for action in row["actions"]]}
