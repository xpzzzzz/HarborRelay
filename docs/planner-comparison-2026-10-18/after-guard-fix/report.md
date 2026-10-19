# Offline referral planner comparison

Budget 15s; temperature 0.35; repeats 1.

|Planner|Expected outcomes|Model-only outcomes|Model requests|Fallbacks|Tool calls|P50 ms|P95 ms|
|---|---:|---:|---:|---:|---:|---:|---:|
|rules|9/9|0/0|0|0|29|15.00|986.39|
|mindbridge-qwen2.5-7b-ft:latest|3/9|2/8|20|2|13|13048.08|15068.46|
|deepseek-r1:1.5b|2/9|1/8|19|13|12|11172.33|15065.09|

Inputs/fixtures paired: True. Rule suite passed: True.
Candidates meeting all selected outcomes without fallback: {"mindbridge-qwen2.5-7b-ft:latest": false, "deepseek-r1:1.5b": false}.

An accepted candidate still requires demonstrated benefit before changing online behavior.

- Synthetic bounded directory tasks; no claim of general agent or clinical safety evaluation
- Rules, model, and model fallback are distinct; no inference guards are not model successes
- Models run sequentially after separate warmups; timings reflect this shared local machine
- TaskSpec and initial fixtures match; closed-loop observations follow each planner's actual actions
- No fixed sampling seed; repeated measurements do not imply population-level significance
- No model downloads, online model routing, external writes, DB or notifications
- Thinking defaults are not overridden; private thinking content is never recorded
- This comparison does not establish a need for independent agents or a second MCP client
