"""Small typed collaboration contracts; no generic event bus."""

from datetime import datetime, timezone
from typing import Any, Literal, TypedDict
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.core.enums import IntentType, RiskLevel


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskSpec(StrictModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    kind: Literal["referral"] = "referral"
    constraints: dict[str, str | bool | None] = Field(default_factory=dict)
    max_steps: int = Field(default=8, ge=1, le=20)
    max_tool_calls: int = Field(default=6, ge=1, le=12)
    timeout_seconds: float = Field(default=15, gt=0, le=120)


class ToolObservation(StrictModel):
    tool: str = Field(min_length=1, max_length=100)
    status: Literal[
        "success", "empty", "timeout", "error", "denied", "unknown", "repeat"
    ]
    data: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[str] = Field(default_factory=list)
    error: str | None = None
    latency_ms: float = Field(default=0, ge=0)


class SafetyReview(StrictModel):
    approved: bool
    subject: Literal["final_text"] = "final_text"
    draft_id: str
    reason: str
    mode: Literal["safety_template", "model_stream", "referral_result"]


class IntentPayload(StrictModel):
    intent: IntentType


class RiskPayload(StrictModel):
    risk_level: RiskLevel
    emotion: str
    confidence: float = Field(ge=0, le=1)


class EvidenceRef(StrictModel):
    source: str
    chunk_id: int | None
    score: float


class RetrievalPayload(StrictModel):
    items: list[EvidenceRef]


class ReferralPayload(StrictModel):
    resource_ids: list[str]
    simulated: Literal[True] = True
    stop_reason: str


class ResponsePayload(StrictModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    chars: int = Field(ge=0)


_PAYLOADS = {
    "intent": IntentPayload,
    "risk_assessment": RiskPayload,
    "retrieval_evidence": RetrievalPayload,
    "referral_options": ReferralPayload,
    "response_draft": ResponsePayload,
    "safety_review": SafetyReview,
}


class Artifact(StrictModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    kind: Literal[
        "intent",
        "risk_assessment",
        "retrieval_evidence",
        "referral_options",
        "response_draft",
        "safety_review",
    ]
    producer: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    schema_version: Literal[1] = 1
    payload: dict[str, Any]
    source_refs: list[str] = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_payload(self):
        self.payload = (
            _PAYLOADS[self.kind].model_validate(self.payload).model_dump(mode="json")
        )
        return self


class TurnState(TypedDict, total=False):
    turn_id: str
    input_ref: str
    user_id: int
    display_name: str
    session_id: int
    session_public_id: str
    model_input: str
    prechecked: bool
    memory_loaded: bool
    risk_assessed: bool
    intent_routed: bool
    knowledge_handled: bool
    response_planned: bool
    finished: bool
    memory_brief: str
    intent: IntentType | None
    risk_level: RiskLevel
    assessment: Any
    knowledge_query: str
    retrieved_knowledge: list
    model_history: list
    response_messages: list
    response_agent: str
    response_plan: str
    steps: list
    artifacts: list[Artifact]
    task: TaskSpec | None
    observations: list[ToolObservation]
    stop_reason: str
    referral_result: Any
