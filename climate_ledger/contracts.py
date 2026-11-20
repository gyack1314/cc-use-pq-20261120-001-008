from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


class DomainError(ValueError):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.details = details

    def as_dict(self):
        return {"error": self.code, "message": str(self), "details": self.details}


def require(condition, code, message, **details):
    if not condition:
        raise DomainError(code, message, **details)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def text(value, field, *, maximum=160):
    require(isinstance(value, str) and bool(value.strip()), "field", f"{field}需要非空文本")
    require(value == value.strip() and len(value) <= maximum and "\x00" not in value,
            "field", f"{field}包含非法字符或长度")
    return value


def identifier(value, field="id"):
    value = text(value, field, maximum=100)
    require(all(c.isalnum() or c in "_-.:" for c in value), "identity", f"{field}格式不正确")
    return value


def integer(value, field, minimum=0, maximum=1_000_000_000):
    require(type(value) is int and minimum <= value <= maximum, "field", f"{field}需要范围内整数")
    return value


def number(value, field, minimum=None, maximum=None):
    require(isinstance(value, (int, float, str, Decimal)) and not isinstance(value, bool),
            "number", f"{field}需要有限数值")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise DomainError("number", f"{field}不能解析") from None
    require(result.is_finite() and len(result.as_tuple().digits) <= 24
            and -12 <= result.as_tuple().exponent <= 12, "number", f"{field}数值范围过大")
    require(minimum is None or result >= Decimal(str(minimum)), "number", f"{field}低于下限")
    require(maximum is None or result <= Decimal(str(maximum)), "number", f"{field}超过上限")
    return result


def decimal_text(value, digits=6):
    if value is None:
        return None
    return format(Decimal(value).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP), "f")


def day(value, field="day"):
    require(isinstance(value, str), "date", f"{field}需要ISO日期")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise DomainError("date", f"{field}日期无效") from None
    require(parsed.isoformat() == value, "date", f"{field}需要YYYY-MM-DD")
    return value


def period(start, end, *, limit=3660):
    start, end = day(start, "start"), day(end, "end")
    span = (date.fromisoformat(end) - date.fromisoformat(start)).days
    require(0 <= span < limit, "period", "日期区间为空、倒置或过长")
    return start, end


def days(start, end):
    start, end = period(start, end)
    current = date.fromisoformat(start)
    finish = date.fromisoformat(end)
    while current <= finish:
        yield current.isoformat()
        current += timedelta(days=1)


def contains(record, when):
    return record["start"] <= when <= record["end"]


def overlaps(left, right):
    return max(left["start"], right["start"]) <= min(left["end"], right["end"])


def shape(record, required, optional=()):
    require(isinstance(record, dict), "shape", "记录需要对象")
    missing = set(required) - set(record)
    extra = set(record) - set(required) - set(optional)
    require(not missing and not extra, "shape", "记录字段不匹配", missing=sorted(missing), extra=sorted(extra))


def distinct_strings(value, field, *, nonempty=True):
    require(isinstance(value, list), "field", f"{field}需要数组")
    require(bool(value) or not nonempty, "field", f"{field}不能为空")
    result = [identifier(item, field) for item in value]
    require(len(set(result)) == len(result), "field", f"{field}不能重复")
    return sorted(result)


def ratio(numerator, denominator):
    return None if denominator == 0 else decimal_text(Decimal(numerator) / Decimal(denominator))


def row_key(kind, record):
    if kind == "reading":
        return ":".join([record["station"], record["metric"], record["day"]])
    return record["id"]


KINDS = ("zone", "metric", "station", "population", "action", "reading")


def query_spec(value):
    shape(value, ("metric", "start", "end", "zones"), ("minimum_coverage",))
    start, end = period(value["start"], value["end"])
    return {"metric": identifier(value["metric"], "metric"), "start": start, "end": end,
            "zones": distinct_strings(value["zones"], "zones"),
            "minimum_coverage": str(number(value.get("minimum_coverage", "0.8"), "minimum_coverage", 0, 1))}
