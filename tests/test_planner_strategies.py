import unittest
from unittest.mock import patch

from app.agents.contracts import TaskSpec
from app.agents.referral import ReferralAction, ReferralAgent, extract_constraints
from app.core.config import Settings
from app.evaluation.ollama_runner import evaluate_case
from app.evaluation.planner_comparison import DEFAULT_CASES, verify_pairs
from app.evaluation.scenarios import FaultTools
from app.services.referral_tools import ReferralTools


class PlannerStrategyTests(unittest.TestCase):
    def task(self, **kwargs):
        return TaskSpec(
            constraints=extract_constraints("找心理中心咨询，保密，周五下午"), **kwargs
        )

    def test_rule_policy_needs_no_ai_and_still_reacts_to_tools(self):
        regular = ReferralAgent(None, planner="rules").run(self.task())
        tools = ReferralTools()
        tools.resources["demo-campus-center"].availability["friday_afternoon"] = (
            "available"
        )
        available = ReferralAgent(None, planner="rules", tools=tools).run(self.task())
        self.assertEqual((regular.stop_reason, regular.tool_calls), ("completed", 5))
        self.assertEqual(
            (available.stop_reason, available.tool_calls), ("completed", 3)
        )
        self.assertEqual(set(regular.planner_modes), {"deterministic_policy"})
        self.assertNotEqual(regular.options[0]["id"], available.options[0]["id"])

    def test_rule_policy_cannot_bypass_executor_authorization(self):
        forbidden = ReferralAction(kind="tool", tool="send_notification", arguments={})
        with patch("app.agents.referral.fallback_action", return_value=forbidden):
            result = ReferralAgent(None, planner="rules").run(self.task())
        self.assertEqual(result.stop_reason, "permission_denied")
        self.assertEqual(result.tool_calls, 0)

    def test_rule_policy_preserves_risk_interrupts_and_budgets(self):
        for options, task, expected in [
            ({"risk_check": lambda: True}, self.task(), "risk_escalated"),
            ({"tools": FaultTools("risk")}, self.task(), "risk_escalated"),
            ({}, self.task(max_tool_calls=1), "tool_budget"),
            ({"tools": FaultTools("timeout")}, self.task(), "tool_timeout"),
        ]:
            with self.subTest(expected=expected):
                result = ReferralAgent(None, planner="rules", **options).run(task)
                self.assertEqual(result.stop_reason, expected)

    def test_custom_runtime_preserves_rule_policy_behavior(self):
        graph = ReferralAgent(None, planner="rules").run(self.task())
        custom = ReferralAgent(None, planner="rules", use_graph=False).run(self.task())
        self.assertEqual(graph.options, custom.options)
        self.assertEqual(graph.stop_reason, custom.stop_reason)
        self.assertEqual(graph.planner_modes, custom.planner_modes)

    def test_rule_default_is_explicit_and_unknown_strategy_is_rejected(self):
        self.assertEqual(Settings(_env_file=None).referral_planner, "rules")
        with self.assertRaises(ValueError):
            ReferralAgent(None, planner="unbounded")

    def test_rule_benchmark_uses_no_inference_and_held_out_variants_pass(self):
        settings = Settings(_env_file=None, ai_provider="ollama")
        cases = []
        with patch(
            "app.services.ai.httpx.post", side_effect=AssertionError("No model needed")
        ) as post:
            for name in DEFAULT_CASES:
                case = evaluate_case(
                    settings, name, 15, 0, planner="rules", task_id=f"fixed-{name}"
                )
                self.assertTrue(case["success"], name)
                self.assertEqual(case["attempts"], [])
                cases.append(case)
            post.assert_not_called()
        self.assertTrue(
            verify_pairs({"rules": {"cases": cases}, "copy": {"cases": cases}})
        )
        altered = [{**cases[0], "fixture_sha256": "changed"}, *cases[1:]]
        with self.assertRaises(ValueError):
            verify_pairs({"rules": {"cases": cases}, "altered": {"cases": altered}})
        with self.assertRaises(ValueError):
            verify_pairs({"rules": {"cases": cases}, "missing": {"cases": cases[:-1]}})


if __name__ == "__main__":
    unittest.main()
