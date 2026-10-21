import json
import shutil
import subprocess
from typing import Literal
from pydantic import Field, model_validator
from .contracts import Contract

DESTINATION = "OpenAI through the saved Codex login"


class Route(Contract):
    model: str = Field(pattern=r"^[a-zA-Z0-9._-]+$", max_length=80)
    effort: Literal["low", "medium", "high", "xhigh"] = "medium"


DEFAULT_ROUTES = {
    "query_expansion": Route(model="gpt-6-luna", effort="low"),
    "requirement_extraction": Route(model="gpt-6-luna", effort="medium"),
    "evidence_selection": Route(model="gpt-6-luna", effort="medium"),
    "fit_assessment": Route(model="gpt-6.1-sol", effort="medium"),
    "draft": Route(model="gpt-6.1-sol", effort="medium"),
    "package_draft": Route(model="gpt-6.1-sol", effort="high"),
    "package_review": Route(model="gpt-6.1-sol", effort="medium"),
    "form_answers": Route(model="gpt-6.1-sol", effort="medium"),
    "form_answer_review": Route(model="gpt-6.1-sol", effort="medium"),
    "conflict_review": Route(model="gpt-6-astra", effort="high"),
}


class LLMConfig(Contract):
    backend: Literal["fixture", "codex_exec"] = "fixture"
    enabled: bool = False
    timeout_seconds: int = Field(default=240, ge=10, le=900)
    routes: dict[str, Route] = Field(default_factory=lambda: dict(DEFAULT_ROUTES))

    @model_validator(mode="after")
    def include_new_routes(self):
        self.routes = dict(DEFAULT_ROUTES) | self.routes
        return self

    def route(self, task):
        return self.routes.get(task, DEFAULT_ROUTES[task])


def catalog():
    if not shutil.which("codex"):
        return {"available": False, "models": [], "error": "Install Codex CLI"}
    try:
        p = subprocess.run(
            ["codex", "debug", "models"], capture_output=True, text=True, timeout=20
        )
        if p.returncode:
            raise ValueError()
        models = json.loads(p.stdout)["models"]
        version = subprocess.run(
            ["codex", "--version"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        return {
            "available": True,
            "version": version,
            "models": [
                {
                    "model": m["slug"],
                    "efforts": [
                        x["effort"] for x in m.get("supported_reasoning_levels", [])
                    ],
                }
                for m in models
                if m.get("visibility") != "hide"
            ],
            "note": "Catalog availability is not a guarantee of account access.",
        }
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return {"available": False, "models": [], "error": "Codex catalog unavailable"}
