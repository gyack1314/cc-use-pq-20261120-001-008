from decimal import Decimal

from ..adaptation import Adaptation
from ..contracts import days, decimal_text, query_spec, ratio, require
from ..observatory.calibration import Calibrations
from ..observatory.readings import Readings
from ..residents import Residents


class Exposure:
    def __init__(self, snapshot, calibration_id=None, conversions=()):
        self.snapshot = snapshot
        self.requested_calibration = calibration_id
        self.allowed_conversions = tuple(conversions)
        self.readings = Readings(snapshot, calibration_id, conversions)
        self.residents = Residents(snapshot)
        self.adaptation = Adaptation(snapshot, calibration_id)
        self.calibrations = Calibrations(snapshot)

    def target_calibration(self, metric):
        return self.calibrations.selected_id(self.requested_calibration, metric)

    def calibration_registry(self, daily):
        calibration_ids = {self.target_calibration(row["weather"]["metric"]) for row in daily}
        calibration_ids.update(item["calibration"] for row in daily for item in row["weather"]["evidence"])
        conversion_ids = {item["conversion"]["id"]
                          for row in daily for item in row["weather"]["evidence"] if "conversion" in item}
        return self.calibrations.registry(calibration_ids, conversion_ids)

    def daily(self, zone, metric, when, minimum_coverage):
        weather = self.readings.zone_day(zone, metric, when, minimum_coverage)
        population = self.residents.day(zone, when)
        usable = weather["usable"] and population["complete"]
        result = {"zone": zone, "day": when, "usable": usable, "weather": weather,
                  "population": population, "hazardous": weather["hazardous"],
                  "protected": None, "exposed": None, "weighted_burden": None,
                  "groups": [], "actions": [], "reason": []}
        if not weather["usable"]:
            result["reason"].append("observation_gap")
        if not population["complete"]:
            result["reason"].append("population_gap")
        if not usable:
            return result
        allocation = self.adaptation.allocate(zone, metric, when, hazardous=weather["hazardous"])
        burden = self.residents.burden(zone, when, weather["severity"], allocation["protected"])
        if not weather["hazardous"]:
            burden["exposed"] = 0
            for group in burden["groups"]:
                group["exposed"] = 0
        result.update(protected=sum(allocation["protected"].values()), exposed=burden["exposed"],
                      weighted_burden=burden["weighted_burden"], groups=burden["groups"], actions=allocation["actions"])
        return result

    def analyze(self, spec):
        spec = query_spec(spec)
        self.readings.catalog.metric(spec["metric"])
        target = self.calibrations.selected_id(spec["calibration"], spec["metric"])
        descriptor = self.calibrations.descriptor(target)
        require(descriptor["metric"] == spec["metric"], "calibration", "查询口径不属于该指标",
                calibration=target, metric=spec["metric"])
        for conversion_id in spec["conversions"]:
            require(conversion_id in self.calibrations.conversions, "conversion",
                    "允许的转换不存在", conversion=conversion_id)
        self.requested_calibration = spec["calibration"]
        self.allowed_conversions = tuple(spec["conversions"])
        self.readings = Readings(self.snapshot, spec["calibration"], spec["conversions"])
        self.adaptation = Adaptation(self.snapshot, spec["calibration"])
        require(set(spec["zones"]).issubset(self.snapshot["zone"]), "zone", "查询含未知行政区")
        daily = [self.daily(zone, spec["metric"], when, spec["minimum_coverage"])
                 for zone in spec["zones"] for when in days(spec["start"], spec["end"])]
        strata = [self.summarize(zone, [row for row in daily if row["zone"] == zone]) for zone in spec["zones"]]
        qualified = all(row["complete"] for row in strata)
        all_known = self.summarize("all", daily)
        return {"query": spec, "status": "qualified" if qualified else "incomplete", "strata": strata,
                "total": all_known, "daily": daily, "episodes": self.episodes(daily),
                "calibration_registry": self.calibration_registry(daily)}

    @staticmethod
    def summarize(zone, daily):
        valid = [row for row in daily if row["usable"]]
        hazard = [row for row in valid if row["hazardous"]]
        population_days = sum(row["population"]["population"] for row in valid)
        hazard_population = sum(row["population"]["population"] for row in hazard)
        protected = sum(row["protected"] for row in hazard)
        exposed = sum(row["exposed"] for row in hazard)
        burden = sum((Decimal(row["weighted_burden"]) for row in valid), Decimal(0))
        complete = len(valid) == len(daily)
        return {"zone": zone, "complete": complete, "expected_zone_days": len(daily),
                "usable_zone_days": len(valid), "unknown_zone_days": len(daily) - len(valid),
                "known_population_days": population_days, "known_hazard_population_days": hazard_population,
                "known_protected_person_days": protected, "known_exposed_person_days": exposed,
                "known_weighted_burden": decimal_text(burden),
                "hazard_fraction": ratio(hazard_population, population_days) if complete else None,
                "protection_fraction": ratio(protected, hazard_population) if complete else None,
                "coverage_fraction": ratio(len(valid), len(daily)),
                "groups": Exposure.group_summary(valid, complete)}

    @staticmethod
    def group_summary(daily, complete):
        groups = {}
        for row in daily:
            for group in row["groups"]:
                target = groups.setdefault(group["group"], {"population_days": 0, "hazard_population_days": 0,
                                                            "protected_person_days": 0, "weighted_burden": Decimal(0)})
                target["population_days"] += group["count"]
                if row["hazardous"]:
                    target["hazard_population_days"] += group["count"]
                    target["protected_person_days"] += group["protected"]
                target["weighted_burden"] += Decimal(group["weighted_burden"])
        return [{"group": key, **value, "weighted_burden": decimal_text(value["weighted_burden"]),
                 "protection_fraction": ratio(value["protected_person_days"], value["hazard_population_days"]) if complete else None}
                for key, value in sorted(groups.items())]

    @staticmethod
    def episodes(daily):
        result = []
        for zone in sorted({row["zone"] for row in daily}):
            current = None
            for row in sorted((r for r in daily if r["zone"] == zone), key=lambda r: r["day"]):
                if not row["usable"] or not row["hazardous"]:
                    if current:
                        current["right_boundary"] = "unknown" if not row["usable"] else "observed"
                        result.append(current)
                        current = None
                    continue
                if current is None:
                    current = {"zone": zone, "start": row["day"], "end": row["day"], "days": 0,
                               "exposed_person_days": 0, "right_boundary": "window"}
                current["end"] = row["day"]
                current["days"] += 1
                current["exposed_person_days"] += row["exposed"]
            if current:
                result.append(current)
        return result

    def priorities(self, spec):
        analysis = self.analyze(spec)
        rows = []
        deferred = []
        for stratum in analysis["strata"]:
            zone = stratum["zone"]
            if not stratum["complete"]:
                deferred.append({"zone": zone, "reason": "资料不完整，不能将未知负担排为零风险",
                                 "unknown_zone_days": stratum["unknown_zone_days"]})
                continue
            population_days = stratum["known_population_days"]
            burden = Decimal(stratum["known_weighted_burden"])
            if not population_days:
                deferred.append({"zone": zone, "reason": "没有有效人口分母", "unknown_zone_days": 0})
                continue
            rate = burden / population_days
            uncovered = stratum["known_exposed_person_days"]
            hazard = stratum["known_hazard_population_days"]
            rows.append({"zone": zone, "weighted_burden": decimal_text(burden),
                         "burden_per_person_day": decimal_text(rate), "uncovered_person_days": uncovered,
                         "uncovered_fraction": ratio(uncovered, hazard),
                         "hazard_population_days": hazard, "groups": stratum["groups"]})
        rows.sort(key=lambda row: (-Decimal(row["burden_per_person_day"]),
                                   -Decimal(row["weighted_burden"]), row["zone"]))
        rank = 0
        previous = None
        for index, row in enumerate(rows, 1):
            key = (row["burden_per_person_day"], row["weighted_burden"])
            if key != previous:
                rank = index
            row["rank"] = rank
            previous = key
        return {"query": analysis["query"], "priorities": rows, "deferred": deferred,
                "interpretation": "按单位人口日的未保护加权负担比较，不能替代设施可行性或因果评估"}
