"""Post-benchmark S3 upload hook for the TypeScript agent."""

import os


S3_BUCKET = os.environ.get("BENCHMARK_S3_BUCKET", "my-benchmark-results")


class Hooks:
    def on_benchmark_complete(self, job_dir, results):
        import boto3
        from pathlib import Path

        s3 = boto3.client("s3")
        for file_path in Path(job_dir).rglob("*"):
            if file_path.is_file():
                key = f"{job_dir.name}/{file_path.relative_to(job_dir)}"
                s3.upload_file(str(file_path), S3_BUCKET, key)
