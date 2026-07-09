"""Installed Strands agents — run user agents unchanged inside Harbor containers."""

from .py import StrandsInstalledAgent
from .ts import StrandsInstalledTSAgent

__all__ = ["StrandsInstalledAgent", "StrandsInstalledTSAgent"]
