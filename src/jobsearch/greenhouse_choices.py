"""Native Greenhouse self-identification controls; no inferred choices."""

import json
from .self_identification import choices, LEAVE_BLANK


def control(page, name):
    return page.locator("#application-form [id=" + json.dumps(name) + "]")


async def dropdown_options(page, c):
    if await c.evaluate("e=>e.tagName") == "SELECT":
        return [
            s.strip()
            for s in await c.locator("option").all_text_contents()
            if s.strip() and s.strip() != "Select..."
        ]
    if await c.get_attribute("role") != "combobox":
        raise ValueError("The employer changed a self-identification dropdown")
    await c.click()
    await page.get_by_role("option").first.wait_for()
    options = await page.get_by_role("option").all_text_contents()
    await c.press("Escape")
    return options


async def validate(page, f):
    name = "hispanic_ethnicity" if f.binding == "eeoc" and f.name == "race" else f.name
    if f.binding == "data_compliance":
        name += "_1"
    c = control(page, name)
    if await c.count() != 1:
        raise ValueError("The employer changed this control: " + f.label)
    info = await c.evaluate("""e=>({label:(document.querySelector('label[for="'+e.id+'"]')?.textContent || '').replace(/\\*/g,'').trim(),
        required:e.required || e.getAttribute('aria-required')==='true',type:e.type})""")
    label = "Are you Hispanic/Latino?" if name == "hispanic_ethnicity" else f.label
    if info["label"] != label or info["required"] != f.required:
        raise ValueError("The employer changed this question: " + f.label)
    if f.binding == "data_compliance":
        if info["type"] != "checkbox":
            raise ValueError("The employer changed the consent control")
        return
    expected = (
        ["Yes", "No", "Decline To Self Identify"]
        if name == "hispanic_ethnicity"
        else f.options
    )
    actual = await dropdown_options(page, c)
    # Greenhouse sorts EEOC options differently from its public API.
    if sorted(actual) != sorted(expected):
        raise ValueError("The employer changed the choices: " + f.label)


async def fill(browser, page, f, value):
    selected = choices(f, value)
    if f.binding == "data_compliance":
        await control(page, f.name + "_1").set_checked(bool(selected))
        return
    if not selected:
        return  # Every review/send uses a fresh, empty browser context.
    if f.binding == "eeoc" and f.name == "race":
        option = str(f.option_ids[selected[0]])
        parent = control(page, "hispanic_ethnicity")
        await browser.select(
            page,
            parent,
            {"4": "Yes", "8": "Decline To Self Identify"}.get(option, "No"),
        )
        if option in ("4", "8"):
            return
        c = control(page, "race")
        actual = await dropdown_options(page, c)
        expected = [label for label, oid in f.option_ids.items() if str(oid) != "4"]
        if sorted(actual) != sorted(expected):
            raise ValueError("The employer changed the race choices")
    c = control(page, f.name)
    if f.type == "multiselect" and await c.evaluate("e=>e.tagName") == "SELECT":
        await c.select_option(label=selected)
    else:
        for label in selected:
            await browser.select(page, c, label)
    if isinstance(value, dict):
        extra = control(page, f.name + "-freeform")
        if await extra.count() != 1 or await extra.get_attribute("type") not in (
            None,
            "text",
        ):
            raise ValueError("The employer changed the self-description control")
        await extra.fill(value["text"])


async def read(browser, page, f):
    if f.binding == "data_compliance":
        return ["I consent"] if await control(page, f.name + "_1").is_checked() else []
    if f.binding == "eeoc" and f.name == "race":
        parent = control(page, "hispanic_ethnicity")
        text = await browser.value(parent, f)
        if text == "":
            return LEAVE_BLANK
        oid = {"Yes": "4", "Decline To Self Identify": "8"}.get(text)
        if oid:
            return next(
                label for label, value in f.option_ids.items() if str(value) == oid
            )
        if text != "No":
            return None
    c = control(page, f.name)
    if f.type == "multiselect":
        selected = await c.evaluate(
            "e=>e.tagName==='SELECT' ? [...e.selectedOptions].map(x=>x.textContent) : [...e.closest('.select__value-container').querySelectorAll('.select__multi-value__label')].map(x=>x.textContent)"
        )
        value = selected
    else:
        value = await browser.value(c, f)
        selected = [value] if value else []
        if not selected:
            value = LEAVE_BLANK
    if set(selected) & set(f.free_form_options):
        return {
            "choices": selected,
            "text": await control(page, f.name + "-freeform").input_value(),
        }
    return value


def matches(f, actual, expected):
    if actual is None:
        return False
    return sorted(choices(f, actual)) == sorted(choices(f, expected)) and (
        not isinstance(expected, dict)
        or isinstance(actual, dict)
        and actual.get("text") == expected.get("text")
    )
