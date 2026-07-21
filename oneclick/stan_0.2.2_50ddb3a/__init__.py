"""Stan's default factory agent packaged for OneClick's DockerAgentBridge."""

from pathlib import Path

_PACKAGE_DIR = Path(__file__).parent
RELEASE_ID = (_PACKAGE_DIR / "RELEASE_ID").read_text().strip()
STAN_COMMIT = (_PACKAGE_DIR / "STAN_COMMIT").read_text().strip()
__version__ = RELEASE_ID.removeprefix("v").split("@", 1)[0]

if RELEASE_ID != f"v{__version__}@{STAN_COMMIT[:7]}":
    raise RuntimeError("OneClick release metadata is inconsistent")
