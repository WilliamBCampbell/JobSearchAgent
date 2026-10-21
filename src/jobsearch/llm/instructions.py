"""Load trusted local instructions explicitly for tool-free inference workers."""

import os
from pathlib import Path

from ..config import REPO, digest
from .runner import InferenceFailure

WRITING_TASKS = {
    "draft",
    "package_draft",
    "package_review",
    "form_answers",
    "form_answer_review",
}
REFERENCE = "references/ai-writing-detection.md"


def writing_skill_path():
    configured = os.getenv("JSA_NO_AI_SLOP_SKILL")
    if configured:
        return Path(configured).expanduser()
    candidates = [
        Path.home() / ".agents/skills/no-ai-slop/SKILL.md",
        REPO.parent / "no-ai-slop/skills/no-ai-slop/SKILL.md",
        REPO.parent / "no-ai-slop/SKILL.md",
        REPO.parent / "no_ai_slop_writing_rules/skills/no-ai-slop/SKILL.md",
    ]
    return next((p for p in candidates if p.is_file()), candidates[0])


def load_instructions(task):
    paths = [REPO / ".agents/skills/llm-evaluation/SKILL.md"]
    if task in {"package_draft", "package_review"}:
        paths.append(REPO / ".agents/skills/package-writing/SKILL.md")
    if task in {"form_answers", "form_answer_review"}:
        paths.append(REPO / ".agents/skills/application-answers/SKILL.md")
    if task in WRITING_TASKS:
        skill = writing_skill_path()
        paths.extend([skill, skill.parent / REFERENCE])
    parts, sources = [], []
    for path in paths:
        try:
            content = path.read_text(encoding="utf-8")
            if not content.strip():
                raise ValueError("Empty instruction file")
        except (OSError, UnicodeError, ValueError) as exc:
            raise InferenceFailure(
                f"Required instruction file is missing or unreadable: {path}. "
                "Install the complete no-ai-slop skill or set JSA_NO_AI_SLOP_SKILL "
                "to its SKILL.md, with references/ai-writing-detection.md alongside it."
            ) from exc
        parts.append(content)
        sources.append(
            {"path": str(path.resolve()), "sha256": digest(content.encode())}
        )
    if task in {"fit_assessment", "requirement_extraction"}:
        parts.append(
            "Use us_remote_unspecified for a U.S.-remote role when Ohio is not named. This is acceptable under the candidate’s search policy: use zero deduction and material=false for the absent state listing. Preserve explicit state/country restrictions and travel, relocation, schedule, or onsite commitments as separate requirements. Do not label unspecified Ohio eligibility confirmed. When compensation is not listed, retain classification=unknown, use zero deduction and material=false for the missing pay disclosure, and do not request employer pay confirmation as a qualification. A required salary-expectations answer remains application-specific. Confirmed driving distance of 0 to 100 miles earns a separate ranking preference; do not infer distance from a city label or invent mileage. Open-ended application prompts requesting project examples, GitHub contributions, or descriptions of agentic-AI work belong to answer preparation. Mark these prompts material=false, central=false, deduction=0. Assess any separate posting requirements for that experience normally. Explicit tenure, credentials, eligibility, and other required qualification thresholds still need evidence."
        )
    if task in WRITING_TASKS:
        parts.append(
            "Apply the supplied no-ai-slop skill and its full reference guide to "
            "all authored prose. The SKILL.md rules take precedence over conflicting "
            "reference examples. Run its complete self-check before returning JSON. "
            "Style examples are not candidate or employer evidence. Preserve exact "
            "source quotations, confirmed names and titles, technical meaning, and "
            "necessary uncertainty. Never invent details to make prose more specific. "
            "For package_review, assess both evidence and prose against these rules; "
            "identify the passage, violated rule, and concrete correction in issues. "
            "Unresolved prose or evidence issues require passed=false. "
            "Return the existing task schema only, without a self-check transcript."
        )
    if task in {"package_draft", "package_review"}:
        parts.append(
            "User-requested cover-letter voice takes precedence over generic style "
            "rules: be cordial, open with interest in the position and an "
            "evidence-grounded reason to apply. Do not reject ordinary interest or "
            "courtesy as synthetic enthusiasm. The renderer adds thanks for the "
            "reader's time and a sentence looking forward to discussing the "
            "company's needs and how the candidate could contribute. Do not "
            "duplicate that closing in the body or invent personal motivations."
        )
    return "\n\n".join(parts), sources
