from __future__ import annotations

from importlib import metadata


def get_version() -> str:
    """Return the installed package version.

    This uses importlib.metadata and will raise `PackageNotFoundError` if
    the package is not installed. Callers must handle that if they expect
    the code to work in a non-installed developer tree.
    """
    return metadata.version("stable-write")
