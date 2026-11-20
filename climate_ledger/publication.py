import json
import os
import tempfile
from pathlib import Path

from .contracts import canonical, fingerprint, identifier, require, text


class Publication:
    def __init__(self, ledger):
        self.ledger = ledger
        self.db = ledger.connection

    @staticmethod
    def assess(analysis):
        reasons = []
        if "query" in analysis:
            if analysis["status"] != "qualified":
                reasons.append("暴露分析存在未知区日，不能发布完整口径结论")
            if not analysis["total"]["known_population_days"]:
                reasons.append("无有效人口分母")
            if not analysis["daily"]:
                reasons.append("没有分析记录")
            for row in analysis["daily"]:
                if row["usable"] and not row["weather"]["evidence"]:
                    reasons.append("有效观测缺少来源依据")
                if row["usable"] and row["protected"] > row["population"]["population"]:
                    reasons.append("保护人数超过有效分母")
        else:
            if analysis.get("status") != "qualified" or analysis.get("estimate") is None:
                reasons.append("研究配对不足，不能发布效果估计")
            for phase in analysis.get("phases", {}).values():
                identities = [(p["control"], p["day"]) for p in phase["pairs"]]
                if len(identities) != len(set(identities)):
                    reasons.append("同阶段重复使用对照")
        return {"publishable": not reasons, "reasons": sorted(set(reasons))}

    def create(self, draft_id, scope, analysis, author):
        require(self.db.in_transaction, "transaction", "草稿必须绑定一致读取事务")
        identifier(draft_id, "draft")
        identifier(scope, "scope")
        text(author, "author")
        seq = self.ledger.watermark()
        content = {"schema_version": 1, "draft": draft_id, "scope": scope, "author": author,
                   "watermark": seq, "analysis": analysis, "evidence": self.ledger.provenance(seq),
                   "assessment": self.assess(analysis)}
        digest = fingerprint(content)
        existing = self.db.execute("SELECT digest FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if existing:
            require(existing[0] == digest, "draft_conflict", "草稿编号已用于不同内容")
            return self.draft(draft_id)
        self.db.execute("INSERT INTO drafts(id,seq,digest,payload) VALUES(?,?,?,?)",
                        (draft_id, seq, digest, canonical(content)))
        return self.draft(draft_id)

    def draft(self, draft_id):
        row = self.db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        require(row is not None, "draft", "草稿不存在")
        content = json.loads(row["payload"])
        require(fingerprint(content) == row["digest"], "digest", "草稿证据摘要不匹配")
        return {"content": content, "digest": row["digest"], "reviewer": row["reviewer"],
                "review_digest": row["review_digest"], "stale": row["seq"] != self.ledger.watermark()}

    def review(self, draft_id, digest, reviewer):
        require(self.db.in_transaction, "transaction", "审核需要写事务")
        reviewer = text(reviewer, "reviewer")
        draft = self.draft(draft_id)
        require(draft["digest"] == digest, "digest", "审核的草稿版本不一致")
        require(not draft["stale"], "stale", "资料已有新入账，需重新生成草稿")
        require(reviewer != draft["content"]["author"], "reviewer", "审核人与草稿作者需要不同")
        require(draft["content"]["assessment"]["publishable"], "unpublishable", "当前证据不足以发布",
                reasons=draft["content"]["assessment"]["reasons"])
        published = self.db.execute("SELECT id FROM releases WHERE draft_id=?", (draft_id,)).fetchone()
        require(published is None, "released", "已发布草稿不能重审")
        self.db.execute("UPDATE drafts SET reviewer=?,review_digest=? WHERE id=?", (reviewer, digest, draft_id))
        return self.draft(draft_id)

    def head(self, scope):
        row = self.db.execute("SELECT release_id FROM release_heads WHERE scope=?", (scope,)).fetchone()
        return row[0] if row else None

    def publish(self, draft_id, release_id, expected_head):
        require(self.db.in_transaction, "transaction", "发布需要写事务")
        identifier(release_id, "release")
        draft = self.draft(draft_id)
        existing = self.db.execute("SELECT draft_id FROM releases WHERE id=?", (release_id,)).fetchone()
        if existing:
            require(existing[0] == draft_id, "release_conflict", "发布编号不能用于另一草稿")
            return self.release(release_id)
        require(not draft["stale"], "stale", "审核后的资料发生变化，不能直接发布")
        require(draft["reviewer"] and draft["review_digest"] == draft["digest"], "review", "当前草稿尚未审核")
        require(draft["content"]["assessment"]["publishable"], "unpublishable", "发布资格不足")
        scope = draft["content"]["scope"]
        actual = self.head(scope)
        require(actual == expected_head, "head_conflict", "专题已有其他发布，请确认替代关系", actual=actual)
        prior = self.db.execute("SELECT id FROM releases WHERE draft_id=?", (draft_id,)).fetchone()
        require(prior is None, "released", "同一草稿不能重复签发")
        content = {"schema_version": 1, "release": release_id, "supersedes": actual,
                   "reviewer": draft["reviewer"], "draft_digest": draft["digest"], "document": draft["content"]}
        digest = fingerprint(content)
        self.db.execute("INSERT INTO releases VALUES(?,?,?,?,?)", (release_id, draft_id, canonical(content), digest, actual))
        self.db.execute("INSERT INTO release_heads VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET release_id=excluded.release_id",
                        (scope, release_id))
        return self.release(release_id)

    def release(self, release_id):
        row = self.db.execute("SELECT payload,digest FROM releases WHERE id=?", (release_id,)).fetchone()
        require(row is not None, "release", "发布记录不存在")
        content = json.loads(row["payload"])
        require(fingerprint(content) == row["digest"], "digest", "发布内容被改变")
        return {"content": content, "digest": row["digest"],
                "current_evidence": content["document"]["watermark"] == self.ledger.watermark(),
                "is_head": self.head(content["document"]["scope"]) == release_id}

    def list_releases(self, scope=None):
        rows = self.db.execute("SELECT id FROM releases ORDER BY rowid").fetchall()
        result = []
        for row in rows:
            release = self.release(row["id"])
            if scope is not None and release["content"]["document"]["scope"] != scope:
                continue
            result.append({"release": row["id"], "digest": release["digest"], "is_head": release["is_head"],
                           "current_evidence": release["current_evidence"],
                           "supersedes": release["content"]["supersedes"]})
        return result

    def export(self, release_id, destination):
        release = self.release(release_id)
        envelope = {"content": release["content"], "digest": release["digest"]}
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".release-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(canonical(envelope) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return {"path": str(path), "digest": release["digest"]}

    @staticmethod
    def verify_export(destination):
        try:
            envelope = json.loads(Path(destination).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            from .contracts import DomainError
            raise DomainError("archive", "无法读取发布文件") from exc
        require(isinstance(envelope, dict) and set(envelope) == {"content", "digest"}, "archive", "发布文件结构错误")
        require(fingerprint(envelope["content"]) == envelope["digest"], "digest", "发布文件校验失败")
        return {"valid": True, "release": envelope["content"]["release"], "digest": envelope["digest"]}
