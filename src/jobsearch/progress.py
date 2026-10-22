"""Saved checkpoints and the next pre-submission action, shared by UI and API."""

from datetime import datetime, timedelta, timezone
from typing import Literal
from pydantic import BaseModel, Field

ACTIVE = {
    "discovered",
    "verified",
    "scored",
    "selected",
    "preparing",
    "prepared",
    "filling",
    "missing_answers",
    "review",
    "manual_handoff",
}
LEGACY = {
    "Greenhouse browser completion needs a portal adapter; use the saved answer plan",
    "Additional demographic or consent questions need review before browser filling",
}


class Progress(BaseModel):
    step: int
    title: str
    current: str
    instruction: str
    following: str
    button: str
    action: str = ""
    target: str = "role-fit"
    status: Literal["ready", "blocked", "working", "paused"] = "ready"
    completed: list[bool] = Field(default_factory=lambda: [False] * 6)
    blockers: list[str] = Field(default_factory=list)
    opening_blocked: bool = False


def opening_current(official):
    try:
        at = datetime.fromisoformat(
            official.get("verified_at", "").replace("Z", "+00:00")
        )
        return bool(
            official.get("open")
            and official.get("application_url")
            and at.tzinfo
            and at > datetime.now(timezone.utc) - timedelta(hours=24)
        )
    except (TypeError, ValueError):
        return False


def document_review_needed(app):
    package = app["payload"].get("package", {})
    version = package.get("version")
    return bool(
        version and app["payload"].get("document_review", {}).get("version") != version
    )


def progress(app, review_hash=None, review_error=None):
    """Pure projection. Reading a page never changes an application's state."""
    state, payload = app["state"], app["payload"]
    if (
        app.get("imported")
        or state not in ACTIVE
        or app.get("attempts")
        or app.get("receipts")
    ):
        return None
    package = bool(payload.get("package"))
    selected = package or state not in {"discovered", "verified", "scored"}
    docs = package and not document_review_needed(app)
    pending = [
        q
        for q in app.get("questions", [])
        if not q.get("answer_fact") or q.get("needs_review")
    ]
    spec = payload.get("form_plan", {}).get("spec", {})
    answers = (
        bool(payload.get("form_plan") or payload.get("form_schema")) and not pending
    )
    done = [selected, docs, answers, False, False, False]
    result = Progress(
        step=0,
        title="Check this role before proceeding",
        current="The role and its evidence are available below.",
        instruction="Review the assessment and decide whether to proceed.",
        following="Selection starts document preparation.",
        button="Review selection decision",
        target="role-selection",
        completed=done,
    )
    if not opening_current(app.get("official", {})):
        result.title = "Refresh the official opening"
        result.current = "The opening has not been verified in the past 24 hours. Saved documents and answers are retained."
        result.instruction = (
            "Check that this requisition is still open before continuing."
        )
        result.following = (
            "The app refreshes the opening and checks fit against the current evidence."
        )
        result.button, result.action = "Verify official opening", "refresh-opening"
        result.status = "blocked"
        result.completed[0] = False
        result.opening_blocked = True
        official = app.get("official", {})
        if official.get("status") == "unavailable":
            result.title = "This opening is no longer available"
            result.current = (
                official.get("reason") or "The employer no longer lists this opening."
            )
            result.instruction = "Inspect the employer posting, keep your saved documents, or discard this role from active work."
            result.following = "Your documents and answers remain saved. Continuing requires a current verified opening."
            result.button = "Check opening again"
        elif not official.get("open") and (
            official.get("errors") or official.get("reason")
        ):
            result.title = "The opening could not be verified"
            result.current = "The last check did not establish an open employer posting. This can happen when a posting is removed or the employer site cannot be reached."
            result.instruction = "Inspect the employer posting before retrying. You can view saved documents or discard this role."
            result.following = "Your documents and answers remain saved. Continuing requires a current verified opening."
            result.button = "Retry opening check"
    elif not app.get("score", {}).get("scores"):
        result.title = "Evaluate the role against your evidence"
        result.current = "The official opening is verified. Fit has not been assessed."
        result.instruction = "Start the assessment to see requirements, deductions, and unresolved questions."
        result.button, result.action = "Evaluate fit", "evaluate"
    elif app["score"].get("evidence_changed"):
        result.title = "Reassess the changed posting"
        result.current = "The refreshed employer posting differs from the assessed version. Documents and answers are saved."
        result.instruction = "Rescore against the current posting before continuing."
        result.following = "Review any new gaps, then continue with the saved package."
        result.button, result.target, result.status = (
            "Review fit and rescore",
            "central-requirements",
            "blocked",
        )
        result.completed[0] = False
    elif not selected:
        conditional = not app["score"].get("primary")
        result.title = "Decide whether to pursue this role"
        result.instruction = (
            "Review the uncertainties and record a reason if you want to prepare a conditional application. Sending remains blocked until the fit checks pass."
            if conditional
            else "Review the fit assessment and select this role."
        )
    elif not package:
        result.step = 1
        result.title = "Prepare your application documents"
        result.current = (
            "This role is selected and passes the fit checks."
            if app["score"].get("primary")
            else "This conditional role is selected for preparation. Resolve the fit concerns before sending."
        )
        result.instruction = (
            "Generate the resume and cover letter, then review their previews."
        )
        result.following = "Approve the document version or request changes before preparing the employer answers."
        result.button, result.action = "Prepare documents", "prepare"
        if state == "preparing":
            result.title = "Document preparation stopped"
            result.current = "No document version was published. Your role selection and source evidence are saved."
            result.instruction = (
                "Check the task error under Work progress, then retry preparation."
            )
            result.button = "Retry document preparation"
    elif not app["score"].get("primary"):
        result.title = "Resolve the fit blockers before continuing"
        result.current = "Your selection, documents, and answers are saved. The assessment does not pass the sending checks."
        result.instruction = "Review the concerns below, correct requirement evidence where needed, then rescore. You can still inspect and revise saved documents."
        result.following = "When fit passes, continue from the first unfinished step. Reviewed documents do not need to be generated again."
        result.button, result.target, result.status = (
            "Review fit blockers",
            "role-fit",
            "blocked",
        )
        result.completed[0] = False
        result.blockers = list(app["score"].get("reasons", []))
    elif state == "scored":
        result.title = "Confirm this role after reevaluation"
        result.current = (
            "The updated fit passes. Your package and answers remain saved."
        )
        result.instruction = "Select the role to continue with the saved package."
        result.following = "Continue from document review or browser filling without regenerating reviewed files."
        result.completed[0] = False
    elif review_error and any(
        word in review_error.lower() for word in ("attachment", "attached document")
    ):
        result.step = 1
        result.title = "Check the saved documents before continuing"
        result.current = review_error
        result.instruction = "If career evidence changed, request a document revision. If a file was moved or edited outside the app, restore the saved file before continuing."
        result.following = (
            "The agent checks the selected files again before filling or sending."
        )
        result.button, result.target, result.status = (
            "Review document files",
            "package-review",
            "blocked",
        )
        result.completed[1] = False
    elif document_review_needed(app) or state == "preparing":
        result.step = 1
        result.title = "Review your resume and cover letter"
        result.current = (
            "Your documents are saved. The current version needs your review."
        )
        result.instruction = (
            "Read the previews, suggest changes, or choose Documents look good."
        )
        result.following = "Inspect the employer form and prepare answers."
        result.button, result.target = (
            "Review documents and suggest changes",
            "package-review",
        )
        if state == "preparing":
            result.title = "Document preparation needs attention"
            result.instruction = "Review the work result and retry preparation. Earlier files remain available."
            result.button, result.target = "Review preparation result", "work-progress"
    elif pending:
        result.step = 2
        result.title = f"Answer {len(pending)} outstanding {'question' if len(pending) == 1 else 'questions'}"
        result.current = (
            "Your documents are reviewed. The agent needs these answers to continue."
        )
        result.instruction = "Confirm the missing answers. Choose Global only for answers that apply across applications."
        result.following = "After the last answer, the agent refreshes the answer plan. Any new qualification returns to fit review."
        result.button, result.target = "Go to questions", "question-" + pending[0]["id"]
    elif payload.get("form_plan"):
        result.step = 3
        result.title = "Review answers and create your submission prompt"
        result.current = "The employer fields and prepared answers are saved. Nothing has been submitted."
        result.instruction = "Review the answers and documents, then create and copy the Codex prompt. Each answer can also be copied for manual entry."
        result.following = "Complete the application in Codex CLI or your browser, then record employer confirmation."
        result.button, result.target = "Go to Codex handoff", "submission-handoff"
    elif (
        state == "manual_handoff"
        and spec.get("unsupported")
        and any(r not in LEGACY for r in spec["unsupported"])
    ):
        return None  # Existing manual-completion and receipt controls remain explicit.
    elif (
        state == "review" and review_hash and payload.get("browser_hash") == review_hash
    ):
        result.step = 3
        result.title = "Review and approve this application"
        result.current = "Your documents are reviewed and the application is filled. Nothing has been submitted."
        result.instruction = "Check the answers, attachments, salary, and employer notices in final review."
        result.following = "Approve & send authorizes these exact answers and files. The agent then checks for a receipt."
        result.button, result.target = "Go to final review", "final-review"
    else:
        result.step = 2
        result.title = "Continue with the employer application"
        result.current = "Your reviewed documents and saved answers are ready to reuse."
        result.instruction = "Let the agent open the current form, extract its fields, and prepare answers from your profile."
        result.following = "Review the prepared answers, then copy them or create the Codex submission prompt."
        result.button, result.action = (
            "Inspect application and prepare answers",
            "continue",
        )
        if spec.get("unsupported") and all(r in LEGACY for r in spec["unsupported"]):
            result.title = "Refresh the form to continue with the agent"
            result.button = "Refresh form and prepare answers"
    jobs = [
        j
        for j in app.get("jobs", [])
        if j["status"] in ("queued", "running")
        and not (pending and state == "missing_answers" and j["kind"] == "fill")
    ]
    if jobs:
        job = sorted(jobs, key=lambda j: str(j.get("created_at", "")))[0]
        kind = job["kind"]
        result.step = {
            "refresh_opening": 0,
            "rescore": 0,
            "form_fit": 0,
            "llm": 0,
            "draft_answer_context": 2,
            "draft_form_answer": 2,
            "package": 1,
            "fill": 2,
        }.get(kind, result.step)
        result.title = {
            "refresh_opening": "Checking the official opening",
            "rescore": "Reassessing fit",
            "form_fit": "Checking new employer requirements",
            "llm": "Evaluating evidence",
            "draft_answer_context": "Drafting your project examples",
            "draft_form_answer": "Preparing a suggested answer",
            "package": "Preparing your documents",
            "fill": "Preparing the employer application",
        }.get(kind, "Work is in progress")
        result.current = (
            "This task is queued."
            if job["status"] == "queued"
            else "The agent is working on this step."
        )
        result.instruction = (
            "Wait for the result. You can inspect progress or cancel this task below."
        )
        result.button, result.action, result.target, result.status = (
            "View work progress",
            "",
            "work-progress",
            "working",
        )
    if app.get("paused"):
        result.title, result.instruction = (
            "This application is paused",
            "Resume to continue from the saved step.",
        )
        result.button, result.action, result.status = (
            "Resume this application",
            "resume",
            "paused",
        )
    return result.model_dump()
