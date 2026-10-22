"""Approved Lever execution. No candidate data enters an employer page before approval."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import httpx
from email import policy
from email.parser import BytesParser
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from .config import digest
from .forms import FormPreparation, lever_form
from .workflow import Workflow


class PortalBrowser:
    def __init__(self, db):
        self.db = db
        self.flow = Workflow(db)
        self.sessions = {}
        self.gates = {}
        self.playwright = None

    async def close(self):
        for context, _ in self.sessions.values():
            await context.close()
        self.sessions.clear()
        self.gates.clear()
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None

    def enabled(self, app):
        if app["candidate"] != "fixture" and not self.db.settings.live_applications:
            raise ValueError("Real application sending is disabled in this deployment")

    def active(self, aid):
        app = self.flow.get(aid)
        if app["paused"] or self.db.one(
            "SELECT 1 FROM jobs WHERE input->>'application_id'=%s AND status='running' AND cancel_requested",
            (aid,),
        ):
            raise ValueError("Application work is paused or cancelled")
        return app

    async def session(self, app, spec, gate):
        from .browser import GUARD

        if not self.playwright:
            self.playwright = await async_playwright().start()
        old = self.sessions.pop(app["id"], None)
        if old:
            await old[0].close()
        profile = self.db.settings.state / "browser-profiles" / (app["id"] + "-portal")
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        context = await self.playwright.chromium.launch_persistent_context(
            str(profile),
            executable_path="/usr/bin/chromium",
            headless=self.db.settings.headless,
            service_workers="block",
            accept_downloads=False,
            args=["--disable-background-networking"],
            viewport={"width": 1100, "height": 850},
        )
        # No local API credential is installed on an employer browser context.
        await context.clear_cookies()
        await context.add_init_script(GUARD)
        await context.route_web_socket("**/*", lambda socket: socket.close())
        target = urlparse(spec.url)
        read_paths = {
            target.path,
            "/js/jquery-3.6.1.min.js",
            "/js/application.js",
            "/js/parseResume.js",
            "/js/debounce.min.js",
            "/js/retrieveLocations.js",
        }

        if app["candidate"] == "fixture":
            read_paths.add("/fixture/lever.js")

        async def route_handler(route):
            request = route.request
            u = urlparse(request.url)
            same = (u.scheme, u.netloc) == (target.scheme, target.netloc)
            if not same or request.resource_type in ("websocket", "eventsource"):
                await route.abort()
                return
            headers = {
                k: v
                for k, v in request.headers.items()
                if k.lower()
                not in ("x-jsa-token", "x-jsa-profile", "authorization", "cookie")
            }
            if app["candidate"] == "fixture":
                headers.update(self.db.settings.local_headers())
            # After entering personal data, even GET requests can leak it. Permit only
            # the one reviewed POST and its receipt navigation. Resume parsing is blocked.
            approved_body = None
            if gate["phase"] == "inspect":
                allowed = (
                    request.method == "GET"
                    and not u.query
                    and (
                        u.path in read_paths
                        or (
                            request.resource_type in ("stylesheet", "font")
                            and u.path.startswith("/")
                        )
                    )
                )
            elif (
                gate["phase"] == "send"
                and request.method == "POST"
                and request.url == spec.url
                and not gate["posted"]
            ):
                allowed = False
                try:
                    self.active(app["id"])
                    review = self.flow.review(app["id"])
                    attempt = self.db.one(
                        "SELECT * FROM attempts WHERE application_id=%s", (app["id"],)
                    )
                    if attempt and attempt["payload_hash"] == review["hash"]:
                        approved_body, content_type = approved_multipart(
                            self.db,
                            request.post_data_buffer or b"",
                            request.headers.get("content-type", ""),
                            spec,
                            review["payload"],
                        )
                        headers["content-type"] = content_type
                        headers.pop("content-length", None)
                        allowed = True
                except (ValueError, KeyError):
                    allowed = False
                if allowed:
                    gate["posted"] = True
            elif (
                gate["posted"]
                and request.method == "GET"
                and not u.query
                and u.path == target.path.removesuffix("/apply") + "/thanks"
            ):
                allowed = True
            else:
                allowed = False
            if allowed:
                await route.continue_(
                    headers=headers,
                    **(
                        {"post_data": approved_body}
                        if approved_body is not None
                        else {}
                    ),
                )
            else:
                gate["blocked"] += 1
                await route.abort()

        await context.route("**/*", route_handler)
        page = context.pages[0] if context.pages else await context.new_page()
        self.sessions[app["id"]] = (context, page)
        self.gates[app["id"]] = gate

        def response_observed(response):
            if response.url == spec.url and response.request.method == "POST":
                gate["post_status"] = response.status
            if (
                response.url == spec.url.removesuffix("/apply") + "/thanks"
                and response.request.method == "GET"
            ):
                gate["receipt_status"] = response.status

        page.on("response", response_observed)
        await page.goto(spec.url, wait_until="domcontentloaded", timeout=25000)
        return context, page

    async def challenge(self, page):
        for selector in (
            "[data-challenge]",
            'iframe[src*="captcha"]',
            ".h-captcha iframe",
            '[name="password"]',
            '[autocomplete="one-time-code"]',
        ):
            for control in await page.locator(selector).all():
                if await control.is_visible():
                    return True
        return False

    async def fill_reviewed(self, page, spec, review):
        for field in spec.fields:
            control = page.locator("[name=" + json.dumps(field.name) + "]")
            if field.type == "file":
                artifact = self.db.one(
                    "SELECT path FROM artifacts WHERE id=%s",
                    (review["payload"]["attachments"][0]["id"],),
                )
                await control.set_input_files(artifact["path"])
                continue
            answer = review["payload"]["answers"].get(field.key)
            value = (
                answer["value"] if answer else ([] if field.type == "checkbox" else "")
            )
            if field.type in ("checkbox", "radio"):
                for item in await control.all():
                    selected = await item.get_attribute("value")
                    await item.set_checked(
                        selected in value
                        if field.type == "checkbox"
                        else selected == value
                    )
            elif field.type == "select":
                await control.select_option(str(value))
            else:
                await control.fill(str(value))
        # Optional pronouns are not inferred, and the resume parser cannot add answers.
        for control in await page.locator('[name="pronouns"]').all():
            if await control.get_attribute("type") == "checkbox":
                await control.set_checked(False)
            elif await control.is_visible():
                await control.fill("")

    async def values_match(self, page, spec, review):
        if (
            lever_form(await page.content(), spec.url, spec.requisition).fingerprint
            != spec.fingerprint
        ):
            return False
        for field in spec.fields:
            control = page.locator("[name=" + json.dumps(field.name) + "]")
            if field.type == "file":
                files = await control.evaluate(
                    'async e=>Promise.all([...e.files].map(async f=>({name:f.name,hash:[...new Uint8Array(await crypto.subtle.digest("SHA-256",await f.arrayBuffer()))].map(x=>x.toString(16).padStart(2,"0")).join("")})))'
                )
                if [(f["name"], f["hash"]) for f in files] != [
                    (a["name"], a["hash"]) for a in review["payload"]["attachments"]
                ]:
                    return False
                continue
            answer = review["payload"]["answers"].get(field.key)
            expected = (
                answer["value"] if answer else ([] if field.type == "checkbox" else "")
            )
            if field.type in ("radio", "checkbox"):
                values = [
                    await c.get_attribute("value")
                    for c in await control.all()
                    if await c.is_checked()
                ]
                value = (
                    values
                    if field.type == "checkbox"
                    else (values[0] if values else "")
                )
            else:
                value = await control.input_value()
                expected = str(expected)
            if value != expected:
                return False
        return True

    async def submit(self, aid):
        app = self.active(aid)
        self.flow.editable(app)
        self.enabled(app)
        if self.db.one("SELECT 1 FROM attempts WHERE application_id=%s", (aid,)):
            raise ValueError(
                "An attempt already exists; reconcile the outcome without submitting again"
            )
        if app["state"] != "authorized":
            raise ValueError(
                "Matching approval is required before opening the employer application"
            )
        spec = FormPreparation(self.db).validate_ready(app)
        review = self.flow.review(aid)
        if not self.db.one(
            "SELECT 1 FROM approvals WHERE application_id=%s AND payload_hash=%s AND NOT invalidated",
            (aid, review["hash"]),
        ):
            raise ValueError("Approval no longer matches the application")
        gate = {"phase": "inspect", "posted": False, "blocked": 0}
        _, page = await self.session(app, spec, gate)
        if await self.challenge(page):
            gate["phase"] = "hold"
            self.flow.transition(aid, "awaiting_challenge")
            from .browser import BrowserWorker

            BrowserWorker(self.db).handoff(
                aid,
                "Employer verification requires completion in a normal browser. No candidate data was entered by the worker.",
            )
            return {
                "state": "awaiting_challenge",
                "message": "Complete verification in your normal browser and use the saved handoff. No candidate data was entered.",
            }
        current = lever_form(await page.content(), spec.url, spec.requisition)
        if current.fingerprint != spec.fingerprint:
            gate["phase"] = "hold"
            self.flow.update_payload(aid, {"browser_hash": None})
            return {
                "state": "review",
                "message": "The employer changed the form. Inspect it again before approval.",
            }
        self.active(aid)
        # Record before typing or attaching: employer JavaScript can react to either.
        attempt = self.flow.begin_attempt(aid)
        gate["phase"] = "fill"
        try:
            await self.fill_reviewed(page, spec, review)
            self.active(aid)
            if not await self.values_match(page, spec, self.flow.review(aid)):
                raise ValueError("Employer form values changed after filling")
            if await self.challenge(page):
                raise ValueError("Employer verification requires user takeover")
            gate["phase"] = "send"
            await page.evaluate("window.__jsaArm(true)")
            await page.locator("#btn-submit").click(timeout=10000)
            self.flow.transition(aid, "receipt_verification")
            for _ in range(20):
                if gate["posted"]:
                    receipt = await self.receipt(page, app, spec)
                    if receipt:
                        return self.flow.record_receipt(aid, attempt, receipt)
                await asyncio.sleep(0.25)
            self.flow.unresolved(
                aid,
                "No employer receipt was captured. Do not send again until the outcome is reconciled",
            )
            return {"state": "unresolved", "attempt_id": attempt}
        except Exception:
            self.flow.unresolved(
                aid,
                "Browser work stopped after the durable attempt checkpoint; reconcile before retrying",
            )
            raise
        finally:
            gate["phase"] = "hold"
            self.db.audit(
                "portal.attempt_finished",
                aid,
                {
                    "attempt_id": attempt,
                    "post_observed": gate["posted"],
                    "post_status": gate.get("post_status"),
                    "receipt_status": gate.get("receipt_status"),
                    "blocked_requests": gate["blocked"],
                },
            )
            try:
                await page.evaluate("window.__jsaArm?.(false)")
            except Exception:
                pass

    async def receipt(self, page, app, spec):
        expected = spec.url.removesuffix("/apply") + "/thanks"
        gate = self.gates.get(app["id"], {})
        if (
            page.url != expected
            or not gate.get("posted")
            or not (200 <= gate.get("post_status", 0) < 400)
            or gate.get("receipt_status") != 200
        ):
            return None
        heading = " ".join(await page.locator("h1,h2").all_text_contents()).strip()
        if heading not in (
            "Application submitted!",
            "Application submitted",
            "Thank you for applying!",
        ):
            return None
        if await page.locator("#application-form").count():
            return None
        path = (
            self.db.settings.state
            / "browser-evidence"
            / (app["id"] + "-portal-receipt.png")
        )
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        await page.screenshot(path=str(path), full_page=True)
        return {
            "confirmed": True,
            "text": heading,
            "requisition": app["requisition"],
            "url": page.url,
            "screenshot": str(path),
            "screenshot_hash": digest(path.read_bytes()),
            "fixture": app["candidate"] == "fixture",
            "adapter": "lever",
        }

    async def reconcile(self, aid):
        app = self.flow.get(aid)
        session = self.sessions.get(aid)
        attempt = self.db.one("SELECT * FROM attempts WHERE application_id=%s", (aid,))
        if not session or not attempt:
            return {
                "state": app["state"],
                "receipt": False,
                "message": "No captured receipt is available. Confirm employer receipt explicitly or use the saved handoff.",
            }
        from .forms import FormSpec

        spec = FormSpec.model_validate(app["payload"]["form_plan"]["spec"])
        receipt = await self.receipt(session[1], app, spec)
        return (
            self.flow.record_receipt(aid, attempt["id"], receipt)
            if receipt
            else {"state": app["state"], "receipt": False}
        )


# Hidden operational fields are permitted only under their exact known names and are
# never logged. Unknown populated controls, parser-generated answers, and changed files
# block the POST. Validation runs in Python, outside the untrusted page.
HIDDEN = {
    "accountId",
    "linkedInData",
    "origin",
    "referer",
    "timezone",
    "socialReferralKey",
    "socialSource",
    "resumeStorageId",
    "h-captcha-response",
    "source",
    "selectedLocation",
}


def matches_multipart(body, content_type, spec, payload):
    if not content_type.startswith("multipart/form-data;") or len(body) > 25_000_000:
        return False
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: "
        + content_type.encode()
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
        + body
    )
    if not message.is_multipart():
        return False
    values, files = {}, []
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        data = part.get_payload(decode=True) or b""
        if part.get_filename():
            files.append((name, part.get_filename(), digest(data)))
        else:
            try:
                values.setdefault(name, []).append(data.decode("utf-8"))
            except UnicodeDecodeError:
                return False
    expected_files = [("resume", a["name"], a["hash"]) for a in payload["attachments"]]
    if files != expected_files:
        return False
    for field in spec.fields:
        if field.type == "file":
            continue
        answer = payload["answers"].get(field.key)
        value = (
            answer["value"]
            if answer
            else ([] if field.type in ("checkbox", "radio") else "")
        )
        expected = value if isinstance(value, list) else [str(value)]
        actual = values.pop(field.name, [])
        if [v for v in actual if v != ""] != [v for v in expected if v != ""]:
            return False
    for name, values_ in values.items():
        if (
            name not in HIDDEN
            and not (
                isinstance(name, str)
                and name.startswith("cards[")
                and name.endswith("[baseTemplate]")
            )
            and any(values_)
        ):
            return False
        if name in ("resumeStorageId", "linkedInData", "selectedLocation") and any(
            values_
        ):
            return False
    return True


def approved_multipart(db, body, content_type, spec, payload):
    """CDP omits file bodies. Replace them with verified bytes, then validate the POST.

    The page controls neither attachment bytes nor the set of approved answers.
    Live CSRF/challenge fields pass through transiently and are never persisted.
    """
    if not content_type.startswith("multipart/form-data;") or len(body) > 25_000_000:
        raise ValueError("Unsupported employer request encoding")
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: "
        + content_type.encode()
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
        + body
    )
    if not message.is_multipart():
        raise ValueError("Incomplete employer request")
    parts = []
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        data = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename:
            matches = [a for a in payload["attachments"] if a["name"] == filename]
            if name != "resume" or len(matches) != 1:
                raise ValueError("An unapproved attachment was selected")
            artifact = matches[0]
            row = db.one(
                "SELECT path FROM artifacts WHERE id=%s AND application_id=%s",
                (artifact["id"], payload["application_id"]),
            )
            if not row:
                raise ValueError("Attachment is not owned by this application")
            approved = Path(row["path"]).read_bytes()
            if digest(approved) != artifact["hash"] or (
                data and digest(data) != artifact["hash"]
            ):
                raise ValueError("Attachment bytes changed after approval")
            parts.append((name, (filename, approved, "application/pdf")))
        else:
            parts.append((name, (None, data)))
    request = httpx.Request("POST", spec.url, files=parts)
    encoded = request.read()
    if not matches_multipart(encoded, request.headers["content-type"], spec, payload):
        raise ValueError("The outgoing answers differ from the approved application")
    return encoded, request.headers["content-type"]
