"""Instruction delivery, review gates, and cache invalidation without hosted calls."""

import pytest

from jobsearch.documents.content import fixture_document
from jobsearch.llm.contracts import InferenceRequest
from jobsearch.llm.instructions import REFERENCE, load_instructions, writing_skill_path
from jobsearch.llm.routing import LLMConfig
from jobsearch.llm.runner import InferenceFailure
from jobsearch.llm.service import InferenceService
from jobsearch.packages import Packages
from .test_llm import application


def request_for(service, app, task):
    draft_request = InferenceRequest(task="package_draft", application_id=app["id"])
    document = (
        fixture_document(service.context(draft_request))
        if task == "package_review"
        else None
    )
    return InferenceRequest(
        task=task, application_id=app["id"], package_document=document
    )


@pytest.mark.parametrize("task", ["draft", "package_draft", "package_review"])
def test_full_skill_delivery_and_file_changes_invalidate_cache(
    db, writing_skill, monkeypatch, task
):
    app = application(db)
    service = InferenceService(db)
    service.configure(LLMConfig(backend="codex_exec", enabled=True))
    request = request_for(service, app, task)
    prompts = []

    def execute(prompt, *args):
        prompts.append(prompt)
        assert writing_skill.read_text() in prompt
        assert (writing_skill.parent / REFERENCE).read_text() in prompt
        assert "Run its complete self-check" in prompt
        assert "Unresolved prose or evidence issues require passed=false" in prompt
        return service.fixture(request, service.context(request)), {}

    monkeypatch.setattr("jobsearch.llm.service.execute", execute)
    first = service.run(request)
    assert service.run(request)["cached"]
    for path in [writing_skill, writing_skill.parent / REFERENCE]:
        path.write_text(path.read_text() + "\nAn updated instruction.\n")
        refreshed = service.run(request)
        assert not refreshed["cached"]
        assert refreshed["run_id"] != first["run_id"]
        assert service.run(request)["cached"]
    assert len(prompts) == 3
    events = db.all("SELECT detail FROM llm_events WHERE event='started'")
    assert all(len(e["detail"]["instruction_sources"]) >= 3 for e in events)
    assert all(
        len(source["sha256"]) == 64
        for event in events
        for source in event["detail"]["instruction_sources"]
    )


@pytest.mark.parametrize("missing", ["SKILL.md", REFERENCE])
def test_missing_instructions_block_even_cached_results(db, writing_skill, missing):
    app = application(db)
    service = InferenceService(db)
    request = request_for(service, app, "package_review")
    service.run(request)
    (writing_skill.parent / missing).unlink()
    with pytest.raises(InferenceFailure, match="Required instruction file"):
        service.run(request)
    assert len(db.all("SELECT * FROM llm_runs")) == 1


def test_instruction_change_during_review_rejects_output(
    db, writing_skill, monkeypatch
):
    app = application(db)
    service = InferenceService(db)
    service.configure(LLMConfig(backend="codex_exec", enabled=True))
    request = request_for(service, app, "package_review")

    def execute(*args):
        writing_skill.write_text("Changed instructions")
        return {"passed": True, "issues": []}, {}

    monkeypatch.setattr("jobsearch.llm.service.execute", execute)
    with pytest.raises(InferenceFailure, match="instructions changed"):
        service.run(request)
    assert db.one("SELECT status FROM llm_runs")["status"] == "failed"


def test_installed_skill_precedes_repository_fallback(tmp_path, monkeypatch):
    from jobsearch.llm import instructions

    monkeypatch.delenv("JSA_NO_AI_SLOP_SKILL", raising=False)
    monkeypatch.setattr(instructions.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(instructions, "REPO", tmp_path / "repo" / "app")
    installed = tmp_path / "home/.agents/skills/no-ai-slop/SKILL.md"
    fallback = tmp_path / "repo/no_ai_slop_writing_rules/skills/no-ai-slop/SKILL.md"
    fallback.parent.mkdir(parents=True)
    fallback.write_text("Repository skill")
    assert writing_skill_path() == fallback
    installed.parent.mkdir(parents=True)
    installed.write_text("Installed skill")
    assert writing_skill_path() == installed
    monkeypatch.setenv("JSA_NO_AI_SLOP_SKILL", str(tmp_path / "missing/SKILL.md"))
    assert writing_skill_path() == tmp_path / "missing/SKILL.md"


def test_nonwriting_task_does_not_require_external_skill(tmp_path, monkeypatch):
    monkeypatch.setenv("JSA_NO_AI_SLOP_SKILL", str(tmp_path / "missing/SKILL.md"))
    _, sources = load_instructions("query_expansion")
    assert len(sources) == 1


def test_prose_review_failure_blocks_package(db, monkeypatch):
    app = application(db)
    original = InferenceService.fixture

    def review(self, request, packet):
        if request.task == "package_review":
            return {
                "passed": False,
                "issues": [
                    "Cover-letter opening: rule 14, synthetic enthusiasm. "
                    "Replace it with a supported example of API delivery."
                ],
            }
        return original(self, request, packet)

    monkeypatch.setattr(InferenceService, "fixture", review)
    with pytest.raises(InferenceFailure, match="rule 14"):
        Packages(db).prepare(app)
    assert not db.all("SELECT * FROM artifacts")
    assert len(db.all("SELECT * FROM llm_runs WHERE task='package_draft'")) == 2
