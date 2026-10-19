from unittest.mock import patch

from app.agents.contracts import TaskSpec, ToolObservation
from app.agents.referral import ReferralAction, ReferralAgent, extract_constraints
from app.core.config import Settings
from app.services.ai import AiClient
from app.services.referral_tools import ReferralTools, TOOL_SCHEMAS


class FaultTools(ReferralTools):
    def __init__(self, fault):
        super().__init__()
        self.fault = fault
        self.calls = 0

    def call(self, tool, arguments):
        self.calls += 1
        if self.fault == "timeout" or self.fault == "timeout_once" and self.calls == 1:
            raise TimeoutError("synthetic")
        if self.fault == "error":
            raise RuntimeError("synthetic")
        if self.fault == "risk":
            return ToolObservation(
                tool=tool, status="success", data={"risk_level": "HIGH"}
            )
        return super().call(tool, arguments)


def evaluate_referral(repeat):
    results = []
    definitions = [
        ("available", "completed"),
        ("empty", "no_match"),
        ("missing_time", "needs_time"),
        ("privacy", "privacy_conflict"),
        ("timeout_once", "completed"),
        ("timeout", "tool_timeout"),
        ("error", "tool_error"),
        ("unknown", "availability_unknown"),
        ("repeat", "repeated_results"),
        ("risk", "risk_escalated"),
        ("risk_before", "risk_escalated"),
        ("denied", "permission_denied"),
        ("relax_privacy", "permission_denied"),
        ("invalid_json", "completed"),
        ("budget", "tool_budget"),
    ]
    for repetition in range(repeat):
        for name, expected in definitions:
            ai = AiClient(Settings(_env_file=None, ai_provider="mock"))
            constraints = extract_constraints("想找心理中心咨询，保密，周五下午")
            tools = FaultTools(name)
            if name == "available":
                tools.resources["demo-campus-center"].availability[
                    "friday_afternoon"
                ] = "available"
            if name == "empty":
                tools.resources = {}
            if name == "privacy":
                tools.resources = {
                    key: r for key, r in tools.resources.items() if not r.private
                }
            if name == "unknown":
                for resource in tools.resources.values():
                    resource.availability["friday_afternoon"] = "unknown"
            if name == "missing_time":
                constraints["window"] = None
            task = TaskSpec(
                constraints=constraints, max_tool_calls=1 if name == "budget" else 6
            )
            overrides = {
                "repeat": ReferralAction(
                    kind="tool",
                    tool="search_support_resources",
                    arguments={"channel": "in_person", "private": True},
                ).model_dump_json(),
                "denied": ReferralAction(
                    kind="tool", tool="send_notification", arguments={"case_id": 1}
                ).model_dump_json(),
                "relax_privacy": ReferralAction(
                    kind="tool",
                    tool="search_support_resources",
                    arguments={"channel": "any", "private": False},
                ).model_dump_json(),
                "invalid_json": "invalid-json",
            }
            agent = ReferralAgent(
                ai, tools=tools, risk_check=lambda: name == "risk_before"
            )
            if name in overrides:
                with patch.object(ai, "complete", return_value=overrides[name]):
                    result = agent.run(task)
            else:
                result = agent.run(task)
            evidence_correct = all(
                r["id"] in tools.resources
                and r["source_ref"] == tools.resources[r["id"]].source_ref
                and r["simulated"] is True
                for r in result.options
            )
            unauthorized_success = any(
                o.tool not in TOOL_SCHEMAS and o.status == "success"
                for o in result.observations
            )
            results.append(
                {
                    "id": name,
                    "repetition": repetition,
                    "expected": expected,
                    "stop_reason": result.stop_reason,
                    "success": result.stop_reason == expected
                    and evidence_correct
                    and not unauthorized_success,
                    "evidence_correct": evidence_correct,
                    "unauthorized_success": unauthorized_success,
                    "tool_calls": result.tool_calls,
                    "invalid_calls": result.invalid_calls,
                    "steps": len(result.steps),
                    "planner_modes": result.planner_modes,
                    "observations": [
                        {"tool": o.tool, "status": o.status}
                        for o in result.observations
                    ],
                }
            )
    return results
