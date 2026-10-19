"""Run: python -m app.evaluation.runner --baseline-ref f172dd8 --repeat 3."""

import argparse
import hashlib
import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def distribution(values):
    values = sorted(value for value in values if value is not None)
    if not values:
        return None
    return {
        "n": len(values),
        "mean": statistics.mean(values),
        "p50": statistics.median(values),
        "p95": values[math.ceil(len(values) * 0.95) - 1],
    }


def summarize(result):
    cases = result["cases"]
    return {
        "task_cases": len(cases),
        "task_success_rate": sum(r["success"] for r in cases) / len(cases),
        "elapsed_ms": distribution([r["elapsed_ms"] for r in cases]),
        "sse_first_token_ms": distribution([r["first_token_ms"] for r in cases]),
        "tool_calls": sum(r["tool_calls"] for r in cases),
        "invalid_calls": sum(r["invalid_calls"] for r in cases),
        "safety_checks": result["safety"],
        "safety_failures": sum(not ok for ok in result["safety"].values()),
        "queue_retry_recovered": result["queue_retry_recovered"],
        "tool_audit_events": result["tool_audit_events"],
        "rag": result["rag"],
    }


def run_probe(root, dataset, output, repeat):
    probe = Path(__file__).with_name("probe.py").resolve()
    command = [
        sys.executable,
        str(probe),
        "--project",
        str(root),
        "--dataset",
        str(dataset),
        "--output",
        str(output),
        "--repeat",
        str(repeat),
    ]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(root)}
    result = subprocess.run(
        command,
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    output.with_suffix(".stderr.txt").write_text(result.stderr, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(
            f"Evaluation probe failed ({result.returncode}): {result.stderr[-2400:]}"
        )
    return json.loads(output.read_text(encoding="utf-8"))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Isolated synthetic HarborRelay architecture evaluation"
    )
    parser.add_argument(
        "--baseline-ref",
        default=None,
        help="Optional verified local commit; no network access",
    )
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--output-dir", default="target/evaluation")
    args = parser.parse_args(argv)
    if not 1 <= args.repeat <= 20:
        parser.error("repeat must be 1..20")
    root = Path(__file__).resolve().parents[2]
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    dataset = Path(__file__).with_name("cases.json").resolve()
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "repetitions": args.repeat,
        "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "comparisons": {},
    }
    if args.baseline_ref:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--verify", f"{args.baseline_ref}^{{commit}}"],
            cwd=root,
            text=True,
        ).strip()
        snapshot_root = (root / "target/evaluation-snapshots").resolve()
        snapshot_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="baseline-", dir=snapshot_root
        ) as temporary:
            temporary = Path(temporary).resolve()
            if not temporary.is_relative_to(snapshot_root):
                raise RuntimeError("Unsafe snapshot directory")
            archive = temporary / "snapshot.zip"
            subprocess.run(
                ["git", "archive", "--format=zip", f"--output={archive}", sha],
                cwd=root,
                check=True,
            )
            checkout = temporary / "source"
            checkout.mkdir()
            with zipfile.ZipFile(archive) as bundle:
                if any(
                    not (checkout / name).resolve().is_relative_to(checkout)
                    for name in bundle.namelist()
                ):
                    raise RuntimeError("Unsafe archive path")
                bundle.extractall(checkout)
            baseline = run_probe(
                checkout, dataset, output / "baseline.json", args.repeat
            )
        report["baseline_commit"] = sha
        report["comparisons"]["old_workflow"] = summarize(baseline)
    current = run_probe(root, dataset, output / "current.json", args.repeat)
    report["comparisons"]["single_referral_agent"] = summarize(current)
    from app.evaluation.scenarios import evaluate_referral

    faults = evaluate_referral(args.repeat)
    (output / "referral-scenarios.json").write_text(
        json.dumps(faults, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    transient = [r for r in faults if r["id"] == "timeout_once"]
    completed = [r for r in faults if r["stop_reason"] == "completed"]
    report["referral_scenarios"] = {
        "cases": len(faults),
        "success_rate": sum(r["success"] for r in faults) / len(faults),
        "completed_evidence_accuracy": sum(r["evidence_correct"] for r in completed)
        / len(completed),
        "transient_failure_recovery_rate": sum(r["success"] for r in transient)
        / len(transient),
        "unauthorized_successes": sum(r["unauthorized_success"] for r in faults),
        "tool_calls": sum(r["tool_calls"] for r in faults),
        "denied_action_attempts": sum(r["invalid_calls"] for r in faults),
        "old_workflow": "not_supported; no referral tool loop existed",
    }
    failures = [
        {"suite": "turns", "id": r["id"], "repetition": r["repetition"]}
        for r in current["cases"]
        if not r["success"]
    ]
    failures += [
        {"suite": "safety", "id": key}
        for key, value in current["safety"].items()
        if not value
    ]
    failures += [
        {"suite": "referral", "id": r["id"], "repetition": r["repetition"]}
        for r in faults
        if not r["success"]
    ]
    rag = current["rag"]
    if rag["recallAtK"] < 0.95 or rag["mrr"] < 0.75 or rag["ndcgAtK"] < 0.75:
        failures.append({"suite": "rag", "id": "quality_gate"})
    report["passed"] = not failures
    report["p3"] = "not_implemented: no independent-agent ablation benefit demonstrated"
    report["p5"] = (
        "not_implemented: no real second client or authenticated integration requirement"
    )
    report["limitations"] = [
        "Mock-only synthetic fixtures; not clinical validation or real-model benchmarks",
        "SSE timings measured inside generator, not over network; template/referral output is explicitly precomputed",
        "Old workflow lacked referral tools; completion difference is capability coverage, not an LLM intelligence gain",
        "Recall@K retains legacy any-relevant-hit definition",
        "This Mock suite does not exercise MySQL contention, real Redis/Chroma/SMTP or Ollama; see separate integration probes",
    ]
    source_digest = hashlib.sha256()
    for path in sorted((root / "app").rglob("*.py")):
        source_digest.update(path.relative_to(root).as_posix().encode())
        source_digest.update(path.read_bytes())
    report["app_source_sha256"] = source_digest.hexdigest()
    (output / "failures.json").write_text(
        json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# Synthetic architecture evaluation",
        "",
        f"Passed: {report['passed']}; repetitions: {args.repeat}",
        "",
        "|Implementation|Task completion|Safety failures|Mean first token (ms)|RAG MRR|",
        "|---|---:|---:|---:|---:|",
    ]
    for name, metrics in report["comparisons"].items():
        lines.append(
            f"|{name}|{metrics['task_success_rate']:.3f}|{metrics['safety_failures']}|{metrics['sse_first_token_ms']['mean']:.2f}|{metrics['rag']['mrr']:.6f}|"
        )
    lines += [
        "",
        f"Referral scenarios: {report['referral_scenarios']['cases']}; pass rate: {report['referral_scenarios']['success_rate']:.3f}.",
        "",
        "## Limits",
        "",
        *[f"- {item}" for item in report["limitations"]],
    ]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "report": str(output / "report.json"),
                "comparisons": report["comparisons"],
                "referral_scenarios": report["referral_scenarios"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
