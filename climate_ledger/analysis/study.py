from decimal import Decimal

from ..adaptation import Adaptation
from ..contracts import (days, decimal_text, distinct_strings, identifier, number, period,
                         query_spec, require, shape)
from .exposure import Exposure


def study_spec(value):
    shape(value, ("metric", "treated", "controls", "before", "after", "tolerance", "minimum_pairs"),
          ("minimum_coverage", "calibration", "conversions"))
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
    conversions = distinct_strings(value.get("conversions", []), "conversions", nonempty=False)
    basic = query_spec({"metric": value["metric"], "start": before[0], "end": after[1],
                        "zones": treated + controls, "minimum_coverage": value.get("minimum_coverage", "0.8"),
                        "calibration": value.get("calibration"), "conversions": conversions})
    return {"metric": basic["metric"], "treated": treated, "controls": controls,
            "before": before, "after": after, "tolerance": str(number(value["tolerance"], "tolerance", 0, 100000)),
            "minimum_pairs": value["minimum_pairs"], "minimum_coverage": basic["minimum_coverage"],
            "calibration": basic["calibration"], "conversions": basic["conversions"]}


class Study:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.adaptation = Adaptation(snapshot)

    def compare(self, value):
        spec = study_spec(value)
        exposure = Exposure(self.snapshot, spec["calibration"], spec["conversions"])
        target = exposure.target_calibration(spec["metric"])
        descriptor = exposure.calibrations.descriptor(target)
        require(descriptor["metric"] == spec["metric"], "calibration", "研究口径不属于该指标",
                calibration=target, metric=spec["metric"])
        for conversion_id in spec["conversions"]:
            row = exposure.calibrations.conversions[conversion_id]
            require(row["source"] == target or row["target"] == target, "conversion",
                    "允许的转换与研究共同口径无关", conversion=conversion_id)
        require(set(spec["treated"] + spec["controls"]).issubset(self.snapshot["zone"]), "zone", "研究行政区不存在")
        pools = {}
        excluded = []
        for label in ("before", "after"):
            rows = []
            for zone in spec["treated"] + spec["controls"]:
                for when in days(*spec[label]):
                    row = exposure.daily(zone, spec["metric"], when, spec["minimum_coverage"])
                    reason, detail = self.eligibility(row, zone in spec["treated"], label, spec["metric"], exposure)
                    if reason:
                        entry = {"phase": label, "zone": zone, "day": when, "reason": reason}
                        if detail:
                            entry["detail"] = detail
                        excluded.append(entry)
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
                "interpretation": "配对暴露差的前后变化，不等同因果证明；仅在共同口径支持的范围内解释",
                "phases": phase_results, "excluded": excluded,
                "calibration_registry": exposure.calibration_registry(
                    [row for phase in pools.values() for row in phase])}

    def eligibility(self, row, treated, phase, metric, exposure):
        if not row["usable"]:
            calibration_issues = [issue for issue in row["weather"]["issues"]
                                  if issue["reason"] in ("calibration_mismatch", "not_allowed", "outside_period",
                                                         "out_of_range", "path_conflict", "converted_out_of_range")]
            if calibration_issues:
                issue = calibration_issues[0]
                return "incomparable_calibration", issue
            return "+".join(row["reason"]), None
        if row["population"]["population"] == 0:
            return "zero_population", None
        active = exposure.adaptation.is_treated(row["zone"], metric, row["day"])
        if not treated and active:
            return "control_contaminated", None
        if treated and phase == "before" and active:
            return "treated_before_activation", None
        if treated and phase == "after" and not active:
            return "action_not_active", None
        return None, None

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
