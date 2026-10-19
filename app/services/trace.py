
from __future__ import annotations

import json
import hashlib
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from app.agents.runtime import AgentRunResult
from app.models.entities import AgentRunTrace, ChatSession, UserAccount
from app.agents.contracts import Artifact, SafetyReview


class AgentTraceService:
    def __init__(self, db: Session):
        self.db = db

    def save_run(
        self,
        user: UserAccount,
        session: ChatSession,
        original_input: str,
        sanitized_input: str,
        memory_brief: str,
        agent_run: AgentRunResult,
        report_id: int | None,
    ) -> AgentRunTrace:
        trace = AgentRunTrace(
            user_id=user.id,
            session_id=session.id,
            report_id=report_id,
            intent=agent_run.intent.value,
            risk_level=agent_run.risk_level.value,
            original_input="",
            sanitized_input="",
            memory_brief="",
            agent_steps_json=_json(agent_run.steps),
            retrieved_knowledge_json=_json([{"source": item.source, "chunk_id": item.chunk_id, "score": item.score}
                                            for item in agent_run.retrieved_knowledge]),
            response_messages_json="[]",
            assessment_json=_json({"risk": agent_run.risk_level.value, "turn_id": agent_run.turn_id,
                                   "stop_reason": agent_run.stop_reason,
                                   "artifacts": agent_run.artifacts, "model_calls": agent_run.model_calls}),
        )
        self.db.add(trace)
        self.db.commit()
        self.db.refresh(trace)
        return trace

    def record_response(self, trace_id: int, text: str, *, mode: str, approved: bool | None = None,
                        reason: str = "completed", model_call: dict | None = None) -> None:
        trace = self.db.get(AgentRunTrace, trace_id)
        if trace is None:
            raise RuntimeError("Missing turn trace")
        metadata = json.loads(trace.assessment_json)
        metadata["response_stop_reason"] = reason
        turn_id = metadata["turn_id"]
        draft = Artifact(kind="response_draft", producer="response_boundary", task_id=turn_id,
                         payload={"sha256": hashlib.sha256(text.encode()).hexdigest(), "chars": len(text)},
                         source_refs=[f"turn:{turn_id}:response"])
        metadata["artifacts"].append(draft.model_dump(mode="json"))
        if approved is not None:
            review = SafetyReview(approved=approved, draft_id=draft.id, reason=reason, mode=mode)
            artifact = Artifact(kind="safety_review", producer="response_boundary", task_id=turn_id,
                                payload=review.model_dump(), source_refs=[draft.id])
            metadata["artifacts"].append(artifact.model_dump(mode="json"))
        if model_call:
            metadata["model_calls"].append(model_call)
        steps = json.loads(trace.agent_steps_json)
        steps.append({"step": len(steps) + 1, "agent": "ResponseBoundary", "action": "FINAL_TEXT_REVIEW" if approved is not None else "STREAM_COMPLETED",
                      "task_id": turn_id, "artifact_ref": draft.id,
                      "observation": reason, "observation_status": "success" if approved is not False else "denied",
                      "stop_reason": reason, "output_mode": mode})
        trace.agent_steps_json = _json(steps)
        trace.assessment_json = _json(metadata)
        self.db.commit()

    def record_interruption(self, trace_id: int, reason: str) -> None:
        trace = self.db.get(AgentRunTrace, trace_id)
        metadata = json.loads(trace.assessment_json)
        metadata["response_stop_reason"] = reason
        steps = json.loads(trace.agent_steps_json)
        steps.append({"step":len(steps)+1,"agent":"ResponseBoundary","action":"STREAM_INTERRUPTED",
                      "observation_status":"error","observation":reason,"stop_reason":reason})
        trace.assessment_json = _json(metadata)
        trace.agent_steps_json = _json(steps)
        self.db.commit()


def _json(value: Any) -> str:
    return json.dumps(_to_jsonable(value), ensure_ascii=False, default=str)


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if hasattr(value, "model_dump"):
        return _to_jsonable(value.model_dump())
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    return value
