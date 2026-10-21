from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from ..documents.contracts import PackageDraft, PackageReview

Task = Literal[
    "query_expansion",
    "requirement_extraction",
    "evidence_selection",
    "fit_assessment",
    "draft",
    "conflict_review",
    "package_draft",
    "package_review",
    "form_answers",
    "form_answer_review",
]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FormQuestion(Contract):
    purpose: str = "ordinary"
    binding: str = "standard"
    option_ids: dict[str, str | int] = Field(default_factory=dict)
    free_form_options: list[str] = Field(default_factory=list)
    key: str
    name: str
    label: str
    type: str
    required: bool
    options: list[str]
    material: bool
    qualification: bool


class FormAnswer(Contract):
    key: str
    decision: Literal["answer", "draft", "needs_input"]
    value: str | list[str] | None
    fact_ids: list[str]
    source_refs: list[str]
    reason: str


class FormAnswers(Contract):
    answers: list[FormAnswer]


class AnswerCheck(Contract):
    key: str
    supported: bool
    issue: str


class FormAnswerReview(Contract):
    checks: list[AnswerCheck]


class InferenceRequest(Contract):
    task: Task
    candidate: Literal["real", "fixture"] = "fixture"
    application_id: str | None = None
    question: str = Field(default="", max_length=4000)
    source_versions: list[str] = Field(default_factory=list, max_length=12)
    package_document: PackageDraft | None = None
    previous_package_document: PackageDraft | None = None
    revision_notes: str = Field(default="", max_length=2000)
    form_fields: list[FormQuestion] = Field(default_factory=list, max_length=40)
    answer_proposals: FormAnswers | None = None


class Queries(Contract):
    queries: list[str] = Field(min_length=1, max_length=6)


class SelectedEvidence(Contract):
    fact_ids: list[str]
    missing_questions: list[str]


class Requirement(Contract):
    text: str
    source: str
    category: str
    classification: Literal["required", "preferred"]
    central: bool


class Requirements(Contract):
    requirements: list[Requirement]
    missing_questions: list[str]


class AssessedRequirement(Requirement):
    material: bool
    assessment: Literal["strong", "partial", "unconfirmed", "gap", "hard_stop"]
    fact_ids: list[str]
    deduction: float = Field(ge=0)
    reason: str


class SalaryRange(Contract):
    min: float | None
    max: float | None


class Compensation(Contract):
    base: SalaryRange | None
    bonus: str | None
    commission: str | None
    equity: str | None
    ote: str | None
    classification: Literal["unknown", "ambiguous", "base"]


class Fit(Contract):
    track: Literal["software", "applied_ai", "solutions"]
    requirements: list[AssessedRequirement]
    ohio: Literal["confirmed", "us_remote_unspecified", "unknown", "excluded"]
    compensation: Compensation
    missing_questions: list[str]


class Paragraph(Contract):
    text: str
    fact_ids: list[str]
    source_refs: list[str]


class Draft(Contract):
    paragraphs: list[Paragraph]
    missing_questions: list[str]


class ConflictReview(Contract):
    summary: str
    fact_ids: list[str]
    source_refs: list[str]
    missing_questions: list[str]


OUTPUTS = {
    "query_expansion": Queries,
    "requirement_extraction": Requirements,
    "evidence_selection": SelectedEvidence,
    "fit_assessment": Fit,
    "draft": Draft,
    "conflict_review": ConflictReview,
    "package_draft": PackageDraft,
    "package_review": PackageReview,
    "form_answers": FormAnswers,
    "form_answer_review": FormAnswerReview,
}
