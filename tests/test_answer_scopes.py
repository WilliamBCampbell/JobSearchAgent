import pytest
from playwright.async_api import async_playwright, expect

from jobsearch.answer_scopes import allows_global
from jobsearch.forms import FieldSpec, field_key
from jobsearch.memory import Memory, FactInput
from jobsearch.workflow import Workflow
from jobsearch.llm.contracts import InferenceRequest
from jobsearch.llm.service import InferenceService
from .test_llm import application
from .test_browser import fixture_server  # noqa: F401


def field(label="How many years have you used Python?", material=False):
    return FieldSpec(
        key=field_key("custom", label, []),
        name="custom",
        label=label,
        type="text",
        required=True,
        qualification=True,
        material=material,
    ).model_dump()


def question(db, app, spec):
    flow = Workflow(db)
    flow.update_payload(app["id"], {"form_schema": [spec]})
    return flow.ask(app["id"], spec["key"], spec["label"])


def test_global_form_answer_reuse_correction_and_local_override(db, monkeypatch):
    first = application(db)
    second = Workflow(db).create("Second Demo", "Engineer", "SECOND")
    third = Workflow(db).create("Third Demo", "Engineer", "THIRD")
    spec = field()
    q1, q2 = question(db, first, spec), question(db, second, spec)
    flow = Workflow(db)
    local = flow.answer(q1["id"], "3", "application")["fact_id"]
    shared = flow.answer(q1["id"], "4", "global")["fact_id"]
    assert not db.one("SELECT active FROM facts WHERE id=%s", (local,))["active"]
    assert not db.all(
        "SELECT * FROM answer_cache WHERE valid AND fact_ids @> %s::jsonb",
        ('["' + local + '"]',),
    )
    monkeypatch.setattr(
        "jobsearch.corpus.Corpus.retrieve",
        lambda *a, **k: pytest.fail("Confirmed answers must bypass RAG"),
    )
    memory = Memory(db)
    for app in (first, second, third):
        assert memory.resolve(spec["key"], "fixture", app["id"])["value"] == "4"
    shared_answer = memory.resolve(spec["key"], "fixture", first["id"])
    with pytest.raises(ValueError, match="scope"):
        memory.cache("salary", "fixture", first["id"], shared_answer)
    flow.answer(q2["id"], "2", "application")
    assert memory.resolve(spec["key"], "fixture", second["id"])["value"] == "2"
    flow.answer(q1["id"], "5", "global")
    assert not db.one("SELECT active FROM facts WHERE id=%s", (shared,))["active"]
    assert memory.resolve(spec["key"], "fixture", third["id"])["value"] == "5"
    assert memory.resolve(spec["key"], "fixture", second["id"])["value"] == "2"
    payload = flow.review_payload(flow.get(first["id"]))
    assert payload["answers"][spec["key"]]["scope"] == "global"
    packet = InferenceService(db).context(
        InferenceRequest(task="fit_assessment", application_id=first["id"])
    )
    assert any(f["key"] == spec["key"] and f["value"] == "5" for f in packet["facts"])
    flow.update_payload(first["id"], {"form_schema": [spec | {"material": True}]})
    with pytest.raises(ValueError, match="scope"):
        flow.review_payload(flow.get(first["id"]))


@pytest.mark.parametrize(
    "label",
    [
        "Salary target",
        "Travel up to 10%?",
        "Relocation commitment",
        "May we contact your references?",
        "I certify these answers are accurate",
        "Why are you interested in this role?",
        "When are you available to start?",
        "Can you work onsite?",
        "Do you consent to background checks?",
    ],
)
def test_material_answers_cannot_be_made_global(db, label):
    app = application(db)
    spec = field(label)
    q = question(db, app, spec)
    assert not allows_global(spec["key"], spec)
    with pytest.raises(ValueError, match="application scope"):
        Workflow(db).answer(q["id"], "Yes", "global")
    with pytest.raises(ValueError, match="application scope"):
        FactInput(
            key=spec["key"],
            value="Yes",
            status="confirmed",
            scope="global",
            provenance={"type": "user_confirmation", "global_form_field": spec},
        )
    assert not db.all("SELECT * FROM facts WHERE key=%s", (spec["key"],))


def test_global_reuse_requires_matching_question_evidence():
    spec = field()
    assert allows_global(spec["key"], spec)
    assert not allows_global(spec["key"], spec | {"label": "A different question"})
    assert not allows_global(spec["key"], spec | {"options": ["Yes", "No"]})
    assert not allows_global(spec["key"], spec | {"material": True})
    assert not allows_global(spec["key"])


def test_confirmed_work_status_is_reusable_without_granting_authority(db, monkeypatch):
    app = application(db)
    spec = field(
        "Do you require or will you require a work authorization?", material=True
    )
    q = question(db, app, spec)
    assert allows_global(spec["key"], spec)
    Workflow(db).answer(q["id"], "No", "global")
    monkeypatch.setattr(
        "jobsearch.corpus.Corpus.retrieve",
        lambda *a, **k: pytest.fail("Confirmed work status must bypass retrieval"),
    )
    answer = Memory(db).resolve(spec["key"], "fixture", "another-application")
    assert answer["value"] == "No" and answer["scope"] == "global"
    assert (
        Workflow(db).review_payload(Workflow(db).get(app["id"]))["answers"][
            spec["key"]
        ]["value"]
        == "No"
    )
    assert not db.all("SELECT * FROM approvals")
    assert not db.all("SELECT * FROM attempts")


@pytest.mark.parametrize(
    "label",
    [
        "Do you authorize us to submit this application?",
        "Do you authorize a background check?",
        "Do you require or will you require a work authorization? I consent to submission.",
        "Do you require or will you require a work authorization in Canada?",
    ],
)
def test_work_status_exception_does_not_cover_other_authorization(label):
    spec = field(label, material=True)
    assert not allows_global(spec["key"], spec)


@pytest.mark.usefixtures("fixture_server")
async def test_radio_scope_selection_persists_global_and_shows_scope_limits(db):
    app = application(db)
    spec = field()
    q = question(db, app, spec)
    second = field(
        "Do you require or will you require a work authorization?", material=True
    )
    third = field("How many years have you used JavaScript?")
    travel = field("Travel up to 10%?")
    Workflow(db).update_payload(
        app["id"], {"form_schema": [spec, second, third, travel]}
    )
    remaining = {
        f["key"]: Workflow(db).ask(app["id"], f["key"], f["label"])
        for f in (second, third, travel)
    }
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium", headless=True
        )
        page = await browser.new_page()
        try:
            await page.goto(db.settings.origin + "/#token=" + db.settings.token())
            await page.get_by_role(
                "button", name="Privacy & settings", exact=True
            ).click()
            await page.get_by_label("Data mode", exact=True).select_option("fixture")
            await page.get_by_role("button", name="Applications", exact=True).click()
            await page.get_by_text("Evaluation Demo", exact=True).click()
            # These questions belong to an earlier workflow step in this fixture.
            await expect(
                page.locator('[data-workflow-section="question"] > summary')
            ).to_have_count(4)
            for summary in await page.locator(
                '[data-workflow-section="question"] > summary'
            ).all():
                await summary.click()
            panel = page.locator("section.question").filter(
                has=page.get_by_role("heading", name=spec["label"], exact=True)
            )
            scope = panel.get_by_role("group", name="Use this answer for")
            await expect(scope.get_by_role("radio")).to_have_count(2)
            await expect(
                scope.get_by_role("radio", name="This application only")
            ).to_be_checked()
            await scope.get_by_role("radio", name="Global", exact=True).check()
            await scope.get_by_role("radio", name="This application only").check()
            await scope.get_by_role("radio", name="Global", exact=True).check()
            await panel.get_by_role("textbox").fill("5")
            async with page.expect_response(
                lambda r: r.url.endswith("/questions/" + q["id"] + "/answer")
            ) as pending:
                await panel.get_by_role("button", name="Confirm answer").click()
            assert (await pending.value).status == 200
            answer = Memory(db).resolve(spec["key"], "fixture", "another-application")
            assert answer["scope"] == "global" and answer["value"] == "5"
            for f, choice, expected_scope, value in (
                (second, "Global", "global", "No"),
                (third, "This application only", app["id"], "4"),
            ):
                following = page.locator("section.question").filter(
                    has=page.get_by_role("heading", name=f["label"], exact=True)
                )
                scope = following.get_by_role("group", name="Use this answer for")
                await scope.get_by_role("radio", name="Global", exact=True).check()
                await scope.get_by_role("radio", name="This application only").check()
                await scope.get_by_role("radio", name=choice, exact=True).check()
                await following.get_by_role("textbox").fill(value)
                qid = remaining[f["key"]]["id"]
                async with page.expect_response(
                    lambda r: r.url.endswith("/questions/" + qid + "/answer")
                ) as pending:
                    await following.get_by_role("button", name="Confirm answer").click()
                assert (await pending.value).status == 200
                answer = Memory(db).resolve(f["key"], "fixture", app["id"])
                assert answer["scope"] == expected_scope and answer["value"] == value
            blocked = page.locator("section.question").filter(
                has=page.get_by_role("heading", name=travel["label"], exact=True)
            )
            await expect(blocked.get_by_role("radio")).to_have_count(0)
            await expect(
                blocked.get_by_role("note", name="Answer scope")
            ).to_contain_text("This application only")
            await expect(
                blocked.get_by_text(
                    "This answer depends on the employer or role", exact=False
                )
            ).to_be_visible()
            await blocked.get_by_role("textbox").fill("Yes")
            travel_id = remaining[travel["key"]]["id"]
            async with page.expect_response(
                lambda r: r.url.endswith("/questions/" + travel_id + "/answer")
            ) as pending:
                await blocked.get_by_role("button", name="Confirm answer").click()
            assert (await pending.value).status == 200
            assert (
                Memory(db).resolve(travel["key"], "fixture", app["id"])["scope"]
                == app["id"]
            )
            assert not db.all("SELECT * FROM attempts")
        finally:
            await browser.close()
