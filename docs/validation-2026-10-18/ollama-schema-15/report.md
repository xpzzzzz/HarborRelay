# Local Ollama synthetic referral evaluation

Model: mindbridge-qwen2.5-7b-ft:latest
Format: schema; task budget: 15.0s; temperature: .35

|Case|Expected|Observed|Pass|Model calls|Fallback plans|Elapsed ms|
|---|---|---|---|---:|---:|---:|
|available|completed|time_budget|False|4|1|16015.0|
|replan|completed|availability_unknown|False|3|0|13479.4|
|missing_time|needs_time|needs_time|True|0|0|25.3|
|timeout_once|completed|tool_timeout|False|2|0|7673.6|
|available|completed|repeated_results|False|3|0|12877.3|
|replan|completed|availability_unknown|False|3|0|13513.3|
|missing_time|needs_time|needs_time|True|0|0|29.4|
|timeout_once|completed|tool_timeout|False|2|0|7564.2|
|available|completed|time_budget|False|4|1|15058.6|
|replan|completed|availability_unknown|False|3|0|11440.3|
|missing_time|needs_time|needs_time|True|0|0|17.5|
|timeout_once|completed|tool_timeout|False|2|0|6260.9|

## Counts

```json
{
  "cases": 12,
  "expected_outcomes": 3,
  "model_cases": 9,
  "model_only_expected_outcomes": 0,
  "zero_inference_cases": 3,
  "model_attempts": 26,
  "json_valid": 24,
  "action_valid": 24,
  "decoder_schema_valid": 24,
  "model_errors": 2,
  "fallback_plans": 2,
  "fallback_rate": 0.07692307692307693,
  "fault_cases_exercised": 3,
  "fault_cases_recovered": 0,
  "unauthorized_successes": 0,
  "elapsed_ms": {
    "n": 12,
    "mean": 8662.898258340041,
    "p50": 9556.915200009826,
    "p95": 16015.044200001284
  },
  "reported_prompt_tokens": 14360,
  "reported_completion_tokens": 740
}
```

- Small local samples; no clinical safety, full chat, RAG or network SSE validation
- Synthetic local tools only; no DB, booking, notifications or sensitive user data
- JSON conformance does not establish semantic planning quality
- Provider usage unavailable for timed-out calls; reported token totals are incomplete
- Long diagnostic budgets do not establish readiness under the production default
- No independent Agent or alternate-model benefit demonstrated; P3/P5 remain deferred
