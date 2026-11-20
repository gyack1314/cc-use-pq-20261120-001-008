from decimal import Decimal

from ..contracts import (contains, day, decimal_text, identifier, integer, number,
                         require, shape)
from .catalog import Catalog, UNITS


def normalize_reading(record):
    shape(record, ("station", "metric", "day", "revision", "value", "unit", "quality"))
    require(record["quality"] in ("valid", "suspect", "missing"), "quality", "观测质量标记无效")
    if record["quality"] == "missing":
        require(record["value"] is None, "missing", "缺测值必须为null")
        value = None
    else:
        value = str(number(record["value"], "value"))
    return {"station": identifier(record["station"], "station"), "metric": identifier(record["metric"], "metric"),
            "day": day(record["day"]), "revision": integer(record["revision"], "revision", 1),
            "value": value, "unit": identifier(record["unit"], "unit"), "quality": record["quality"]}


def convert(value, unit, dimension):
    require(unit in UNITS[dimension], "unit", "观测单位与指标量纲不兼容")
    if value is None:
        return None
    value = Decimal(value)
    if unit == "F":
        return (value - 32) * Decimal(5) / 9
    if unit in ("m3", "MWh"):
        return value * 1000
    return value


class Readings:
    def __init__(self, snapshot):
        self.catalog = Catalog(snapshot)
        self.rows = snapshot["reading"]

    def validate(self):
        for row in self.rows.values():
            station = self.catalog.station(row["station"])
            metric = self.catalog.metric(row["metric"])
            require(row["metric"] in station["metrics"] and contains(station, row["day"]),
                    "station_scope", "观测不在站点生效范围内")
            value = convert(row["value"], row["unit"], metric["dimension"])
            if value is not None:
                require(Decimal(metric["minimum"]) <= value <= Decimal(metric["maximum"]),
                        "reading_range", "换算后的观测值超出合理范围")

    def zone_day(self, zone, metric_id, when, minimum_coverage):
        metric = self.catalog.metric(metric_id)
        stations = self.catalog.active_stations(zone, metric_id, when)
        valid_weight = Decimal(0)
        expected_weight = sum((Decimal(s["weight"]) for s in stations), Decimal(0))
        weighted = Decimal(0)
        evidence = []
        issues = []
        for station in stations:
            key = ":".join((station["id"], metric_id, when))
            row = self.rows.get(key)
            if row is None or row["quality"] != "valid":
                issues.append({"station": station["id"], "reason": "absent" if row is None else row["quality"]})
                continue
            weight = Decimal(station["weight"])
            value = convert(row["value"], row["unit"], metric["dimension"])
            weighted += value * weight
            valid_weight += weight
            evidence.append({"reading": key, "revision": row["revision"], "weight": str(weight),
                             "station_revision": station["revision"]})
        usable = valid_weight > 0 and valid_weight >= Decimal(str(minimum_coverage))
        value = weighted / valid_weight if usable else None
        return {"zone": zone, "day": when, "metric": metric_id,
                "value": decimal_text(value), "coverage": decimal_text(valid_weight),
                "expected_weight": decimal_text(expected_weight), "usable": usable,
                "hazardous": self.catalog.hazardous(metric_id, value) if usable else None,
                "severity": decimal_text(self.catalog.severity(metric_id, value)) if usable else None,
                "evidence": evidence, "issues": issues, "metric_revision": metric["revision"]}

    def diagnostics(self, zone, metric, start, end):
        from ..contracts import days
        rows = [self.zone_day(zone, metric, when, "1") for when in days(start, end)]
        return {"zone": zone, "metric": metric, "days": len(rows),
                "complete": sum(row["usable"] for row in rows),
                "problems": [{"day": row["day"], "coverage": row["coverage"], "issues": row["issues"]}
                             for row in rows if not row["usable"]]}
