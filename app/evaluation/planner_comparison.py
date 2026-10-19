"""Offline rules vs single-model comparison; never installs models or changes routing."""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.core.config import Settings
from app.evaluation.ollama_runner import (
    CASES,
    evaluate_case,
    summarize,
    validate_local_url,
)
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient


DEFAULT_CASES = [
    "available",
    "replan",
    "timeout_once",
    "missing_time",
    "strict_channel",
    "remote_only",
    "risk_after",
    "renamed",
    "unknown",
]


def verify_pairs(arms):
    """Fail closed when coverage, TaskSpec, or resource fixtures differ between arms."""
    reference = None
    for arm in arms.values():
        pairs = {
            (case["id"], case["repetition"]): (
                case["task_sha256"],
                case["fixture_sha256"],
            )
            for case in arm["cases"]
        }
        if len(pairs) != len(arm["cases"]):
            raise ValueError("Duplicate case/repetition in comparison arm")
        if reference is None:
            reference = pairs
        elif pairs != reference:
            raise ValueError("Comparison arms have different inputs or fixtures")
    return True


def arm_summary(cases):
    metrics = summarize(cases)
    metrics.update(
        tool_calls=sum(case["result"]["tool_calls"] for case in cases),
        denied_calls=sum(case["result"]["invalid_calls"] for case in cases),
    )
    return metrics


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:11434")
    parser.add_argument(
        "--models",
        nargs="*",
        default=["mindbridge-qwen2.5-7b-ft:latest", "deepseek-r1:1.5b"],
    )
    parser.add_argument(
        "--cases", nargs="+", choices=list(CASES), default=DEFAULT_CASES
    )
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--budget-seconds", type=float, default=15)
    parser.add_argument("--temperature", type=float, default=0.35)
    parser.add_argument("--output-dir", default="target/planner-comparison")
    args = parser.parse_args(argv)
    if (
        not 1 <= args.repeat <= 5
        or not 0 < args.budget_seconds <= 120
        or not 0 <= args.temperature <= 1
        or len(set(args.models)) != len(args.models)
        or len(set(args.cases)) != len(args.cases)
    ):
        parser.error("Invalid repeat, budget, temperature or duplicate model/case")
    origin = validate_local_url(args.url)
    model_info = {}
    version = None
    if args.models:
        with httpx.Client(base_url=origin, timeout=10, trust_env=False) as client:
            response = client.get("/api/version")
            response.raise_for_status()
            version = response.json()["version"]
            response = client.get("/api/tags")
            response.raise_for_status()
            tags = {item["name"]: item for item in response.json()["models"]}
            for name in args.models:
                if name not in tags or "completion" not in tags[name].get(
                    "capabilities", []
                ):
                    raise ValueError(
                        f"An existing local completion model is required: {name}"
                    )
                response = client.post("/api/show", json={"model": name})
                response.raise_for_status()
                metadata = response.json()
                model_info[name] = {
                    **tags[name],
                    "thinking_controls": metadata.get("thinking"),
                    "parameters": metadata.get("parameters"),
                }
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    for path in sorted((root / "app").rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "app_source_sha256": digest.hexdigest(),
        "ollama_version": version,
        "models": model_info,
        "cases": args.cases,
        "repetitions": args.repeat,
        "budget_seconds": args.budget_seconds,
        "temperature": args.temperature,
        "max_output_tokens": 512,
        "format": "schema",
        "arms": {},
        "complete": False,
        "paired_inputs_verified": False,
        "limitations": [
            "Synthetic bounded directory tasks; no claim of general agent or clinical safety evaluation",
            "Rules, model, and model fallback are distinct; no inference guards are not model successes",
            "Models run sequentially after separate warmups; timings reflect this shared local machine",
            "TaskSpec and initial fixtures match; closed-loop observations follow each planner's actual actions",
            "No fixed sampling seed; repeated measurements do not imply population-level significance",
            "No model downloads, online model routing, external writes, DB or notifications",
            "Thinking defaults are not overridden; private thinking content is never recorded",
            "This comparison does not establish a need for independent agents or a second MCP client",
        ],
    }

    def save():
        (output / "report.json").write_bytes(
            json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")
        )

    save()
    for planner, model in [("rules", None), *[("model", name) for name in args.models]]:
        arm_id = "rules" if planner == "rules" else model
        settings = Settings(
            _env_file=None,
            ai_provider="ollama",
            ollama_base_url=origin,
            ollama_model=model or "unused",
            ai_temperature=args.temperature,
            ai_max_tokens=512,
            referral_action_format="schema",
            knowledge_vector_enabled=False,
        )
        arm = {"planner": planner, "model": model, "cases": [], "warmup": None}
        report["arms"][arm_id] = arm
        if model is not None:
            print(f"Warming existing {model}; excluded from case timings", flush=True)
            ai = AiClient(settings.model_copy(update={"ai_max_tokens": 8}))
            try:
                ai.complete(
                    [AiMessage(role="user", content="Reply OK")], timeout_seconds=120
                )
            except Exception as exc:
                arm["warmup_error"] = type(exc).__name__
            arm["warmup"] = ai.calls
            save()
        for repetition in range(args.repeat):
            for name in args.cases:
                print(f"{arm_id}: {name}, repetition {repetition + 1}", flush=True)
                case = evaluate_case(
                    settings,
                    name,
                    args.budget_seconds,
                    repetition,
                    planner=planner,
                    task_id=f"planner-comparison-{name}-{repetition}",
                )
                arm["cases"].append(case)
                arm["summary"] = arm_summary(arm["cases"])
                save()
                print(
                    json.dumps(
                        {
                            "stop": case["result"]["stop_reason"],
                            "success": case["success"],
                            "tools": case["result"]["tool_calls"],
                            "calls": len(case["attempts"]),
                        }
                    ),
                    flush=True,
                )
    report["paired_inputs_verified"] = verify_pairs(report["arms"])
    rules = report["arms"]["rules"]["summary"]
    report["rules_passed"] = (
        rules["expected_outcomes"] == rules["cases"]
        and rules["unauthorized_successes"] == 0
    )
    report["candidate_passed"] = {
        key: arm["summary"]["expected_outcomes"] == arm["summary"]["cases"]
        and arm["summary"]["fallback_plans"] == 0
        and arm["summary"]["unauthorized_successes"] == 0
        for key, arm in report["arms"].items()
        if arm["planner"] == "model"
    }
    report["complete"] = True
    save()
    failures = [
        {
            "arm": key,
            "case_index": index,
            "id": case["id"],
            "repetition": case["repetition"],
            "expected": case["expected"],
            "observed": case["result"]["stop_reason"],
        }
        for key, arm in report["arms"].items()
        for index, case in enumerate(arm["cases"])
        if not case["success"]
    ]
    (output / "failures.json").write_bytes(json.dumps(failures, indent=2).encode())
    lines = [
        "# Offline referral planner comparison",
        "",
        f"Budget {args.budget_seconds}s; temperature {args.temperature}; repeats {args.repeat}.",
        "",
        "|Planner|Expected outcomes|Model-only outcomes|Model requests|Fallbacks|Tool calls|P50 ms|P95 ms|",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key, arm in report["arms"].items():
        m = arm["summary"]
        lines.append(
            f"|{key}|{m['expected_outcomes']}/{m['cases']}|{m['model_only_expected_outcomes']}/{m['model_cases']}|"
            f"{m['model_attempts']}|{m['fallback_plans']}|{m['tool_calls']}|{m['elapsed_ms']['p50']:.2f}|{m['elapsed_ms']['p95']:.2f}|"
        )
    lines += [
        "",
        f"Inputs/fixtures paired: {report['paired_inputs_verified']}. Rule suite passed: {report['rules_passed']}.",
        f"Candidates meeting all selected outcomes without fallback: {json.dumps(report['candidate_passed'])}.",
        "",
        "An accepted candidate still requires demonstrated benefit before changing online behavior.",
        "",
        *[f"- {item}" for item in report["limitations"]],
    ]
    (output / "report.md").write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    print(
        json.dumps(
            {key: arm["summary"] for key, arm in report["arms"].items()}, indent=2
        ),
        flush=True,
    )
    return (
        0 if report["rules_passed"] and all(report["candidate_passed"].values()) else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
