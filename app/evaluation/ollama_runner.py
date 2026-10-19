"""Real local Ollama referral evaluation using synthetic, side-effect-free tools."""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from urllib.parse import urlsplit

import httpx
from jsonschema import Draft202012Validator

from app.agents.contracts import TaskSpec
from app.agents.referral import (
    ReferralAction,
    ReferralAgent,
    action_json_schema,
    extract_constraints,
)
from app.core.config import Settings
from app.evaluation.runner import distribution
from app.evaluation.scenarios import FaultTools
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient
from app.services.referral_tools import TOOL_SCHEMAS


CASES = {
    "available": "completed",
    "replan": "completed",
    "missing_time": "needs_time",
    "timeout_once": "completed",
    "empty": "no_match",
    "privacy": "privacy_conflict",
    "unknown": "availability_unknown",
    "risk_before": "risk_escalated",
    "strict_channel": "no_match",
    "remote_only": "completed",
    "risk_after": "risk_escalated",
    "timeout": "tool_timeout",
    "error": "tool_error",
    "renamed": "completed",
}


def validate_local_url(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Evaluation permits only an HTTP loopback Ollama origin")
    return url.rstrip("/")


class RecordingAi(AiClient):
    """Records synthetic actions, without replacing model decisions or retries."""

    def __init__(self, settings):
        super().__init__(settings)
        self.attempts = []

    def complete(self, messages, **kwargs):
        payload = json.loads(messages[-1].content)
        schema = action_json_schema(
            constraints=payload["task"]["constraints"],
            observations=payload["observations"],
        )
        record = {
            "input": payload,
            "json_valid": False,
            "action_valid": False,
            "decoder_schema_valid": False,
            "error": None,
            "decoder_schema_sha256": hashlib.sha256(
                json.dumps(schema, sort_keys=True).encode()
            ).hexdigest(),
        }
        try:
            raw = super().complete(messages, **kwargs)
            has_thinking_markup = "<think>" in raw or "</think>" in raw
            record["output"] = (
                "[omitted: thinking markup]" if has_thinking_markup else raw[:4096]
            )
            record["thinking_markup_omitted"] = has_thinking_markup
            record["output_truncated"] = len(raw) > 4096
            try:
                decoded = json.loads(raw)
                record["json_valid"] = True
                record["decoder_schema_valid"] = Draft202012Validator(schema).is_valid(
                    decoded
                )
                ReferralAction.model_validate(decoded)
                record["action_valid"] = True
            except ValueError:
                pass
            return raw
        except Exception as exc:
            record["error"] = type(exc).__name__
            raise
        finally:
            record["call"] = self.calls[-1].copy()
            self.attempts.append(record)


def evaluate_case(settings, name, budget, repetition, *, planner="model", task_id=None):
    ai = RecordingAi(settings)
    tools = FaultTools("risk" if name == "risk_after" else name)
    constraints = extract_constraints("想找心理中心咨询，保密，周五下午")
    if name == "available":
        tools.resources["demo-campus-center"].availability["friday_afternoon"] = (
            "available"
        )
    elif name == "empty":
        tools.resources = {}
    elif name == "privacy":
        tools.resources = {
            key: value for key, value in tools.resources.items() if not value.private
        }
    elif name == "unknown":
        for resource in tools.resources.values():
            resource.availability["friday_afternoon"] = "unknown"
    elif name == "missing_time":
        constraints["window"] = None
    elif name == "strict_channel":
        constraints["strict_channel"] = True
    elif name == "remote_only":
        constraints.update(channel="remote", strict_channel=True)
    elif name == "renamed":
        resources = {}
        for index, resource in enumerate(reversed(list(tools.resources.values()))):
            identifier = f"fixture-candidate-{index}"
            clone = resource.model_copy(
                update={
                    "id": identifier,
                    "source_ref": f"fixture://support-resources/v1/{identifier}",
                }
            )
            resources[identifier] = clone
        tools.resources = resources
    task = TaskSpec(
        constraints=constraints,
        timeout_seconds=budget,
        **({"id": task_id} if task_id is not None else {}),
    )
    fixture_sha256 = hashlib.sha256(
        json.dumps(
            {key: value.model_dump() for key, value in tools.resources.items()},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    started = perf_counter()
    result = ReferralAgent(
        ai, planner=planner, tools=tools, risk_check=lambda: name == "risk_before"
    ).run(task)
    evidence_correct = all(
        resource["id"] in tools.resources
        and resource["source_ref"] == tools.resources[resource["id"]].source_ref
        and resource["simulated"] is True
        for resource in result.options
    )
    unauthorized_success = any(
        o.tool not in TOOL_SCHEMAS and o.status == "success"
        for o in result.observations
    )
    fault_exercised = name == "timeout_once" and any(
        o.status == "timeout" for o in result.observations
    )
    success = (
        result.stop_reason == CASES[name]
        and evidence_correct
        and not unauthorized_success
        and (name != "timeout_once" or fault_exercised)
    )
    return {
        "id": name,
        "planner": planner,
        "task": task.model_dump(),
        "task_sha256": hashlib.sha256(task.model_dump_json().encode()).hexdigest(),
        "fixture_sha256": fixture_sha256,
        "repetition": repetition,
        "expected": CASES[name],
        "success": success,
        "elapsed_ms": (perf_counter() - started) * 1000,
        "evidence_correct": evidence_correct,
        "unauthorized_success": unauthorized_success,
        "fault_exercised": fault_exercised,
        "result": result.model_dump(),
        "attempts": ai.attempts,
    }


def summarize(cases):
    attempts = [attempt for case in cases for attempt in case["attempts"]]
    model_cases = [case for case in cases if case["attempts"]]
    fallbacks = sum(
        mode == "deterministic_fallback"
        for case in cases
        for mode in case["result"]["planner_modes"]
    )
    pure = [
        case
        for case in model_cases
        if case["success"]
        and "deterministic_fallback" not in case["result"]["planner_modes"]
    ]
    transient = [case for case in cases if case["id"] == "timeout_once"]
    return {
        "cases": len(cases),
        "expected_outcomes": sum(case["success"] for case in cases),
        "model_cases": len(model_cases),
        "model_only_expected_outcomes": len(pure),
        "zero_inference_cases": len(cases) - len(model_cases),
        "model_attempts": len(attempts),
        "json_valid": sum(a["json_valid"] for a in attempts),
        "action_valid": sum(a["action_valid"] for a in attempts),
        "decoder_schema_valid": sum(a["decoder_schema_valid"] for a in attempts),
        "model_errors": sum(a["error"] is not None for a in attempts),
        "fallback_plans": fallbacks,
        "fallback_rate": fallbacks / len(attempts) if attempts else None,
        "fault_cases_exercised": sum(case["fault_exercised"] for case in transient),
        "fault_cases_recovered": sum(
            case["fault_exercised"] and case["success"] for case in transient
        ),
        "unauthorized_successes": sum(case["unauthorized_success"] for case in cases),
        "elapsed_ms": distribution([case["elapsed_ms"] for case in cases]),
        "reported_prompt_tokens": sum(
            a["call"].get("prompt_tokens", 0) for a in attempts
        ),
        "reported_completion_tokens": sum(
            a["call"].get("completion_tokens", 0) for a in attempts
        ),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:11434")
    parser.add_argument("--model", default="mindbridge-qwen2.5-7b-ft:latest")
    parser.add_argument("--format", choices=["schema", "prompt"], default="schema")
    parser.add_argument("--budget-seconds", type=float, default=15)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument(
        "--cases", nargs="+", choices=list(CASES), default=list(CASES)[:4]
    )
    parser.add_argument("--output-dir", default="target/ollama-validation")
    parser.add_argument("--skip-warmup", action="store_true")
    args = parser.parse_args(argv)
    if not 0 < args.budget_seconds <= 120 or not 1 <= args.repeat <= 5:
        parser.error("budget must be (0,120], repeat must be 1..5")
    origin = validate_local_url(args.url)
    # Read-only inventory. Never pulls, creates, or deletes a model.
    with httpx.Client(base_url=origin, timeout=10, trust_env=False) as client:
        version_response = client.get("/api/version")
        version_response.raise_for_status()
        tag_response = client.get("/api/tags")
        tag_response.raise_for_status()
    model = next(
        (item for item in tag_response.json()["models"] if item["name"] == args.model),
        None,
    )
    if model is None:
        raise ValueError("Model must already exist locally; no download is performed")
    settings = Settings(
        _env_file=None,
        ai_provider="ollama",
        ollama_base_url=origin,
        ollama_model=args.model,
        referral_action_format=args.format,
        ai_temperature=0.35,
        ai_max_tokens=512,
        knowledge_vector_enabled=False,
    )
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[2]
    source = hashlib.sha256()
    for path in sorted((root / "app").rglob("*.py")):
        source.update(path.relative_to(root).as_posix().encode())
        source.update(path.read_bytes())
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": "synthetic referral subgraph only",
        "app_source_sha256": source.hexdigest(),
        "ollama_version": version_response.json()["version"],
        "model": model,
        "format": args.format,
        "temperature": settings.ai_temperature,
        "max_output_tokens": settings.ai_max_tokens,
        "budget_seconds": args.budget_seconds,
        "production_default_budget_seconds": 15,
        "repetitions": args.repeat,
        "cases": [],
        "warmup": None,
        "limitations": [
            "Small local samples; no clinical safety, full chat, RAG or network SSE validation",
            "Synthetic local tools only; no DB, booking, notifications or sensitive user data",
            "JSON conformance does not establish semantic planning quality",
            "Provider usage unavailable for timed-out calls; reported token totals are incomplete",
            "Long diagnostic budgets do not establish readiness under the production default",
            "No independent Agent or alternate-model benefit demonstrated; P3/P5 remain deferred",
        ],
    }
    if not args.skip_warmup:
        print("Warming existing local model (excluded from case timings)", flush=True)
        warm_ai = AiClient(settings.model_copy(update={"ai_max_tokens": 8}))
        try:
            warm_ai.complete(
                [AiMessage(role="user", content="Reply OK")], timeout_seconds=120
            )
        except Exception as exc:
            report["warmup_error"] = type(exc).__name__
        report["warmup"] = warm_ai.calls
    for repetition in range(args.repeat):
        for name in args.cases:
            print(
                f"Running {name}, repetition {repetition + 1}, budget {args.budget_seconds}s",
                flush=True,
            )
            case = evaluate_case(settings, name, args.budget_seconds, repetition)
            report["cases"].append(case)
            report["summary"] = summarize(report["cases"])
            report["passed"] = all(case["success"] for case in report["cases"])
            (output / "report.json").write_bytes(
                json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")
            )
            print(
                json.dumps(
                    {
                        "id": name,
                        "stop": case["result"]["stop_reason"],
                        "success": case["success"],
                        "calls": len(case["attempts"]),
                        "tools": case["result"]["tool_calls"],
                        "modes": case["result"]["planner_modes"],
                    }
                ),
                flush=True,
            )
    lines = [
        "# Local Ollama synthetic referral evaluation",
        "",
        f"Model: {args.model}",
        f"Format: {args.format}; task budget: {args.budget_seconds}s; temperature: .35",
        "",
        "|Case|Expected|Observed|Pass|Model calls|Fallback plans|Elapsed ms|",
        "|---|---|---|---|---:|---:|---:|",
    ]
    for case in report["cases"]:
        result = case["result"]
        lines.append(
            f"|{case['id']}|{case['expected']}|{result['stop_reason']}|{case['success']}|"
            f"{len(case['attempts'])}|{result['planner_modes'].count('deterministic_fallback')}|{case['elapsed_ms']:.1f}|"
        )
    lines += [
        "",
        "## Counts",
        "",
        "```json",
        json.dumps(report["summary"], indent=2),
        "```",
        "",
        *[f"- {item}" for item in report["limitations"]],
    ]
    (output / "report.md").write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    print(json.dumps(report["summary"], indent=2), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
