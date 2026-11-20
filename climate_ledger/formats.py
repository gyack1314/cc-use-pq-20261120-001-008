import csv
import io
import json
from pathlib import Path

from .application import NORMALIZERS
from .contracts import DomainError, canonical, identifier, require, row_key


CSV_FIELDS = {
    "zone": ("id", "revision", "name"),
    "metric": ("id", "revision", "name", "dimension", "unit", "threshold", "direction", "minimum", "maximum"),
    "station": ("id", "revision", "zone", "start", "end", "weight", "metrics"),
    "reading": ("station", "metric", "day", "revision", "value", "unit", "quality"),
    "population": ("id", "revision", "zone", "group", "start", "end", "count", "vulnerability"),
    "action": ("id", "revision", "name", "zone", "metric", "start", "end", "status", "capacity", "groups", "priority", "facility"),
}
INTEGER_FIELDS = {"revision", "count", "capacity", "priority"}
ARRAY_FIELDS = {"metrics", "groups"}


def decode_csv(content, kind):
    require(kind in CSV_FIELDS, "kind", "CSV资料类型无效")
    require(isinstance(content, str), "encoding", "CSV需要UTF-8文本")
    stream = io.StringIO(content.lstrip("\ufeff"), newline="")
    reader = csv.DictReader(stream, strict=True)
    try:
        fields = reader.fieldnames
        require(fields is not None and len(fields) == len(set(fields)), "header", "CSV表头为空或包含重复列")
        require(set(fields) == set(CSV_FIELDS[kind]), "header", "CSV列与资料契约不一致",
                expected=list(CSV_FIELDS[kind]))
        records = []
        identities = set()
        for line, raw in enumerate(reader, 2):
            require(None not in raw and all(value is not None for value in raw.values()),
                    "csv", "CSV列数不匹配", line=line)
            value = {}
            for field, item in raw.items():
                if field in INTEGER_FIELDS:
                    require(item.isascii() and item.isdecimal(), "csv", "整数字段格式错误", field=field, line=line)
                    value[field] = int(item)
                elif field in ARRAY_FIELDS:
                    try:
                        value[field] = json.loads(item)
                    except ValueError:
                        raise DomainError("csv", "数组列需要JSON数组", field=field, line=line) from None
                elif field == "value" and item == "":
                    value[field] = None
                else:
                    value[field] = item
            try:
                normalized = NORMALIZERS[kind](value)
            except DomainError as exc:
                raise DomainError(exc.code, str(exc), **{**exc.details, "line": line}) from exc
            key = row_key(kind, normalized)
            require(key not in identities, "duplicate", "同一CSV对象重复", identity=key, line=line)
            identities.add(key)
            records.append({"kind": kind, "data": value})
        require(bool(records), "csv", "CSV没有资料行")
        return records
    except csv.Error as exc:
        raise DomainError("csv", "CSV转义或引号无效", line=reader.line_num) from exc


def encode_csv(records, kind):
    require(kind in CSV_FIELDS, "kind", "CSV资料类型无效")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, CSV_FIELDS[kind], lineterminator="\n")
    writer.writeheader()
    for record in records:
        value = NORMALIZERS[kind](record)
        row = {}
        for key in CSV_FIELDS[kind]:
            item = value[key]
            if key in ARRAY_FIELDS:
                row[key] = canonical(item)
            elif item is None:
                row[key] = ""
            else:
                row[key] = str(item)
        writer.writerow(row)
    return output.getvalue()


def bundle_from_csv(batch_id, source, files):
    identifier(batch_id)
    require(isinstance(files, dict) and bool(files), "csv", "需要按资料类型提供CSV路径")
    records = []
    for kind, path in sorted(files.items()):
        try:
            content = Path(path).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise DomainError("input", "CSV文件不可读", path=str(path)) from exc
        records.extend(decode_csv(content, kind))
    return {"id": batch_id, "source": source, "records": records}


def snapshot_csv(snapshot, directory):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    outputs = []
    for kind in CSV_FIELDS:
        path = root / (kind + ".csv")
        records = [row for _, row in sorted(snapshot[kind].items())]
        path.write_text(encode_csv(records, kind), encoding="utf-8", newline="\n")
        outputs.append({"kind": kind, "path": str(path), "rows": len(records)})
    return outputs
