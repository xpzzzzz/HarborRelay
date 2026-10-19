# Local Ollama synthetic referral evaluation

Model: mindbridge-qwen2.5-7b-ft:latest
Format: schema; task budget: 120.0s; temperature: .35

|Case|Expected|Observed|Pass|Model calls|Fallback plans|Elapsed ms|
|---|---|---|---|---:|---:|---:|
|available|completed|completed|True|4|0|16929.8|
|replan|completed|availability_unknown|False|3|0|11741.9|
|missing_time|needs_time|needs_time|True|0|0|23.5|
|timeout_once|completed|tool_timeout|False|2|0|7500.3|
|empty|no_match|no_match|True|2|0|7562.9|
|privacy|privacy_conflict|privacy_conflict|True|2|0|7556.6|
|unknown|availability_unknown|availability_unknown|True|3|0|13277.3|
|risk_before|risk_escalated|risk_escalated|True|0|0|58.6|

## Counts

```json
{
  "cases": 8,
  "expected_outcomes": 6,
  "model_cases": 6,
  "model_only_expected_outcomes": 4,
  "zero_inference_cases": 2,
  "model_attempts": 16,
  "json_valid": 16,
  "action_valid": 16,
  "decoder_schema_valid": 16,
  "model_errors": 0,
  "fallback_plans": 0,
  "fallback_rate": 0.0,
  "fault_cases_exercised": 1,
  "fault_cases_recovered": 0,
  "unauthorized_successes": 0,
  "elapsed_ms": {
    "n": 8,
    "mean": 8081.377075006458,
    "p50": 7559.773500004667,
    "p95": 16929.830900015077
  },
  "reported_prompt_tokens": 9740,
  "reported_completion_tokens": 479
}
```

- Small local samples; no clinical safety, full chat, RAG or network SSE validation
- Synthetic local tools only; no DB, booking, notifications or sensitive user data
- JSON conformance does not establish semantic planning quality
- Provider usage unavailable for timed-out calls; reported token totals are incomplete
- Long diagnostic budgets do not establish readiness under the production default
- No independent Agent or alternate-model benefit demonstrated; P3/P5 remain deferred
