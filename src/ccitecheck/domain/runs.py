"""Scheduler 的流程历史与下一步决策模型。"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class RunState(str, Enum):
    NEW = "new"
    HYPOTHESIS_READY = "hypothesis_ready"
    RETRIEVING = "retrieving"
    EVIDENCE_READY = "evidence_ready"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    OUTPUT = "output"
    STOPPED = "stopped"


class SourceAttempt(BaseModel):
    source_id: str
    hypothesis_id: str
    evidence_id: str | None = None
    technical_status: str
    model_config = ConfigDict(extra="forbid", frozen=True)


class SchedulerDecision(BaseModel):
    kind: Literal[
        "output", "next_source", "technical_retry", "rebuild_hypothesis", "stop"
    ]
    reason: str
    source_id: str | None = None
    feedback: dict[str, Any] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid", frozen=True)


class VerificationRun(BaseModel):
    run_id: str = Field(default_factory=lambda: str(uuid4()))
    claim_id: str
    state: RunState = RunState.NEW
    hypothesis_history: list[str] = Field(default_factory=list)
    evidence_history: list[str] = Field(default_factory=list)
    verification_history: list[str] = Field(default_factory=list)
    source_attempts: list[SourceAttempt] = Field(default_factory=list)
    technical_retry_count: int = 0
    hypothesis_retry_count: int = 0
    max_technical_retries: int = 2
    max_hypothesis_retries: int = 3
    terminal_reason: str | None = None
    model_config = ConfigDict(extra="forbid")


__all__ = [
    "RunState",
    "SchedulerDecision",
    "SourceAttempt",
    "VerificationRun",
]
