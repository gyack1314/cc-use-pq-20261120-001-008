from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .contracts import DomainError, KINDS, canonical, fingerprint, identifier, integer, require, row_key


SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO metadata VALUES('schema','1');
CREATE TABLE IF NOT EXISTS batches(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
 digest TEXT NOT NULL, source TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS facts(
 kind TEXT NOT NULL, identity TEXT NOT NULL, revision INTEGER NOT NULL,
 seq INTEGER NOT NULL REFERENCES batches(seq), payload TEXT NOT NULL,
 PRIMARY KEY(kind,identity,revision));
CREATE INDEX IF NOT EXISTS facts_snapshot ON facts(kind,seq,identity);
CREATE TABLE IF NOT EXISTS drafts(
 id TEXT PRIMARY KEY, seq INTEGER NOT NULL, digest TEXT NOT NULL,
 payload TEXT NOT NULL, reviewer TEXT, review_digest TEXT);
CREATE TABLE IF NOT EXISTS releases(
 id TEXT PRIMARY KEY, draft_id TEXT NOT NULL UNIQUE REFERENCES drafts(id),
 payload TEXT NOT NULL, digest TEXT NOT NULL, supersedes TEXT REFERENCES releases(id));
CREATE TABLE IF NOT EXISTS release_heads(scope TEXT PRIMARY KEY, release_id TEXT NOT NULL REFERENCES releases(id));
"""


class Ledger:
    def __init__(self, path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=10000")
        self.connection.executescript(SCHEMA)
        version = self.connection.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()[0]
        require(version == "1", "schema", "不支持的证据账版本")

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def transaction(self, *, write=False):
        require(not self.connection.in_transaction, "transaction", "不能嵌套业务事务")
        self.connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield self
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def watermark(self):
        return self.connection.execute("SELECT COALESCE(MAX(seq),0) FROM batches").fetchone()[0]

    def snapshot(self, seq=None):
        seq = self.watermark() if seq is None else integer(seq, "seq")
        require(seq <= self.watermark(), "snapshot", "快照尚不存在")
        rows = self.connection.execute("""
            SELECT f.* FROM facts f JOIN (
              SELECT kind,identity,MAX(revision) revision FROM facts WHERE seq<=? GROUP BY kind,identity
            ) h USING(kind,identity,revision) ORDER BY kind,identity
            """, (seq,))
        result = {kind: {} for kind in KINDS}
        for row in rows:
            result[row["kind"]][row["identity"]] = json.loads(row["payload"])
        return result

    def append(self, batch, normalized, validate):
        require(self.connection.in_transaction, "transaction", "入账必须处于写事务")
        batch_id = identifier(batch["id"], "batch.id")
        digest = fingerprint(batch)
        existing = self.connection.execute("SELECT seq,digest FROM batches WHERE id=?", (batch_id,)).fetchone()
        if existing:
            require(existing["digest"] == digest, "idempotency", "同批次编号不能对应不同内容")
            return {"seq": existing["seq"], "replayed": True, "changed": 0}
        current = self.snapshot()
        changed = []
        seen = set()
        for kind, record in normalized:
            key = row_key(kind, record)
            identity = (kind, key)
            require(identity not in seen, "duplicate", "同一批次对象重复", kind=kind, identity=key)
            seen.add(identity)
            old = current[kind].get(key)
            if old:
                require(record["revision"] >= old["revision"], "revision", "不能覆盖更高修订")
                if record["revision"] == old["revision"]:
                    require(record == old, "revision_conflict", "同修订内容冲突", identity=key)
                    continue
                previous = self.connection.execute(
                    "SELECT b.source FROM facts f JOIN batches b USING(seq) WHERE kind=? AND identity=? ORDER BY revision DESC LIMIT 1",
                    (kind, key)).fetchone()[0]
                require(previous == batch["source"], "source", "修订不能改变对象资料来源", identity=key)
            else:
                require(record["revision"] == 1, "revision", "新对象修订必须从1开始")
            current[kind][key] = record
            changed.append((kind, key, record))
        validate(current)
        cursor = self.connection.execute("INSERT INTO batches(id,digest,source,payload) VALUES(?,?,?,?)",
                                         (batch_id, digest, batch["source"], canonical(batch)))
        seq = cursor.lastrowid
        for kind, key, record in changed:
            self.connection.execute("INSERT INTO facts VALUES(?,?,?,?,?)",
                                    (kind, key, record["revision"], seq, canonical(record)))
        return {"seq": seq, "replayed": False, "changed": len(changed)}

    def history(self, kind, identity):
        require(kind in KINDS, "kind", "未知资料类型")
        rows = self.connection.execute("""
            SELECT f.revision,f.seq,f.payload,b.id batch,b.source FROM facts f
            JOIN batches b USING(seq) WHERE f.kind=? AND f.identity=? ORDER BY f.revision
            """, (kind, identity))
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def provenance(self, seq):
        rows = self.connection.execute("SELECT seq,id,source,digest FROM batches WHERE seq<=? ORDER BY seq", (seq,))
        return [dict(row) for row in rows]

    def integrity(self):
        result = self.connection.execute("PRAGMA integrity_check").fetchone()[0]
        require(result == "ok", "integrity", "证据账存储损坏")
        require(not self.connection.execute("PRAGMA foreign_key_check").fetchall(), "integrity", "证据引用损坏")
        for row in self.connection.execute("SELECT digest,payload FROM batches"):
            require(fingerprint(json.loads(row["payload"])) == row["digest"], "integrity", "批次摘要不一致")
        return {"status": "ok", "watermark": self.watermark()}
