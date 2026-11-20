import json
import os
import sqlite3
import tempfile
from pathlib import Path

from .contracts import KINDS, canonical, fingerprint, require
from .publication import Publication


class Audit:
    def __init__(self, ledger):
        self.ledger = ledger

    def verify(self, validate):
        self.ledger.integrity()
        rows = self.ledger.connection.execute("SELECT * FROM batches ORDER BY seq").fetchall()
        previous_seq = 0
        checked = 0
        for batch in rows:
            require(batch["seq"] > previous_seq, "audit", "入账顺序错误")
            payload = json.loads(batch["payload"])
            require(payload["id"] == batch["id"] and payload["source"] == batch["source"],
                    "audit", "资料包身份与索引不一致")
            require(fingerprint(payload) == batch["digest"], "audit", "资料包摘要损坏")
            snapshot = self.ledger.snapshot(batch["seq"])
            validate(snapshot)
            previous_seq = batch["seq"]
            checked += 1
        facts = self.ledger.connection.execute("SELECT DISTINCT kind,identity FROM facts ORDER BY kind,identity").fetchall()
        versions = 0
        for fact in facts:
            history = self.ledger.history(fact["kind"], fact["identity"])
            require(history and history[0]["revision"] == 1, "audit", "对象缺少首版资料")
            source = history[0]["source"]
            last_revision = 0
            last_seq = 0
            for edition in history:
                require(edition["source"] == source, "audit", "对象修订来源被改变")
                require(edition["revision"] > last_revision and edition["seq"] > last_seq,
                        "audit", "对象修订与入账顺序不一致")
                require(edition["payload"]["revision"] == edition["revision"], "audit", "修订索引不一致")
                last_revision, last_seq = edition["revision"], edition["seq"]
                versions += 1
        publication = Publication(self.ledger)
        releases = self.ledger.connection.execute("SELECT * FROM releases ORDER BY rowid").fetchall()
        heads = {}
        for row in releases:
            release = publication.release(row["id"])
            document = release["content"]["document"]
            draft = publication.draft(row["draft_id"])
            require(release["content"]["draft_digest"] == draft["digest"], "audit", "发布与草稿摘要不一致")
            require(document == draft["content"], "audit", "发布与草稿资料不一致")
            require(release["content"]["reviewer"] != document["author"], "audit", "发布审核职责未分离")
            require(release["content"]["supersedes"] == row["supersedes"], "audit", "发布前序索引不一致")
            scope = document["scope"]
            require(row["supersedes"] == heads.get(scope), "audit", "专题发布链断裂")
            require(document["evidence"] == self.ledger.provenance(document["watermark"]),
                    "audit", "发布的资料来源清单不完整")
            heads[scope] = row["id"]
        actual_heads = dict(self.ledger.connection.execute("SELECT scope,release_id FROM release_heads"))
        require(heads == actual_heads, "audit", "专题当前发布指针损坏")
        return {"status": "verified", "batches": checked, "objects": len(facts),
                "versions": versions, "releases": len(releases), "watermark": previous_seq}

    def backup(self, destination):
        require(not self.ledger.connection.in_transaction, "transaction", "备份不能嵌套业务事务")
        target = Path(destination).resolve()
        require(self.ledger.path == ":memory:" or target != Path(self.ledger.path).resolve(),
                "backup", "备份不能覆盖正在使用的证据账")
        require(not target.exists(), "backup", "备份目标已存在")
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".ledger-", dir=target.parent)
        os.close(fd)
        try:
            connection = sqlite3.connect(temporary)
            try:
                self.ledger.connection.backup(connection)
                require(connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "backup", "备份校验失败")
                connection.commit()
            finally:
                connection.close()
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return {"path": str(target), "bytes": target.stat().st_size}
