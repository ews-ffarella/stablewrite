from __future__ import annotations

from importlib import metadata


def get_version() -> str:
    """Return the installed package version, or 'unknown' if not installed."""
    try:
        return metadata.version("stable-write")
    except metadata.PackageNotFoundError:
        return "unknown"
