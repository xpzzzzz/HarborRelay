# Offline referral planner comparison

Budget 15s; temperature 0.35; repeats 3.

|Planner|Expected outcomes|Model-only outcomes|Model requests|Fallbacks|Tool calls|P50 ms|P95 ms|
|---|---:|---:|---:|---:|---:|---:|---:|
|rules|27/27|0/0|0|0|87|12.90|14.45|
|mindbridge-qwen2.5-7b-ft:latest|10/27|7/24|64|0|43|9813.04|10767.56|
|deepseek-r1:1.5b|7/27|0/24|63|41|42|11372.37|15060.21|

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
