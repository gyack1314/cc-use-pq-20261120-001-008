from decimal import Decimal

from ..adaptation import Adaptation
from ..contracts import days, decimal_text, query_spec, ratio, require
from ..observatory.readings import Readings
from ..residents import Residents


class Equity:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.readings = Readings(snapshot)
        self.residents = Residents(snapshot)
        self.adaptation = Adaptation(snapshot)

    def analyze(self, value):
        spec = query_spec(value)
        definition = self.readings.catalog.metric(spec["metric"])
        require(set(spec["zones"]).issubset(self.snapshot["zone"]), "zone", "公平性分析行政区不存在")
        threshold = Decimal(definition["threshold"])
        maximum_severity = (Decimal(definition["maximum"]) - threshold if definition["direction"] == "high"
                            else threshold - Decimal(definition["minimum"]))
        groups = {}
        population_unknown = []
        gaps = []
        for zone in spec["zones"]:
            for when in days(spec["start"], spec["end"]):
                population = self.residents.day(zone, when)
                if not population["complete"]:
                    population_unknown.append({"zone": zone, "day": when, "groups": population["missing_groups"]})
                    continue
                weather = self.readings.zone_day(zone, spec["metric"], when, spec["minimum_coverage"])
                protection = self.adaptation.allocate(zone, spec["metric"], when, hazardous=True)
                unknown_upper = Decimal(0)
                for group in population["groups"]:
                    key = (zone, group["group"])
                    totals = groups.setdefault(key, {"population_days": 0, "known_hazard_days": 0,
                                                    "known_protected_days": 0, "unknown_population_days": 0,
                                                    "lower_burden": Decimal(0), "upper_burden": Decimal(0)})
                    totals["population_days"] += group["count"]
                    exposed = group["count"] - protection["protected"].get(group["group"], 0)
                    multiplier = Decimal(exposed) * Decimal(group["vulnerability"])
                    if weather["usable"]:
                        burden = multiplier * Decimal(weather["severity"])
                        totals["lower_burden"] += burden
                        totals["upper_burden"] += burden
                        if weather["hazardous"]:
                            totals["known_hazard_days"] += group["count"]
                            totals["known_protected_days"] += protection["protected"].get(group["group"], 0)
                    else:
                        upper = multiplier * maximum_severity
                        totals["upper_burden"] += upper
                        totals["unknown_population_days"] += group["count"]
                        unknown_upper += upper
                if not weather["usable"]:
                    gaps.append({"zone": zone, "day": when, "coverage": weather["coverage"],
                                 "maximum_unresolved_burden": decimal_text(unknown_upper)})
        rows = []
        for (zone, group), totals in sorted(groups.items()):
            denominator = totals["population_days"]
            lower_rate = totals["lower_burden"] / denominator if denominator else None
            upper_rate = totals["upper_burden"] / denominator if denominator else None
            rows.append({"zone": zone, "group": group, **totals,
                         "lower_burden": decimal_text(totals["lower_burden"]),
                         "upper_burden": decimal_text(totals["upper_burden"]),
                         "lower_rate": decimal_text(lower_rate), "upper_rate": decimal_text(upper_rate),
                         "known_protection_fraction": ratio(totals["known_protected_days"], totals["known_hazard_days"]),
                         "complete": not population_unknown and not totals["unknown_population_days"]})
        contrasts = []
        for index, left in enumerate(rows):
            for right in rows[index + 1:]:
                relation = "undetermined"
                if not population_unknown and left["lower_rate"] is not None and right["lower_rate"] is not None:
                    if Decimal(left["lower_rate"]) > Decimal(right["upper_rate"]):
                        relation = "left_higher"
                    elif Decimal(right["lower_rate"]) > Decimal(left["upper_rate"]):
                        relation = "right_higher"
                    elif left["complete"] and right["complete"]:
                        relation = "equal"
                contrasts.append({"left": [left["zone"], left["group"]], "right": [right["zone"], right["group"]],
                                  "relation": relation})
        return {"query": spec, "status": "population_unknown" if population_unknown else "bounded",
                "groups": rows, "contrasts": contrasts, "population_gaps": population_unknown,
                "observation_priorities": sorted(gaps, key=lambda item: (-Decimal(item["maximum_unresolved_burden"]), item["zone"], item["day"])),
                "interpretation": "区间来自指标合理值域，不是置信区间；资料不足时不强排风险名次"}
