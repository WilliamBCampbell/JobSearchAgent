from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Claim(Contract):
    text: str = Field(min_length=1, max_length=1000)
    fact_ids: list[str] = Field(min_length=1)


class SkillGroup(Claim):
    label: str = Field(min_length=1, max_length=80)


class Experience(Contract):
    employment_fact_id: str
    bullets: list[Claim] = Field(min_length=1, max_length=6)


class LetterParagraph(Claim):
    source_refs: list[str]


class PackageDraft(Contract):
    summary: Claim
    skills: list[SkillGroup] = Field(min_length=1, max_length=4)
    experience: list[Experience] = Field(max_length=12)
    education: list[Claim] = Field(max_length=3)
    cover_letter: list[LetterParagraph] = Field(min_length=2, max_length=4)
    missing_questions: list[str]


class PackageReview(Contract):
    passed: bool
    issues: list[str]
