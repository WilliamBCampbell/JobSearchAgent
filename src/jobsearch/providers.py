"""Providers have no tools or action authority. Network destinations require explicit opt-in."""

from __future__ import annotations
import hashlib
import math
import re
from urllib.parse import urlparse
import httpx
from pydantic import BaseModel, Field
from typing import Literal
from .config import digest


class ProviderConfig(BaseModel):
    provider: str = "fixture"
    destination: str = ""
    embedding_model: str = "fixture-concepts-v1"
    model: str = "fixture-extractive-v1"
    enabled: bool = False
    retrieval_mode: Literal["hybrid", "lexical"] = "hybrid"


class DraftSelection(BaseModel):
    fact_ids: list[str] = Field(default_factory=list)
    requirement_assessments: list[dict] = Field(default_factory=list)
    missing_questions: list[str] = Field(default_factory=list)


class Providers:
    def __init__(self, db):
        self.db = db

    @property
    def config(self):
        return ProviderConfig(**self.db.setting("provider", {}))

    @property
    def embedding_id(self):
        c = self.config
        return (
            "fixture-concepts-v1"
            if c.provider == "fixture"
            else digest([c.provider, c.destination, c.embedding_model])
        )

    def configure(self, config: ProviderConfig):
        if config.provider not in ("fixture", "ollama"):
            raise ValueError(
                "Supported providers: fixture, ollama (local or explicitly enabled remote server)"
            )
        if config.provider != "fixture":
            u = urlparse(config.destination)
            if (
                u.scheme not in ("http", "https")
                or not u.hostname
                or u.username
                or u.password
                or u.query
                or u.fragment
            ):
                raise ValueError(
                    "Use a server URL without credentials or query parameters"
                )
            if u.scheme != "https" and u.hostname not in (
                "localhost",
                "127.0.0.1",
                "::1",
            ):
                raise ValueError("Remote destinations require HTTPS")
        self.db.set_setting("provider", config.model_dump())
        self.db.audit(
            "provider.configured",
            config.provider,
            {"enabled": config.enabled, "destination": config.destination},
        )

    def _post(self, route, payload):
        c = self.config
        if not c.enabled:
            raise ValueError(
                "Enable the displayed data destination before sending evidence"
            )
        with httpx.Client(
            timeout=60, follow_redirects=False, trust_env=False
        ) as client:
            response = client.post(c.destination.rstrip("/") + route, json=payload)
            response.raise_for_status()
            return response.json()

    def embed(self, texts: list[str]) -> list[list[float]]:
        c = self.config
        if c.provider != "fixture":
            result = self._post(
                "/api/embed",
                {"model": c.embedding_model, "input": texts, "truncate": False},
            )["embeddings"]
            if (
                len(result) != len(texts)
                or not result
                or not all(math.isfinite(x) for v in result for x in v)
            ):
                raise ValueError("Invalid embeddings")
            return result
        # Offline surrogate tests retrieval mechanics. It is explicitly not a learned semantic model.
        synonyms = {
            "postgresql": "database",
            "postgres": "database",
            "sql": "database",
            "travel": "travel",
            "trips": "travel",
            "journeys": "travel",
            "csharp": "dotnet",
            "salary": "compensation",
            "pay": "compensation",
            "employment": "work",
            "employed": "work",
            "engineer": "engineering",
        }
        vectors = []
        for text in texts:
            vector = [0.0] * 96
            for word in re.findall(r"[a-z0-9]+", text.lower().replace("c#", "csharp")):
                word = synonyms.get(word, word)
                h = hashlib.sha256(word.encode()).digest()
                vector[int.from_bytes(h[:2], "big") % 96] += 1 if h[2] % 2 else -1
            norm = math.sqrt(sum(v * v for v in vector)) or 1
            vectors.append([v / norm for v in vector])
        return vectors

    def select_evidence(
        self, app, job_id=None, progress=lambda value: None, cancelled=lambda: False
    ):
        # All language-model judgments go through the durable inference boundary.
        from .llm.service import InferenceService
        from .llm.contracts import InferenceRequest

        result = InferenceService(self.db).run(
            InferenceRequest(
                task="evidence_selection",
                candidate=app["candidate"],
                application_id=app["id"],
            ),
            job_id,
            progress,
            cancelled,
        )
        return DraftSelection.model_validate(result["output"])
