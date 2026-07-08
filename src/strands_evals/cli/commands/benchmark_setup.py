"""`strands-evals benchmark setup` — verify prerequisites for running benchmarks.

Checks harbor installation, authentication, and Docker availability.
Guides the user through fixing anything that's missing.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def _check_harbor() -> bool:
    """Check if harbor CLI is installed and reachable."""
    bin_dir = Path(sys.executable).parent
    local = bin_dir / "harbor"
    if local.exists():
        return True
    return shutil.which("harbor") is not None


def _check_harbor_auth() -> bool:
    """Check if harbor is authenticated."""
    bin_dir = Path(sys.executable).parent
    harbor = str(bin_dir / "harbor") if (bin_dir / "harbor").exists() else "harbor"
    try:
        result = subprocess.run(
            [harbor, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return "Not authenticated" not in result.stdout
    except Exception:
        return False


def _check_docker() -> bool:
    """Check if Docker daemon is running."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def _run(args: argparse.Namespace) -> int:
    print("strands-evals benchmark setup")  # noqa: T201
    print("=" * 40)  # noqa: T201
    print()  # noqa: T201

    all_good = True

    # 1. Harbor installed
    if _check_harbor():
        print("✓ harbor CLI installed")  # noqa: T201
    else:
        print("✗ harbor CLI not found")  # noqa: T201
        print("  Fix: pip install harbor")  # noqa: T201
        all_good = False

    # 2. Harbor auth
    if not _check_harbor():
        print("  (skipping auth check — harbor not installed)")  # noqa: T201
    elif _check_harbor_auth():
        print("✓ harbor authenticated")  # noqa: T201
    else:
        print("✗ harbor not authenticated")  # noqa: T201
        print("  Fix: harbor auth login")  # noqa: T201
        all_good = False

    # 3. Docker
    if _check_docker():
        print("✓ Docker running")  # noqa: T201
    else:
        print("✗ Docker not running")  # noqa: T201
        print("  Fix: start Docker Desktop or run `colima start`")  # noqa: T201
        all_good = False

    print()  # noqa: T201
    if all_good:
        print("Ready to run benchmarks.")  # noqa: T201
    else:
        print("Fix the issues above, then run this again.")  # noqa: T201
        return 1

    return 0


def add_subparser(
    subparsers: argparse._SubParsersAction,
    parent: argparse.ArgumentParser,
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "benchmark-setup",
        parents=[parent],
        help="check prerequisites for running benchmarks (harbor, auth, Docker)",
    )
    parser.set_defaults(func=_run)
    return parser
