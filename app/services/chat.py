from __future__ import annotations

import json
import asyncio
import logging
from time import perf_counter

from sqlalchemy.orm import Session

from app.agents.harness import MindBridgeAgentHarness
from app.core.config import Settings
from app.models.entities import UserAccount
from app.schemas.dtos import ChatRequest, ChatStreamEvent
from app.services.ai import AiClient
from app.core.enums import RiskLevel
from app.services.safety import HIGH_RISK_RESPONSE, review_final_response
from app.services.trace import AgentTraceService


logger = logging.getLogger(__name__)


class ChatService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.ai = AiClient(settings)
        self.agent_harness = MindBridgeAgentHarness(db, settings)

    async def stream_chat(self, user: UserAccount, request: ChatRequest):
        outcome = self.agent_harness.run(user, request)
        stream = self._stream_response(user, outcome)
        try:
            async for event in stream:
                yield event
        except (GeneratorExit, asyncio.CancelledError):
            AgentTraceService(self.db).record_interruption(outcome.trace_id,"client_disconnected")
            raise
        finally:
            await stream.aclose()

    async def _stream_response(self, user, outcome):
        # Reports/outbox are already committed; disconnects cannot undo scheduling.
        yield sse("meta", {**ChatStreamEvent(type="meta", sessionId=outcome.session.public_id).model_dump(by_alias=True),
                           "outputMode": "safety_template" if outcome.risk_level == "HIGH" else "referral_result" if outcome.referral_result else "model_stream"})
        assistant = []
        if outcome.risk_level == "HIGH":
            try:
                review = review_final_response(HIGH_RISK_RESPONSE, RiskLevel.HIGH)
            except Exception:
                AgentTraceService(self.db).record_interruption(outcome.trace_id,"review_unavailable")
                yield sse("error", {"type":"error","message":"安全回复暂时不可用，请联系人工支持。"})
                return
            AgentTraceService(self.db).record_response(outcome.trace_id, HIGH_RISK_RESPONSE, mode="safety_template",
                                                       approved=review.approved, reason=review.reason)
            if not review.approved:
                yield sse("error", {"type": "error", "message": "安全回复审核未通过，请联系人工支持。"})
                return
            assistant.append(HIGH_RISK_RESPONSE)
            yield sse("token", ChatStreamEvent(type="token", sessionId=outcome.session.public_id, content=HIGH_RISK_RESPONSE).model_dump())
        elif outcome.referral_result is not None:
            text = outcome.referral_result.render()
            AgentTraceService(self.db).record_response(outcome.trace_id, text, mode="referral_result",
                                                       approved=True, reason="validated_referral_result")
            assistant.append(text)
            yield sse("token", ChatStreamEvent(type="token", sessionId=outcome.session.public_id, content=text).model_dump())
        else:
            started = perf_counter()
            try:
                async for token in self.ai.stream(outcome.response_messages):
                    assistant.append(token)
                    yield sse("token", ChatStreamEvent(type="token", sessionId=outcome.session.public_id, content=token).model_dump())
            except Exception as exc:
                logger.warning("Response generation failed: %s", type(exc).__name__)
                AgentTraceService(self.db).record_interruption(outcome.trace_id,"generation_failed")
                yield sse("error", {"type": "error", "message": "回复生成暂时不可用，请稍后重试。"})
                return
        if assistant:
            self.agent_harness.save_assistant_message(user, outcome.session, "".join(assistant))
            if outcome.risk_level != "HIGH" and outcome.referral_result is None:
                AgentTraceService(self.db).record_response(outcome.trace_id, "".join(assistant), mode="model_stream",
                    model_call=self.ai.call_metadata(started, sum(len(m.content) for m in outcome.response_messages), len("".join(assistant))))
        yield sse("done", ChatStreamEvent(type="done", sessionId=outcome.session.public_id).model_dump())


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
