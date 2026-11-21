"""测量口径与口径转换的不可变定义。

口径（calibration）声明稳定的测量概念、版本、统计时间窗口、量纲、标准单位、
合理值域、危险阈值及生效范围。没有显式口径的旧资料投影为确定性的 legacy 口径，
其内容取自对应指标修订，不随当前配置漂移。

转换（conversion）是资料提供方举证的跨口径关系，带适用日期、数值范围与可逆性声明。
时间窗口或统计含义不同的口径默认不兼容；只有显式允许且唯一命中的在范围转换才能使用。
"""
from decimal import Decimal

from ..contracts import (contains, day, distinct_strings, identifier, integer, number,
                         overlaps, period, require, shape, text)


LEGACY_PREFIX = "legacy."
WINDOW_KINDS = ("day", "fixed_clock")
FORMULA_KINDS = ("scale", "offset", "affine")


def normalize_window(record):
    shape(record, ("kind",), ("anchor",))
    require(record["kind"] in WINDOW_KINDS, "window", "统计时间窗口类型无效")
    if record["kind"] == "fixed_clock":
        anchor = record.get("anchor")
        require(isinstance(anchor, str), "window", "固定时刻窗口需要anchor")
        pieces = anchor.split(":")
        require(len(pieces) == 2 and all(piece.isascii() and piece.isdecimal() for piece in pieces),
                "window", "anchor需要HH:MM格式")
        hour, minute = int(pieces[0]), int(pieces[1])
        require(0 <= hour <= 23 and 0 <= minute <= 59 and len(pieces[0]) == 2 and len(pieces[1]) == 2,
                "window", "anchor时刻超出范围")
        return {"kind": "fixed_clock", "anchor": anchor}
    require("anchor" not in record, "window", "日统计窗口不能携带anchor")
    return {"kind": "day"}


def normalize_calibration(record):
    shape(record, ("id", "revision", "name", "metric", "concept", "concept_version", "window",
                   "effective_start", "effective_end", "dimension", "unit",
                   "minimum", "maximum", "threshold", "direction"))
    calibration_id = identifier(record["id"])
    require(not calibration_id.startswith(LEGACY_PREFIX), "identity", "口径编号不能占用legacy前缀")
    effective_start, effective_end = period(record["effective_start"], record["effective_end"])
    window = normalize_window(record["window"])
    minimum = number(record["minimum"], "minimum")
    maximum = number(record["maximum"], "maximum")
    threshold = number(record["threshold"], "threshold")
    require(minimum < maximum and minimum <= threshold <= maximum, "range", "口径阈值与有效值域不一致")
    require(record["direction"] in ("high", "low"), "direction", "异常方向需要high或low")
    return {"id": calibration_id, "revision": integer(record["revision"], "revision", 1),
            "name": text(record["name"], "name"), "metric": identifier(record["metric"], "metric"),
            "concept": identifier(record["concept"], "concept"),
            "concept_version": text(record["concept_version"], "concept_version", maximum=40),
            "window": window, "effective_start": effective_start, "effective_end": effective_end,
            "dimension": text(record["dimension"], "dimension", maximum=40),
            "unit": identifier(record["unit"], "unit"), "minimum": str(minimum), "maximum": str(maximum),
            "threshold": str(threshold), "direction": record["direction"]}


def normalize_formula(record):
    shape(record, ("kind",), ("a", "b"))
    require(record["kind"] in FORMULA_KINDS, "formula", "转换公式类型无效")
    kind = record["kind"]
    if kind == "scale":
        require(set(record) == {"kind", "a"}, "formula", "scale公式只接受系数a")
        coefficient = number(record["a"], "a")
        require(coefficient != 0, "formula", "scale系数不能为零")
        return {"kind": "scale", "a": str(coefficient)}
    if kind == "offset":
        require(set(record) == {"kind", "b"}, "formula", "offset公式只接受常数b")
        return {"kind": "offset", "b": str(number(record["b"], "b"))}
    require(set(record) == {"kind", "a", "b"}, "formula", "affine公式需要a与b")
    coefficient = number(record["a"], "a")
    require(coefficient != 0, "formula", "affine系数不能为零")
    return {"kind": "affine", "a": str(coefficient), "b": str(number(record["b"], "b"))}


def normalize_conversion(record):
    shape(record, ("id", "revision", "name", "source", "target", "formula",
                   "minimum", "maximum", "start", "end", "reversible"))
    conversion_id = identifier(record["id"])
    require(not conversion_id.startswith(LEGACY_PREFIX), "identity", "转换编号不能占用legacy前缀")
    start, end = period(record["start"], record["end"])
    minimum = number(record["minimum"], "minimum")
    maximum = number(record["maximum"], "maximum")
    require(minimum <= maximum, "range", "转换适用值域无效")
    formula = normalize_formula(record["formula"])
    require(type(record["reversible"]) is bool, "field", "reversible需要布尔值")
    return {"id": conversion_id, "revision": integer(record["revision"], "revision", 1),
            "name": text(record["name"], "name"),
            "source": identifier(record["source"], "source"),
            "target": identifier(record["target"], "target"), "formula": formula,
            "minimum": str(minimum), "maximum": str(maximum), "start": start, "end": end,
            "reversible": record["reversible"]}


def apply_formula(formula, value):
    value = Decimal(value)
    if formula["kind"] == "scale":
        return value * Decimal(formula["a"])
    if formula["kind"] == "offset":
        return value + Decimal(formula["b"])
    return value * Decimal(formula["a"]) + Decimal(formula["b"])


class Calibrations:
    def __init__(self, snapshot):
        self.metrics = snapshot["metric"]
        self.rows = snapshot["calibration"]
        self.conversions = snapshot["conversion"]

    @staticmethod
    def legacy_id(metric):
        return LEGACY_PREFIX + metric

    def descriptor(self, calibration_id):
        require(isinstance(calibration_id, str), "calibration", "口径编号需要文本")
        if calibration_id.startswith(LEGACY_PREFIX):
            metric_id = calibration_id[len(LEGACY_PREFIX):]
            metric = self.metrics.get(metric_id)
            require(metric is not None, "calibration", "口径不存在", calibration=calibration_id)
            return {"id": calibration_id, "legacy": True, "metric": metric_id,
                    "calibration_revision": None, "metric_revision": metric["revision"],
                    "name": metric["name"], "concept": "legacy:" + metric_id,
                    "concept_version": "metric-rev-" + str(metric["revision"]),
                    "window": {"kind": "unspecified", "legacy": True},
                    "effective_start": "0001-01-01", "effective_end": "9999-12-31",
                    "dimension": metric["dimension"], "unit": metric["unit"],
                    "minimum": metric["minimum"], "maximum": metric["maximum"],
                    "threshold": metric["threshold"], "direction": metric["direction"]}
        row = self.rows.get(calibration_id)
        require(row is not None, "calibration", "口径不存在", calibration=calibration_id)
        return {"id": calibration_id, "legacy": False, "metric": row["metric"],
                "calibration_revision": row["revision"], "metric_revision": self.metrics[row["metric"]]["revision"],
                "name": row["name"], "concept": row["concept"], "concept_version": row["concept_version"],
                "window": row["window"], "effective_start": row["effective_start"],
                "effective_end": row["effective_end"], "dimension": row["dimension"], "unit": row["unit"],
                "minimum": row["minimum"], "maximum": row["maximum"],
                "threshold": row["threshold"], "direction": row["direction"]}

    def reading_binding(self, reading):
        explicit = reading.get("calibration")
        return explicit if explicit else self.legacy_id(reading["metric"])

    def action_binding(self, action):
        explicit = action.get("calibration")
        return explicit if explicit else self.legacy_id(action["metric"])

    def selected_id(self, calibration_id, metric):
        return calibration_id if calibration_id else self.legacy_id(metric)

    def hazardous(self, calibration_id, value):
        definition = self.descriptor(calibration_id)
        threshold = Decimal(definition["threshold"])
        return value >= threshold if definition["direction"] == "high" else value <= threshold

    def severity(self, calibration_id, value):
        definition = self.descriptor(calibration_id)
        threshold = Decimal(definition["threshold"])
        difference = value - threshold if definition["direction"] == "high" else threshold - value
        return max(Decimal(0), difference)

    def validate(self):
        seen = set()
        for row in self.rows.values():
            metric = self.metrics.get(row["metric"])
            require(metric is not None, "reference", "口径指标不存在", calibration=row["id"])
            require(row["dimension"] == metric["dimension"], "dimension", "口径量纲与指标不一致",
                    calibration=row["id"])
            require(row["unit"] == metric["unit"], "unit", "口径标准单位与指标不一致", calibration=row["id"])
            require(row["direction"] == metric["direction"], "direction", "口径异常方向与指标不一致",
                    calibration=row["id"])
            require(Decimal(metric["minimum"]) <= Decimal(row["minimum"])
                    and Decimal(row["maximum"]) <= Decimal(metric["maximum"]),
                    "range", "口径值域不能超出指标合理值域", calibration=row["id"])
            identity = (row["metric"], row["concept"], row["concept_version"],
                        row["window"]["kind"], row["window"].get("anchor"))
            require(identity not in seen, "calibration_duplicate", "同一指标下存在重复测量定义",
                    metric=row["metric"], concept=row["concept"], version=row["concept_version"])
            seen.add(identity)
        latest = list(self.conversions.values())
        for index, row in enumerate(latest):
            source = self.descriptor(row["source"])
            target = self.descriptor(row["target"])
            require(row["source"] != row["target"], "conversion", "转换不能连接同一口径", conversion=row["id"])
            require(source["dimension"] == target["dimension"], "dimension",
                    "转换两端量纲不一致", conversion=row["id"])
            for other in latest[index + 1:]:
                if other["source"] != row["source"] or other["target"] != row["target"] or other["id"] == row["id"]:
                    continue
                period_overlap = overlaps(row, other)
                range_overlap = (Decimal(row["minimum"]) <= Decimal(other["maximum"])
                                 and Decimal(other["minimum"]) <= Decimal(row["maximum"]))
                require(not (period_overlap and range_overlap), "conversion_conflict",
                        "同方向转换路径在重叠日期与值域内相互矛盾",
                        source=row["source"], target=row["target"], left=row["id"], right=other["id"])

    def find_conversion(self, source, target, when, value, allowed):
        """返回 (转换记录, None) 或 (None, 不可比原因)。不做链式寻路。"""
        day(when)
        candidates = [row for row in self.conversions.values()
                      if row["id"] in allowed and row["source"] == source and row["target"] == target]
        if not candidates:
            return None, "not_allowed"
        in_scope = [row for row in candidates
                    if contains(row, when) and Decimal(row["minimum"]) <= Decimal(value) <= Decimal(row["maximum"])]
        if not in_scope:
            sample = candidates[0]
            if not contains(sample, when):
                return None, "outside_period"
            return None, "out_of_range"
        if len(in_scope) > 1:
            return None, "path_conflict"
        return in_scope[0], None

    def registry(self, calibration_ids, conversion_ids=()):
        calibrations = [self.descriptor(calibration_id) for calibration_id in sorted(set(calibration_ids))]
        conversions = []
        for conversion_id in distinct_strings(list(conversion_ids), "conversions", nonempty=False):
            row = self.conversions.get(conversion_id)
            require(row is not None, "conversion", "允许的转换不存在", conversion=conversion_id)
            conversions.append({"id": row["id"], "revision": row["revision"], "source": row["source"],
                                "target": row["target"], "formula": row["formula"],
                                "minimum": row["minimum"], "maximum": row["maximum"],
                                "start": row["start"], "end": row["end"], "reversible": row["reversible"]})
        return {"calibrations": calibrations, "conversions": conversions}
