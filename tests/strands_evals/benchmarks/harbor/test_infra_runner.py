import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parents[4]
RUNNER = REPO_ROOT / "strands-infra-runner" / "run.py"
BENCHMARK_LAUNCHER = REPO_ROOT / "strands-infra-runner" / "run-benchmark.sh"
MATRIX_LAUNCHER = REPO_ROOT / "strands-infra-runner" / "run-matrix.sh"
FULL_SUITE_LAUNCHER = REPO_ROOT / "strands-infra-runner" / "run-full-native-suite.sh"
INSTANCE_PROFILE = "StrandsBenchmarkHarborNodeRole"


def _dry_run(**overrides: str) -> list[str]:
    env = {
        **os.environ,
        "HARBOR_DRY_RUN": "1",
        "IAM_INSTANCE_PROFILE": INSTANCE_PROFILE,
        "JOB_NAME": "test-job",
        "OUTPUT_DIR": "jobs",
        **overrides,
    }
    result = subprocess.run(
        [sys.executable, str(RUNNER)],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _option_values(argv: list[str], option: str) -> list[str]:
    return [argv[index + 1] for index, arg in enumerate(argv[:-1]) if arg == option]


def _agent_env(argv: list[str]) -> dict[str, str]:
    return dict(value.split("=", 1) for value in _option_values(argv, "--ae"))


def _matrix_plan(*args: str, **overrides: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "MATRIX_DRY_RUN": "1",
        "HARBOR_STRANDS_CHECKOUT": "/nonexistent",
        **overrides,
    }
    return subprocess.run(
        ["bash", str(MATRIX_LAUNCHER), *args],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def _benchmark_plan(
    agent: str,
    model: str,
    **overrides: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            str(BENCHMARK_LAUNCHER),
            agent,
            model,
            "strands-harness-benchmark-index",
            "1",
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "BENCHMARK_DRY_RUN": "1", **overrides},
        check=False,
        capture_output=True,
        text=True,
    )


def _full_suite_plan(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(FULL_SUITE_LAUNCHER), *args],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "FULL_SUITE_DRY_RUN": "1",
            "HARBOR_STRANDS_CHECKOUT": "/nonexistent",
        },
        check=False,
        capture_output=True,
        text=True,
    )


def test_claude_code_uses_native_adapter_and_pins_bedrock_models() -> None:
    model_id = "us.anthropic.claude-sonnet-4-6"
    argv = _dry_run(
        HARBOR_AGENT="claude-code",
        HARBOR_MODEL_NAME=model_id,
        AGENT_VERSION="2.1.220",
    )

    assert _option_values(argv, "-a") == ["claude-code"]
    assert _option_values(argv, "-m") == [model_id]
    assert "version=2.1.220" in _option_values(argv, "--ak")

    agent_env = _agent_env(argv)
    assert agent_env["AWS_REGION"] == "us-east-1"
    assert agent_env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    for key in (
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "CLAUDE_CODE_SUBAGENT_MODEL",
    ):
        assert agent_env[key] == model_id
    assert not any("API_KEY" in key for key in agent_env)


def test_opencode_uses_native_adapter_and_bedrock_provider() -> None:
    model_name = "amazon-bedrock/us.anthropic.claude-sonnet-4-6"
    argv = _dry_run(
        HARBOR_AGENT="opencode",
        HARBOR_MODEL_NAME=model_name,
        AGENT_VERSION="1.18.9",
    )

    assert _option_values(argv, "-a") == ["opencode"]
    assert _option_values(argv, "-m") == [model_name]
    kwargs = _option_values(argv, "--ak")
    assert "version=1.18.9" in kwargs
    assert f'opencode_config={{"small_model":"{model_name}"}}' in kwargs
    assert _agent_env(argv) == {
        "AWS_REGION": "us-east-1",
        "AWS_BEARER_TOKEN_BEDROCK": "${AWS_BEARER_TOKEN_BEDROCK}",
    }


def test_opencode_uses_mantle_for_openai_compatible_models() -> None:
    model_name = "openai/openai.gpt-5.6-sol"
    base_url = "https://bedrock-mantle.us-east-1.api.aws/openai/v1"
    argv = _dry_run(
        HARBOR_AGENT="opencode",
        HARBOR_MODEL_NAME=model_name,
        AGENT_VERSION="1.18.9",
        OPENCODE_OPENAI_BASE_URL=base_url,
    )

    assert _option_values(argv, "-m") == [model_name]
    assert (
        f'opencode_config={{"small_model":"{model_name}",'
        f'"provider":{{"openai":{{"options":{{"baseURL":"{base_url}"}}}}}}}}' in _option_values(argv, "--ak")
    )
    assert _agent_env(argv) == {
        "AWS_REGION": "us-east-1",
        "OPENAI_API_KEY": "${OPENAI_API_KEY}",
        "OPENAI_BASE_URL": "${OPENCODE_OPENAI_BASE_URL}",
    }


def test_opencode_gpt_uses_mantle_openai_route() -> None:
    result = _benchmark_plan("opencode", "openai.gpt-5.6-sol")

    assert result.returncode == 0, result.stderr
    assert "Harbor model: openai/openai.gpt-5.6-sol" in result.stdout
    assert "Mantle URL:  https://bedrock-mantle.us-east-1.api.aws/openai/v1" in result.stdout


def test_opencode_glm_uses_bare_model_id_on_mantle_compatible_route() -> None:
    result = _benchmark_plan("opencode", "openai.zai.glm-5")

    assert result.returncode == 0, result.stderr
    assert "Harbor model: openai/zai.glm-5" in result.stdout
    assert "Mantle URL:  https://bedrock-mantle.us-east-1.api.aws/v1" in result.stdout


def test_custom_agent_keeps_strands_adapter_arguments() -> None:
    agent_path = "strands-infra-runner/agents/stan"
    argv = _dry_run(
        HARBOR_AGENT="strands_evals.benchmarks.harbor.installed.py:StrandsInstalledPyAgent",
        AGENT_NAME="stan",
        AGENT_PATH=agent_path,
        AGENT_MODULE="agent:MyAgent",
        STRANDS_MODEL="us.anthropic.claude-sonnet-4-6",
    )

    assert _option_values(argv, "-a") == ["strands_evals.benchmarks.harbor.installed.py:StrandsInstalledPyAgent"]
    kwargs = _option_values(argv, "--ak")
    assert "display_name=stan" in kwargs
    assert f"agent_path={agent_path}" in kwargs
    assert "agent_module=agent:MyAgent" in kwargs
    assert _agent_env(argv)["STRANDS_MODEL"] == "us.anthropic.claude-sonnet-4-6"


def test_native_index_run_passes_local_dataset_plugin_and_attempt_count() -> None:
    argv = _dry_run(
        HARBOR_AGENT="claude-code",
        HARBOR_MODEL_NAME="us.anthropic.claude-sonnet-4-6",
        DATASET_PATH="/tmp/strands-harness-benchmark-index",
        JOB_PLUGIN="metric_plugin:StrandsHarnessBenchmarkMetricPlugin",
        N_ATTEMPTS="2",
        CONCURRENCY="412",
    )

    assert _option_values(argv, "-p") == ["/tmp/strands-harness-benchmark-index"]
    assert _option_values(argv, "--plugin") == ["metric_plugin:StrandsHarnessBenchmarkMetricPlugin"]
    assert _option_values(argv, "-k") == ["2"]
    assert _option_values(argv, "-n") == ["412"]
    assert "-d" not in argv


def test_ec2_launches_are_rate_limited() -> None:
    argv = _dry_run(
        HARBOR_AGENT="claude-code",
        HARBOR_MODEL_NAME="us.anthropic.claude-sonnet-4-6",
    )

    environment_kwargs = _option_values(argv, "--ek")
    assert "launch_rate_per_sec=1.5" in environment_kwargs
    assert "launch_burst=3" in environment_kwargs


def test_native_matrix_creates_independent_agent_cells() -> None:
    result = _matrix_plan(
        "-a",
        "claude-code,opencode",
        "-m",
        "sonnet-4.6",
        "-k",
        "2",
    )

    assert result.returncode == 0, result.stderr
    assert (
        "matrix_id=strands-harness-benchmark-index--agents-claude-code@2.1.220+opencode@1.18.9--models-sonnet-4.6--harbor-strands--k2"
    ) in result.stdout
    assert "cell=claude-code\tsonnet-4.6\t412" in result.stdout
    assert "cell=opencode\tsonnet-4.6\t412" in result.stdout


def test_native_matrix_interleaves_agents_by_model() -> None:
    result = _matrix_plan(
        "-a",
        "claude-code,opencode",
        "-m",
        "opus-4.8,sonnet-5",
    )

    assert result.returncode == 0, result.stderr
    cells = [
        tuple(line.removeprefix("cell=").split("\t")[:2])
        for line in result.stdout.splitlines()
        if line.startswith("cell=")
    ]
    assert cells == [
        ("claude-code", "opus-4.8"),
        ("opencode", "opus-4.8"),
        ("claude-code", "sonnet-5"),
        ("opencode", "sonnet-5"),
    ]


def test_native_matrix_pins_harbor_commit_in_run_identity() -> None:
    harbor_sha = "1234567890abcdef1234567890abcdef12345678"
    result = _matrix_plan(
        "-a",
        "claude-code,opencode",
        "-m",
        "sonnet-4.6",
        HARBOR_REF=harbor_sha,
    )

    assert result.returncode == 0, result.stderr
    assert "--harbor-1234567--k2" in result.stdout
    assert f"harbor_ref={harbor_sha}" in result.stdout


def test_native_matrix_includes_run_group_in_state_and_job_identity() -> None:
    result = _matrix_plan(
        "-a",
        "claude-code,opencode",
        "-m",
        "sonnet-4.6",
        HARBOR_REF="1234567890abcdef1234567890abcdef12345678",
        RUN_GROUP="strands-full-native-20260803",
    )

    assert result.returncode == 0, result.stderr
    assert "run_group=strands-full-native-20260803" in result.stdout
    assert (
        "job_suffix=--run-strands-full-native-20260803--harbor1234567--k2"
        in result.stdout
    )
    assert (
        "matrix_id=strands-harness-benchmark-index"
        "--run-strands-full-native-20260803--agents-"
    ) in result.stdout


def test_native_matrix_defaults_to_full_supported_model_set() -> None:
    result = _matrix_plan("-a", "claude-code,opencode")

    assert result.returncode == 0, result.stderr
    planned = {
        tuple(line.removeprefix("cell=").split("\t")[:2])
        for line in result.stdout.splitlines()
        if line.startswith("cell=")
    }
    assert planned == {
        ("claude-code", "sonnet-4.6"),
        ("claude-code", "opus-4.8"),
        ("claude-code", "sonnet-5"),
        ("opencode", "sonnet-4.6"),
        ("opencode", "opus-4.8"),
        ("opencode", "openai.gpt-5.6-sol"),
        ("opencode", "sonnet-5"),
        ("opencode", "openai.zai.glm-5"),
        ("opencode", "kimi-k2.5"),
    }
    assert result.stdout.count("unsupported=claude-code/") == 3


def test_full_native_suite_plans_all_sources_and_trials() -> None:
    result = _full_suite_plan()

    assert result.returncode == 0, result.stderr
    assert "source=tb21\tdataset=terminal-bench/terminal-bench-2-1\ttasks=89\ttrials_per_cell=178" in result.stdout
    assert "source=gaia\tdataset=gaia/gaia\ttasks=165\ttrials_per_cell=330" in result.stdout
    assert "source=tau3\tdataset=sierra-research/tau3-bench\ttasks=375\ttrials_per_cell=750" in result.stdout
    assert "source=swe-bench-pro\tdataset=scale-ai/swe-bench-pro\ttasks=731\ttrials_per_cell=1462" in result.stdout
    assert "total_tasks=1360" in result.stdout
    assert "total_cells=24" in result.stdout
    assert "total_trials=16320" in result.stdout
    assert "run_group=strands-full-native-20260803" in result.stdout
    assert "--run-strands-full-native-20260803--agents-" in result.stdout
    assert "harbor_ref=strands-working-fork" in result.stdout
    assert "parallel_models=1" in result.stdout
    assert "model_stagger_seconds=420" in result.stdout
    assert "parallel_fleet_vcpu=9216" in result.stdout
    assert "lane=opus-4.8\tstart_delay_seconds=0" in result.stdout
    assert "lane=sonnet-5\tstart_delay_seconds=420" in result.stdout
    assert "lane=sonnet-4.6\tstart_delay_seconds=840" in result.stdout

    cells = [
        tuple(line.removeprefix("cell=").split("\t")[:4])
        for line in result.stdout.splitlines()
        if line.startswith("cell=")
    ]
    assert [cell[1] for cell in cells] == ["claude-code", "opencode"] * 12
    assert cells[:6] == [
        ("tb21", "claude-code", "opus-4.8", "178"),
        ("tb21", "opencode", "opus-4.8", "178"),
        ("tb21", "claude-code", "sonnet-5", "178"),
        ("tb21", "opencode", "sonnet-5", "178"),
        ("tb21", "claude-code", "sonnet-4.6", "178"),
        ("tb21", "opencode", "sonnet-4.6", "178"),
    ]
    assert {cell[3] for cell in cells if cell[0] == "gaia"} == {"330"}
    assert {cell[3] for cell in cells if cell[0] in {"tau3", "swe-bench-pro"}} == {"500"}


def test_full_native_suite_runs_one_lane_per_source_and_model(
    tmp_path: Path,
) -> None:
    invocation_log = tmp_path / "matrix-invocations.log"
    fake_matrix = tmp_path / "fake-matrix.sh"
    fake_matrix.write_text(
        '#!/bin/bash\nprintf "%s\\n" "$*" >>"$MATRIX_INVOCATION_LOG"\n'
    )
    fake_matrix.chmod(0o755)

    result = subprocess.run(
        ["bash", str(FULL_SUITE_LAUNCHER)],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "FULL_SUITE_STATE_ROOT": str(tmp_path / "state"),
            "HARBOR_REF": "1234567890abcdef1234567890abcdef12345678",
            "MATRIX_INVOCATION_LOG": str(invocation_log),
            "MATRIX_RUNNER": str(fake_matrix),
            "MODEL_STAGGER_SECONDS": "0",
            "ORCHESTRATOR_EVALS_DIR": str(tmp_path / "runtime"),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    invocations = [shlex.split(line) for line in invocation_log.read_text().splitlines()]
    assert len(invocations) == 12
    observed = {
        (args[args.index("-d") + 1], args[args.index("-m") + 1])
        for args in invocations
    }
    assert observed == {
        (dataset, model)
        for dataset in (
            "terminal-bench/terminal-bench-2-1",
            "gaia/gaia",
            "sierra-research/tau3-bench",
            "scale-ai/swe-bench-pro",
        )
        for model in ("opus-4.8", "sonnet-5", "sonnet-4.6")
    }

    suite_state = next((tmp_path / "state").iterdir())
    assert len(list((suite_state / "sources").glob("*.COMPLETE"))) == 4
    assert (suite_state / "COMPLETE").is_file()


def test_native_matrix_rejects_incompatible_claude_code_model() -> None:
    result = _matrix_plan(
        "-a",
        "claude-code",
        "-m",
        "openai.gpt-5.6-sol",
    )

    assert result.returncode == 2
    assert "no compatible agent/model cells" in result.stderr
    assert "Claude Code requires the Anthropic Claude protocol" in result.stderr


def test_benchmark_launcher_derives_attempt_suffix() -> None:
    result = subprocess.run(
        [
            "bash",
            str(BENCHMARK_LAUNCHER),
            "claude-code",
            "sonnet-4.6",
            "strands-harness-benchmark-index",
            "412",
        ],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "BENCHMARK_DRY_RUN": "1",
            "N_ATTEMPTS": "2",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert ("jobs/claude-code@2.1.220--sonnet-4.6--strands-harness-benchmark-index--k2") in result.stdout
    assert "Attempts:    2" in result.stdout


def test_benchmark_launcher_separates_code_and_runtime_roots() -> None:
    result = _benchmark_plan(
        "claude-code",
        "sonnet-4.6",
        ORCHESTRATOR_EVALS_DIR="/home/ubuntu/evals",
    )

    assert result.returncode == 0, result.stderr
    assert f"Code root:   {REPO_ROOT}" in result.stdout
    assert "Runtime root: /home/ubuntu/evals" in result.stdout
