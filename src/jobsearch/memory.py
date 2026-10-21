from __future__ import annotations
import json
import time
from typing import Literal, Any
from pydantic import BaseModel, Field, model_validator
from .config import digest, POLICY
from .db import uid, js
from .corpus import Corpus, SECRET_KEYS
from .answer_scopes import SCOPED, allows_global, fact_allows_global  # noqa: F401


class FactInput(BaseModel):
    candidate: Literal["real", "fixture"] = "fixture"
    kind: Literal[
        "candidate",
        "employment",
        "skill",
        "skill_duration",
        "preference",
        "answer",
        "decision",
        "workflow",
        "authorization",
        "receipt",
    ] = "candidate"
    key: str = Field(min_length=1, max_length=120)
    value: Any
    status: Literal["confirmed", "unconfirmed", "disputed"] = "unconfirmed"
    scope: str = "global"
    provenance: dict
    effective_from: str | None = None
    effective_to: str | None = None
    evidence_versions: list[str] = Field(default_factory=list)
    supersedes: str | None = None

    @model_validator(mode="after")
    def validate_safety(self):
        if SECRET_KEYS.search(self.key) or contains_secret_key(self.value):
            raise ValueError(
                "Passwords, codes, tokens, and secrets are not career memory"
            )
        if (
            not allows_global(self.key, self.provenance.get("global_form_field"))
            and self.scope == "global"
            and self.kind != "preference"
        ):
            raise ValueError("This answer requires an application scope")
        if self.status == "confirmed" and self.provenance.get("type") not in (
            "user_confirmation",
            "explicit_record",
        ):
            raise ValueError(
                "Only user confirmation or an explicit confirmation record establishes a fact"
            )
        if self.kind == "authorization":
            raise ValueError(
                "Submission authorization can only be created by the application approval service"
            )
        return self


def contains_secret_key(value):
    if isinstance(value, dict):
        return any(
            SECRET_KEYS.search(k) or contains_secret_key(v) for k, v in value.items()
        )
    if isinstance(value, list):
        return any(contains_secret_key(v) for v in value)
    return False


class Memory:
    def __init__(self, db):
        self.db = db

    def put(
        self, fact: FactInput, actor="user", resolve_scope=False, replace_scope=None
    ):
        if replace_scope and (
            not resolve_scope or fact.scope != "global" or replace_scope == "global"
        ):
            raise ValueError(
                "Only a global answer confirmation can replace a local answer"
            )
        if resolve_scope and (
            actor != "user" or fact.provenance.get("type") != "user_confirmation"
        ):
            raise ValueError(
                "Only explicit user confirmation can resolve conflicting answers"
            )
        if fact.provenance.get("type") == "user_confirmation" and actor != "user":
            raise ValueError("An agent cannot impersonate a candidate confirmation")
        fid = uid()
        with self.db.tx() as c:
            for scope in sorted(
                {fact.scope} | ({replace_scope} if replace_scope else set())
            ):
                c.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (fact.candidate + fact.key + scope,),
                )
            for version in fact.evidence_versions:
                doc = c.execute(
                    "SELECT d.kind,d.current_version,d.deleted,d.metadata FROM versions v JOIN documents d ON d.id=v.document_id WHERE v.id=%s",
                    (version,),
                ).fetchone()
                if not doc or doc["deleted"] or doc["current_version"] != version:
                    raise ValueError("Evidence is missing or stale")
                if (
                    fact.status == "confirmed"
                    and fact.provenance.get("type") == "explicit_record"
                    and doc["kind"] != "confirmation_record"
                    and not (
                        fact.kind == "receipt" and doc["kind"] == "submission_evidence"
                    )
                    and fact.provenance.get("confirmation_section")
                    not in doc["metadata"].get("confirmation_sections", [])
                ):
                    raise ValueError(
                        "Imported source claims and generated drafts cannot confirm facts"
                    )
            old = c.execute(
                "SELECT * FROM facts WHERE candidate=%s AND key=%s AND scope=ANY(%s) AND active",
                (
                    fact.candidate,
                    fact.key,
                    [fact.scope] + ([replace_scope] if replace_scope else []),
                ),
            ).fetchall()
            identical = next(
                (
                    x
                    for x in old
                    if x["value"] == fact.value
                    and x["status"] == fact.status
                    and x["evidence_versions"] == fact.evidence_versions
                    and x["effective_from"] == fact.effective_from
                    and x["effective_to"] == fact.effective_to
                ),
                None,
            )
            if (
                identical
                and not replace_scope
                and not (resolve_scope and any(x["value"] != fact.value for x in old))
            ):
                return identical
            targets = (
                [x["id"] for x in old]
                if resolve_scope
                else ([fact.supersedes] if fact.supersedes else [])
            )
            if resolve_scope and targets:
                fact = fact.model_copy(
                    update={
                        "supersedes": targets[0],
                        "provenance": fact.provenance
                        | {"superseded_fact_ids": targets},
                    }
                )
            for target_id in targets:
                target = c.execute(
                    "SELECT * FROM facts WHERE id=%s", (target_id,)
                ).fetchone()
                if not target or (
                    target["candidate"] != fact.candidate
                    or target["key"] != fact.key
                    or target["scope"] not in (fact.scope, replace_scope)
                ):
                    raise ValueError(
                        "Correction must match the original candidate, key, and scope"
                    )
                c.execute(
                    "UPDATE facts SET active=false,status='superseded' WHERE id=%s",
                    (target_id,),
                )
                c.execute(
                    "UPDATE answer_cache SET valid=false WHERE fact_ids @> %s",
                    (js([target_id]),),
                )
                c.execute(
                    "UPDATE conflicts SET resolved=true,resolution=%s WHERE kind='fact' AND subject=%s",
                    (js({"corrected_by": fid}), target_id),
                )
                affected = c.execute(
                    "SELECT DISTINCT a.id,a.state FROM applications a LEFT JOIN artifacts r ON r.application_id=a.id WHERE a.payload::text LIKE %s OR r.claims @> %s",
                    ("%" + target_id + "%", js([target_id])),
                ).fetchall()
                for application in affected:
                    c.execute(
                        "UPDATE approvals SET invalidated=true WHERE application_id=%s",
                        (application["id"],),
                    )
                    if application["state"] == "authorized":
                        c.execute(
                            "UPDATE applications SET state='review',revision=revision+1 WHERE id=%s",
                            (application["id"],),
                        )
                old = [x for x in old if x["id"] != target_id]
            c.execute(
                "INSERT INTO facts(id,candidate,kind,key,value,status,scope,provenance,effective_from,effective_to,evidence_versions,supersedes) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    fid,
                    fact.candidate,
                    fact.kind,
                    fact.key,
                    js(fact.value),
                    fact.status,
                    fact.scope,
                    js(fact.provenance),
                    fact.effective_from,
                    fact.effective_to,
                    js(fact.evidence_versions),
                    fact.supersedes,
                ),
            )
            for other in old:
                if other["value"] != fact.value:
                    c.execute(
                        "INSERT INTO conflicts(id,kind,subject,detail) VALUES(%s,'fact',%s,%s)",
                        (
                            uid(),
                            other["id"],
                            js(
                                {
                                    "facts": [other["id"], fid],
                                    "key": fact.key,
                                    "scope": fact.scope,
                                }
                            ),
                        ),
                    )
                    c.execute(
                        "UPDATE answer_cache SET valid=false WHERE fact_ids @> %s",
                        (js([other["id"]]),),
                    )
            self.db.audit(
                "fact.corrected" if fact.supersedes else "fact.recorded",
                fid,
                {"status": fact.status, "scope": fact.scope, "kind": fact.kind},
                c,
            )
        return self.db.one("SELECT * FROM facts WHERE id=%s", (fid,))

    def _valid(self, answer):
        for fid in answer.get("fact_ids", []):
            row = self.db.one(
                "SELECT * FROM facts WHERE id=%s AND active AND status='confirmed'",
                (fid,),
            )
            if not row:
                return False
            from datetime import date

            today = date.today().isoformat()
            if (row["effective_from"] and row["effective_from"] > today) or (
                row["effective_to"] and row["effective_to"] < today
            ):
                return False
            if self.db.one(
                "SELECT 1 FROM facts WHERE active AND candidate=%s AND key=%s AND scope=%s AND id<>%s AND value<>%s",
                (row["candidate"], row["key"], row["scope"], fid, js(row["value"])),
            ):
                return False
        for vid in answer.get("version_ids", []):
            if not self.db.one(
                "SELECT 1 FROM documents WHERE current_version=%s AND NOT deleted",
                (vid,),
            ):
                return False
        return True

    def resolve(
        self,
        key,
        candidate="fixture",
        scope="global",
        query=None,
        context_fact_ids=None,
        retrieve=True,
    ):
        start = time.monotonic()
        try:
            return self._resolve(
                key, candidate, scope, query, context_fact_ids or [], retrieve
            )
        finally:
            self.db.metric("answer_latency_ms", (time.monotonic() - start) * 1000)
            self.db.metric("answer_requests")

    def _resolve(self, key, candidate, scope, query, context_ids, retrieve=True):
        if SECRET_KEYS.search(key):
            raise ValueError(
                "Complete secrets and verification challenges directly in the browser"
            )
        scopes = list(dict.fromkeys([scope, "global"]))
        rows = self.db.all(
            "SELECT * FROM facts WHERE candidate=%s AND key=%s AND scope=ANY(%s) AND active AND (effective_from IS NULL OR effective_from<=to_char(current_date,'YYYY-MM-DD')) AND (effective_to IS NULL OR effective_to>=to_char(current_date,'YYYY-MM-DD'))",
            (candidate, key, scopes),
        )
        rows = [r for r in rows if r["scope"] != "global" or fact_allows_global(r)]
        specific = [r for r in rows if r["scope"] == scope]
        applicable = specific or rows
        if len({json.dumps(r["value"], sort_keys=True) for r in applicable}) > 1:
            self.db.metric("memory_conflicts")
            return {
                "source": "question",
                "question": f"Which value is current for {key.replace('_', ' ')}?",
                "conflicting_facts": applicable,
            }
        confirmed = [
            r
            for r in applicable
            if r["status"] == "confirmed" and r["kind"] != "preference"
        ]
        for row in sorted(confirmed, key=lambda r: r["id"] not in context_ids):
            response = {
                "value": row["value"],
                "fact_ids": [row["id"]],
                "version_ids": row["evidence_versions"],
                "provenance": row["provenance"],
                "scope": row["scope"],
            }
            if self._valid(response):
                self.db.metric("memory_hits")
                self.db.metric("retrieval_calls_avoided")
                self.cache(key, candidate, scope, response)
                return response | {
                    "source": "workflow_context"
                    if row["id"] in context_ids
                    else "memory"
                }
        cache_key = digest([candidate, scope, key, query or key, POLICY])
        cached = self.db.one(
            "SELECT * FROM answer_cache WHERE request_key=%s AND valid AND policy=%s ORDER BY created_at DESC LIMIT 1",
            (cache_key, POLICY),
        )
        if cached and self._valid(cached["answer"]):
            self.db.metric("cache_hits")
            self.db.metric("retrieval_calls_avoided")
            return cached["answer"] | {"source": "cache"}
        if cached:
            self.db.execute(
                "UPDATE answer_cache SET valid=false WHERE key=%s", (cached["key"],)
            )
            self.db.metric("stale_answers_blocked")
        evidence = (
            Corpus(self.db).retrieve(query or key.replace("_", " "), candidate)
            if retrieve
            else {"evidence": []}
        )
        # Retrieval never promotes a claim into confirmation, even if many drafts repeat it.
        return {
            "source": "question",
            "question": f"Please confirm {key.replace('_', ' ')} for this application.",
            "scope": scope,
            "evidence": evidence["evidence"],
            "value": None,
        }

    def cache(self, key, candidate, scope, answer, query=None):
        if not self._valid(answer) or not answer.get("fact_ids"):
            raise ValueError(
                "Only currently confirmed, evidence-backed answers can be cached"
            )
        request_key = digest([candidate, scope, key, query or key, POLICY])
        ck = digest(
            [
                request_key,
                sorted(answer["fact_ids"]),
                sorted(answer.get("version_ids", [])),
            ]
        )
        for fid in answer["fact_ids"]:
            f = self.db.one("SELECT * FROM facts WHERE id=%s", (fid,))
            if (
                f["candidate"] != candidate
                or f["scope"] not in (scope, "global")
                or (
                    f["scope"] == "global"
                    and (
                        not fact_allows_global(f)
                        or not allows_global(
                            key, f["provenance"].get("global_form_field")
                        )
                    )
                )
            ):
                raise ValueError("Cache evidence scope does not match the question")
        self.db.execute(
            "INSERT INTO answer_cache(key,candidate,answer,fact_ids,version_ids,policy,request_key) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(key) DO UPDATE SET answer=EXCLUDED.answer,fact_ids=EXCLUDED.fact_ids,version_ids=EXCLUDED.version_ids,policy=EXCLUDED.policy,valid=true,created_at=now()",
            (
                ck,
                candidate,
                js(answer),
                js(answer["fact_ids"]),
                js(answer.get("version_ids", [])),
                POLICY,
                request_key,
            ),
        )
