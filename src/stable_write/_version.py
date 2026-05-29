try:
    from importlib.metadata import PackageNotFoundError, version
except ImportError:  # Python < 3.8
    from importlib_metadata import PackageNotFoundError, version  # type: ignore[no-redef]


def get_version() -> str:
    """Return the installed package version, or 'unknown' if not installed."""
    try:
        return version("stable-write")
    except PackageNotFoundError:
        return "unknown"
