# Synthetic architecture evaluation

Passed: True; repetitions: 3

|Implementation|Task completion|Safety failures|Mean first token (ms)|RAG MRR|
|---|---:|---:|---:|---:|
|old_workflow|0.429|7|275.25|0.911111|
|single_referral_agent|1.000|0|337.35|0.911111|

Referral scenarios: 45; pass rate: 1.000.

## Limits

- Mock-only synthetic fixtures; not clinical validation or real-model benchmarks
- SSE timings measured inside generator, not over network; template/referral output is explicitly precomputed
- Old workflow lacked referral tools; completion difference is capability coverage, not an LLM intelligence gain
- Recall@K retains legacy any-relevant-hit definition
- This Mock suite does not exercise MySQL contention, real Redis/Chroma/SMTP or Ollama; see separate integration probes
