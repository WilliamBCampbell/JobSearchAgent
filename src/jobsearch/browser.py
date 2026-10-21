from __future__ import annotations
import json
import html
import re
from urllib.parse import urlparse
from playwright.async_api import async_playwright
from .config import atomic_write
from .memory import Memory
from .workflow import Workflow

# Adapted from workspace automation/guardrails.js: final buttons and native submission
# are blocked until code explicitly arms the reviewed action. No page instruction can arm it.
GUARD = r"""(() => {
  let armed = false;
  const terminal = /submit|finish|complete|attest|sign|continue|save|apply/i;
  const block = e => { if (!armed) { e.preventDefault(); e.stopImmediatePropagation(); } };
  document.addEventListener('submit', block, true);
  document.addEventListener('click', e => { const b=e.target.closest('button,input[type=submit]'); if(b && b.id!=='challenge-done' && terminal.test(b.textContent || b.value || '')) block(e); },true);
  document.addEventListener('keydown', e => { if(e.key==='Enter' && e.target.tagName!=='TEXTAREA') block(e); },true);
  const nativeSubmit=HTMLFormElement.prototype.submit;
  HTMLFormElement.prototype.submit=function(){if(armed)nativeSubmit.call(this)};
  window.__jsaArm = value => { armed = value === true; };
})();"""


class BrowserWorker:
    def __init__(self, db):
        self.db = db
        self.playwright = None
        self.sessions = {}
        self.workflow = Workflow(db)
        from .portal_browser import PortalBrowser

        self.portal = PortalBrowser(db)
        from .greenhouse_browser import GreenhouseBrowser

        self.greenhouse = GreenhouseBrowser(db)

    async def close(self):
        await self.portal.close()
        await self.greenhouse.close()
        for context, page in self.sessions.values():
            await context.close()
        self.sessions = {}
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None

    async def session(self, app):
        if app["candidate"] != "fixture":
            raise ValueError(
                "Development browser actions are restricted to local fixtures"
            )
        if app["id"] in self.sessions:
            context, page = self.sessions[app["id"]]
            if not page.is_closed():
                return context, page
        if not self.playwright:
            self.playwright = await async_playwright().start()
        profile = self.db.settings.state / "browser-profiles" / app["id"]
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        context = await self.playwright.chromium.launch_persistent_context(
            str(profile),
            headless=self.db.settings.headless,
            executable_path="/usr/bin/chromium",
            viewport={"width": 1100, "height": 850},
            service_workers="block",
            args=["--disable-background-networking"],
            extra_http_headers={"X-JSA-Token": self.db.settings.token()},
        )

        async def route_handler(route):
            # Before and after filling, browser requests cannot leave the fixture server.
            if (
                urlparse(route.request.url).netloc
                != urlparse(self.db.settings.origin).netloc
            ):
                await route.abort()
                return
            if route.request.method not in ("GET", "HEAD"):
                latest = self.workflow.get(app["id"])
                if latest["state"] not in (
                    "submission_attempt",
                    "receipt_verification",
                ):
                    await route.abort()
                    return
            await route.continue_()

        await context.route("**/*", route_handler)
        await context.add_init_script(GUARD)
        page = context.pages[0] if context.pages else await context.new_page()
        self.sessions[app["id"]] = (context, page)
        await page.goto(
            self.db.settings.origin + "/fixture/application/" + app["id"],
            wait_until="domcontentloaded",
        )
        return context, page

    async def schema(self, page):
        return await page.locator(
            "input[data-key],textarea[data-key],select[data-key]"
        ).evaluate_all(
            'els => els.map(e => ({key:e.dataset.key, label:e.labels?.[0]?.innerText || e.name, required:e.required, type:e.type, options:e.tagName==="SELECT" ? [...e.options].map(o=>o.value) : null, qualification:e.dataset.requirement || null, category:e.dataset.category || null}))'
        )

    def check_active(self, aid):
        app = self.workflow.get(aid)
        if app["paused"] or self.db.one(
            "SELECT 1 FROM jobs WHERE input->>'application_id'=%s AND status='running' AND cancel_requested",
            (aid,),
        ):
            raise ValueError("Work paused or cancelled before the next browser action")

    async def fill(
        self, aid, job_id=None, progress=lambda _: None, cancelled=lambda: False
    ):
        app = self.workflow.get(aid)
        from .progress import document_review_needed

        if document_review_needed(app):
            return {
                "state": app["state"],
                "waiting_for": "document_review",
                "message": "Review the current documents before preparing the employer application",
            }
        if app["candidate"] != "fixture" or app["payload"].get(
            "scenario", ""
        ).startswith(("lever", "greenhouse")):
            import asyncio
            from .forms import FormPreparation

            result = await asyncio.to_thread(
                FormPreparation(self.db).plan, aid, job_id, progress, cancelled
            )
            if (
                result.get("state") == "review"
                and result.get("adapter") == "greenhouse"
            ):
                return await self.greenhouse.stage(aid)
            return result
        self.workflow.editable(app)
        if app["paused"]:
            raise ValueError("Application is paused")
        if app["state"] not in (
            "prepared",
            "missing_answers",
            "awaiting_challenge",
            "review",
            "filling",
        ):
            raise ValueError("Application is not ready for filling")
        if app["state"] != "filling":
            self.workflow.transition(aid, "filling")
        context, page = await self.session(app)
        if await page.locator("[data-challenge]").count():
            self.workflow.transition(
                aid, "awaiting_challenge", {"challenge": "user_takeover"}
            )
            return {
                "state": "awaiting_challenge",
                "message": "Complete verification in the visible browser, then resume. Codes are not recorded.",
            }
        if await page.locator("[data-bot-rejected]").count():
            self.workflow.transition(aid, "manual_handoff")
            return self.handoff(aid, "Controlled browser was rejected")
        schema = await self.schema(page)
        if any(
            re.search(r"signature|attestation", field["key"], re.I) for field in schema
        ):
            self.workflow.transition(aid, "manual_handoff")
            return self.handoff(
                aid,
                "A signature or attestation needs a reviewed portal-specific final-action adapter",
            )
        introduced = [
            field
            for field in schema
            if field.get("qualification")
            and not any(
                r["text"] == field["qualification"]
                for r in app["score"].get("requirements", [])
            )
        ]
        if introduced:
            from .scoring import Assessment, Requirement

            score = app["score"]
            matrix = [Requirement(**r) for r in score["requirements"]]
            for field in introduced:
                matrix.append(
                    Requirement(
                        text=field["qualification"],
                        source=page.url,
                        category=field.get("category") or "technical",
                        classification="required",
                        central=True,
                        assessment="unconfirmed",
                        deduction=0,
                        reason="The application form introduced this requirement; current candidate evidence has not established it.",
                    )
                )
            result = self.workflow.score(
                aid,
                Assessment(
                    track=score["track"],
                    requirements=matrix,
                    ohio=score["ohio"],
                    compensation=score["compensation"],
                ),
            )
            return {
                "state": "scored",
                "message": "New required qualifications changed the fit assessment. Review the gaps before continuing.",
                "score": result["score"],
            }
        answers = app["payload"].get("answers", {}).copy()
        missing = []
        for field in schema:
            self.check_active(aid)
            if field["type"] in ("file", "password") or field["key"] in (
                "verification_code",
                "session_token",
            ):
                continue
            answer = Memory(self.db).resolve(field["key"], app["candidate"], aid)
            if answer.get("source") == "question":
                answers.pop(field["key"], None)
                if field["required"]:
                    self.workflow.ask(aid, field["key"], field["label"], answer)
                    missing.append(field["key"])
                continue
            answers[field["key"]] = answer
            control = page.locator(f'[data-key="{field["key"]}"]')
            if field["type"] == "checkbox":
                await control.set_checked(bool(answer["value"]))
            elif field["options"] is not None:
                await control.select_option(str(answer["value"]))
            else:
                await control.fill(str(answer["value"]))
        attachments = app["payload"].get("attachments", [])
        paths = []
        for artifact_id in attachments:
            artifact = self.db.one(
                "SELECT * FROM artifacts WHERE id=%s AND application_id=%s",
                (artifact_id, aid),
            )
            if not artifact:
                raise ValueError("Attachment does not belong to this application")
            paths.append(artifact["path"])
        if paths:
            await page.locator("input[type=file]").set_input_files(paths)
        self.workflow.update_payload(aid, {"answers": answers, "form_schema": schema})
        if missing:
            handoff = self.workflow.require_answers(aid)
            return {
                "state": "missing_answers",
                "questions": missing,
                "handoff": handoff,
            }
        current = self.workflow.review(aid)
        self.workflow.update_payload(aid, {"browser_hash": current["hash"]})
        self.workflow.transition(aid, "review")
        path = self.db.settings.state / "browser-evidence" / f"{aid}-review.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(path), full_page=True)
        self.db.audit(
            "browser.filled",
            aid,
            {
                "field_count": len(schema),
                "attachment_count": len(paths),
                "payload_hash": current["hash"],
            },
        )
        return {"state": "review", "hash": current["hash"], "screenshot": str(path)}

    async def current_values_match(self, page, review):
        schema = await self.schema(page)
        if schema != review["payload"]["form_schema"]:
            return False
        for field in schema:
            if field["type"] == "file":
                continue
            answer = review["payload"]["answers"].get(field["key"])
            if not answer:
                continue
            control = page.locator(f'[data-key="{field["key"]}"]')
            value = (
                await control.is_checked()
                if field["type"] == "checkbox"
                else await control.input_value()
            )
            expected = (
                answer["value"] if field["type"] == "checkbox" else str(answer["value"])
            )
            if value != expected:
                return False
        files = await page.locator("input[type=file]").evaluate(
            'async e=>Promise.all([...e.files].map(async f=>({name:f.name,hash:[...new Uint8Array(await crypto.subtle.digest("SHA-256",await f.arrayBuffer()))].map(x=>x.toString(16).padStart(2,"0")).join("")})))'
        )
        expected = review["payload"]["attachments"]
        return sorted((f["name"], f["hash"]) for f in files) == sorted(
            (a["name"], a["hash"]) for a in expected
        )

    async def submit(self, aid):
        app = self.workflow.get(aid)
        if app["payload"].get("form_plan"):
            if app["payload"]["form_plan"]["spec"]["adapter"] == "greenhouse":
                return await self.greenhouse.submit(aid)
            return await self.portal.submit(aid)
        if app["paused"]:
            raise ValueError("Application is paused")
        context, page = await self.session(app)
        review = self.workflow.review(aid)
        # A restart before an attempt can restore the already approved payload without new consent.
        if (
            app["state"] == "authorized"
            and not await page.locator("input[name=professional_name]").input_value()
        ):
            for field in review["payload"]["form_schema"]:
                answer = review["payload"]["answers"].get(field["key"])
                if answer:
                    control = page.locator(f'[data-key="{field["key"]}"]')
                    if field["type"] == "checkbox":
                        await control.set_checked(bool(answer["value"]))
                    elif field["options"] is not None:
                        await control.select_option(str(answer["value"]))
                    else:
                        await control.fill(str(answer["value"]))
            paths = [
                self.db.one("SELECT path FROM artifacts WHERE id=%s", (a["id"],))[
                    "path"
                ]
                for a in review["payload"]["attachments"]
            ]
            await page.locator("input[type=file]").set_input_files(paths)
        if not await self.current_values_match(page, review):
            self.workflow.update_payload(aid, {"browser_hash": None})
            return {
                "state": "review",
                "message": "The visible form changed. Review the new payload before approval.",
            }
        if await page.locator("[data-challenge]").count():
            self.workflow.transition(aid, "awaiting_challenge")
            return {"state": "awaiting_challenge"}
        self.check_active(aid)
        attempt = self.workflow.begin_attempt(aid)
        # No retry around this final action. A crash after the checkpoint requires reconciliation.
        try:
            await page.evaluate("window.__jsaArm(true)")
            await page.get_by_role(
                "button", name="Submit application", exact=True
            ).click(timeout=10000)
            self.workflow.transition(aid, "receipt_verification")
            try:
                await page.locator("[data-receipt]").wait_for(timeout=5000)
            except Exception:
                self.workflow.unresolved(
                    aid,
                    "No employer confirmation appeared after the submission attempt",
                )
                return {"state": "unresolved", "attempt_id": attempt}
            evidence = await page.locator("[data-receipt]").inner_text()
            req = await page.locator("[data-receipt]").get_attribute("data-requisition")
            if req != app["requisition"] or "received" not in evidence.lower():
                self.workflow.unresolved(
                    aid, "Receipt did not identify this application"
                )
                return {"state": "unresolved"}
            path = (
                self.db.settings.state / "browser-evidence" / f"{attempt}-receipt.png"
            )
            await page.screenshot(path=str(path), full_page=True)
            return self.workflow.record_receipt(
                aid,
                attempt,
                {
                    "confirmed": True,
                    "text": evidence,
                    "requisition": req,
                    "url": page.url,
                    "screenshot": str(path),
                    "fixture": True,
                },
            )
        except Exception:
            self.workflow.unresolved(
                aid, "Worker interrupted after the durable submission checkpoint"
            )
            raise
        finally:
            try:
                await page.evaluate("window.__jsaArm?.(false)")
            except Exception:
                pass

    async def reconcile(self, aid):
        app = self.workflow.get(aid)
        if app["payload"].get("form_plan"):
            if app["payload"]["form_plan"]["spec"]["adapter"] == "greenhouse":
                return await self.greenhouse.reconcile(aid)
            return await self.portal.reconcile(aid)
        if aid not in self.sessions:
            return {
                "state": app["state"],
                "message": "No browser receipt is available. Confirm employer receipt explicitly or use the saved handoff.",
            }
        page = self.sessions[aid][1]
        if not await page.locator("[data-receipt]").count():
            return {"state": app["state"], "receipt": False}
        text = await page.locator("[data-receipt]").inner_text()
        req = await page.locator("[data-receipt]").get_attribute("data-requisition")
        attempt = self.db.one(
            "SELECT id FROM attempts WHERE application_id=%s ORDER BY started_at DESC",
            (aid,),
        )
        return self.workflow.record_receipt(
            aid,
            attempt["id"] if attempt else None,
            {
                "confirmed": True,
                "text": text,
                "requisition": req,
                "url": page.url,
                "fixture": True,
            },
        )

    def handoff(self, aid, reason):
        app = self.workflow.get(aid)
        try:
            review = self.workflow.review(aid)
        except ValueError:
            # A stale attachment or answer is a reason for intervention, not a
            # reason to lose the handoff or export unverified values as usable.
            review = {
                "state": app["state"],
                "payload": {
                    "application_id": aid,
                    "employer": app["employer"],
                    "role": app["role"],
                    "requisition": app["requisition"],
                    "application_url": app["official"].get("application_url"),
                    "answers": {},
                    "attachments": [],
                },
                "evidence_error": "Saved answers or attachments failed current evidence checks. Review and regenerate them before use.",
                "questions": self.db.all(
                    "SELECT * FROM questions WHERE application_id=%s AND (answer_fact IS NULL OR needs_review)",
                    (aid,),
                ),
            }
        outcome_unknown = (
            bool(self.db.one("SELECT 1 FROM attempts WHERE application_id=%s", (aid,)))
            or app["state"] == "unresolved"
        )
        instructions = (
            "Employer receipt is recorded. Keep this package for follow-up; do not send a duplicate application."
            if app["state"] in ("submitted", "follow_up", "rejected")
            else "Confirm the outstanding answers in the app. Preparation resumes after all answers are resolved; sending still requires approval."
            if app["state"] == "missing_answers"
            else "Check employer receipts before retrying. Do not send another application while the outcome is unknown."
            if outcome_unknown
            else "Open the employer application in your browser. Transfer the prepared answers and attach only the selected files. Review additional questions and consents before you choose Submit. Return to the app to record the employer confirmation."
        )
        path = self.db.settings.state / "handoffs" / f"{aid}.json"
        atomic_write(
            path,
            json.dumps(
                {
                    "reason": reason,
                    "application": review,
                    "instructions": instructions,
                },
                indent=2,
                default=str,
            ),
        )
        page = path.with_suffix(".html")
        lines = [
            '<!doctype html><meta charset="utf-8"><title>Application handoff</title><h1>Manual application handoff</h1>',
            "<p>" + html.escape(reason) + "</p>",
            "<p>" + html.escape(instructions) + "</p>",
            "<p>" + html.escape(review.get("evidence_error", "")) + "</p>",
            "<p>"
            + (
                "Check employer receipts before retrying. This file does not authorize another submission."
                if outcome_unknown
                else "Opening this handoff does not submit the application."
            )
            + "</p>",
            "<h2>" + html.escape(app["employer"] + " / " + app["role"]) + "</h2>",
            "<p>Requisition: " + html.escape(app["requisition"]) + "</p>",
            '<a href="'
            + html.escape(app["official"].get("application_url") or "#", quote=True)
            + '">Official application route</a>',
            "<h2>Saved answers</h2><pre>"
            + html.escape(
                json.dumps(review["payload"]["answers"], indent=2, default=str)
            )
            + "</pre>",
            "<h2>Reviewed attachments</h2><ul>",
        ]
        for artifact in review["payload"]["attachments"]:
            lines.append(
                '<li><a href="'
                + self.db.settings.origin
                + "/api/artifacts/"
                + artifact["id"]
                + '">'
                + html.escape(artifact["name"])
                + "</a> ("
                + artifact["hash"]
                + ")</li>"
            )
        lines.append("</ul>")
        if review["questions"]:
            lines.append("<h2>Answers requiring your confirmation</h2><ul>")
            for q in review["questions"]:
                lines.append(
                    "<li>"
                    + html.escape(q["prompt"])
                    + "<p>"
                    + html.escape(
                        q.get("reason", {}).get(
                            "message", "Confirm this answer in the app."
                        )
                    )
                    + "</p></li>"
                )
            lines.append("</ul>")
        atomic_write(page, "\n".join(lines))
        self.db.audit("handoff.saved", aid, {"path": str(path)})
        return {
            "state": app["state"],
            "path": str(path),
            "reason": reason,
            "review_url": "/api/handoffs/" + aid,
        }
