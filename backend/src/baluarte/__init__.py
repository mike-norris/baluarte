"""Baluarte: governance and audit evidence for self-managed GitLab."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("baluarte")
except PackageNotFoundError:  # pragma: no cover - only when running from an uninstalled tree
    __version__ = "0.0.0"
