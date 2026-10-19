# Local Ollama synthetic referral evaluation

Model: mindbridge-qwen2.5-7b-ft:latest
Format: schema; task budget: 15.0s; temperature: .35

|Case|Expected|Observed|Pass|Model calls|Fallback plans|Elapsed ms|
|---|---|---|---|---:|---:|---:|
|available|completed|repeated_results|False|3|0|13247.9|
|replan|completed|availability_unknown|False|3|0|12678.2|
|missing_time|needs_time|needs_time|True|0|0|21.3|
|timeout_once|completed|tool_timeout|False|2|0|7912.2|

## Counts

```json
{
  "cases": 4,
  "expected_outcomes": 1,
  "model_cases": 3,
  "model_only_expected_outcomes": 0,
  "zero_inference_cases": 1,
  "model_attempts": 8,
  "json_valid": 8,
  "action_valid": 8,
  "decoder_schema_valid": 8,
  "model_errors": 0,
  "fallback_plans": 0,
  "fallback_rate": 0.0,
  "fault_cases_exercised": 1,
  "fault_cases_recovered": 0,
  "unauthorized_successes": 0,
  "elapsed_ms": {
    "n": 4,
    "mean": 8464.885749999667,
    "p50": 10295.20274999959,
    "p95": 13247.866699995939
  },
  "reported_prompt_tokens": 4797,
  "reported_completion_tokens": 248
}
```

- Small local samples; no clinical safety, full chat, RAG or network SSE validation
- Synthetic local tools only; no DB, booking, notifications or sensitive user data
- JSON conformance does not establish semantic planning quality
- Provider usage unavailable for timed-out calls; reported token totals are incomplete
- Long diagnostic budgets do not establish readiness under the production default
- No independent Agent or alternate-model benefit demonstrated; P3/P5 remain deferred
