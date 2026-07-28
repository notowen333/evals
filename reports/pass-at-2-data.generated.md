# pass@2 — strands-harness-benchmark-index

5 models · 206 tasks · 2 attempts/task · 412 trials per model (2060 total) · agent `stan@45fed43`

## Headline

| Model | pass@1 | pass@2 | lift | harness errors |
|---|---|---|---|---|
| openai.gpt-5.6-sol | 63.6% | **75.2%** | +11.7pp | 44/412 (11%) |
| opus-4.8 | 56.6% | **70.4%** | +13.8pp | 24/412 (6%) |
| sonnet-5 | 48.1% | **64.6%** | +16.5pp | 121/412 (29%) |
| openai.zai.glm-5 | 28.9% | **41.7%** | +12.9pp | 125/412 (30%) |
| kimi-k2.5 | 25.0% | **34.0%** | +9.0pp | 66/412 (16%) |

pass@1 is the mean reward across all 2 attempts (expected score of one attempt). At k=n=2 the estimator reduces exactly to *at least one of 2 attempts passed* — no extrapolation.

## pass@2 by source benchmark

| Model | tb21 | gaia | tau3 | swe_bench_pro | equal-weighted |
|---|---|---|---|---|---|
| openai.gpt-5.6-sol | 82.1% (28) | 63.8% (47) | 68.8% (48) | 83.1% (83) | **74.5%** |
| opus-4.8 | 85.7% (28) | 83.0% (47) | 54.2% (48) | 67.5% (83) | **72.6%** |
| sonnet-5 | 53.6% (28) | 66.0% (47) | 50.0% (48) | 75.9% (83) | **61.4%** |
| openai.zai.glm-5 | 42.9% (28) | 48.9% (47) | 50.0% (48) | 32.5% (83) | **43.6%** |
| kimi-k2.5 | 39.3% (28) | 40.4% (47) | 33.3% (48) | 28.9% (83) | **35.5%** |

Task counts in parentheses. Equal-weighted averages the four sources so swe_bench_pro (166 tasks) does not dominate tb21 (56).

## Harness errors

Every errored attempt still ran the verifier, so all of them carry a
reward and count toward the score above. These are agent-side failures,
not missing data.

| Model | AgentSetupTimeoutError | AgentTimeoutError | NonZeroAgentExitCodeError | total |
|---|---|---|---|---|
| openai.gpt-5.6-sol | 0 | 11 | 33 | 44 |
| opus-4.8 | 0 | 6 | 18 | 24 |
| sonnet-5 | 0 | 20 | 101 | 121 |
| openai.zai.glm-5 | 0 | 42 | 83 | 125 |
| kimi-k2.5 | 3 | 36 | 27 | 66 |

## Error-adjusted pass@2

Errored attempts dropped rather than scored 0. This is the ceiling if the
harness were perfectly reliable — an upper bound, since a timeout can also
mean the model was stuck.

| Model | pass@2 as-run | error-adjusted | gap | tasks w/ no clean attempt |
|---|---|---|---|---|
| openai.gpt-5.6-sol | 75.2% | 76.2% | +1.0pp | 4 |
| opus-4.8 | 70.4% | 71.5% | +1.1pp | 6 |
| sonnet-5 | 64.6% | 70.6% | +6.0pp | 26 |
| openai.zai.glm-5 | 41.7% | 46.6% | +4.9pp | 28 |
| kimi-k2.5 | 34.0% | 37.9% | +3.9pp | 24 |

## Run cost

| Model | wall clock | input tokens | output tokens | retries |
|---|---|---|---|---|
| openai.gpt-5.6-sol | 49m | 228,827,906 | 3,016,711 | 0 |
| opus-4.8 | 36m | 265,389,247 | 4,978,077 | 2 |
| sonnet-5 | 91m | 402,265,728 | 4,613,724 | 3 |
| openai.zai.glm-5 | 94m | 599,172,870 | 3,474,249 | 0 |
| kimi-k2.5 | 114m | 501,424,276 | 4,074,678 | 0 |

## Verification

Recomputed pass@k matches the value Harbor wrote to result.json:

| Model | this report | harbor result.json | agrees |
|---|---|---|---|
| openai.gpt-5.6-sol | 0.752427 | 0.752427 | yes |
| opus-4.8 | 0.703883 | 0.703883 | yes |
| sonnet-5 | 0.645631 | 0.645631 | yes |
| openai.zai.glm-5 | 0.417476 | 0.417476 | yes |
| kimi-k2.5 | 0.339806 | 0.339806 | yes |
