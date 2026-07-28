# pass@2 — Strands Harness Benchmark Index

**Run date:** 2026-07-27 23:21 → 2026-07-28 05:50 UTC (6h29m wall clock)
**Agent:** `stan@45fed43` (pinned; identical build across all five cells)
**Dataset:** `strands-harness-benchmark-index` — 206 tasks
**Attempts:** k=2 per task → 412 trials per model, 2,060 total
**Job dirs:** `jobs/stan@45fed43--<model>--strands-harness-benchmark-index--k2/`

Regenerate the tables below with:

```bash
python3 strands-infra-runner/report-pass-at-k.py -k 2 -o reports/pass-at-2-data.generated.md
```

---

## 1. Headline

| Model | pass@1 | pass@2 | lift | harness errors |
|---|---|---|---|---|
| openai.gpt-5.6-sol | 63.6% | **75.2%** | +11.7pp | 44/412 (11%) |
| opus-4.8 | 56.6% | **70.4%** | +13.8pp | 24/412 (6%) |
| sonnet-5 | 48.1% | **64.6%** | +16.5pp | 121/412 (29%) |
| openai.zai.glm-5 | 28.9% | **41.7%** | +12.9pp | 125/412 (30%) |
| kimi-k2.5 | 25.0% | **34.0%** | +9.0pp | 66/412 (16%) |

`pass@1` is the mean reward over all 2 attempts — the expected score of a single
attempt. At k=n=2 the unbiased estimator reduces exactly to *at least one of the
two attempts passed*, so there is no extrapolation in the pass@2 column.

**Validity check.** pass@k was recomputed from raw `trial_results` and agrees
with the value Harbor independently wrote to each `result.json` to six decimal
places for all five models. Every model ran all 206 tasks twice; only 4 trials
in the entire matrix (kimi) lack a reward.

---

## 2. The ranking flips depending on how you weight the sources

| Model | tb21 (28) | gaia (47) | tau3 (48) | swe_bench_pro (83) | equal-weighted |
|---|---|---|---|---|---|
| openai.gpt-5.6-sol | 82.1% | 63.8% | 68.8% | **83.1%** | **74.5%** |
| opus-4.8 | **85.7%** | **83.0%** | 54.2% | 67.5% | **72.6%** |
| sonnet-5 | 53.6% | 66.0% | 50.0% | 75.9% | 61.4% |
| openai.zai.glm-5 | 42.9% | 48.9% | 50.0% | 32.5% | 43.6% |
| kimi-k2.5 | 39.3% | 40.4% | 33.3% | 28.9% | 35.5% |

Equal-weighted averages the four source benchmarks so that swe_bench_pro
(83 tasks, 40% of the index) does not dominate tb21 (28 tasks, 14%).

**gpt-5.6-sol's overall lead is partly a task-mix artifact.** Equal-weighted, the
top two are within 1.9pp — effectively tied — and they are strong in different
places:

- opus-4.8 is **+19.2pp on gaia** and +31.5pp on tb21-vs-tau3 balance
- gpt-5.6-sol is **+15.6pp on swe_bench_pro** and **+14.6pp on tau3**

Because swe_bench_pro is the single largest slice, the raw ranking rewards
whoever wins it. Any headline claim from this run should state which weighting
it uses.

**tb21 is weak across the board** (39–86%, and only 28 tasks). A strong model
scoring 85.7% while two models sit below 43% on the same 28 tasks is worth
investigating as a possible harness or task-definition issue, not just capability.

---

## 3. Harness errors

Every errored attempt **still ran the verifier** and carries a reward, so all of
them are counted in the scores above. These are agent-side failures, not missing
data — there are no gaps in the pass@2 computation.

| Model | NonZeroAgentExitCode | AgentTimeout | AgentSetupTimeout | total |
|---|---|---|---|---|
| sonnet-5 | **101** | 20 | 0 | 121 |
| openai.zai.glm-5 | 83 | **42** | 0 | 125 |
| kimi-k2.5 | 27 | 36 | 3 | 66 |
| openai.gpt-5.6-sol | 33 | 11 | 0 | 44 |
| opus-4.8 | 18 | 6 | 0 | 24 |

Two distinct mechanisms, both agent-side:

- **`NonZeroAgentExitCodeError`** — the agent process exited 1. Dominant everywhere.
- **`AgentTimeoutError`** — hit the 1800s agent wall. Concentrated in glm-5 and kimi.

**Notably absent: `ApiRateLimitError`.** Zero occurrences, and 0–3 retries per
model. See §6 — the concurrency caps applied to this run were sized for a failure
mode that did not occur.

---

## 4. Error-adjusted pass@2

Errored attempts dropped rather than scored 0. This is a **ceiling**, not a
correction: an `AgentTimeoutError` can also mean the model was genuinely stuck,
in which case a 0 is the honest score.

| Model | as-run | error-adjusted | gap | tasks with no clean attempt |
|---|---|---|---|---|
| openai.gpt-5.6-sol | 75.2% | 76.2% | +1.0pp | 4 |
| opus-4.8 | 70.4% | 71.5% | +1.1pp | 6 |
| sonnet-5 | 64.6% | 70.6% | **+6.0pp** | 26 |
| openai.zai.glm-5 | 41.7% | 46.6% | +4.9pp | 28 |
| kimi-k2.5 | 34.0% | 37.9% | +3.9pp | 24 |

The 29–30% error rates on sonnet-5 and glm-5 are real but **not rank-changing at
the top**. sonnet-5 would need every dropped attempt to have succeeded to reach
opus, which its 20 timeouts argue against.

---

## 5. Run cost

| Model | wall clock | `-n` | input tokens | output tokens | retries |
|---|---|---|---|---|---|
| opus-4.8 | 36m | 206 | 265,389,247 | 4,978,077 | 2 |
| openai.gpt-5.6-sol | 49m | 206 | 228,827,906 | 3,016,711 | 0 |
| sonnet-5 | 91m | 80 | 402,265,728 | 4,613,724 | 3 |
| openai.zai.glm-5 | 94m | 100 | 599,172,870 | 3,474,249 | 0 |
| kimi-k2.5 | 114m | 100 | 501,424,276 | 4,074,678 | 0 |

**opus-4.8 was the best-behaved and the fastest** — fewest errors (24), lowest
wall clock (36m), second-lowest input tokens.

**glm-5 is the efficiency outlier**: 599M input tokens — 2.3× opus — to land
28.7pp lower. Cost per point of pass@2 is roughly 4× opus's.

---

## 6. Open items

**a. sonnet-5's 101 `NonZeroAgentExitCodeError` (24.5% of its trials).** The
largest single distortion in the matrix and the highest-value thing to fix. This
is a bug to locate in the per-trial agent logs, not a statistic to reweight.

**b. The concurrency caps did not pay for themselves.** sonnet-5, glm-5 and kimi
ran at `-n` 80/100/100 on the theory that Bedrock throttling was the dominant
error class (it was, at 53/206, in the k=1 baselines). This run recorded **zero**
`ApiRateLimitError` and ≤3 retries. The caps cost roughly 2.5h of the 6h29m and
the two most-capped models still had the worst error rates. Raising them toward
206 is the obvious next change.

**c. tb21 weakness across all five models** — see §2.

**d. pass@2 gives one datapoint, no curve.** k=4 would yield pass@1/2/4 for 2×
the compute and is cheaper per datapoint if a saturation curve is wanted.

**e. sonnet-4.6 has no k=1 baseline on this index** (only a 4-task smoke test),
so it is absent from this matrix.

---

## 7. Method

- pass@k uses the unbiased Codex estimator `1 - Π((n-c-i)/(n-i))`, matching
  `harbor/utils/pass_at_k.py`; verified against Harbor's own output (§1).
- Source attribution mirrors `SOURCE_PREFIXES` in the index adapter's
  `metric_plugin.py` (`tb-`, `gaia-`, `tau3-`, `swebenchpro-`).
- Results were pulled from `s3://strands-benchmark-results-mirror/jobs/` and
  verified object-for-object against S3 (12,872 / 12,882 / 12,907 / 12,905 /
  12,797 files) before analysis.
- **Not attempted: pooling attempts across models.** pass@k assumes the n
  attempts for a task are i.i.d. draws from one policy; mixing an opus attempt
  with a sonnet attempt would estimate a random-routing ensemble that nobody
  would ship. A per-task best-of-any-model figure is computable but should be
  labelled an oracle upper bound, not pass@k.
