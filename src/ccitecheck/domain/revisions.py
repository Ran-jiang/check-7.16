"""供后端和前端共同遵守的安全修订协议。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class RevisionProposal(BaseModel):
    strategy: Literal[
        "replace_exact_text",
        "replace_citation_locator",
        "replace_case_metadata",
        "manual_edit",
    ]
    original_text: str
    revised_text: str | None = None
    rationale: str
    machine_applicable: bool = False
    preconditions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_machine_revision(self) -> "RevisionProposal":
        if self.machine_applicable:
            if self.strategy == "manual_edit":
                raise ValueError("manual_edit cannot be machine applicable")
            if not self.original_text or not self.revised_text:
                raise ValueError("machine-applicable revision requires exact original and revised text")
            if self.original_text == self.revised_text:
                raise ValueError("revision must change the original text")
        return self


__all__ = ["RevisionProposal"]
