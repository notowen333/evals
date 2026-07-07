"""External Strands agent — LLM on host, tools route to container via build()."""

from .agent import StrandsAgent
from .tools import TOOLS, bash, read_file, submit, write_file

__all__ = ["StrandsAgent", "TOOLS", "bash", "read_file", "write_file", "submit"]
