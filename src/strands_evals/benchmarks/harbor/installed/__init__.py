"""Installed Strands agents — run user agents unchanged inside Harbor containers."""

from .py import StrandsInstalledPyAgent
from .ts import StrandsInstalledTSAgent

__all__ = ["StrandsInstalledPyAgent", "StrandsInstalledTSAgent"]
