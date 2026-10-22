"""Synthetic native fieldsets and whitespace in saved question labels."""

from types import SimpleNamespace

import pytest
from playwright.async_api import async_playwright

from jobsearch.forms import FieldSpec, FormSpec
from jobsearch.greenhouse_browser import GreenhouseBrowser


@pytest.fixture
async def native_form():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path="/usr/bin/chromium", headless=True
        )
        page = await browser.new_page()
        await page.set_content("""<form id="application-form">
            <label for="question_1">Portfolio link:</label><textarea id="question_1"></textarea>
            <fieldset id="question_2[]" aria-required="true">
              <legend>Coding tools <span>*</span></legend>
              <input type="checkbox" id="option_a" name="question_2[]" required value="11"><label for="option_a">Editor</label>
              <input type="checkbox" id="option_b" name="question_2[]" required value="12"><label for="option_b">Assistant</label>
              <input type="checkbox" id="option_c" name="question_2[]" required value="13"><label for="option_c">Other</label>
            </fieldset><button type="button">Submit application</button></form>""")
        spec = FormSpec(
            adapter="greenhouse",
            url="http://example.invalid",
            requisition="test",
            fields=[
                FieldSpec(
                    key="portfolio",
                    name="question_1",
                    label="Portfolio link: \n",
                    type="textarea",
                ),
                FieldSpec(
                    key="tools",
                    name="question_2[]",
                    label="Coding tools ",
                    type="checkbox",
                    required=True,
                    options=["Editor", "Assistant", "Other"],
                ),
            ],
        )
        adapter = GreenhouseBrowser(SimpleNamespace())
        try:
            yield adapter, page, spec
        finally:
            await browser.close()


async def test_native_group_fills_only_confirmed_choices_and_checks_them(
    native_form, monkeypatch
):
    adapter, page, spec = native_form
    await adapter.validate_dom(page, spec)
    # The browser's whitespace comparison must not rewrite the approval contract.
    assert spec.fields[0].label == "Portfolio link: \n"
    monkeypatch.setattr(adapter, "active", lambda *args, **kwargs: None)
    monkeypatch.setattr(adapter.flow, "get", lambda *args: {"revision": 1})
    adapter.sessions["synthetic"] = {"page": page, "gate": {"phase": "inspect"}}
    review = {
        "payload": {
            "answers": {
                "portfolio": {"value": "https://example.invalid/project"},
                "tools": {"value": ["Assistant", "Editor"]},
            }
        }
    }
    # A preselected unreviewed option must be cleared.
    await page.locator("#option_c").check()
    await adapter.fill_values("synthetic", page, spec, review)
    assert await adapter.values_match(page, spec, review)
    assert not await page.locator("#option_c").is_checked()
    await page.locator("#option_c").check()
    assert not await adapter.values_match(page, spec, review)


@pytest.mark.parametrize(
    "change",
    ["wording", "required", "option", "extra", "renamed_input", "unknown_required"],
)
async def test_native_changes_still_block_filling(native_form, change):
    adapter, page, spec = native_form
    if change == "wording":
        await page.locator('label[for="question_1"]').evaluate(
            "e=>e.textContent='Different question'"
        )
    elif change == "required":
        await page.locator("fieldset").evaluate(
            "e=>e.setAttribute('aria-required','false')"
        )
    elif change == "option":
        await page.locator('label[for="option_a"]').evaluate(
            "e=>e.textContent='Different choice'"
        )
    elif change == "extra":
        await page.locator("fieldset").evaluate(
            "e=>e.insertAdjacentHTML('beforeend','<input type=checkbox id=extra name=\"question_2[]\"><label for=extra>Extra</label>')"
        )
    elif change == "renamed_input":
        await page.locator("#option_a").evaluate("e=>e.name='unreviewed'")
    else:
        await page.locator("form").evaluate(
            "e=>e.insertAdjacentHTML('beforeend','<input id=unknown required>')"
        )
    with pytest.raises(ValueError, match="employer"):
        await adapter.validate_dom(page, spec)


async def test_character_limits_stop_before_filling_and_count_utf16(native_form):
    adapter, page, spec = native_form
    await page.locator("#question_1").evaluate("e=>e.maxLength=3")
    review = {"payload": {"answers": {"portfolio": {"value": "AB😀"}}}}
    limits = await adapter.text_limit_violations(page, spec, review)
    assert [(limit, length) for _, limit, length in limits] == [(3, 4)]
    with pytest.raises(ValueError, match="at most 3 characters"):
        await adapter.fill_values("synthetic", page, spec, review)
    assert await page.locator("#question_1").input_value() == ""


def test_answer_confirmation_enforces_discovered_character_limit(db):
    from jobsearch.workflow import Workflow

    flow = Workflow(db)
    app = flow.create("Example", "Engineer", "LIMIT-1")
    q = flow.ask(
        app["id"],
        "portfolio",
        "Portfolio",
        {
            "reason": {
                "code": "answer_too_long",
                "max_length": 3,
                "message": "Use at most three characters.",
            }
        },
    )
    with pytest.raises(ValueError, match="at most 3 characters"):
        flow.answer(q["id"], "AB😀")
    assert not db.all("SELECT * FROM facts")
