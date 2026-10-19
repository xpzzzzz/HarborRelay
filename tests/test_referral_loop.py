import asyncio
import json
import unittest
from unittest.mock import patch

import test_safety_boundaries as fixtures
from app.agents.contracts import TaskSpec, ToolObservation
from app.agents.referral import ReferralAction, ReferralAgent, extract_constraints
from app.core.config import Settings
from app.models.entities import AgentRunTrace, ToolJob
from app.schemas.dtos import ChatRequest
from app.services.ai import AiClient
from app.services.chat import ChatService
from app.services.referral_tools import ReferralTools


DEMO_INPUT = "想找心理中心咨询，但不想让同学知道，只有周五下午有空"


class FaultTools(ReferralTools):
    def __init__(self, fault):
        super().__init__()
        self.fault, self.count = fault, 0

    def call(self, tool, arguments):
        self.count += 1
        if self.fault == "timeout" or self.fault == "timeout_once" and self.count == 1:
            raise TimeoutError("synthetic")
        if self.fault == "error":
            raise RuntimeError("synthetic")
        if self.fault == "risk":
            return ToolObservation(
                tool=tool, status="success", data={"risk_level": "HIGH"}
            )
        observation = super().call(tool, arguments)
        if self.fault == "unknown_as_success" and tool == "get_resource_availability":
            return observation.model_copy(
                update={
                    "status": "success",
                    "data": {**observation.data, "availability": "unknown"},
                }
            )
        return observation


class ReferralLoopTests(unittest.TestCase):
    def setUp(self):
        self.ai = AiClient(Settings(_env_file=None, ai_provider="mock"))
        self.task = TaskSpec(constraints=extract_constraints(DEMO_INPUT))

    def run_agent(self, tools=None, **kwargs):
        return ReferralAgent(self.ai, tools=tools, **kwargs).run(self.task)

    def test_observation_changes_next_action_and_has_real_provenance(self):
        result = self.run_agent()
        self.assertEqual(result.stop_reason, "completed")
        self.assertEqual(result.tool_calls, 5)
        self.assertEqual(
            [(o.tool, o.status) for o in result.observations],
            [
                ("search_support_resources", "success"),
                ("get_resource_availability", "empty"),
                ("search_support_resources", "success"),
                ("get_resource_availability", "success"),
                ("prepare_referral_summary", "success"),
            ],
        )
        self.assertEqual(result.options[0]["id"], "demo-remote-center")
        self.assertIn("不代表真实机构", result.render())
        self.assertTrue(all(s.startswith("fixture://") for s in result.source_refs))
        tools = ReferralTools()
        tools.resources["demo-campus-center"].availability["friday_afternoon"] = (
            "available"
        )
        alternative = self.run_agent(tools)
        self.assertEqual(alternative.tool_calls, 3)
        self.assertEqual(alternative.options[0]["id"], "demo-campus-center")
        self.assertEqual(alternative.observations[2].tool, "prepare_referral_summary")

    def test_missing_time_asks_without_tool_or_model_call(self):
        self.task = TaskSpec(
            constraints=extract_constraints("想找心理中心咨询，不想让同学知道")
        )
        with patch.object(
            self.ai, "complete", side_effect=AssertionError("unnecessary planning")
        ):
            result = self.run_agent()
        self.assertEqual(result.stop_reason, "needs_time")
        self.assertEqual(result.tool_calls, 0)

    def test_empty_results_broaden_then_stop(self):
        result = self.run_agent(ReferralTools(resources=[]))
        self.assertEqual(result.stop_reason, "no_match")
        self.assertEqual(result.tool_calls, 2)
        self.assertFalse(result.options)

    def test_privacy_conflict_does_not_relax_privacy(self):
        tools = ReferralTools()
        tools.resources = {k: r for k, r in tools.resources.items() if not r.private}
        result = self.run_agent(tools)
        self.assertEqual(result.stop_reason, "privacy_conflict")
        self.assertEqual(result.tool_calls, 1)

    def test_transient_timeout_retries_and_recovers(self):
        result = self.run_agent(FaultTools("timeout_once"))
        self.assertEqual(result.stop_reason, "completed")
        self.assertEqual(result.tool_calls, 6)
        self.assertEqual(result.observations[0].status, "timeout")

    def test_persistent_timeout_exits_after_one_retry(self):
        result = self.run_agent(FaultTools("timeout"))
        self.assertEqual(result.stop_reason, "tool_timeout")
        self.assertEqual(result.tool_calls, 2)
        self.assertFalse(result.options)

    def test_tool_error_is_not_success(self):
        result = self.run_agent(FaultTools("error"))
        self.assertEqual(result.stop_reason, "tool_error")
        self.assertFalse(result.options)

    def test_unknown_availability_is_not_invented(self):
        tools = ReferralTools()
        for resource in tools.resources.values():
            resource.availability["friday_afternoon"] = "unknown"
        result = self.run_agent(tools)
        self.assertEqual(result.stop_reason, "availability_unknown")
        self.assertFalse(result.options)
        self.assertNotIn("已完成预约", result.render().split("\n")[-1])

    def test_mislabeled_unknown_observation_is_rejected(self):
        result = self.run_agent(FaultTools("unknown_as_success"))
        self.assertEqual(result.stop_reason, "tool_error")
        self.assertFalse(result.options)

    def test_duplicate_action_is_bounded(self):
        action = ReferralAction(
            kind="tool",
            tool="search_support_resources",
            arguments={"channel": "in_person", "private": True},
        )
        with patch.object(self.ai, "complete", return_value=action.model_dump_json()):
            result = self.run_agent()
        self.assertEqual(result.stop_reason, "repeated_results")
        self.assertEqual(result.tool_calls, 1)

    def test_omitted_defaults_cannot_bypass_duplicate_call_guard(self):
        actions = [
            ReferralAction(kind="tool", tool="search_support_resources", arguments={}),
            ReferralAction(
                kind="tool",
                tool="search_support_resources",
                arguments={"channel": "in_person", "private": True},
            ),
        ]
        with patch.object(
            self.ai, "complete", side_effect=[a.model_dump_json() for a in actions]
        ):
            result = self.run_agent()
        self.assertEqual(result.stop_reason, "repeated_results")
        self.assertEqual(result.tool_calls, 1)

    def test_retry_exception_applies_only_to_exact_timed_out_call(self):
        class TimeoutRemote(ReferralTools):
            def call(self, tool, arguments):
                if arguments.get("resource_id") == "demo-remote-center":
                    raise TimeoutError("synthetic remote timeout")
                return super().call(tool, arguments)

        actions = [
            ReferralAction(
                kind="tool",
                tool="search_support_resources",
                arguments={"channel": "any", "private": True},
            )
        ]
        actions += [
            ReferralAction(
                kind="tool",
                tool="get_resource_availability",
                arguments={"resource_id": identifier, "window": "friday_afternoon"},
            )
            for identifier in [
                "demo-campus-center",
                "demo-remote-center",
                "demo-campus-center",
            ]
        ]
        with patch.object(
            self.ai, "complete", side_effect=[a.model_dump_json() for a in actions]
        ):
            result = self.run_agent(TimeoutRemote())
        self.assertEqual(result.stop_reason, "repeated_results")
        self.assertEqual(result.tool_calls, 3)
        self.assertEqual(result.observations[-2].status, "timeout")

    def test_unauthorized_side_effect_is_denied_without_execution(self):
        action = ReferralAction(
            kind="tool", tool="mindbridge_alert_send", arguments={"case_id": 1}
        )
        tools = ReferralTools()
        with (
            patch.object(self.ai, "complete", return_value=action.model_dump_json()),
            patch.object(tools, "call", side_effect=AssertionError("must not execute")),
        ):
            result = self.run_agent(tools)
        self.assertEqual(result.stop_reason, "permission_denied")
        self.assertEqual(result.tool_calls, 0)
        self.assertEqual(result.invalid_calls, 1)

    def test_model_cannot_relax_privacy_or_time(self):
        action = ReferralAction(
            kind="tool",
            tool="search_support_resources",
            arguments={"channel": "any", "private": False},
        )
        with patch.object(self.ai, "complete", return_value=action.model_dump_json()):
            result = self.run_agent()
        self.assertEqual(result.stop_reason, "permission_denied")
        self.assertEqual(result.tool_calls, 0)

    def test_unobserved_finish_is_rejected(self):
        action = ReferralAction(kind="finish", resource_ids=["invented"])
        with patch.object(self.ai, "complete", return_value=action.model_dump_json()):
            result = self.run_agent()
        self.assertEqual(result.stop_reason, "invalid_finish")
        self.assertFalse(result.options)

    def test_malformed_json_is_explicit_fallback(self):
        with patch.object(self.ai, "complete", return_value="not-json"):
            result = self.run_agent()
        self.assertEqual(result.stop_reason, "completed")
        self.assertEqual(set(result.planner_modes), {"deterministic_fallback"})

    def test_model_receives_updated_observations(self):
        original = self.ai.complete
        with patch.object(self.ai, "complete", wraps=original) as complete:
            self.run_agent()
        second = json.loads(complete.call_args_list[1].args[0][-1].content)
        third = json.loads(complete.call_args_list[2].args[0][-1].content)
        self.assertEqual(len(second["observations"]), 1)
        self.assertEqual(third["observations"][-1]["status"], "empty")
        self.assertNotIn("想找心理中心", complete.call_args_list[0].args[0][-1].content)

    def test_step_and_tool_budgets_stop(self):
        for field, reason in [
            ("max_steps", "step_budget"),
            ("max_tool_calls", "tool_budget"),
        ]:
            self.task = TaskSpec(
                constraints=extract_constraints(DEMO_INPUT), **{field: 1}
            )
            result = self.run_agent()
            self.assertEqual(result.stop_reason, reason)
            self.assertEqual(result.tool_calls, 1)

    def test_deadline_during_planning_prevents_tool_execution(self):
        clock = [0]
        original = self.ai.complete

        def delayed(*args, **kwargs):
            result = original(*args, **kwargs)
            clock[0] = 100
            return result

        with patch.object(self.ai, "complete", side_effect=delayed):
            result = self.run_agent(clock=lambda: clock[0])
        self.assertEqual(result.stop_reason, "time_budget")
        self.assertEqual(result.tool_calls, 0)

    def test_risk_preempts_before_and_after_tools(self):
        result = self.run_agent(risk_check=lambda: True)
        self.assertEqual(result.stop_reason, "risk_escalated")
        self.assertEqual(result.tool_calls, 0)
        result = self.run_agent(FaultTools("risk"))
        self.assertEqual(result.stop_reason, "risk_escalated")
        self.assertEqual(result.tool_calls, 1)

    def test_custom_fallback_matches_subgraph(self):
        graph = self.run_agent()
        native = self.run_agent(use_graph=False)
        self.assertEqual(graph.stop_reason, native.stop_reason)
        self.assertEqual(graph.options, native.options)
        self.assertEqual(
            [(o.tool, o.status) for o in graph.observations],
            [(o.tool, o.status) for o in native.observations],
        )


class ReferralIntegrationTests(unittest.TestCase):
    setUp = fixtures.SafetyBoundariesTests.setUp
    tearDown = fixtures.SafetyBoundariesTests.tearDown

    def collect(self, message, session_id=None):
        async def run():
            return [
                event
                async for event in ChatService(self.db, self.settings).stream_chat(
                    self.user, ChatRequest(message=message, sessionId=session_id)
                )
            ]

        return asyncio.run(run())

    def test_api_compatible_sse_with_evidence_and_no_handoff_side_effect(self):
        events = self.collect(DEMO_INPUT)
        text = "".join(events)
        self.assertIn('"outputMode": "referral_result"', text)
        self.assertIn("fixture://support-resources/", text)
        self.assertIn("event: done", text)
        self.assertEqual([j.kind for j in self.db.query(ToolJob)], ["EXCEL_REPORT"])

    def test_high_risk_during_referral_switches_to_safety_template(self):
        with patch(
            "app.agents.referral.ReferralTools", return_value=FaultTools("risk")
        ):
            events = self.collect(DEMO_INPUT)
        self.assertIn('"outputMode": "safety_template"', "".join(events))
        self.assertEqual(self.db.query(ToolJob).count(), 3)

    def test_missing_time_can_be_supplied_on_next_turn(self):
        first = self.collect("想找心理中心咨询，不想让同学知道")
        session_id = json.loads(first[0].split("data: ", 1)[1])["sessionId"]
        second = self.collect("只有周五下午有空", session_id)
        self.assertIn("fixture://support-resources/", "".join(second))

    def test_feature_switch_preserves_safety_and_original_reply_path(self):
        self.settings.referral_enabled = False
        text = "".join(self.collect(DEMO_INPUT))
        self.assertIn('"outputMode": "model_stream"', text)
        trace = self.db.query(AgentRunTrace).one()
        self.assertIn("RiskGuardianAgent", trace.agent_steps_json)

    def test_runtime_selects_explicit_policy_and_keeps_model_option(self):
        self.collect(DEMO_INPUT)
        traces = self.db.query(AgentRunTrace).all()
        self.assertIn("deterministic_policy", traces[-1].agent_steps_json)
        self.settings.referral_planner = "model"
        self.collect(DEMO_INPUT)
        traces = self.db.query(AgentRunTrace).order_by(AgentRunTrace.id).all()
        self.assertIn("mock_policy", traces[-1].agent_steps_json)

    def test_framework_unavailable_preserves_full_referral_fallback(self):
        with patch("app.agents.factory.langgraph_available", return_value=False):
            text = "".join(self.collect(DEMO_INPUT))
        self.assertIn("fixture://support-resources/", text)
        self.assertIn("event: done", text)


if __name__ == "__main__":
    unittest.main()
