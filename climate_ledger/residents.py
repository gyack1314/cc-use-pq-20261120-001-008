from decimal import Decimal

from .contracts import (contains, decimal_text, identifier, integer, number,
                        overlaps, period, require, shape)


def normalize_population(record):
    shape(record, ("id", "revision", "zone", "group", "start", "end", "count", "vulnerability"))
    start, end = period(record["start"], record["end"])
    return {"id": identifier(record["id"]), "revision": integer(record["revision"], "revision", 1),
            "zone": identifier(record["zone"], "zone"), "group": identifier(record["group"], "group"),
            "start": start, "end": end, "count": integer(record["count"], "count"),
            "vulnerability": str(number(record["vulnerability"], "vulnerability", 1, 10))}


class Residents:
    def __init__(self, snapshot):
        self.rows = snapshot["population"]
        self.zones = snapshot["zone"]

    def validate(self):
        grouped = {}
        for row in self.rows.values():
            require(row["zone"] in self.zones, "reference", "人口行政区不存在")
            key = (row["zone"], row["group"])
            others = grouped.setdefault(key, [])
            require(not any(overlaps(row, other) for other in others), "population_overlap",
                    "同区同组人口有效期不能重叠", zone=row["zone"], group=row["group"])
            others.append(row)

    def groups(self, zone, when):
        require(zone in self.zones, "zone", "行政区不存在")
        return {row["group"]: row for row in self.rows.values()
                if row["zone"] == zone and contains(row, when)}

    def registered_groups(self, zone):
        return sorted({row["group"] for row in self.rows.values() if row["zone"] == zone})

    def day(self, zone, when):
        active = self.groups(zone, when)
        known = self.registered_groups(zone)
        missing = sorted(set(known) - set(active))
        complete = bool(known) and not missing
        groups = [{"group": group, "count": row["count"], "vulnerability": row["vulnerability"],
                   "population": row["id"], "revision": row["revision"]}
                  for group, row in sorted(active.items())]
        return {"zone": zone, "day": when, "complete": complete, "missing_groups": missing,
                "population": sum(row["count"] for row in groups), "groups": groups}

    def burden(self, zone, when, severity, protected):
        population = self.day(zone, when)
        require(population["complete"], "population_gap", "人口分母不完整，不能形成暴露估计")
        total = Decimal(0)
        weighted = Decimal(0)
        detail = []
        for row in population["groups"]:
            served = protected.get(row["group"], 0)
            require(0 <= served <= row["count"], "protection", "保护人数不能超过有效人口")
            exposed = row["count"] - served
            burden = Decimal(exposed) * Decimal(severity) * Decimal(row["vulnerability"])
            total += exposed
            weighted += burden
            detail.append({**row, "protected": served, "exposed": exposed, "weighted_burden": decimal_text(burden)})
        return {"exposed": int(total), "weighted_burden": decimal_text(weighted), "groups": detail}

    def period_coverage(self, zone, start, end):
        from .contracts import days
        daily = [self.day(zone, when) for when in days(start, end)]
        return {"zone": zone, "complete_days": sum(row["complete"] for row in daily),
                "total_days": len(daily), "population_days": sum(row["population"] for row in daily if row["complete"]),
                "missing": [{"day": row["day"], "groups": row["missing_groups"]}
                            for row in daily if not row["complete"]]}
