import asyncio
import json
import unittest
from copy import deepcopy

from pydantic import ValidationError

import test_safety_boundaries as fixtures
from app.agents.contracts import Artifact, ToolObservation
from app.models.entities import AgentRunTrace
from app.services.chat import ChatService
from app.services.trace import _to_jsonable


class StateContractTests(unittest.TestCase):
    setUp = fixtures.SafetyBoundariesTests.setUp
    tearDown = fixtures.SafetyBoundariesTests.tearDown
    runtime = fixtures.SafetyBoundariesTests.runtime

    def test_nodes_return_updates_without_mutating_incoming_state(self):
        runtime, session = self.runtime()
        state = runtime.initial_state(
            self.user, session, "电话 13800138000，解释 Python"
        )
        original = deepcopy(state)
        update = runtime.execute_step(state, "precheck")
        self.assertEqual(state, original)
        self.assertNotIn("context", state)
        self.assertNotIn("user", state)
        self.assertNotIn("session", state)
        self.assertTrue(update["prechecked"])
        self.assertNotIn(
            "13800138000", json.dumps(_to_jsonable(state), ensure_ascii=False)
        )
        update["steps"].clear()
        self.assertEqual(state["steps"], [])

    def test_artifact_schema_rejects_wrong_payload_and_missing_provenance(self):
        base = {
            "producer": "test",
            "task_id": "synthetic",
            "source_refs": ["fixture://test"],
        }
        with self.assertRaises(ValidationError):
            Artifact(
                kind="risk_assessment",
                payload={"risk_level": "LOW", "emotion": "NORMAL", "confidence": 2},
                **base,
            )
        with self.assertRaises(ValidationError):
            Artifact(kind="intent", payload={"risk_level": "HIGH"}, **base)
        with self.assertRaises(ValidationError):
            Artifact(
                kind="intent",
                producer="test",
                task_id="synthetic",
                payload={"intent": "CHAT"},
                source_refs=[],
            )
        with self.assertRaises(ValidationError):
            ToolObservation(tool="test", status="pretend_success")

    def test_custom_and_langgraph_have_matching_safety_and_artifact_contracts(self):
        results = []
        for framework in ["custom", "langgraph"]:
            runtime, session = self.runtime(framework)
            result = runtime.run(self.user, session, "我不想活了", "我不想活了")
            results.append(result)
            self.assertTrue(result.turn_id)
            self.assertEqual(result.stop_reason, "completed")
            self.assertEqual(
                [a.kind for a in result.artifacts], ["risk_assessment", "intent"]
            )
            self.assertTrue(
                all(
                    a.source_refs and a.task_id == result.turn_id
                    for a in result.artifacts
                )
            )
        self.assertEqual(results[0].risk_level, results[1].risk_level)
        self.assertEqual(
            [s.action for s in results[0].steps], [s.action for s in results[1].steps]
        )
        self.assertEqual(
            [a.payload for a in results[0].artifacts],
            [a.payload for a in results[1].artifacts],
        )

    def test_trace_contains_refs_and_review_but_no_full_sensitive_transcript(self):
        service = ChatService(self.db, self.settings)

        async def collect():
            from app.schemas.dtos import ChatRequest

            return [
                event
                async for event in service.stream_chat(
                    self.user, ChatRequest(message="电话13800138000，我不想活了")
                )
            ]

        asyncio.run(collect())
        trace = self.db.query(AgentRunTrace).one()
        self.assertEqual(trace.original_input, "")
        self.assertEqual(trace.sanitized_input, "")
        self.assertEqual(trace.response_messages_json, "[]")
        data = json.loads(trace.assessment_json)
        self.assertNotIn("13800138000", trace.assessment_json + trace.agent_steps_json)
        self.assertNotIn("现在先把安全", trace.assessment_json)
        kinds = [a["kind"] for a in data["artifacts"]]
        self.assertIn("response_draft", kinds)
        self.assertIn("safety_review", kinds)
        review = next(a for a in data["artifacts"] if a["kind"] == "safety_review")
        self.assertEqual(review["payload"]["subject"], "final_text")
        self.assertTrue(review["payload"]["approved"])


if __name__ == "__main__":
    unittest.main()
