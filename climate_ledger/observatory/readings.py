from decimal import Decimal

from ..contracts import (contains, day, decimal_text, identifier, integer, number,
                         require, shape)
from .calibration import Calibrations, apply_formula
from .catalog import Catalog, UNITS


def normalize_reading(record):
    shape(record, ("station", "metric", "day", "revision", "value", "unit", "quality"),
          ("calibration",))
    require(record["quality"] in ("valid", "suspect", "missing"), "quality", "观测质量标记无效")
    if record["quality"] == "missing":
        require(record["value"] is None, "missing", "缺测值必须为null")
        value = None
    else:
        value = str(number(record["value"], "value"))
    result = {"station": identifier(record["station"], "station"), "metric": identifier(record["metric"], "metric"),
              "day": day(record["day"]), "revision": integer(record["revision"], "revision", 1),
              "value": value, "unit": identifier(record["unit"], "unit"), "quality": record["quality"]}
    if record.get("calibration"):
        result["calibration"] = identifier(record["calibration"], "calibration")
    return result


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
    def __init__(self, snapshot, calibration_id=None, conversions=()):
        self.catalog = Catalog(snapshot)
        self.calibrations = Calibrations(snapshot)
        self.rows = snapshot["reading"]
        self.requested_calibration = calibration_id
        self.allowed_conversions = tuple(conversions)

    def target_calibration(self, metric_id):
        return self.calibrations.selected_id(self.requested_calibration, metric_id)

    def validate(self):
        for row in self.rows.values():
            station = self.catalog.station(row["station"])
            metric = self.catalog.metric(row["metric"])
            require(row["metric"] in station["metrics"] and contains(station, row["day"]),
                    "station_scope", "观测不在站点生效范围内")
            binding_id = self.calibrations.reading_binding(row)
            binding = self.calibrations.descriptor(binding_id)
            require(binding["metric"] == row["metric"], "reference", "观测口径与指标不一致",
                    reading=":".join((row["station"], row["metric"], row["day"])))
            if not binding["legacy"]:
                require(binding["effective_start"] <= row["day"] <= binding["effective_end"],
                        "calibration_scope",
                        "观测日期不在口径生效范围内",
                        calibration=binding_id, day=row["day"])
            value = convert(row["value"], row["unit"], metric["dimension"])
            if value is not None:
                require(Decimal(metric["minimum"]) <= value <= Decimal(metric["maximum"]),
                        "reading_range", "换算后的观测值超出合理范围")
                require(Decimal(binding["minimum"]) <= value <= Decimal(binding["maximum"]),
                        "calibration_range", "观测值超出口径合理值域",
                        calibration=binding_id, day=row["day"])

    def resolve_reading(self, row, target_id, native_value):
        """把已换算到指标标准单位的观测翻译到目标口径，返回 (值, 依据, 不可用原因)。"""
        binding_id = self.calibrations.reading_binding(row)
        if binding_id == target_id:
            return None
        if not self.allowed_conversions:
            return "calibration_mismatch"
        conversion, reason = self.calibrations.find_conversion(
            binding_id, target_id, row["day"], native_value, self.allowed_conversions)
        if conversion is None:
            return reason
        translated = apply_formula(conversion["formula"], native_value)
        target = self.calibrations.descriptor(target_id)
        if not (Decimal(target["minimum"]) <= translated <= Decimal(target["maximum"])):
            return "converted_out_of_range"
        return {"id": conversion["id"], "revision": conversion["revision"],
                "source": conversion["source"], "target": conversion["target"],
                "reversible": conversion["reversible"], "value": decimal_text(translated)}

    def zone_day(self, zone, metric_id, when, minimum_coverage):
        metric = self.catalog.metric(metric_id)
        target_id = self.target_calibration(metric_id)
        target = self.calibrations.descriptor(target_id)
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
            native = convert(row["value"], row["unit"], metric["dimension"])
            binding_id = self.calibrations.reading_binding(row)
            binding = self.calibrations.descriptor(binding_id)
            translation = self.resolve_reading(row, target_id, native)
            if isinstance(translation, dict):
                value = Decimal(translation["value"])
                used_conversion = translation
            elif translation is None:
                value = native
                used_conversion = None
            else:
                issues.append({"station": station["id"], "reason": translation,
                               "binding": binding_id, "calibration": target_id})
                continue
            weighted += value * weight
            valid_weight += weight
            entry = {"reading": key, "revision": row["revision"], "weight": str(weight),
                     "station_revision": station["revision"],
                     "calibration": binding_id,
                     "calibration_revision": binding["calibration_revision"],
                     "calibration_legacy": binding["legacy"],
                     "unit": target["unit"]}
            if used_conversion:
                entry["conversion"] = used_conversion
            evidence.append(entry)
        usable = valid_weight > 0 and valid_weight >= Decimal(str(minimum_coverage))
        value = weighted / valid_weight if usable else None
        return {"zone": zone, "day": when, "metric": metric_id, "calibration": target_id,
                "value": decimal_text(value), "coverage": decimal_text(valid_weight),
                "expected_weight": decimal_text(expected_weight), "usable": usable,
                "hazardous": self.calibrations.hazardous(target_id, value) if usable else None,
                "severity": decimal_text(self.calibrations.severity(target_id, value)) if usable else None,
                "evidence": evidence, "issues": issues,
                "metric_revision": metric["revision"],
                "calibration_revision": target["calibration_revision"],
                "calibration_legacy": target["legacy"],
                "window": target["window"]}

    def diagnostics(self, zone, metric, start, end):
        from ..contracts import days
        rows = [self.zone_day(zone, metric, when, "1") for when in days(start, end)]
        return {"zone": zone, "metric": metric,
                "calibration": self.target_calibration(metric),
                "days": len(rows),
                "complete": sum(row["usable"] for row in rows),
                "problems": [{"day": row["day"], "coverage": row["coverage"], "issues": row["issues"]}
                             for row in rows if not row["usable"]]}
