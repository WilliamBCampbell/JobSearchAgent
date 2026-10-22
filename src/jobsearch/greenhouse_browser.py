"""Greenhouse DOM adapter. Review staging has no outbound candidate traffic.

The employer's React file widget normally uploads immediately. During staging,
its upload metadata and transport are handled locally, with an explicit browser
banner. This is not an employer upload or submission receipt.
"""

from __future__ import annotations

import json
import re
import asyncio
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, TimeoutError as BrowserTimeout

from .config import digest
from .forms import FormPreparation
from .workflow import Workflow
from . import greenhouse_choices


FILE_HASHES = """async e=>Promise.all([...e.files].map(async f=>({
 name:f.name,hash:[...new Uint8Array(await crypto.subtle.digest(
 "SHA-256",await f.arrayBuffer()))].map(x=>x.toString(16).padStart(2,"0")).join("")
})))"""


class GreenhouseBrowser:
    def __init__(self, db):
        self.db = db
        self.flow = Workflow(db)
        self.sessions = {}
        self.playwright = None

    def save_handoff(self, aid, reason):
        from .browser import BrowserWorker

        return BrowserWorker(self.db).handoff(aid, reason)

    async def close(self):
        for session in self.sessions.values():
            await session["browser"].close()
        self.sessions.clear()
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None

    def active(self, aid, expected_hash=None, attempt_id=None, expected_revision=None):
        app = self.flow.get(aid)
        self.flow.editable(app)
        if app["paused"] or self.db.one(
            "SELECT 1 FROM jobs WHERE input->>'application_id'=%s AND status='running' AND cancel_requested",
            (aid,),
        ):
            raise ValueError("Application filling is paused or cancelled")
        attempt = self.db.one("SELECT * FROM attempts WHERE application_id=%s", (aid,))
        if attempt and (
            attempt["id"] != attempt_id
            or (
                app["state"] not in ("submission_attempt", "receipt_verification")
                and not (
                    app["state"] == "authorized" and attempt["state"] == "retry_ready"
                )
            )
        ):
            raise ValueError(
                "Reconcile the existing attempt before opening another form"
            )
        if expected_revision is not None:
            if app["revision"] != expected_revision:
                raise ValueError("The application changed while filling")
            return app, None
        review = self.flow.review(aid)
        if review["questions"]:
            raise ValueError("Confirm the outstanding answers before filling")
        if expected_hash and review["hash"] != expected_hash:
            raise ValueError("The answers or selected files changed while filling")
        return app, review

    async def session(self, app, spec, transport=None):
        from .browser import GUARD

        aid = app["id"]
        old = self.sessions.pop(aid, None)
        if old:
            await old["browser"].close()
        if not self.playwright:
            self.playwright = await async_playwright().start()
        # Ephemeral context: no credentials, API token, saved applicant account,
        # or old form state can be carried into the employer page.
        browser = await self.playwright.chromium.launch(
            executable_path="/usr/bin/chromium",
            headless=self.db.settings.headless,
            args=["--disable-background-networking"],
        )
        context = await browser.new_context(
            service_workers="block",
            accept_downloads=False,
            viewport={"width": 1100, "height": 900},
        )
        await context.add_init_script(GUARD)
        await context.route_web_socket("**/*", lambda socket: socket.close())
        target = urlparse(spec.url)
        staging_url = spec.url + "/__jsa_stage_upload"
        # A fulfilled request sends no bytes, so Chromium emits no upload progress.
        # Greenhouse keeps its widget spinning until progress reaches 100. Emit
        # that event only for this locally answered staging URL, never a real upload.
        if not transport:
            await context.add_init_script(
                """(() => {
            const target=STAGING_URL, urls=new WeakMap();
            const open=XMLHttpRequest.prototype.open, send=XMLHttpRequest.prototype.send;
            XMLHttpRequest.prototype.open=function(method,url,...rest){
                urls.set(this,String(url));return open.call(this,method,url,...rest);
            };
            XMLHttpRequest.prototype.send=function(body){
                if(urls.get(this)===target){
                    const size=body?.get?.('file')?.size || 1;
                    this.addEventListener('load',()=>this.upload.dispatchEvent(
                        new ProgressEvent('progress',{lengthComputable:true,loaded:size,total:size})
                    ),{once:true});
                }
                return send.call(this,body);
            };
        })();""".replace("STAGING_URL", json.dumps(staging_url))
            )
        gate = {"phase": "inspect", "blocked": 0, "staged": set()}
        files = [f for f in spec.fields if f.type == "file"]

        async def route(route):
            request = route.request
            u = urlparse(request.url)
            if transport:
                try:
                    if await transport.handle(route, gate):
                        return
                except Exception:
                    gate["transport_error"] = True
                    transport.error = "The employer request failed validation or could not be completed."
                    transport.progress("request_blocked")
                    await route.abort("blockedbyclient")
                    return
            # These responses exist only inside this browser. Nothing is sent to
            # the presign endpoint or storage service during a review fill.
            if (
                not transport
                and request.method == "GET"
                and (
                    (
                        u.hostname == "boards.greenhouse.io"
                        and u.path == "/uncacheable_attributes/presigned_fields"
                    )
                    or (
                        app["candidate"] == "fixture"
                        and u.netloc == target.netloc
                        and u.path == "/fixture/greenhouse-presign"
                    )
                )
            ):
                await route.fulfill(
                    json={
                        "url": staging_url,
                        **{f.name: {"fields": {}, "key": f.name} for f in files},
                    }
                )
                return
            if (
                not transport
                and request.method == "POST"
                and request.url == staging_url
            ):
                if gate["phase"] == "fill":
                    await route.fulfill(
                        status=204, headers={"Access-Control-Allow-Origin": "*"}
                    )
                    return
            if (
                not transport
                and request.method == "OPTIONS"
                and request.url == staging_url
            ):
                await route.fulfill(
                    status=204,
                    headers={
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Methods": "POST",
                        "Access-Control-Allow-Headers": "*",
                    },
                )
                return
            same = (u.scheme, u.netloc) == (target.scheme, target.netloc)
            allowed = (
                gate["phase"] == "inspect"
                and request.method == "GET"
                and (
                    (same and u.path == target.path and not u.query)
                    or (
                        u.scheme == "https"
                        and u.hostname == "job-boards.cdn.greenhouse.io"
                        and not u.query
                        and u.path.startswith(("/assets/", "/fonts/", "/locales/"))
                        and request.resource_type
                        in ("script", "stylesheet", "font", "fetch")
                    )
                    or (
                        app["candidate"] == "fixture"
                        and same
                        and u.path == "/fixture/greenhouse.js"
                        and not u.query
                    )
                )
            )
            if allowed:
                headers = {
                    k: v
                    for k, v in request.headers.items()
                    if k.lower()
                    not in ("authorization", "cookie", "x-jsa-token", "x-jsa-profile")
                }
                if app["candidate"] == "fixture":
                    headers.update(self.db.settings.local_headers())
                await route.continue_(headers=headers)
            else:
                gate["blocked"] += 1
                await route.abort("blockedbyclient")

        await context.route("**/*", route)
        page = await context.new_page()
        page.set_default_timeout(8000)
        self.sessions[aid] = {
            "browser": browser,
            "context": context,
            "page": page,
            "gate": gate,
            "files": {},
            "transport": transport,
        }
        if transport:
            page.on("response", transport.response)
            page.on("console", transport.browser_console)
        await page.goto(spec.url, wait_until="domcontentloaded", timeout=30000)
        await page.locator("#application-form").wait_for(state="visible", timeout=30000)
        await (
            page.locator("#application-form")
            .get_by_role("button", name=spec.submit_label, exact=True)
            .wait_for(state="visible", timeout=30000)
        )
        # Background requests need not finish for the form to be usable.
        # Control validation still checks hydration, labels, and choices.
        return page, gate

    def control(self, page, field):
        return page.locator("#application-form").locator(
            "[id=" + json.dumps(field.name) + "]"
        )

    async def challenge(self, page):
        for selector in (
            "[data-challenge]",
            '[autocomplete="one-time-code"]',
            'input[id^="security-input-"]',
            'input[type="password"]',
            'iframe[title*="challenge" i]',
            'iframe[src*="/bframe"]',
        ):
            for control in await page.locator(selector).all():
                if await control.is_visible():
                    return True
        return False

    async def validate_dom(self, page, spec):
        """Reject changed labels/types/requirements and unknown required controls."""
        form = page.locator("#application-form")
        if await form.count() != 1:
            raise ValueError("The employer application form is missing")
        for f in spec.fields:
            if f.purpose != "ordinary":
                await greenhouse_choices.validate(page, f)
                continue
            c = self.control(page, f)
            if await c.count() != 1:
                raise ValueError("The employer changed this control: " + f.label)
            if f.type == "checkbox":
                group = await c.evaluate("""e=>({
                    tag:e.tagName,
                    label:(e.querySelector('legend')?.textContent || '').replace(/\\*/g,'').trim(),
                    required:e.getAttribute('aria-required')==='true',
                    options:[...e.querySelectorAll('input[type=checkbox]')].map(i=>({
                        id:i.id,name:i.name,label:[...i.labels].map(l=>l.textContent).join('').trim()
                    }))
                })""")
                if (
                    group["tag"] != "FIELDSET"
                    or group["label"] != f.label.strip()
                    or group["required"] != f.required
                ):
                    raise ValueError("The employer changed this question: " + f.label)
                if (
                    [o["label"] for o in group["options"]] != f.options
                    or any(o["name"] != f.name or not o["id"] for o in group["options"])
                    or len(set(f.options)) != len(f.options)
                ):
                    raise ValueError("The employer changed the choices: " + f.label)
                continue
            info = await c.evaluate("""e=>({
                label:(document.querySelector('label[for="'+e.id+'"]')?.textContent || '').replace(/\\*/g,'').trim(),
                required:e.required || e.getAttribute('aria-required')==='true',
                type:e.type,tag:e.tagName,role:e.getAttribute('role'),
                fileLabel:e.closest('[role=group]')?.getAttribute('aria-labelledby')
            })""")
            if f.type == "file":
                if info["type"] != "file":
                    raise ValueError("The employer changed the upload control")
                continue  # file requiredness is supplied by the public questions API
            if info["label"] != f.label.strip() or info["required"] != f.required:
                raise ValueError("The employer changed this question: " + f.label)
            if f.type == "select":
                if info["tag"] == "SELECT":
                    options = await c.locator("option").all_text_contents()
                    options = [
                        s.strip()
                        for s in options
                        if s.strip() and s.strip() != "Select..."
                    ]
                elif info["role"] == "combobox":
                    await c.click()
                    if f.options:
                        await page.get_by_role("option").first.wait_for()
                    options = await page.get_by_role("option").all_text_contents()
                    await c.press("Escape")
                else:
                    raise ValueError("The employer changed the dropdown: " + f.label)
                if options != f.options:
                    raise ValueError("The employer changed the choices: " + f.label)
            elif f.type in ("checkbox", "radio"):
                raise ValueError(
                    "This Greenhouse selection control needs manual review: " + f.label
                )
            elif f.type == "textarea" and info["tag"] != "TEXTAREA":
                raise ValueError("The employer changed the narrative control")
        ids = {f.name for f in spec.fields}
        ids.update(f.name + "_1" for f in spec.fields if f.binding == "data_compliance")
        ids.update(f.name + "-freeform" for f in spec.fields if f.free_form_options)
        for f in spec.fields:
            if f.purpose == "ordinary" and f.type == "checkbox":
                ids.update(
                    await self.control(page, f)
                    .locator("input[type=checkbox]")
                    .evaluate_all("els=>els.map(e=>e.id)")
                )
        if any(f.binding == "eeoc" and f.name == "race" for f in spec.fields):
            ids.add("hispanic_ethnicity")
        required = await form.locator("input,textarea,select").evaluate_all("""els=>els.filter(e=>
            (e.required || e.getAttribute('aria-required')==='true') &&
            e.getAttribute('aria-hidden')!=='true' && e.type!=='hidden'
        ).map(e=>e.id)""")
        if any(i not in ids for i in required):
            raise ValueError(
                "The employer added a required question; inspect the form again"
            )
        if (
            await form.get_by_role("button", name=spec.submit_label, exact=True).count()
            != 1
        ):
            raise ValueError("The employer changed the final submission control")

    async def select(self, page, control, value):
        if await control.evaluate("e=>e.tagName") == "SELECT":
            await control.select_option(label=value)
        else:
            await control.click()
            await page.get_by_role("option", name=value, exact=True).click()

    async def value(self, control, field):
        if field.type == "checkbox":
            return await control.locator("input[type=checkbox]:checked").evaluate_all(
                "els=>els.map(e=>[...e.labels].map(l=>l.textContent).join('').trim())"
            )
        if (
            field.type == "select"
            and await control.evaluate("e=>e.tagName") == "SELECT"
        ):
            return await control.evaluate("e=>e.selectedOptions[0]?.textContent || ''")
        if field.type == "select" and await control.get_attribute("role") == "combobox":
            return await control.evaluate(
                "e=>e.closest('.select__value-container')?.querySelector('.select__single-value')?.textContent || ''"
            )
        return await control.input_value()

    async def fill_values(self, aid, page, spec, review, attempt_id=None):
        limits = await self.text_limit_violations(page, spec, review)
        if limits:
            field, limit, length = limits[0]
            raise ValueError(
                f"The employer allows at most {limit} characters for {field.label.strip()} Your saved answer has {length}. Shorten it and confirm the revised answer."
            )
        self.sessions[aid]["gate"]["phase"] = "fill"
        revision = self.flow.get(aid)["revision"]
        for f in spec.fields:
            # Check cancellation and edits between controls. Full evidence checks
            # run before/after filling and at every actual outbound upload/POST.
            self.active(aid, attempt_id=attempt_id, expected_revision=revision)
            if await self.challenge(page):
                raise InterruptedError("Employer verification requires user action")
            control = self.control(page, f)
            if f.type == "file":
                selected = [
                    a
                    for a in review["payload"]["attachments"]
                    if a["kind"] == f.name + ".pdf"
                ]
                if selected:
                    a = selected[0]
                    row = self.db.one(
                        "SELECT path FROM artifacts WHERE id=%s AND application_id=%s",
                        (a["id"], aid),
                    )
                    content = Path(row["path"]).read_bytes()
                    if digest(content) != a["hash"]:
                        raise ValueError("Selected attachment changed before filling")
                    self.sessions[aid]["files"][f.name] = await control.element_handle()
                    await control.set_input_files(
                        {
                            "name": a["name"],
                            "mimeType": "application/pdf",
                            "buffer": content,
                        }
                    )
                    try:
                        await page.locator(
                            '[aria-labelledby="upload-label-'
                            + f.name
                            + '"] .file-upload__filename'
                        ).wait_for(timeout=30000)
                    except BrowserTimeout:
                        transport = self.sessions[aid].get("transport")
                        detail = transport.error if transport else None
                        raise ValueError(
                            "The document upload did not finish. "
                            + (
                                detail
                                or "The employer upload widget did not confirm the file."
                            )
                            + " The final application request has not been sent."
                        ) from None
                continue
            answer = review["payload"]["answers"].get(f.key)
            if not answer:
                continue
            if f.purpose != "ordinary":
                await greenhouse_choices.fill(self, page, f, answer["value"])
                continue
            if f.type == "checkbox":
                from .forms import valid_value

                if not valid_value(f, answer["value"]):
                    raise ValueError("Confirm the checkbox answers before filling")
                for option in f.options:
                    await control.get_by_role(
                        "checkbox", name=option, exact=True
                    ).set_checked(option in answer["value"])
                continue
            value = str(answer["value"])
            if f.type == "select":
                await self.select(page, control, value)
            else:
                await control.fill(value)
        await page.locator("#application-form").click(position={"x": 1, "y": 1})

    async def text_limit_violations(self, page, spec, review):
        violations = []
        for field in spec.fields:
            if field.type not in ("text", "email", "tel", "url", "textarea"):
                continue
            value = str(
                review["payload"]["answers"].get(field.key, {}).get("value", "")
            )
            limit = await self.control(page, field).evaluate("e=>e.maxLength ?? -1")
            # HTML maxlength counts UTF-16 code units, including surrogate pairs.
            length = len(value.encode("utf-16-le")) // 2
            if limit >= 0 and length > limit:
                violations.append((field, limit, length))
        return violations

    async def values_match(self, page, spec, review):
        session = next(s for s in self.sessions.values() if s["page"] is page)
        for f in spec.fields:
            # Report the control, never the candidate's answer, when review stops.
            session["mismatch"] = f.label.strip()
            if f.purpose != "ordinary":
                expected = review["payload"]["answers"].get(f.key, {}).get("value")
                actual = await greenhouse_choices.read(self, page, f)
                if not greenhouse_choices.matches(f, actual, expected):
                    return False
                continue
            control = self.control(page, f)
            if f.type == "file":
                expected = [
                    {"name": a["name"], "hash": a["hash"]}
                    for a in review["payload"]["attachments"]
                    if a["kind"] == f.name + ".pdf"
                ]
                handle = (
                    await control.element_handle()
                    if await control.count()
                    else session["files"].get(f.name)
                )
                if expected and not handle:
                    return False
                if handle and await handle.evaluate(FILE_HASHES) != expected:
                    return False
                if expected:
                    shown = page.locator(
                        '[aria-labelledby="upload-label-'
                        + f.name
                        + '"] .file-upload__filename'
                    )
                    if (
                        not await shown.count()
                        or expected[0]["name"] not in await shown.inner_text()
                    ):
                        return False
            else:
                if f.type == "checkbox":
                    expected = (
                        review["payload"]["answers"].get(f.key, {}).get("value", [])
                    )
                    if sorted(await self.value(control, f)) != sorted(expected):
                        return False
                    continue
                expected = str(
                    review["payload"]["answers"].get(f.key, {}).get("value", "")
                )
                actual = await self.value(control, f)
                # The phone widget formats a confirmed international number.
                if f.name == "phone":
                    actual, expected = (
                        re.sub(r"\D", "", actual),
                        re.sub(r"\D", "", expected),
                    )
                if actual != expected:
                    return False
        session.pop("mismatch", None)
        return True

    async def stage(self, aid):
        app, review = self.active(aid)
        spec = FormPreparation(self.db).validate_ready(app)
        if spec.adapter != "greenhouse" or app["state"] != "review":
            raise ValueError("Prepare the Greenhouse answers before browser filling")
        self.flow.update_payload(aid, {"browser_hash": None})
        self.flow.transition(aid, "filling")
        page = None
        try:
            page, gate = await self.session(app, spec)
            if await self.challenge(page):
                raise InterruptedError("Employer verification requires user action")
            await self.validate_dom(page, spec)
            limits = await self.text_limit_violations(page, spec, review)
            if limits:
                for field, limit, length in limits:
                    self.flow.ask(
                        aid,
                        field.key,
                        field.label,
                        {
                            "reason": {
                                "code": "answer_too_long",
                                "max_length": limit,
                                "message": f"The employer allows at most {limit} characters. Your saved answer has {length}. Shorten it and confirm the revised answer. The original answer remains saved.",
                            }
                        },
                    )
                self.flow.require_answers(aid)
                return {
                    "state": "missing_answers",
                    "filled": False,
                    "message": "Shorten the answers that exceed the employer's character limits, then confirm them.",
                }
            await page.evaluate("""() => {
                const banner=document.createElement('div');
                banner.id='jsa-browser-status'; banner.setAttribute('role','status');
                banner.style='position:fixed;top:0;left:0;right:0;z-index:2147483647;background:#172c3f;color:white;padding:14px;font:16px sans-serif';
                banner.textContent='Local review: answers and files stay in this browser. Uploads and submission are blocked. Return to Job Search to review.';
                document.body.prepend(banner);
            }""")
            await self.fill_values(aid, page, spec, review)
            self.active(aid, review["hash"])
            if not await self.values_match(page, spec, review):
                raise ValueError(
                    "The filled form differs from the confirmed answers or files: "
                    + self.sessions[aid]["mismatch"]
                )
            gate["phase"] = "hold"
            await page.locator("#first_name").scroll_into_view_if_needed()
            path = (
                self.db.settings.state
                / "browser-evidence"
                / (aid + "-greenhouse-review.png")
            )
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            await page.screenshot(path=str(path), full_page=True)
            self.flow.update_payload(aid, {"browser_hash": review["hash"]})
            self.flow.transition(aid, "review")
            result = {
                "state": "review",
                "adapter": "greenhouse",
                "mode": "local_browser_review",
                "filled": True,
                "reviewed_hash": review["hash"],
                "staged_attachments": [
                    a["id"] for a in review["payload"]["attachments"]
                ],
                "screenshot": str(path),
                "screenshot_hash": digest(path.read_bytes()),
                "message": "The browser form is filled. Files are staged locally; nothing has been sent. Review the answers and selected files in Job Search.",
            }
            self.db.audit(
                "greenhouse.browser_filled",
                aid,
                {k: v for k, v in result.items() if k != "screenshot"},
            )
            self.db.execute(
                "UPDATE followups SET resolved=true WHERE application_id=%s AND category='manual_application'",
                (aid,),
            )
            return result
        except InterruptedError:
            self.flow.transition(aid, "awaiting_challenge")
            self.save_handoff(
                aid, "Employer verification needs user action. No application was sent."
            )
            return {
                "state": "awaiting_challenge",
                "message": "Employer verification needs your attention. No application was sent.",
            }
        except Exception:
            # No candidate network traffic or durable submission attempt occurred.
            # Keep the saved answers editable and make a retry safe after inspection.
            if self.flow.get(aid)["state"] == "filling":
                with self.db.tx() as c:
                    c.execute(
                        "UPDATE applications SET state='review',revision=revision+1,updated_at=now() WHERE id=%s AND state='filling'",
                        (aid,),
                    )
                    self.db.audit(
                        "greenhouse.fill_interrupted", aid, {"sending": False}, c
                    )
            self.save_handoff(
                aid,
                "Browser filling stopped. Check the reported issue; the current answers are saved for recovery.",
            )
            raise
        finally:
            if aid in self.sessions:
                self.sessions[aid]["gate"]["phase"] = "hold"

    async def submit(self, aid):
        from .greenhouse_transport import GreenhouseTransport
        from .forms import greenhouse_form
        from .openings import OfficialOpenings

        existing = self.db.one("SELECT * FROM attempts WHERE application_id=%s", (aid,))
        retry_id = (
            existing["id"] if existing and existing["state"] == "retry_ready" else None
        )
        app, review = self.active(aid, attempt_id=retry_id)
        if app["candidate"] != "fixture" and not self.db.settings.live_applications:
            raise ValueError("Real application sending is disabled in this deployment")
        if app["state"] != "authorized":
            raise ValueError("Matching approval is required before sending")
        spec = FormPreparation(self.db).validate_ready(app)
        if spec.adapter != "greenhouse":
            raise ValueError("Choose the matching employer adapter")
        if app["candidate"] == "fixture":
            from .greenhouse_fixture import definition

            data = definition(app["payload"].get("scenario"))
        else:
            parts = urlparse(spec.url).path.strip("/").split("/")
            data = await asyncio.to_thread(
                OfficialOpenings(self.db).get_json,
                f"https://boards-api.greenhouse.io/v1/boards/{parts[0]}/jobs/{parts[2]}?questions=true&demographic_questions=true",
            )
            if str(data.get("requisition_id") or data.get("id")) != app["requisition"]:
                raise ValueError("The employer requisition changed")
        if (
            greenhouse_form(data, spec.url, spec.requisition).fingerprint
            != spec.fingerprint
        ):
            self.flow.update_payload(aid, {"browser_hash": None})
            return {
                "state": "review",
                "message": "The employer changed the form. Inspect it before approving again.",
            }
        options = {
            field["name"]: {
                str(v["label"]): v["value"] for v in field.get("values", [])
            }
            for q in data["questions"]
            for field in q["fields"]
        }
        transport = GreenhouseTransport(self.db, app, spec, review, options)
        transport.check()
        attempt = None
        gate = None
        try:
            page, gate = await self.session(app, spec, transport)
            if await self.challenge(page):
                raise InterruptedError("Employer verification needs your attention")
            await self.validate_dom(page, spec)
            if await self.text_limit_violations(page, spec, review):
                raise ValueError(
                    "A saved answer exceeds the employer's character limit. Return to browser review and shorten it before approving again."
                )
            transport.progress("checking_upload")
            if not transport.metadata:
                raise ValueError("The employer upload metadata could not be verified")
            transport.check()
            # This checkpoint precedes the native widget's immediate upload.
            attempt = self.flow.begin_attempt(aid, mark_attached=False)
            transport.attempt = attempt
            transport.progress("filling_approved_answers")
            await self.fill_values(aid, page, spec, review, attempt)
            transport.check()
            if not await self.values_match(page, spec, review):
                raise ValueError("Filled answers or attachment bytes changed")
            if await self.challenge(page):
                raise InterruptedError("Employer verification needs your attention")
            gate["phase"] = "send"
            transport.progress("ready_to_send")
            await page.evaluate("window.__jsaArm(true)")
            await (
                page.locator("#application-form")
                .get_by_role("button", name=spec.submit_label, exact=True)
                .click(timeout=10000)
            )
            for _ in range(40):
                if transport.error and not transport.posted:
                    raise ValueError(transport.error)
                if (
                    transport.post_status == 428
                    and not transport.response_ready.is_set()
                ):
                    try:
                        await asyncio.wait_for(
                            transport.response_ready.wait(), timeout=5
                        )
                    except TimeoutError:
                        raise InterruptedError(
                            "Employer verification response is still pending"
                        ) from None
                if transport.verification_pending or await self.challenge(page):
                    raise InterruptedError("Employer verification needs your attention")
                if transport.post_status == 428:
                    self.flow.unresolved(
                        aid,
                        "Greenhouse returned HTTP 428 without a usable verification step. "
                        "No receipt is confirmed. Resolve the outcome before retrying.",
                    )
                    return {
                        "state": "unresolved",
                        "attempt_id": attempt,
                        "message": "Greenhouse blocked verification without offering a code or challenge. Resolve the submission outcome before retrying.",
                    }
                if (
                    transport.posted
                    and self.flow.get(aid)["state"] == "submission_attempt"
                ):
                    self.flow.transition(aid, "receipt_verification")
                receipt = await self.receipt(aid)
                if receipt:
                    return self.flow.record_receipt(aid, attempt, receipt)
                # Evidence is rechecked at the outgoing request boundary.
                # Polling sends no candidate data; a later memory correction
                # must not erase a genuine employer receipt.
                if self.flow.get(aid)["paused"] or self.db.one(
                    "SELECT 1 FROM jobs WHERE input->>'application_id'=%s AND status='running' AND cancel_requested",
                    (aid,),
                ):
                    break
                await asyncio.sleep(0.25)
            self.flow.unresolved(
                aid,
                "No Greenhouse receipt was captured. Reconcile the outcome before any retry.",
            )
            return {"state": "unresolved", "attempt_id": attempt}
        except InterruptedError:
            try:
                await asyncio.wait_for(transport.response_ready.wait(), timeout=5)
            except TimeoutError:
                pass
            self.flow.transition(aid, "awaiting_challenge")
            self.save_handoff(
                aid,
                "Employer verification requires user action. Check the attempt record before taking any further submission action.",
            )
            return {
                "state": "awaiting_challenge",
                "attempt_id": attempt,
                "message": "Employer verification requires user action. Do not start another application while this attempt is unresolved.",
            }
        except Exception as exc:
            message = (
                str(exc)
                if isinstance(exc, ValueError)
                else (
                    "The employer browser timed out during "
                    + transport.stage.replace("_", " ")
                    + "."
                    if isinstance(exc, BrowserTimeout)
                    else "The employer browser stopped during "
                    + transport.stage.replace("_", " ")
                    + "."
                )
            )
            if attempt:
                self.flow.unresolved(
                    aid,
                    message,
                )
                self.save_handoff(
                    aid,
                    "Browser work stopped after the attempt checkpoint. Reconcile the employer outcome before retrying.",
                )
            elif retry_id:
                self.flow.unresolved(aid, message)
            else:
                self.flow.update_payload(aid, {"browser_hash": None})
            raise ValueError(message) from None
        finally:
            if gate:
                gate["phase"] = "hold"
            if aid in self.sessions:
                try:
                    await self.sessions[aid]["page"].evaluate(
                        "window.__jsaArm?.(false)"
                    )
                except Exception:
                    pass
            self.db.audit(
                "greenhouse.attempt_finished",
                aid,
                {
                    "attempt_id": attempt,
                    "post_observed": transport.posted,
                    "post_status": transport.post_status,
                    "receipt_status": transport.receipt_status,
                    "run_id": (
                        self.db.one(
                            "SELECT result FROM attempts WHERE id=%s", (attempt,)
                        )["result"].get("run_id")
                        if attempt
                        else None
                    ),
                    "stage": transport.stage,
                },
            )

    async def continue_verification(self, aid):
        """Use the code entered in the native page, without reading it into a job."""
        app = self.flow.get(aid)
        if app["candidate"] != "fixture" and not self.db.settings.live_applications:
            raise ValueError("Real application sending is disabled in this deployment")
        session = self.sessions.get(aid)
        transport = session.get("transport") if session else None
        if not transport or session["page"].is_closed():
            raise ValueError(
                "The original verification browser is no longer connected. "
                "No request was retried. Use the saved handoff and check the employer outcome."
            )
        if app["state"] != "awaiting_challenge" or not transport.verification_pending:
            raise ValueError(
                "No resumable email verification is pending in this browser"
            )
        if transport.verification_count >= 3:
            raise ValueError(
                "Three verification attempts have been made. Check the employer instructions before continuing."
            )
        transport.check()
        page, gate = session["page"], session["gate"]
        # Return a boolean only. Codes never enter API inputs, jobs, or records.
        ready = await page.locator('input[id^="security-input-"]').evaluate_all(
            "els => els.length === 8 && els.every(e => e.value.length === 1)"
        )
        if not ready:
            raise ValueError(
                "Type the eight-character code into the employer browser first, then continue here."
            )
        if not await self.values_match(page, transport.spec, transport.review):
            raise ValueError("The employer answers or files changed after approval")
        try:
            transport.error = None
            transport.verification_allowed = True
            gate["phase"] = "send"
            self.flow.transition(aid, "receipt_verification")
            await page.evaluate("window.__jsaArm(true)")
            await (
                page.locator("#application-form")
                .get_by_role("button", name=transport.spec.submit_label, exact=True)
                .click(timeout=10000)
            )
            for _ in range(40):
                if transport.error:
                    raise ValueError(transport.error)
                # Until the native request is released this remains true.
                if not transport.verification_allowed and transport.post_status == 428:
                    try:
                        await asyncio.wait_for(
                            transport.response_ready.wait(), timeout=5
                        )
                    except TimeoutError:
                        pass
                    self.flow.transition(aid, "awaiting_challenge")
                    return {
                        "state": "awaiting_challenge",
                        "attempt_id": transport.attempt,
                    }
                receipt = await self.receipt(aid)
                if receipt:
                    return self.flow.record_receipt(aid, transport.attempt, receipt)
                if self.flow.get(aid)["paused"]:
                    break
                await asyncio.sleep(0.25)
            if transport.verification_pending:
                self.flow.transition(aid, "awaiting_challenge")
                return {"state": "awaiting_challenge", "attempt_id": transport.attempt}
            self.flow.unresolved(
                aid,
                "No receipt after email verification. Check the employer outcome before any retry.",
            )
            return {"state": "unresolved", "attempt_id": transport.attempt}
        except Exception:
            # A released continuation with no response remains uncertain.
            if transport.verification_pending:
                self.flow.transition(aid, "awaiting_challenge")
            else:
                self.flow.unresolved(
                    aid,
                    "Email verification stopped. Reconcile the outcome before any retry.",
                )
            raise ValueError(
                "Email verification did not finish. Check the employer browser and application status."
            ) from None
        finally:
            transport.verification_allowed = False
            gate["phase"] = "hold"
            try:
                await page.evaluate("window.__jsaArm?.(false)")
            except Exception:
                pass
            self.db.audit(
                "greenhouse.verification_finished",
                aid,
                {
                    "attempt_id": transport.attempt,
                    "post_status": transport.post_status,
                    "verification_count": transport.verification_count,
                },
            )

    async def receipt(self, aid):
        session = self.sessions.get(aid)
        transport = session.get("transport") if session else None
        if not transport:
            return None
        page = session["page"]
        if (
            not transport.posted
            or not transport.post_status
            or not 200 <= transport.post_status < 300
            or transport.receipt_status != 200
            or page.url != transport.receipt_url
            or await page.locator("#application-form").count()
        ):
            return None
        text = " ".join(await page.locator("h1,h2").all_text_contents()).strip()
        if text not in (
            "Thank you for applying!",
            "Thank you for applying",
            "Application submitted",
        ):
            return None
        path = (
            self.db.settings.state
            / "browser-evidence"
            / (aid + "-greenhouse-receipt.png")
        )
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        await page.screenshot(path=str(path), full_page=True)
        return {
            "confirmed": True,
            "text": text,
            "url": page.url,
            "requisition": transport.app["requisition"],
            "adapter": "greenhouse",
            "screenshot": str(path),
            "screenshot_hash": digest(path.read_bytes()),
            "fixture": transport.fixture,
        }

    async def reconcile(self, aid):
        receipt = await self.receipt(aid)
        attempt = self.db.one("SELECT * FROM attempts WHERE application_id=%s", (aid,))
        if receipt and attempt:
            return self.flow.record_receipt(aid, attempt["id"], receipt)
        return {
            "state": self.flow.get(aid)["state"],
            "receipt": False,
            "message": "No captured employer receipt is available. Confirm the outcome before retrying.",
        }
