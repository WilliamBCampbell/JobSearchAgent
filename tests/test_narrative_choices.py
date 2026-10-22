# ruff: noqa: F811
import pytest
from fastapi.testclient import TestClient
from playwright.async_api import async_playwright, expect
from jobsearch.api import create_app
from jobsearch.forms import greenhouse_form
from jobsearch.form_policy import narrative_choice_mismatch, narrative_context_key
from jobsearch.form_answers import FormAnswers
from jobsearch.llm.service import InferenceService
from jobsearch.memory import Memory
from jobsearch.queue import Worker, enqueue
from .test_interventions import pending_plan
from .test_form_evidence import confirmed, evaluator
from .test_browser import fixture_server  # noqa: F401

PROMPT = "Describe the agentic AI projects you built and provide examples of your contributions and what you can demonstrate in an interview."
DRAFT = "I built a local API assistant and can demonstrate it with synthetic data."


def pending(db):
    flow, aid, _ = pending_plan(db)
    spec = greenhouse_form(
        {
            "questions": [
                {
                    "label": PROMPT,
                    "required": True,
                    "fields": [
                        {
                            "name": "question_demo",
                            "type": "multi_value_single_select",
                            "values": [
                                {"label": "Yes", "value": 1},
                                {"label": "No", "value": 0},
                            ],
                        }
                    ],
                }
            ]
        },
        "https://job-boards.greenhouse.io/example/jobs/1",
        "1",
    )
    field = spec.fields[0].model_dump()
    flow.update_payload(
        aid, {"form_schema": [field], "form_plan": {"spec": spec.model_dump()}}
    )
    question = flow.ask(
        aid, field["key"], PROMPT, {"message": "Only Yes or No available"}
    )
    flow.transition(aid, "missing_answers")
    return flow, aid, field, question["id"]


def test_narrative_intent_is_independent_of_employer_control(db, monkeypatch):
    flow, aid, field, _ = pending(db)
    assert narrative_choice_mismatch(field) and not field["qualification"]
    assert field["options"] == ["Yes", "No"] and field["required"]
    assert not narrative_choice_mismatch(
        field | {"label": "Have you implemented production agents?"}
    )
    assert not narrative_choice_mismatch(
        field | {"label": "Describe your minimum 5 years of professional C# experience"}
    )
    monkeypatch.setattr(
        InferenceService,
        "run",
        lambda *a, **k: pytest.fail("Do not infer a portal choice"),
    )
    result = FormAnswers(db).prepare(flow.get(aid), [field])[field["key"]]
    assert result["source"] == "question" and "value" not in result
    assert result["reason"]["code"] == "narrative_choice_mismatch"


def test_notes_saved_separately_and_never_coerced_to_portal_answer(db):
    flow, aid, field, qid = pending(db)
    with pytest.raises(ValueError, match="available employer option"):
        flow.answer(qid, DRAFT, context=DRAFT)
    assert not db.all(
        "SELECT * FROM facts WHERE key=%s", (narrative_context_key(field),)
    )
    flow.answer(qid, "Yes", scope="global", context=DRAFT)
    payload = flow.get(aid)["payload"]
    assert payload["answers"][field["key"]]["value"] == "Yes"
    assert DRAFT not in str(payload)
    fact = db.one("SELECT * FROM facts WHERE key=%s", (narrative_context_key(field),))
    assert fact["scope"] == aid and fact["value"] == DRAFT
    assert (
        Memory(db).resolve(fact["key"], "fixture", "other-role", retrieve=False)[
            "source"
        ]
        == "question"
    )
    with TestClient(create_app(db.settings)) as client:
        response = client.get(
            "/api/applications/" + aid, headers={"X-JSA-Token": db.settings.token()}
        )
        q = next(q for q in response.json()["questions"] if q["id"] == qid)
        assert q["answer_context"] == DRAFT and q["answer_guidance"]["choice_mismatch"]
    flow.answer(qid, "Yes", context="")
    assert (
        Memory(db).resolve(
            narrative_context_key(field), "fixture", aid, retrieve=False
        )["value"]
        == ""
    )


async def test_draft_uses_reviewed_evidence_without_confirming_or_filling(
    db, monkeypatch
):
    flow, aid, field, qid = pending(db)
    fact = confirmed(db, "agent_project", DRAFT, "candidate")
    evaluator(monkeypatch, fact, value=DRAFT, decision="draft")
    job = enqueue(
        db,
        "draft_answer_context",
        {"application_id": aid, "question_id": qid},
        max_attempts=1,
    )
    worker = Worker(db)
    try:
        await worker.run_once()
        stored = db.one("SELECT * FROM jobs WHERE id=%s", (job["job_id"],))
        assert stored["status"] == "complete", stored["error"]
        q = db.one("SELECT * FROM questions WHERE id=%s", (qid,))
        assert q["reason"]["context_suggestion"]["value"] == DRAFT
        assert q["answer_fact"] is None
        assert not flow.get(aid)["payload"].get("answers")
        assert not db.all("SELECT * FROM attempts")
        db.execute(
            "UPDATE facts SET status='superseded',active=false WHERE id=%s",
            (fact["id"],),
        )
        with TestClient(create_app(db.settings)) as client:
            response = client.get(
                "/api/applications/" + aid, headers={"X-JSA-Token": db.settings.token()}
            )
            q = next(q for q in response.json()["questions"] if q["id"] == qid)
            assert "context_suggestion" not in q["reason"]
    finally:
        await worker.browser.close()


async def test_narrative_editor_keeps_notes_and_portal_choice_separate(
    db, fixture_server
):
    flow, aid, field, qid = pending(db)
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium", headless=True
        )
        page = await browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            await page.goto(db.settings.origin + "/#token=" + db.settings.token())
            await page.get_by_role("button", name="Privacy & settings", exact=True).click()
            await page.get_by_label("Data mode", exact=True).select_option("fixture")
            await page.get_by_role("button", name="Applications", exact=True).click()
            await page.get_by_text("Intervention Demo", exact=True).click()
            notes = page.get_by_role(
                "textbox", name="Examples and interview-demo details", exact=True
            )
            await expect(notes).to_be_visible()
            await notes.fill(DRAFT)
            choice = page.get_by_role("combobox", name=PROMPT, exact=True)
            await expect(choice).to_have_value("")
            await expect(
                page.get_by_role(
                    "button", name="Draft examples from my records", exact=True
                )
            ).to_be_enabled()
            await choice.select_option("Yes")
            async with page.expect_response(
                lambda r: r.url.endswith("/answer")
            ) as response:
                await page.get_by_role(
                    "button", name="Confirm answer", exact=True
                ).click()
            assert (await response.value).status == 200
            assert flow.get(aid)["payload"]["answers"][field["key"]]["value"] == "Yes"
            assert (
                db.one(
                    "SELECT value FROM facts WHERE key=%s",
                    (narrative_context_key(field),),
                )["value"]
                == DRAFT
            )
            assert not errors
        finally:
            await browser.close()
