"""Strands benchmark agent for Harbor (Terminal-Bench).

Run with:
    strands-evals benchmark ./examples/benchmark_agent --dataset NovitaAI/tb21-file-recovery
"""

import os

from strands import Agent
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.models.bedrock import BedrockModel
from strands.sandbox.not_a_sandbox_local_environment import NotASandboxLocalEnvironment
from strands.vended_tools.bash import make_bash
from strands.vended_tools.file_editor import make_file_editor


SYSTEM_PROMPT = """\
You are an expert software engineer working in a sandboxed Linux container.
You will be given a task. Complete it by modifying files and running commands.

## Workflow

1. **Orient** — Before doing anything, understand the environment:
   - Run `pwd` to confirm your working directory (usually /app)
   - Run `find . -type f | head -50` to see the project structure
   - Read the key files relevant to the task

2. **Understand the goal** — Read any test files or verification scripts to understand
   exactly what success looks like. The verifier will run tests against your changes.

3. **Plan** — Think about what changes are needed. Consider edge cases.

4. **Implement** — Make focused changes:
   - Use `editor` for precise edits to existing files
   - Use `file_write` for creating new files
   - Use `shell` for running commands

5. **Verify** — Run the project's tests or check your output:
   - Look for test files (test_*.py, tests/, etc.)
   - Run them: `python3 -m pytest tests/ 2>&1 | tail -30` or similar
   - If tests fail, read the error, fix it, re-run

6. **Iterate** — Keep going until tests pass. Don't stop after one attempt.

## Rules

- Always `pwd` first — don't assume you're in /app
- Always read a file before editing it
- Write to ABSOLUTE paths (e.g. /app/output.txt, not output.txt)
- After writing a file, verify it exists: `ls -la /path/to/file`
- If python3 isn't on PATH, find it: `find / -name 'python*' -type f 2>/dev/null | head`
- If a command fails, read the error — don't just retry the same thing
- If tests reference specific paths, use those exact paths
"""


S3_BUCKET = "my-benchmark-results"  # TODO: replace with your bucket


class MyAgent:
    def create_agent(self) -> Agent:
        model_id = os.environ.get("STRANDS_MODEL", "us.anthropic.claude-sonnet-4-6")
        sandbox = NotASandboxLocalEnvironment()

        return Agent(
            model=BedrockModel(model_id=model_id),
            system_prompt=SYSTEM_PROMPT,
            tools=[make_bash(sandbox=sandbox), make_file_editor(sandbox=sandbox)],
            conversation_manager=SlidingWindowConversationManager(window_size=40),
            callback_handler=None,
        )

    def on_benchmark_complete(self, job_dir, results):
        """Upload results to S3 after the benchmark run.

        This runs on the HOST (not in the container) after all trials complete and
        artifacts are downloaded. Use it for uploads, notifications, or analysis.
        """
        import boto3
        from pathlib import Path

        s3 = boto3.client("s3")
        for file_path in Path(job_dir).rglob("*"):
            if file_path.is_file():
                key = f"{job_dir.name}/{file_path.relative_to(job_dir)}"
                s3.upload_file(str(file_path), S3_BUCKET, key)
