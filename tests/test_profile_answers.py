import pytest
from playwright.async_api import async_playwright, expect

from jobsearch.forms import FieldSpec, FormSpec, FormPreparation, field_key
from jobsearch.memory import Memory, FactInput
from jobsearch.packages import Packages
from jobsearch.profile_answers import ProfileAnswers, mapping
from jobsearch.workflow import Workflow
from .test_llm import application
from .test_browser import fixture_server  # noqa: F401


def field(label, name="custom", options=None, required=False):
    options = options or []
    return FieldSpec(
        key=field_key(name, label, options),
        name=name,
        label=label,
        type="select" if options else "text",
        options=options,
        required=required,
    ).model_dump()


def confirm(db, key, value, scope="global", supersedes=None):
    return Memory(db).put(
        FactInput(
            key=key,
            value=value,
            status="confirmed",
            kind="candidate",
            scope=scope,
            provenance={"type": "user_confirmation"},
            supersedes=supersedes,
        )
    )


def test_optional_profile_and_referral_fields_use_shared_memory_without_inference(
    db, monkeypatch
):
    app = application(db)
    confirm(db, "linkedin", "https://www.linkedin.com/in/fixture-candidate")
    confirm(db, "agent_employee_referral", False)
    assert (
        "https://www.linkedin.com/in/fixture-candidate"
        in Packages(db).profile(app)["contacts"]
    )
    fields = [
        field("LinkedIn Profile", name="question_42"),
        field(
            "If one of our Example team members referred you to this role, please let us know who:",
            name="question_43",
        ),
        field(
            "Were you referred by an employee?",
            name="question_44",
            options=["Yes", "No"],
        ),
        field("What interests you about this role?", name="question_45", required=True),
    ]
    monkeypatch.setattr(
        "jobsearch.corpus.Corpus.retrieve",
        lambda *a, **k: pytest.fail("Known profile data needs no retrieval"),
    )
    monkeypatch.setattr(
        "jobsearch.llm.service.InferenceService.run",
        lambda *a, **k: pytest.fail("Known profile data needs no model"),
    )
    db.execute("UPDATE applications SET state='filling' WHERE id=%s", (app["id"],))
    result = FormPreparation(db).plan_spec(
        app["id"],
        FormSpec(
            adapter="lever",
            url=db.settings.origin + "/fixture/form",
            requisition=app["requisition"],
            fields=[FieldSpec(**f) for f in fields],
        ),
    )
    assert result["questions"] == [fields[-1]["key"]]
    updated = Workflow(db).get(app["id"])
    answers = Workflow(db).review_payload(updated)["answers"]
    assert (
        answers[fields[0]["key"]]["value"]
        == "https://www.linkedin.com/in/fixture-candidate"
    )
    assert answers[fields[1]["key"]]["value"] == "Not applicable"
    assert answers[fields[2]["key"]]["value"] == "No"
    assert all(a["source"] == "profile_memory" for a in answers.values())
    assert not db.all("SELECT * FROM attempts")


def test_profile_mapping_rechecks_evidence_and_exact_field_before_review(db):
    app = application(db)
    fact = confirm(db, "linkedin", "https://www.linkedin.com/in/example")
    spec = field("LinkedIn Profile")
    resolver = ProfileAnswers(db)
    answer = resolver.resolve(app, spec)
    assert resolver.valid(app, spec, answer)
    assert not resolver.valid(app, spec, answer | {"value": "invented"})
    assert not resolver.valid(
        app, spec | {"label": "Your employee's LinkedIn Profile"}, answer
    )
    flow = Workflow(db)
    flow.update_payload(
        app["id"], {"form_schema": [spec], "answers": {spec["key"]: answer}}
    )
    confirm(
        db, "linkedin", "https://www.linkedin.com/in/corrected", supersedes=fact["id"]
    )
    assert not resolver.valid(app, spec, answer)
    with pytest.raises(ValueError, match="Profile answer evidence changed"):
        flow.review_payload(flow.get(app["id"]))


def test_referral_rule_requires_confirmation_and_honors_local_overrides(db):
    app = application(db)
    spec = field("Were you referred by an employee?", options=["Yes", "No"])
    resolver = ProfileAnswers(db)
    assert resolver.resolve(app, spec)["source"] == "question"
    confirm(db, "agent_employee_referral", False)
    assert resolver.resolve(app, spec)["value"] == "No"
    assert resolver.resolve(app | {"imported": True}, spec) is None
    confirm(db, "agent_employee_referral", True, scope=app["id"])
    assert resolver.resolve(app, spec) is None
    assert not db.all("SELECT * FROM approvals")


@pytest.mark.parametrize(
    "label",
    [
        "Your employee's LinkedIn profile",
        "LinkedIn Profile. I consent to marketing emails.",
        "How did you hear about us?",
        "May we contact your references?",
        "Legal name on government identification",
        "Were you referred by an employee? I authorize submission.",
    ],
)
def test_profile_aliases_do_not_guess_other_questions(label):
    assert mapping(field(label)) is None


def test_incompatible_referral_options_are_not_guessed(db):
    app = application(db)
    confirm(db, "agent_employee_referral", False)
    spec = field("Were you referred by an employee?", options=["Employee", "Partner"])
    assert ProfileAnswers(db).resolve(app, spec) is None


@pytest.mark.usefixtures("fixture_server")
async def test_change_profile_answer_scrolls_prefills_and_preserves_correction(db):
    app = application(db)
    from jobsearch.fixtures import assessment

    flow = Workflow(db)
    flow.score(app["id"], assessment(db))
    flow.update_payload(app["id"], {"package": {"files": []}})
    confirm(db, "agent_employee_referral", False)
    referral = field(
        "If one of our Example team members referred you to this role, please let us know who:"
    )
    fields = (
        [referral]
        + [field(f"Optional question {i}", name=f"custom_{i}") for i in range(20)]
        + [
            field(
                "What interests you about this role?", name="interests", required=True
            )
        ]
    )
    spec = FormSpec(
        adapter="lever",
        url=db.settings.origin + "/fixture/form",
        requisition=app["requisition"],
        fields=[FieldSpec(**f) for f in fields],
    )
    db.execute("UPDATE applications SET state='filling' WHERE id=%s", (app["id"],))
    FormPreparation(db).plan_spec(app["id"], spec)
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/usr/bin/chromium", headless=True
        )
        page = await browser.new_page(viewport={"width": 1200, "height": 700})
        try:
            await page.goto(db.settings.origin + "/#token=" + db.settings.token())
            await page.get_by_role("button", name="Privacy & settings", exact=True).click()
            await page.get_by_label("Data mode", exact=True).select_option("fixture")
            await page.get_by_role("button", name="Applications", exact=True).click()
            await page.get_by_text("Evaluation Demo", exact=True).click()
            row = page.get_by_role("row").filter(
                has=page.get_by_role("rowheader", name=referral["label"], exact=True)
            )
            await row.get_by_role("button", name="Change answer", exact=True).click()
            panel = page.locator("section.question").filter(
                has=page.get_by_role("heading", name=referral["label"], exact=True)
            )
            editor = panel.get_by_role("textbox")
            await expect(editor).to_have_value("Not applicable")
            await expect(editor).to_be_focused()
            await expect(editor).to_be_in_viewport()
            await expect(panel.get_by_text("EDIT ANSWER", exact=True)).to_be_visible()
            await editor.fill("No employee referral")
            # Reopening must bring the same draft back into view, not reset it.
            await row.get_by_role("button", name="Change answer", exact=True).click()
            await expect(editor).to_have_value("No employee referral")
            await expect(editor).to_be_focused()
            async with page.expect_response(
                lambda r: "/questions/" in r.url and r.url.endswith("/answer")
            ) as pending:
                await panel.get_by_role(
                    "button", name="Confirm answer", exact=True
                ).click()
            assert (await pending.value).status == 200
            await expect(row).to_contain_text("No employee referral")
            FormPreparation(db).plan_spec(app["id"], spec)
            assert (
                Workflow(db).get(app["id"])["payload"]["answers"][referral["key"]][
                    "value"
                ]
                == "No employee referral"
            )
            await row.get_by_role("button", name="Change answer", exact=True).click()
            await expect(editor).to_have_value("No employee referral")
            await expect(editor).to_be_focused()
            assert not db.all("SELECT * FROM attempts")
        finally:
            await browser.close()
