"""Tests for stable_write.registry.

Covers:
  - Profile dataclass defaults
  - register_profile: basic registration, duplicate rejection, force overwrite
  - get_profile: happy path and unknown profile
  - list_profiles: ordering, reflects live registry state
  - Built-in profiles registered by stablewrite module
"""

from pathlib import Path

import pytest

# Importing from stable_write (the package) triggers stablewrite.py which
# registers built-in profiles.  Import registry symbols directly for unit tests.
import stable_write  # noqa: F401 — side-effect: registers built-ins
from stable_write.registry import _REGISTRY, Profile, get_profile, list_profiles, register_profile

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _noop(p: Path) -> None:
    """Dummy finalizer."""


def _always_equal(new: Path, existing: Path) -> bool:
    return True


# ---------------------------------------------------------------------------
# Profile dataclass
# ---------------------------------------------------------------------------


class TestProfile:
    def test_defaults(self):
        p = Profile()
        assert p.finalizers == ()
        assert p.is_equal is None

    def test_with_finalizers(self):
        p = Profile(finalizers=[_noop])
        assert list(p.finalizers) == [_noop]

    def test_with_is_equal(self):
        p = Profile(is_equal=_always_equal)
        assert p.is_equal is _always_equal


# ---------------------------------------------------------------------------
# register_profile
# ---------------------------------------------------------------------------


class TestRegisterProfile:
    def test_registers_new_profile(self, isolated_registry):
        register_profile("my_fmt", finalizers=[_noop])
        assert "my_fmt" in _REGISTRY

    def test_registered_profile_has_correct_finalizers(self, isolated_registry):
        register_profile("my_fmt", finalizers=[_noop])
        assert list(_REGISTRY["my_fmt"].finalizers) == [_noop]

    def test_registered_profile_has_correct_is_equal(self, isolated_registry):
        register_profile("my_fmt", is_equal=_always_equal)
        assert _REGISTRY["my_fmt"].is_equal is _always_equal

    def test_empty_finalizers_is_valid(self, isolated_registry):
        register_profile("empty_fmt")
        assert list(_REGISTRY["empty_fmt"].finalizers) == []

    def test_duplicate_raises_value_error(self, isolated_registry):
        register_profile("dup_fmt")
        with pytest.raises(ValueError, match="already registered"):
            register_profile("dup_fmt")

    def test_duplicate_message_contains_name(self, isolated_registry):
        register_profile("dup_fmt")
        with pytest.raises(ValueError, match="dup_fmt"):
            register_profile("dup_fmt")

    def test_force_overwrites_existing(self, isolated_registry):
        register_profile("overwrite_fmt", finalizers=[_noop])
        register_profile("overwrite_fmt", finalizers=[], force=True)
        assert list(_REGISTRY["overwrite_fmt"].finalizers) == []

    def test_force_false_by_default(self, isolated_registry):
        register_profile("once_fmt")
        with pytest.raises(ValueError):
            register_profile("once_fmt")  # force defaults to False

    def test_multiple_finalizers(self, isolated_registry):
        def fn1(p: Path) -> None: ...
        def fn2(p: Path) -> None: ...

        register_profile("multi_fmt", finalizers=[fn1, fn2])
        assert list(_REGISTRY["multi_fmt"].finalizers) == [fn1, fn2]


# ---------------------------------------------------------------------------
# get_profile
# ---------------------------------------------------------------------------


class TestGetProfile:
    def test_returns_registered_profile(self, isolated_registry):
        register_profile("lookup_fmt", finalizers=[_noop])
        p = get_profile("lookup_fmt")
        assert isinstance(p, Profile)
        assert list(p.finalizers) == [_noop]

    def test_unknown_raises_value_error(self, isolated_registry):
        with pytest.raises(ValueError, match="Unknown profile"):
            get_profile("nonexistent_xyz")

    def test_error_message_contains_name(self, isolated_registry):
        with pytest.raises(ValueError, match="nonexistent_xyz"):
            get_profile("nonexistent_xyz")

    def test_error_message_shows_available(self, isolated_registry):
        register_profile("available_fmt")
        with pytest.raises(ValueError, match="available_fmt"):
            get_profile("nonexistent_xyz")


# ---------------------------------------------------------------------------
# list_profiles
# ---------------------------------------------------------------------------


class TestListProfiles:
    def test_returns_sorted_list(self, isolated_registry):
        register_profile("zzz")
        register_profile("aaa")
        register_profile("mmm")
        names = list_profiles()
        # Result must be sorted and contain the newly registered names
        assert names == sorted(names)
        assert {"aaa", "mmm", "zzz"}.issubset(names)

    def test_reflects_new_registration(self, isolated_registry):
        before = set(list_profiles())
        register_profile("brand_new")
        after = set(list_profiles())
        assert "brand_new" in after - before

    def test_returns_list(self, isolated_registry):
        assert isinstance(list_profiles(), list)


# ---------------------------------------------------------------------------
# Built-in profiles (registered at stablewrite import time)
# ---------------------------------------------------------------------------


class TestBuiltinProfiles:
    @pytest.mark.parametrize("name", ["zip", "xlsx", "docx", "pptx"])
    def test_builtin_profile_exists(self, name):
        assert name in list_profiles()

    @pytest.mark.parametrize("name", ["zip", "xlsx", "docx", "pptx"])
    def test_builtin_profile_has_finalizers(self, name):
        p = get_profile(name)
        assert len(p.finalizers) > 0

    def test_xlsx_uses_normalize_zip(self):
        from stable_write.finalizers import normalize_zip_metadata

        p = get_profile("xlsx")
        assert normalize_zip_metadata in p.finalizers

    def test_zip_finalizer_is_normalize_zip_only(self):
        from stable_write.finalizers import normalize_zip_metadata

        p = get_profile("zip")
        assert list(p.finalizers) == [normalize_zip_metadata]


# ---------------------------------------------------------------------------
# Fixture: isolated registry that restores state after each test
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_registry():
    """Snapshot the registry before the test and restore it afterwards.

    This lets register_profile tests run without polluting the shared
    global ``_REGISTRY`` that backs the built-in profiles.
    """
    snapshot = dict(_REGISTRY)
    yield
    _REGISTRY.clear()
    _REGISTRY.update(snapshot)
