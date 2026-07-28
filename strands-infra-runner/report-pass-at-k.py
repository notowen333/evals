"""Build a pass@k report from local Harbor job dirs.

Reads jobs/*--k<N>/result.json and emits a markdown report: pass@1 vs pass@k
overall and per source benchmark, plus the harness-error breakdown needed to
read those numbers honestly.

Usage:
    python3 strands-infra-runner/report-pass-at-k.py [-k 2] [-o report.md]
"""

import argparse
import glob
import json
import statistics
from collections import Counter, defaultdict

# Mirrors SOURCE_PREFIXES in the index adapter's metric_plugin.py.
SOURCE_PREFIXES = {
    "tb-": "tb21",
    "gaia-": "gaia",
    "tau3-": "tau3",
    "swebenchpro-": "swe_bench_pro",
}
SOURCES = ["tb21", "gaia", "tau3", "swe_bench_pro"]


def source_of(task_name: str) -> str:
    leaf = task_name.split("/")[-1]
    for prefix, source in SOURCE_PREFIXES.items():
        if leaf.startswith(prefix):
            return source
    return "other"


def pass_at_k(n: int, c: int, k: int) -> float:
    """Unbiased Codex estimator, identical to harbor.utils.pass_at_k."""
    if n - c < k:
        return 1.0
    product = 1.0
    for i in range(k):
        product *= (n - c - i) / (n - i)
    return 1.0 - product


def reward_of(trial: dict) -> float | None:
    """Binary reward, or None when the verifier produced none."""
    rewards = (trial.get("verifier_result") or {}).get("rewards") or {}
    if len(rewards) != 1:
        return None
    value = next(iter(rewards.values()))
    return float(value) if isinstance(value, (int, float)) else None


def load(job_dir: str) -> dict:
    with open(f"{job_dir}/result.json") as fh:
        result = json.load(fh)
    model = job_dir.split("/")[-1].split("--")[1]
    trials = result["trial_results"]

    per_task: defaultdict[str, list[dict]] = defaultdict(list)
    for trial in trials:
        per_task[trial["task_name"]].append(trial)

    errors = Counter()
    for trial in trials:
        info = trial.get("exception_info")
        if info is not None:
            errors[info.get("exception_type") or "Unknown"] += 1

    stats = result.get("stats") or {}
    return {
        "model": model,
        "job": job_dir.split("/")[-1],
        "trials": trials,
        "per_task": per_task,
        "errors": errors,
        "n_errored": sum(errors.values()),
        "started_at": result.get("started_at"),
        "finished_at": result.get("finished_at"),
        "n_input_tokens": stats.get("n_input_tokens"),
        "n_output_tokens": stats.get("n_output_tokens"),
        "n_retries": stats.get("n_retries"),
        "harbor_pass_at_k": (
            (stats.get("evals") or {})
            .get("stan__strands-harness-benchmark-index", {})
            .get("pass_at_k")
        ),
    }


def score(per_task: dict[str, list[dict]], k: int, *, drop_errored: bool = False):
    """Return (pass@1, pass@k, n_tasks, n_dropped_tasks).

    pass@1 is the mean reward over attempts — the expected single-attempt score.
    With drop_errored, attempts whose agent raised are excluded entirely rather
    than counted as 0, isolating capability from harness flakiness.
    """
    p1_numer = p1_denom = 0
    pk_values = []
    dropped = 0
    for trials in per_task.values():
        if drop_errored:
            trials = [t for t in trials if t.get("exception_info") is None]
        rewards = [reward_of(t) for t in trials]
        rewards = [0.0 if r is None else r for r in rewards]
        if not rewards:
            dropped += 1
            continue
        p1_numer += sum(rewards)
        p1_denom += len(rewards)
        n, c = len(rewards), int(sum(rewards))
        pk_values.append(pass_at_k(n, c, min(k, n)))
    if not pk_values:
        return None, None, 0, dropped
    return (
        p1_numer / p1_denom,
        statistics.fmean(pk_values),
        len(pk_values),
        dropped,
    )


def by_source(per_task: dict[str, list[dict]], k: int, *, drop_errored: bool = False):
    grouped: defaultdict[str, dict] = defaultdict(dict)
    for name, trials in per_task.items():
        grouped[source_of(name)][name] = trials
    return {
        src: score(tasks, k, drop_errored=drop_errored)
        for src, tasks in grouped.items()
    }


def pct(x) -> str:
    return "—" if x is None else f"{100 * x:.1f}%"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", type=int, default=2)
    ap.add_argument("-o", default="-")
    ap.add_argument("--jobs-glob", default="jobs/*--k{k}")
    args = ap.parse_args()
    k = args.k

    job_dirs = sorted(glob.glob(args.jobs_glob.format(k=k)))
    jobs = [load(d) for d in job_dirs if glob.glob(f"{d}/result.json")]
    if not jobs:
        raise SystemExit(f"No job dirs matched {args.jobs_glob.format(k=k)}")

    # Rank by pass@k, best first.
    for job in jobs:
        job["overall"] = score(job["per_task"], k)
        job["clean"] = score(job["per_task"], k, drop_errored=True)
        job["sources"] = by_source(job["per_task"], k)
    jobs.sort(key=lambda j: j["overall"][1] or 0, reverse=True)

    out = []
    w = out.append

    w(f"# pass@{k} — strands-harness-benchmark-index\n")
    total_trials = sum(len(j["trials"]) for j in jobs)
    n_tasks = jobs[0]["overall"][2]
    w(
        f"{len(jobs)} models · {n_tasks} tasks · {k} attempts/task · "
        f"{total_trials // len(jobs)} trials per model "
        f"({total_trials} total) · agent `stan@45fed43`\n"
    )

    w(f"## Headline\n")
    w(f"| Model | pass@1 | pass@{k} | lift | harness errors |")
    w("|---|---|---|---|---|")
    for job in jobs:
        p1, pk, _, _ = job["overall"]
        lift = f"+{100 * (pk - p1):.1f}pp" if p1 is not None else "—"
        err = job["n_errored"]
        w(
            f"| {job['model']} | {pct(p1)} | **{pct(pk)}** | {lift} | "
            f"{err}/{len(job['trials'])} ({100 * err / len(job['trials']):.0f}%) |"
        )
    w("")
    w(
        f"pass@1 is the mean reward across all {k} attempts (expected score of one "
        f"attempt). At k=n={k} the estimator reduces exactly to *at least one of "
        f"{k} attempts passed* — no extrapolation."
    )
    w("")

    w(f"## pass@{k} by source benchmark\n")
    w("| Model | " + " | ".join(SOURCES) + " | equal-weighted |")
    w("|---" * (len(SOURCES) + 2) + "|")
    for job in jobs:
        cells = []
        vals = []
        for src in SOURCES:
            entry = job["sources"].get(src)
            if not entry or entry[1] is None:
                cells.append("—")
                continue
            cells.append(f"{pct(entry[1])} ({entry[2]})")
            vals.append(entry[1])
        eq = statistics.fmean(vals) if vals else None
        w(f"| {job['model']} | " + " | ".join(cells) + f" | **{pct(eq)}** |")
    w("")
    w(
        "Task counts in parentheses. Equal-weighted averages the four sources so "
        "swe_bench_pro (166 tasks) does not dominate tb21 (56)."
    )
    w("")

    w("## Harness errors\n")
    w("Every errored attempt still ran the verifier, so all of them carry a")
    w("reward and count toward the score above. These are agent-side failures,")
    w("not missing data.\n")
    kinds = sorted({kind for job in jobs for kind in job["errors"]})
    w("| Model | " + " | ".join(kinds) + " | total |")
    w("|---" * (len(kinds) + 2) + "|")
    for job in jobs:
        cells = [str(job["errors"].get(kind, 0)) for kind in kinds]
        w(f"| {job['model']} | " + " | ".join(cells) + f" | {job['n_errored']} |")
    w("")

    w(f"## Error-adjusted pass@{k}\n")
    w("Errored attempts dropped rather than scored 0. This is the ceiling if the")
    w("harness were perfectly reliable — an upper bound, since a timeout can also")
    w("mean the model was stuck.\n")
    w(f"| Model | pass@{k} as-run | error-adjusted | gap | tasks w/ no clean attempt |")
    w("|---|---|---|---|---|")
    for job in jobs:
        _, pk, _, _ = job["overall"]
        _, pk_clean, _, dropped = job["clean"]
        gap = (
            f"+{100 * (pk_clean - pk):.1f}pp"
            if None not in (pk, pk_clean)
            else "—"
        )
        w(f"| {job['model']} | {pct(pk)} | {pct(pk_clean)} | {gap} | {dropped} |")
    w("")

    w("## Run cost\n")
    w("| Model | wall clock | input tokens | output tokens | retries |")
    w("|---|---|---|---|---|")
    for job in jobs:
        import datetime

        dur = "—"
        if job["started_at"] and job["finished_at"]:
            parse = lambda s: datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
            mins = (parse(job["finished_at"]) - parse(job["started_at"])).total_seconds() / 60
            dur = f"{mins:.0f}m"
        fmt = lambda n: f"{n:,}" if isinstance(n, int) else "—"
        w(
            f"| {job['model']} | {dur} | {fmt(job['n_input_tokens'])} | "
            f"{fmt(job['n_output_tokens'])} | {fmt(job['n_retries'])} |"
        )
    w("")

    w("## Verification\n")
    w("Recomputed pass@k matches the value Harbor wrote to result.json:\n")
    w("| Model | this report | harbor result.json | agrees |")
    w("|---|---|---|---|")
    for job in jobs:
        mine = job["overall"][1]
        theirs = (job["harbor_pass_at_k"] or {}).get(str(k))
        ok = "yes" if theirs is not None and abs(mine - theirs) < 1e-9 else "NO"
        w(
            f"| {job['model']} | {mine:.6f} | "
            f"{theirs if theirs is None else f'{theirs:.6f}'} | {ok} |"
        )
    w("")

    text = "\n".join(out)
    if args.o == "-":
        print(text)
    else:
        with open(args.o, "w") as fh:
            fh.write(text)
        print(f"Wrote {args.o}")


if __name__ == "__main__":
    main()
