"""Shared career knowledge and explicit abstention on personal questions."""

import pytest
from playwright.async_api import async_playwright, expect

from jobsearch.corpus import Corpus
from jobsearch.form_answers import FormAnswers
from jobsearch.forms import FieldSpec, FormSpec, FormPreparation, field_key
from jobsearch.llm.contracts import InferenceRequest
from jobsearch.llm.service import InferenceService
from jobsearch.llm.runner import InferenceFailure, InferenceStopped
from jobsearch.memory import Memory, FactInput
from jobsearch.workflow import Workflow
from .test_llm import application
from .test_browser import fixture_server  # noqa: F401

PERSONAL = "What interests you about this role, and what would you bring to the team?"


def field(label, material=False):
    return FieldSpec(
        key=field_key("custom", label, []),
        name="custom",
        label=label,
        type="textarea",
        required=True,
        qualification=False,
        material=material,
    ).model_dump()


def confirmed(db, key="api_years", value="5", kind="skill_duration", scope="global"):
    return Memory(db).put(
        FactInput(
            key=key,
            value=value,
            kind=kind,
            scope=scope,
            status="confirmed",
            provenance={"type": "user_confirmation"},
        )
    )


def evaluator(monkeypatch, fact, value="5", decision="answer", supported=True):
    original = InferenceService.fixture

    def fixture(self, request, packet):
        if request.task == "form_answers":
            return {
                "answers": [
                    {
                        "key": f["key"],
                        "decision": decision,
                        "value": value,
                        "fact_ids": [fact["id"]],
                        "source_refs": [],
                        "reason": "Supported by the cited confirmed record.",
                    }
                    for f in packet["form_fields"]
                ]
            }
        if request.task == "form_answer_review":
            return {
                "checks": [
                    {
                        "key": f["key"],
                        "supported": supported,
                        "issue": "" if supported else "The claim exceeds the evidence.",
                    }
                    for f in packet["form_fields"]
                ]
            }
        return original(self, request, packet)

    monkeypatch.setattr(InferenceService, "fixture", fixture)


def test_form_task_has_resume_writer_career_context_but_not_other_role_commitments(db):
    app = application(db)
    confirmed(db, "salary", "private-other-role", "answer", "other-role")
    service = InferenceService(db)
    resume = service.context(
        InferenceRequest(task="package_draft", application_id=app["id"])
    )
    request = FormAnswers(db).request(app, [field("Describe your API experience")])
    form = service.context(request)
    assert {f["id"] for f in resume["facts"]} <= {f["id"] for f in form["facts"]}
    assert "private-other-role" not in str(form)
    assert any(s["kind"] == "employer_source" for s in form["sources"])


def test_personal_question_abstains_even_with_resume_and_generated_motivation(
    db, monkeypatch
):
    app = application(db)
    path = db.settings.fixture_workspace / "Generated_Cover_Letter.md"
    path.write_text("I have always been passionate about this company's mission.")
    Corpus(db).ingest(path.name, "fixture", kind="generated_draft")
    monkeypatch.setattr(
        InferenceService,
        "run",
        lambda *a, **k: pytest.fail(
            "Career fit cannot supply missing personal motives"
        ),
    )
    service = FormAnswers(db)
    spec = field(PERSONAL)
    result = service.prepare(app, [spec])[spec["key"]]
    assert result["reason"]["code"] == "personal_context_needed"
    assert result["reason"]["context"]
    assert "suggestion" not in result["reason"] and "value" not in result
    assert not db.all("SELECT * FROM llm_runs")


def test_model_cannot_label_career_experience_as_personal_interest(db):
    app = application(db)
    service = InferenceService(db)
    packet = service.context(FormAnswers(db).request(app, [field(PERSONAL)]))
    output = {
        "answers": [
            {
                "key": packet["form_fields"][0]["key"],
                "decision": "draft",
                "value": "I have always loved your mission.",
                "fact_ids": [packet["facts"][0]["id"]],
                "source_refs": [],
                "reason": "Career fit",
            }
        ]
    }
    with pytest.raises(InferenceFailure, match="Personal motivation"):
        service.validate("form_answers", output, packet)


def test_factual_preparation_is_reviewed_reusable_and_invalidated_by_correction(
    db, monkeypatch
):
    app = application(db)
    fact = confirmed(db)
    evaluator(monkeypatch, fact)
    service = FormAnswers(db)
    spec = field("How many years of API experience do you have?")
    before = db.one("SELECT count(*) n FROM facts")["n"]
    result = service.prepare(app, [spec])[spec["key"]]
    assert result["source"] == "form_evidence"
    assert result["value"] == "5" and result["fact_ids"] == [fact["id"]]
    assert service.valid(app, spec, result)
    assert not service.valid(app, spec, result | {"value": "25"})
    assert db.one("SELECT count(*) n FROM facts")["n"] == before
    again = service.prepare(app, [spec])[spec["key"]]
    assert again == result and len(db.all("SELECT * FROM llm_runs")) == 2
    flow = Workflow(db)
    flow.update_payload(
        app["id"], {"form_schema": [spec], "answers": {spec["key"]: result}}
    )
    assert (
        flow.review_payload(flow.get(app["id"]))["answers"][spec["key"]]["value"] == "5"
    )
    assert not service.valid(app, spec | {"material": True}, result)
    confirmed(
        db, value="6"
    )  # A conflicting confirmation cannot silently replace the cited value.
    assert not service.valid(flow.get(app["id"]), spec, result)
    with pytest.raises(ValueError, match="evidence changed"):
        flow.review_payload(flow.get(app["id"]))


def test_known_personal_context_produces_reviewable_draft_not_confirmed_answer(
    db, monkeypatch
):
    app = application(db)
    fact = confirmed(
        db,
        "personal_interests",
        "I enjoy helping customers understand technical options.",
        "preference",
    )
    evaluator(monkeypatch, fact, value=fact["value"], decision="draft")
    spec = field(PERSONAL)
    result = FormAnswers(db).prepare(app, [spec])[spec["key"]]
    assert result["source"] == "question"
    assert result["reason"]["code"] == "draft_needs_review"
    draft = result["reason"]["suggestion"]
    assert FormAnswers(db).valid(app, spec, draft, draft=True)
    assert not FormAnswers(db).valid(app, spec, draft)
    assert not db.all("SELECT * FROM facts WHERE key=%s", (spec["key"],))


def test_failed_evidence_review_cannot_prepare_an_answer(db, monkeypatch):
    app = application(db)
    evaluator(monkeypatch, confirmed(db), supported=False)
    spec = field("How many years of API experience do you have?")
    result = FormAnswers(db).prepare(app, [spec])[spec["key"]]
    assert result["source"] == "question"
    assert "exceeds the evidence" in result["reason"]["message"]
    assert "suggestion" not in result["reason"]


def test_confirmed_personal_answer_supplies_context_only_within_its_application(db):
    app = application(db)
    spec = field(PERSONAL)
    flow = Workflow(db)
    flow.update_payload(app["id"], {"form_schema": [spec]})
    q = flow.ask(app["id"], spec["key"], spec["label"])
    result = flow.answer(
        q["id"], "I want to help customers understand technical options."
    )
    revised_question = field("What motivates you to apply?")
    service = InferenceService(db)
    packet = service.context(FormAnswers(db).request(app, [revised_question]))
    assert result["fact_id"] in packet["personal_context_ids"]
    other = flow.create("Other Demo", "Engineer", "OTHER")
    other_packet = service.context(FormAnswers(db).request(other, [revised_question]))
    assert result["fact_id"] not in other_packet["personal_context_ids"]


def test_role_specific_factual_question_can_use_career_evidence(db, monkeypatch):
    app = application(db)
    fact = confirmed(db, "api_example", "Built customer APIs.", "skill")
    evaluator(monkeypatch, fact, value=fact["value"])
    spec = field("What would you bring to our team?")
    result = FormAnswers(db).prepare(app, [spec])[spec["key"]]
    assert result["source"] == "form_evidence" and result["scope"] == app["id"]


def test_planner_reuses_factual_evidence_after_restart_and_keeps_personal_handoff(
    db, monkeypatch
):
    app = application(db)
    fact = confirmed(db)
    evaluator(monkeypatch, fact)
    factual, personal = (
        field("How many years of API experience do you have?"),
        field(PERSONAL),
    )
    spec = FormSpec(
        adapter="lever",
        url=db.settings.origin + "/fixture/form",
        requisition=app["requisition"],
        fields=[FieldSpec(**factual), FieldSpec(**personal)],
    )
    db.execute("UPDATE applications SET state='filling' WHERE id=%s", (app["id"],))
    first = FormPreparation(db).plan_spec(app["id"], spec)
    assert first["state"] == "missing_answers"
    saved = Workflow(db).get(app["id"])
    assert saved["payload"]["answers"][factual["key"]]["value"] == "5"
    assert personal["key"] not in saved["payload"]["answers"]
    assert [q["key"] for q in db.all("SELECT * FROM questions")] == [personal["key"]]
    assert len(db.all("SELECT * FROM llm_runs")) == 2
    FormPreparation(db).plan_spec(app["id"], spec)
    assert len(db.all("SELECT * FROM llm_runs")) == 2
    assert not db.all("SELECT * FROM approvals")
    assert not db.all("SELECT * FROM attempts")


def test_retrieval_is_fallback_and_keeps_document_references(db, monkeypatch):
    app = application(db)
    path = db.settings.fixture_workspace / "Career_Notes.md"
    path.write_text(
        "# Career notes\nFrobulator workflow experience requires confirmation."
    )
    imported = Corpus(db).ingest(path.name, "fixture", kind="imported_claim")
    spec = field("Describe your frobulator workflow experience")
    result = FormAnswers(db).prepare(app, [spec])[spec["key"]]
    assert result["source"] == "question"
    assert any(
        s["version_id"] == imported["version"]
        for s in result["reason"]["sources_checked"]
    )
    assert len(db.all("SELECT * FROM llm_runs WHERE task='form_answers'")) == 2
    assert not db.all(
        "SELECT * FROM facts WHERE value::text LIKE %s", ("%Frobulator%",)
    )


def test_cancelled_answer_preparation_never_starts_inference(db):
    app = application(db)
    with pytest.raises(InferenceStopped):
        FormAnswers(db).prepare(app, [field("API experience")], cancelled=lambda: True)
    assert not db.all("SELECT * FROM llm_runs")


@pytest.mark.usefixtures("fixture_server")
async def test_personal_question_handoff_shows_shared_evidence_and_blank_answer(db):
    app = application(db)
    from jobsearch.fixtures import assessment

    flow = Workflow(db)
    flow.score(app["id"], assessment(db))
    flow.update_payload(app["id"], {"package": {"files": []}})
    spec = FormSpec(
        adapter="lever",
        url=db.settings.origin + "/fixture/form",
        requisition=app["requisition"],
        fields=[FieldSpec(**field(PERSONAL))],
    )
    db.execute("UPDATE applications SET state='filling' WHERE id=%s", (app["id"],))
    result = FormPreparation(db).plan_spec(app["id"], spec)
    assert result["state"] == "missing_answers"
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium", headless=True
        )
        page = await browser.new_page()
        try:
            await page.goto(db.settings.origin + "/#token=" + db.settings.token())
            await page.get_by_label("Workspace", exact=True).select_option("fixture")
            await page.get_by_role("button", name="Applications", exact=True).click()
            await page.get_by_text("Evaluation Demo", exact=True).click()
            await expect(
                page.get_by_text("YOUR PERSONAL PERSPECTIVE IS NEEDED", exact=True)
            ).to_be_visible()
            await expect(
                page.get_by_role("textbox", name=PERSONAL, exact=True)
            ).to_have_value("")
            await page.get_by_text(
                "Career evidence available to the agent", exact=True
            ).click()
            await expect(
                page.locator("section.question details li").first
            ).to_be_visible()
            await expect(
                page.get_by_role("button", name="Go to questions", exact=True)
            ).to_be_visible()
            assert not db.all("SELECT * FROM attempts")
        finally:
            await browser.close()
