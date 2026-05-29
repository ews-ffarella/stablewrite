from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence


@dataclass
class Profile:
    finalizers: Sequence[Callable[[Path], None]] = ()
    is_equal: Optional[Callable[[Path, Path], bool]] = None


# Internal global registry
_REGISTRY: Dict[str, Profile] = {}


def register_profile(
    name: str,
    finalizers: Sequence[Callable[[Path], None]] = (),
    is_equal: Optional[Callable[[Path, Path], bool]] = None,
    force: bool = False,
) -> None:
    """Register a named profile.

    Args:
        name: Profile identifier used in ``save_if_changed(profile=...)``.
        finalizers: Ordered callables applied to the temp file before hashing.
        is_equal: Optional comparator ``(new, existing) -> bool`` used instead
            of byte-hash comparison when a profile is active and no explicit
            ``is_equal`` is passed to ``save_if_changed``.
        force: When ``True``, overwrite an existing registration silently.

    Raises:
        ValueError: If *name* is already registered and *force* is ``False``.
    """
    if name in _REGISTRY and not force:
        raise ValueError(f"Profile '{name}' is already registered.")
    _REGISTRY[name] = Profile(finalizers=finalizers, is_equal=is_equal)


def get_profile(name: str) -> Profile:
    """Return the :class:`Profile` registered under *name*.

    Raises:
        ValueError: If *name* has not been registered.
    """
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ValueError(f"Unknown profile '{name}'. Available: {sorted(_REGISTRY)}") from None


def list_profiles() -> List[str]:
    """Return a sorted list of all registered profile names."""
    return sorted(_REGISTRY)
