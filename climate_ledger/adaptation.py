from decimal import Decimal

from .contracts import (contains, decimal_text, distinct_strings, identifier, integer,
                        overlaps, period, ratio, require, shape, text)
from .residents import Residents


def normalize_action(record):
    shape(record, ("id", "revision", "name", "zone", "metric", "start", "end", "status",
                   "capacity", "groups", "priority", "facility"))
    start, end = period(record["start"], record["end"])
    require(record["status"] in ("planned", "active", "suspended", "closed"), "status", "行动状态无效")
    return {"id": identifier(record["id"]), "revision": integer(record["revision"], "revision", 1),
            "name": text(record["name"], "name"), "zone": identifier(record["zone"], "zone"),
            "metric": identifier(record["metric"], "metric"), "start": start, "end": end,
            "status": record["status"], "capacity": integer(record["capacity"], "capacity"),
            "groups": distinct_strings(record["groups"], "groups"),
            "priority": integer(record["priority"], "priority", 0, 100),
            "facility": identifier(record["facility"], "facility")}


class Adaptation:
    def __init__(self, snapshot):
        self.rows = snapshot["action"]
        self.snapshot = snapshot
        self.residents = Residents(snapshot)

    def validate(self):
        active = []
        for row in self.rows.values():
            require(row["zone"] in self.snapshot["zone"], "reference", "行动行政区不存在")
            require(row["metric"] in self.snapshot["metric"], "reference", "行动指标不存在")
            known = self.residents.registered_groups(row["zone"])
            require(set(row["groups"]).issubset(known), "reference", "行动人群尚未登记")
            if row["status"] != "active":
                continue
            require(not any(other["facility"] == row["facility"] and overlaps(row, other) for other in active),
                    "facility_conflict", "同一设施不能同时承诺给多项行动", facility=row["facility"])
            active.append(row)

    def active(self, zone, metric, when):
        selected = [row for row in self.rows.values() if row["zone"] == zone and row["metric"] == metric
                    and row["status"] == "active" and contains(row, when)]
        return sorted(selected, key=lambda row: (-row["priority"], row["id"]))

    def is_treated(self, zone, metric, when):
        return any(row["capacity"] > 0 for row in self.active(zone, metric, when))

    def allocate(self, zone, metric, when, *, hazardous):
        population = self.residents.day(zone, when)
        groups = {row["group"]: row for row in population["groups"]}
        protected = {group: 0 for group in groups}
        evidence = []
        unused = 0
        if not hazardous or not population["complete"]:
            return {"complete": population["complete"], "protected": protected, "actions": [], "unused": 0}
        for action in self.active(zone, metric, when):
            remaining = action["capacity"]
            assignments = []
            eligible = sorted((groups[group] for group in action["groups"] if group in groups),
                              key=lambda row: (-Decimal(row["vulnerability"]), row["group"]))
            for group in eligible:
                count = min(remaining, group["count"] - protected[group["group"]])
                if count:
                    protected[group["group"]] += count
                    remaining -= count
                    assignments.append({"group": group["group"], "count": count})
            unused += remaining
            evidence.append({"action": action["id"], "revision": action["revision"],
                             "facility": action["facility"], "capacity": action["capacity"],
                             "used": action["capacity"] - remaining, "unused": remaining,
                             "assignments": assignments})
        return {"complete": True, "protected": protected, "actions": evidence, "unused": unused}

    def utilization(self, zone, metric, start, end, readings, coverage):
        from .contracts import days
        actions = {}
        for when in days(start, end):
            weather = readings.zone_day(zone, metric, when, coverage)
            if not weather["usable"]:
                continue
            allocation = self.allocate(zone, metric, when, hazardous=weather["hazardous"])
            for item in allocation["actions"]:
                totals = actions.setdefault(item["action"], {"capacity_days": 0, "served_days": 0, "hazard_days": 0})
                totals["capacity_days"] += item["capacity"]
                totals["served_days"] += item["used"]
                totals["hazard_days"] += 1
        return [{"action": key, **value, "utilization": ratio(value["served_days"], value["capacity_days"])}
                for key, value in sorted(actions.items())]
