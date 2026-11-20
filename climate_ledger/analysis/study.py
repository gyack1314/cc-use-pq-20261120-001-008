from decimal import Decimal

from ..adaptation import Adaptation
from ..contracts import (days, decimal_text, distinct_strings, number, period,
                         query_spec, require, shape)
from .exposure import Exposure


def study_spec(value):
    shape(value, ("metric", "treated", "controls", "before", "after", "tolerance", "minimum_pairs"),
          ("minimum_coverage",))
    require(isinstance(value["before"], list) and len(value["before"]) == 2, "period", "before需要起止日期")
    require(isinstance(value["after"], list) and len(value["after"]) == 2, "period", "after需要起止日期")
    before = list(period(*value["before"]))
    after = list(period(*value["after"]))
    require(before[1] < after[0], "period", "比较前期必须早于后期")
    treated = distinct_strings(value["treated"], "treated")
    controls = distinct_strings(value["controls"], "controls")
    require(not set(treated) & set(controls), "cohort", "行动区与对照区不能重叠")
    require(type(value["minimum_pairs"]) is int and 1 <= value["minimum_pairs"] <= 10000,
            "pairs", "最低配对数无效")
    basic = query_spec({"metric": value["metric"], "start": before[0], "end": after[1],
                        "zones": treated + controls, "minimum_coverage": value.get("minimum_coverage", "0.8")})
    return {"metric": basic["metric"], "treated": treated, "controls": controls,
            "before": before, "after": after, "tolerance": str(number(value["tolerance"], "tolerance", 0, 100000)),
            "minimum_pairs": value["minimum_pairs"], "minimum_coverage": basic["minimum_coverage"]}


class Study:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.exposure = Exposure(snapshot)
        self.adaptation = Adaptation(snapshot)

    def compare(self, value):
        spec = study_spec(value)
        self.exposure.readings.catalog.metric(spec["metric"])
        require(set(spec["treated"] + spec["controls"]).issubset(self.snapshot["zone"]), "zone", "研究行政区不存在")
        pools = {}
        excluded = []
        for label in ("before", "after"):
            rows = []
            for zone in spec["treated"] + spec["controls"]:
                for when in days(*spec[label]):
                    row = self.exposure.daily(zone, spec["metric"], when, spec["minimum_coverage"])
                    reason = self.eligibility(row, zone in spec["treated"], label, spec["metric"])
                    if reason:
                        excluded.append({"phase": label, "zone": zone, "day": when, "reason": reason})
                    else:
                        rows.append(row)
            pools[label] = rows
        phase_results = {label: self.match(pools[label], spec) for label in pools}
        qualified = all(len(result["pairs"]) >= spec["minimum_pairs"] for result in phase_results.values())
        estimate = None
        if qualified:
            estimate = decimal_text(Decimal(phase_results["after"]["mean_difference"])
                                    - Decimal(phase_results["before"]["mean_difference"]))
        return {"study": spec, "status": "qualified" if qualified else "insufficient", "estimate": estimate,
                "interpretation": "配对暴露差的前后变化，不等同因果证明", "phases": phase_results, "excluded": excluded}

    def eligibility(self, row, treated, phase, metric):
        if not row["usable"]:
            return "+".join(row["reason"])
        if row["population"]["population"] == 0:
            return "zero_population"
        active = self.adaptation.is_treated(row["zone"], metric, row["day"])
        if not treated and active:
            return "control_contaminated"
        if treated and phase == "before" and active:
            return "treated_before_activation"
        if treated and phase == "after" and not active:
            return "action_not_active"
        return None

    @staticmethod
    def normalized_burden(row):
        count = row["population"]["population"]
        return Decimal(row["weighted_burden"]) / count

    def match(self, rows, spec):
        treated = sorted((r for r in rows if r["zone"] in spec["treated"]), key=lambda r: (r["day"], r["zone"]))
        available = {(r["zone"], r["day"]): r for r in rows if r["zone"] in spec["controls"]}
        pairs = []
        unmatched = []
        for row in treated:
            candidates = [other for other in available.values() if other["day"] == row["day"]
                          and abs(Decimal(row["weather"]["value"]) - Decimal(other["weather"]["value"])) <= Decimal(spec["tolerance"])]
            if not candidates:
                unmatched.append({"zone": row["zone"], "day": row["day"], "reason": "no_weather_match"})
                continue
            control = min(candidates, key=lambda other: (abs(Decimal(row["weather"]["value"])
                                                           - Decimal(other["weather"]["value"])), other["zone"]))
            del available[(control["zone"], control["day"])]
            difference = self.normalized_burden(row) - self.normalized_burden(control)
            pairs.append({"treated": row["zone"], "control": control["zone"], "day": row["day"],
                          "difference": decimal_text(difference), "treated_actions": row["actions"],
                          "treated_weather": row["weather"], "control_weather": control["weather"],
                          "treated_population": row["population"], "control_population": control["population"]})
        mean = sum((Decimal(p["difference"]) for p in pairs), Decimal(0)) / len(pairs) if pairs else None
        return {"pairs": pairs, "unmatched": unmatched, "mean_difference": decimal_text(mean)}
