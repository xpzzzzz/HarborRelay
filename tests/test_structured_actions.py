import json
import unittest
from unittest.mock import Mock, patch

import httpx
from jsonschema import Draft202012Validator

from app.agents.contracts import TaskSpec, ToolObservation
from app.agents.referral import (
    ReferralAction,
    ReferralAgent,
    action_json_schema,
    extract_constraints,
    fallback_action,
)
from app.core.config import Settings
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient


class StructuredActionTests(unittest.TestCase):
    def test_decoder_schema_rejects_legacy_nested_ask_and_unknown_tools(self):
        validator = Draft202012Validator(action_json_schema())
        self.assertFalse(
            validator.is_valid(
                {"kind": "ask_user", "arguments": {"search_support_resources": {}}}
            )
        )
        self.assertFalse(
            validator.is_valid({"kind": "tool", "tool": "send_email", "arguments": {}})
        )
        self.assertFalse(
            validator.is_valid({"kind": "safe_exit", "stop_reason": "invalid_action"})
        )
        self.assertTrue(
            validator.is_valid(
                {
                    "kind": "tool",
                    "tool": "search_support_resources",
                    "arguments": {"channel": "in_person", "private": True},
                }
            )
        )
        self.assertTrue(
            validator.is_valid({"kind": "ask_user", "stop_reason": "needs_time"})
        )

    def test_context_schema_requires_evidence_without_weakening_executor(self):
        constraints = extract_constraints("找心理中心，保密，周五下午")
        first = Draft202012Validator(
            action_json_schema(constraints=constraints, observations=[])
        )
        self.assertFalse(
            first.is_valid({"kind": "finish", "resource_ids": ["invented"]})
        )
        self.assertFalse(
            first.is_valid(
                {
                    "kind": "tool",
                    "tool": "get_resource_availability",
                    "arguments": {
                        "resource_id": "invented",
                        "window": "friday_afternoon",
                    },
                }
            )
        )
        self.assertFalse(
            first.is_valid(
                {
                    "kind": "tool",
                    "tool": "search_support_resources",
                    "arguments": {"private": False},
                }
            )
        )
        observations = [
            ToolObservation(
                tool="search_support_resources",
                status="success",
                data={"resources": [{"id": "observed"}]},
            )
        ]
        second = Draft202012Validator(
            action_json_schema(constraints=constraints, observations=observations)
        )
        self.assertTrue(
            second.is_valid(
                {
                    "kind": "tool",
                    "tool": "get_resource_availability",
                    "arguments": {
                        "resource_id": "observed",
                        "window": "friday_afternoon",
                    },
                }
            )
        )
        self.assertFalse(
            second.is_valid(
                {
                    "kind": "tool",
                    "tool": "get_resource_availability",
                    "arguments": {
                        "resource_id": "invented",
                        "window": "friday_afternoon",
                    },
                }
            )
        )
        self.assertFalse(
            second.is_valid(
                {
                    "kind": "tool",
                    "tool": "get_resource_availability",
                    "arguments": {
                        "resource_id": "observed",
                        "window": "monday_afternoon",
                    },
                }
            )
        )

    @patch("app.services.ai.httpx.post")
    def test_native_schema_timeout_and_actual_usage_are_forwarded(self, post):
        response = Mock()
        response.json.return_value = {
            "message": {"content": "{}"},
            "prompt_eval_count": 17,
            "eval_count": 5,
            "load_duration": 2_000_000,
            "done_reason": "stop",
        }
        post.return_value = response
        ai = AiClient(Settings(_env_file=None, ai_provider="ollama"))
        schema = action_json_schema()
        self.assertEqual(
            ai.complete(
                [AiMessage(role="user", content="synthetic")],
                timeout_seconds=3,
                json_schema=schema,
            ),
            "{}",
        )
        self.assertEqual(post.call_args.kwargs["json"]["format"], schema)
        self.assertEqual(post.call_args.kwargs["timeout"], 3)
        self.assertEqual(ai.calls[-1]["tokens_or_estimate"], 22)
        self.assertEqual(ai.calls[-1]["token_measurement"], "provider")
        post.side_effect = httpx.ReadTimeout("synthetic")
        with self.assertRaises(httpx.ReadTimeout):
            ai.complete([AiMessage(role="user", content="synthetic")])
        self.assertEqual(ai.calls[-1]["error"], "ReadTimeout")
        self.assertEqual(ai.calls[-1]["token_measurement"], "character_estimate")
        self.assertNotIn("prompt_tokens", ai.calls[-1])

    @patch("app.services.ai.httpx.post")
    def test_normal_completion_does_not_force_json_format(self, post):
        post.return_value.json.return_value = {"message": {"content": "synthetic"}}
        ai = AiClient(Settings(_env_file=None, ai_provider="ollama"))
        ai.complete([AiMessage(role="user", content="synthetic")])
        self.assertNotIn("format", post.call_args.kwargs["json"])

    def test_native_decoding_never_replaces_tool_authorization(self):
        ai = AiClient(Settings(_env_file=None, ai_provider="ollama"))
        forbidden = ReferralAction(
            kind="tool", tool="send_email", arguments={}
        ).model_dump_json()
        with patch.object(ai, "complete", return_value=forbidden):
            result = ReferralAgent(ai).run(
                TaskSpec(constraints=extract_constraints("找心理中心，周五下午"))
            )
        self.assertEqual(result.stop_reason, "permission_denied")
        self.assertEqual(result.tool_calls, 0)

    def test_transport_failure_has_explicit_fallback_and_no_unstructured_retry(self):
        ai = AiClient(Settings(_env_file=None, ai_provider="ollama"))
        with patch(
            "app.services.ai.httpx.post", side_effect=httpx.ConnectError("synthetic")
        ) as post:
            result = ReferralAgent(ai).run(
                TaskSpec(constraints=extract_constraints("找心理中心，保密，周五下午"))
            )
        self.assertEqual(result.stop_reason, "completed")
        self.assertEqual(set(result.planner_modes), {"deterministic_fallback"})
        self.assertTrue(
            all("format" in call.kwargs["json"] for call in post.call_args_list)
        )

    def test_prompt_compatibility_switch_does_not_remove_validation(self):
        ai = AiClient(
            Settings(
                _env_file=None, ai_provider="ollama", referral_action_format="prompt"
            )
        )
        requests = []

        def planned(messages, **kwargs):
            requests.append(kwargs)
            return fallback_action(json.loads(messages[-1].content)).model_dump_json()

        with patch.object(ai, "complete", side_effect=planned):
            result = ReferralAgent(ai).run(
                TaskSpec(constraints=extract_constraints("找心理中心，保密，周五下午"))
            )
        self.assertEqual(result.stop_reason, "completed")
        self.assertTrue(all("json_schema" not in kwargs for kwargs in requests))
        self.assertEqual(set(result.planner_modes), {"model_json_action"})


if __name__ == "__main__":
    unittest.main()
