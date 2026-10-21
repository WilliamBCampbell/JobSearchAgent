"""Evidence-backed answer preparation without creating confirmed career facts."""

import json
import re

from .config import digest
from .corpus import Corpus
from .form_policy import (
    personal_question,
    requires_explicit_answer,
    narrative_choice_mismatch,
)
from .llm.contracts import InferenceRequest
from .llm.service import InferenceService
from .llm.instructions import load_instructions
from .llm.runner import InferenceStopped
from .memory import Memory


class FormAnswers:
    def __init__(self, db):
        self.db = db
        self.service = InferenceService(db)

    def context_question(self, aid, qid):
        from .workflow import Workflow
        from .form_policy import narrative_choice_mismatch

        flow = Workflow(self.db)
        app = flow.get(aid)
        flow.editable(app)
        if (
            app["paused"]
            or app["state"]
            not in (
                "missing_answers",
                "review",
                "prepared",
                "selected",
                "scored",
                "manual_handoff",
            )
            or self.db.one("SELECT 1 FROM attempts WHERE application_id=%s", (aid,))
        ):
            raise ValueError(
                "Answer drafting is available before submission on an active application"
            )
        q = self.db.one(
            "SELECT * FROM questions WHERE id=%s AND application_id=%s", (qid, aid)
        )
        field = next(
            (
                f
                for f in app["payload"].get("form_schema", [])
                if q and f["key"] == q["key"]
            ),
            None,
        )
        if (
            not field
            or not narrative_choice_mismatch(field)
            or (q["answer_fact"] and not q["needs_review"])
        ):
            raise ValueError(
                "Choose an unanswered narrative question with conflicting employer choices"
            )
        return app, q, field

    def draft_context(
        self, aid, qid, job_id=None, progress=lambda _: None, cancelled=lambda: False
    ):
        from .form_policy import narrative_field
        from .db import js

        app, question, field = self.context_question(aid, qid)
        result = self.prepare(
            app, [narrative_field(field)], job_id, progress, cancelled
        )[field["key"]]
        if cancelled():
            raise InferenceStopped("Answer context drafting cancelled")
        _, current, current_field = self.context_question(aid, qid)
        if current_field != field or current["answer_fact"] != question["answer_fact"]:
            raise ValueError(
                "The question changed while drafting; review the current question"
            )
        reason = result.get("reason", {})
        suggestion = (
            result
            if result.get("source") == "form_evidence"
            else reason.get("suggestion")
        )
        self.db.execute(
            "UPDATE questions SET reason=reason || %s WHERE id=%s",
            (
                js(
                    {
                        "context_suggestion": suggestion,
                        "context_message": reason.get(
                            "message",
                            "Review the draft and add any missing demo details.",
                        ),
                        "context": reason.get("context", []),
                    }
                ),
                qid,
            ),
        )
        return {"question_id": qid, "draft_available": bool(suggestion)}

    def request(self, app, fields, versions=(), proposals=None):
        return InferenceRequest(
            task="form_answer_review" if proposals else "form_answers",
            candidate=app["candidate"],
            application_id=app["id"],
            form_fields=fields,
            source_versions=list(versions),
            answer_proposals=proposals,
        )

    def valid(self, app, field, answer, draft=False):
        """Check the saved evaluation, its exact output, and current evidence."""
        provenance = answer.get("provenance", {})
        if not field.get("key") or not field.get("label"):
            return False
        if (
            provenance.get("type") != "form_evidence"
            or answer.get("scope") != app["id"]
        ):
            return False
        if not Memory(self.db)._valid(answer):
            return False
        if (
            narrative_choice_mismatch(field)
            or requires_explicit_answer(field)
            or (personal_question(field) and not draft)
        ):
            return False
        run, review = [
            self.db.one("SELECT * FROM llm_runs WHERE id=%s", (provenance.get(k, ""),))
            for k in ("run_id", "review_run_id")
        ]
        if (
            not run
            or not review
            or any(
                r["status"] != "complete"
                or r["candidate"] != app["candidate"]
                or r["application_id"] != app["id"]
                for r in (run, review)
            )
        ):
            return False
        if run["task"] != "form_answers" or review["task"] != "form_answer_review":
            return False
        if (
            field not in run["input"]["form_fields"]
            or review["input"].get("answer_proposals") != run["output"]
        ):
            return False
        if digest(review["input"]) != digest(
            run["input"]
            | {"task": "form_answer_review", "answer_proposals": run["output"]}
        ):
            return False
        proposed = next(
            (a for a in run["output"]["answers"] if a["key"] == field["key"]), None
        )
        checked = next(
            (c for c in review["output"]["checks"] if c["key"] == field["key"]), None
        )
        if not proposed or not checked or not checked["supported"] or checked["issue"]:
            return False
        if proposed["decision"] not in (("answer", "draft") if draft else ("answer",)):
            return False
        if any(proposed[k] != answer.get(k) for k in ("value", "fact_ids")):
            return False
        try:
            request = self.request(
                app, run["input"]["form_fields"], run["input"]["source_versions"]
            )
            if digest(self.service.context(request)) != digest(run["input"]):
                return False
            review_request = self.request(
                app,
                run["input"]["form_fields"],
                run["input"]["source_versions"],
                run["output"],
            )
            for record, original in ((run, request), (review, review_request)):
                fingerprint = digest(
                    [
                        record["input"],
                        original.model_dump(),
                        self.service.config.model_dump(),
                        load_instructions(original.task)[0],
                        self.service.output_schema(original.task, record["input"]),
                    ]
                )
                if fingerprint != record["fingerprint"]:
                    return False
            return True
        except ValueError:
            return False

    def prepare(
        self, app, fields, job_id=None, progress=lambda _: None, cancelled=lambda: False
    ):
        if not fields:
            return {}
        if cancelled():
            raise InferenceStopped("Application answer preparation cancelled")
        request = self.request(app, fields)
        try:
            packet = self.service.context(request)
        except ValueError:
            return {
                f["key"]: {
                    "source": "question",
                    "reason": {
                        "code": "evidence_conflict",
                        "message": "Career evidence is conflicting or unavailable. Review the records before answering.",
                    },
                }
                for f in fields
            }
        facts = {f["id"]: f for f in packet["facts"]}
        selected = set(app["payload"].get("package", {}).get("claim_fact_ids", []))
        context = sorted(
            facts.values(),
            key=lambda f: (f["id"] not in selected, f["kind"] != "skill"),
        )[:4]
        evidence = [
            {
                "fact_id": f["id"],
                "text": str(f["value"])[:500],
                "source": str(
                    f["provenance"].get("path")
                    or f["provenance"].get("source")
                    or "Confirmed career record"
                ),
            }
            for f in context
        ]
        results, eligible = {}, []
        for field in fields:
            reason = {
                "code": "evidence_needed",
                "message": "The available records do not yet establish an answer to this question.",
                "context": evidence,
            }
            if narrative_choice_mismatch(field):
                reason.update(
                    code="narrative_choice_mismatch",
                    message="The employer asks for a narrative but only offers choices. Save your examples as application notes and explicitly choose the option the employer will receive. The agent cannot infer what Yes or No means for this prompt.",
                )
            elif personal_question(field) and not packet["personal_context_ids"]:
                reason.update(
                    code="personal_context_needed",
                    message="Your records describe your experience, but do not establish what personally interests or motivates you about this role. Tell us what appeals to you; the agent can help connect that with what you would bring.",
                )
            elif requires_explicit_answer(field):
                reason.update(
                    code="personal_commitment_needed",
                    message="This question requires your explicit answer for this application. General career experience cannot establish a commitment or resolve ambiguous authorization wording.",
                )
            else:
                eligible.append(field)
            results[field["key"]] = {"source": "question", "reason": reason}
        if not eligible:
            return results
        request = self.request(app, eligible)
        config = self.service.config
        if config.backend != "fixture" and not config.enabled:
            for field in eligible:
                results[field["key"]]["reason"]["message"] = (
                    "Answer preparation can use your confirmed career records once the configured model destination is enabled."
                )
            return results
        try:
            progress({"stage": "Preparing answers from confirmed career records"})
            prepared = self.service.run(request, job_id, progress, cancelled)
            unresolved = [
                a
                for a in prepared["output"]["answers"]
                if a["decision"] == "needs_input"
            ]
            versions = []
            # Retrieval is a fallback after the complete confirmed career packet.
            for answer in unresolved[:3]:
                if cancelled():
                    raise InferenceStopped("Application answer preparation cancelled")
                field = next(f for f in eligible if f["key"] == answer["key"])
                found = Corpus(self.db).retrieve(
                    field["label"],
                    app["candidate"],
                    kinds=["confirmation_record", "imported_claim"],
                    limit=2,
                    budget=6000,
                )
                results[field["key"]]["reason"]["sources_checked"] = [
                    {
                        "path": e["path"],
                        "version_id": e["version_id"],
                        "locators": [c["locator"] for c in e["context"]],
                        "warnings": e["warnings"],
                    }
                    for e in found["evidence"]
                ]
                versions.extend(e["version_id"] for e in found["evidence"])
            versions = list(dict.fromkeys(versions))[:3]
            if versions:
                request = self.request(app, eligible, versions)
                prepared = self.service.run(request, job_id, progress, cancelled)
            proposals = prepared["output"]
            if not any(a["decision"] != "needs_input" for a in proposals["answers"]):
                for answer in proposals["answers"]:
                    results[answer["key"]]["reason"]["message"] = answer["reason"]
                return results
            reviewed = self.service.run(
                self.request(app, eligible, versions, proposals),
                job_id,
                progress,
                cancelled,
            )
            checks = {c["key"]: c for c in reviewed["output"]["checks"]}
            for proposal in proposals["answers"]:
                key = proposal["key"]
                field = next(f for f in eligible if f["key"] == key)
                check = checks[key]
                if (
                    proposal["decision"] == "needs_input"
                    or not check["supported"]
                    or check["issue"]
                ):
                    results[key]["reason"]["message"] = (
                        check["issue"] or proposal["reason"]
                    )
                    continue
                # Quantitative claims need support in the cited career facts.
                numbers = re.findall(r"\b\d+(?:\.\d+)?\b", str(proposal["value"]))
                cited = json.dumps([facts[i]["value"] for i in proposal["fact_ids"]])
                if any(
                    n not in re.findall(r"\b\d+(?:\.\d+)?\b", cited) for n in numbers
                ):
                    results[key]["reason"]["message"] = (
                        "The proposed answer included a number unsupported by the cited career records."
                    )
                    continue
                answer = {
                    "value": proposal["value"],
                    "fact_ids": proposal["fact_ids"],
                    "version_ids": list(
                        dict.fromkeys(
                            versions
                            + [
                                v
                                for i in proposal["fact_ids"]
                                for v in facts[i]["evidence_versions"]
                            ]
                        )
                    ),
                    "scope": app["id"],
                    "source": "form_evidence",
                    "provenance": {
                        "type": "form_evidence",
                        "run_id": prepared["run_id"],
                        "review_run_id": reviewed["run_id"],
                        "source_refs": proposal["source_refs"],
                    },
                }
                manual = personal_question(field) or proposal["decision"] == "draft"
                if not self.valid(app, field, answer, draft=manual):
                    results[key]["reason"]["message"] = (
                        "Evidence changed during preparation. Prepare the answers again."
                    )
                    continue
                if manual:
                    results[key]["reason"].update(
                        code="draft_needs_review",
                        message="Review this draft against your own perspective before confirming it.",
                        suggestion=answer,
                    )
                else:
                    results[key] = answer
                    self.db.metric("form_answers_prepared")
        except InferenceStopped:
            raise
        except (ValueError, RuntimeError) as exc:
            self.db.audit(
                "form_answers.unavailable",
                app["id"],
                {"error_type": type(exc).__name__},
            )
            for field in eligible:
                if results[field["key"]].get("source") == "question":
                    results[field["key"]]["reason"]["message"] = (
                        "Answer preparation could not verify a response. Your confirmation is needed; you can retry preparation from your records."
                    )
        return results
