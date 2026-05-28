from __future__ import annotations

from stable_write._version import get_version
from stable_write.registry import get_profile, list_profiles, register_profile
from stable_write.stablewrite import Saver, SaveResult, save_if_changed, save_xlsx_if_changed

__version__ = get_version()
