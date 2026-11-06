"""Visitor-reviewed planning notes, never account facts or configuration authority."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

Label = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=80)]
Value = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=500)]
Gap = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=200)]


class BriefFact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    label: Label
    value: Value


class PlanningBrief(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    format: Literal["xakore-planning-brief"]
    version: Literal[1]
    offer: Literal["Connect", "Jibsy", "Network operations", "Connected devices", "Discovery"]
    facts: list[BriefFact] = Field(default_factory=list, max_length=24)
    gaps: list[Gap] = Field(default_factory=list, max_length=6)

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int:
            raise ValueError("The brief version must be 1.")
        return value

    @model_validator(mode="after")
    def bounded_text(self):
        if not self.facts and not self.gaps:
            raise ValueError("The brief needs at least one note or open question.")
        try:
            size = len(json.dumps(self.model_dump(), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        except UnicodeEncodeError:
            raise ValueError("The brief contains invalid text.") from None
        if size > 18_000:
            raise ValueError("The brief is too large. Shorten its notes before importing.")
        if any("\x00" in text for text in self.texts()):
            raise ValueError("The brief contains invalid text.")
        return self

    def texts(self) -> list[str]:
        return [*[f.label + ": " + f.value for f in self.facts], *self.gaps]

    def context(self) -> dict:
        return {"source": "visitor_reviewed_website_notes", "verified_account_facts": False, **self.model_dump()}
