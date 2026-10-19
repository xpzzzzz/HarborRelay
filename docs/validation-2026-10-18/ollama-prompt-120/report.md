# Local Ollama synthetic referral evaluation

Model: mindbridge-qwen2.5-7b-ft:latest
Format: prompt; task budget: 120.0s; temperature: .35

|Case|Expected|Observed|Pass|Model calls|Fallback plans|Elapsed ms|
|---|---|---|---|---:|---:|---:|
|available|completed|completed|True|4|0|21599.8|
|replan|completed|repeated_results|False|3|0|11396.7|
|timeout_once|completed|repeated_results|False|4|0|15740.0|

## Counts

```json
{
  "cases": 3,
  "expected_outcomes": 1,
  "model_cases": 3,
  "model_only_expected_outcomes": 1,
  "zero_inference_cases": 0,
  "model_attempts": 11,
  "json_valid": 11,
  "action_valid": 11,
  "decoder_schema_valid": 10,
  "model_errors": 0,
  "fallback_plans": 0,
  "fallback_rate": 0.0,
  "fault_cases_exercised": 1,
  "fault_cases_recovered": 0,
  "unauthorized_successes": 0,
  "elapsed_ms": {
    "n": 3,
    "mean": 16245.502966669543,
    "p50": 15740.016099996865,
    "p95": 21599.8311000003
  },
  "reported_prompt_tokens": 7831,
  "reported_completion_tokens": 379
}
```

- Small local samples; no clinical safety, full chat, RAG or network SSE validation
- Synthetic local tools only; no DB, booking, notifications or sensitive user data
- JSON conformance does not establish semantic planning quality
- Provider usage unavailable for timed-out calls; reported token totals are incomplete
- Long diagnostic budgets do not establish readiness under the production default
- No independent Agent or alternate-model benefit demonstrated; P3/P5 remain deferred
