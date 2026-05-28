"""Deterministic, staged, save-only-if-modified file writer.

Core pipeline::

    user writes to isolated system temp file
          ↓
    finalizers normalize file   (before hashing — critical)
          ↓
    hash / compare
          ↓
    if unchanged → discard temp, leave destination untouched
    if changed   → copy to destination-side temp
                   → os.replace onto destination  (atomic within dest filesystem)

All intermediate work happens in an isolated :class:`~tempfile.TemporaryDirectory`
away from the destination tree, so finalizer failures leave the destination
completely untouched.  The final publish step uses a destination-adjacent
temporary file so the final ``os.replace`` is always same-filesystem, making
it atomic even when staging lives on a different filesystem (e.g. system
``/tmp`` vs. a network mount or Docker volume).

.. note::
   For bundles (main file + companion files), each individual file is published
   atomically.  The bundle as a whole is **not** transactional — a crash between
   companion publishes will leave a partially updated bundle.

Typical usage::

    with save_if_changed("report.xlsx", profile="xlsx") as saver:
        df.to_excel(saver.path, index=False)

    if saver.changed:
        print("report updated")
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from logging import getLogger
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Literal

from stable_write.finalizers import normalize_zip_metadata, strip_ooxml_metadata
from stable_write.registry import get_profile, register_profile

logger = getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal publish helper
# ---------------------------------------------------------------------------


def _publish_file(src: Path, dst: Path, *, copy_fn: Callable[[str, str], str]) -> None:
    """Copy *src* to a destination-side temp file then ``os.replace`` onto *dst*.

    This ensures the final replacement is always an ``os.replace`` within the
    destination filesystem — atomic on POSIX, best-effort on Windows — even
    when *src* lives on a different filesystem (e.g. system ``/tmp``).

    Args:
        src: Source path (typically in a staging temp directory).
        dst: Final destination path.  Parent directory must already exist.
        copy_fn: Low-level copy callable, e.g. ``shutil.copy2`` or
            ``shutil.copyfile``.
    """
    with NamedTemporaryFile(
        delete=False,
        prefix=f".{dst.name}.",
        suffix=".tmp",
        dir=str(dst.parent),
    ) as tmp:
        publish_tmp = Path(tmp.name)

    try:
        copy_fn(str(src), str(publish_tmp))
        os.replace(str(publish_tmp), str(dst))  # noqa: PTH105
    finally:
        with contextlib.suppress(FileNotFoundError):
            publish_tmp.unlink()


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class SaveResult:
    """Outcome of a :func:`save_if_changed` operation.

    Attributes:
        destination: Final destination path.
        changed: ``True`` if generated output differed from the existing file.
        saved: ``True`` if the destination was actually replaced.
        old_hash: Hash of the pre-existing destination, or ``None`` if it did
            not exist.
        new_hash: Hash of the finalized temporary file.
        hash_algo: Algorithm used to produce the hashes.
        reason: Human-readable explanation of the decision.
        changed_companions: Names of companion files whose content changed (or
            were new).  Empty when no companions changed.
    """

    destination: Path
    changed: bool | None = None
    saved: bool | None = None
    old_hash: str | None = None
    new_hash: str | None = None
    hash_algo: str = "blake2b"
    reason: str = ""
    changed_companions: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Saver object (yielded by the context manager)
# ---------------------------------------------------------------------------


@dataclass
class Saver:
    """Object yielded inside the :func:`save_if_changed` context.

    After the ``with`` block exits, all result attributes are populated.

    Attributes:
        path: Temporary file path — **write your output here**.
        temp_dir: Temporary directory that contains *path* and any companion
            files.
        destination: Resolved destination path.
        result: :class:`SaveResult` populated after context exit.
    """

    destination: Path
    path: Path
    temp_dir: Path
    result: SaveResult = field(default_factory=lambda: SaveResult(destination=Path()))

    # Convenience pass-throughs so callers can do ``saver.changed`` directly.

    @property
    def changed(self) -> bool | None:
        return self.result.changed

    @property
    def saved(self) -> bool | None:
        return self.result.saved

    @property
    def old_hash(self) -> str | None:
        return self.result.old_hash

    @property
    def new_hash(self) -> str | None:
        return self.result.new_hash

    @property
    def hash_algo(self) -> str:
        return self.result.hash_algo

    @property
    def reason(self) -> str:
        return self.result.reason

    @property
    def changed_companions(self) -> list[str]:
        return self.result.changed_companions

    def __repr__(self) -> str:
        status = "💾 OVERWRITTEN" if self.saved else f"⏭️ SKIPPED ({self.reason})"
        companions = (
            f" [+ {len(self.changed_companions)} companions]" if self.changed_companions else ""
        )
        return f"<SaveResult: {status} | {self.destination.name}{companions}>"


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def file_hash(path: Path, algo: str = "blake2b", block_size: int = 8192) -> str:
    """Compute the hash of a file.

    Args:
        path: Path to the file to hash.
        algo: :mod:`hashlib` algorithm name. Default is ``"blake2b"``.
        block_size: Read chunk size in bytes. Default is ``8192``.

    Returns:
        Hexadecimal digest string.

    Raises:
        ValueError: If *algo* is not available or *block_size* is not positive.
    """
    if algo not in hashlib.algorithms_available:
        raise ValueError(
            f"Hash algorithm '{algo}' is not available. Available: {hashlib.algorithms_available}"
        )
    if block_size <= 0:
        raise ValueError("block_size must be a positive integer")

    h = hashlib.new(algo)
    with path.open("rb") as f:
        while chunk := f.read(block_size):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Built-in profile registration
# ---------------------------------------------------------------------------

_BUILTIN_PROFILES: dict[str, list[Callable[[Path], None]]] = {
    "zip": [normalize_zip_metadata],
    # xlsx/docx/pptx: patch docProps/core.xml in the ZIP (no openpyxl round-trip)
    # then normalise all ZIP entry metadata for a fully deterministic byte output.
    "xlsx": [strip_ooxml_metadata, normalize_zip_metadata],
    "docx": [strip_ooxml_metadata, normalize_zip_metadata],
    "pptx": [strip_ooxml_metadata, normalize_zip_metadata],
}

for _name, _fns in _BUILTIN_PROFILES.items():
    register_profile(_name, finalizers=_fns, force=True)

del _name, _fns


# ---------------------------------------------------------------------------
# Companion validation
# ---------------------------------------------------------------------------


def _validate_companion_name(name: str) -> None:
    """Raise ``ValueError`` if *name* is not a safe plain filename.

    A companion name must be a bare filename (no directory components, no
    absolute path).  This prevents callers from accidentally escaping the
    destination directory via ``../`` or similar tricks.

    Args:
        name: The companion filename to validate.

    Raises:
        ValueError: If *name* is absolute or contains directory separators.
    """
    p = Path(name)
    if p.is_absolute() or p.name != name:
        raise ValueError(
            f"Invalid companion filename {name!r}: must be a plain filename "
            "with no directory components or path separators."
        )


# ---------------------------------------------------------------------------
# Core context manager
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def save_if_changed(
    path: str | Path,
    *,
    profile: str | None = None,
    finalizers: list[Callable[[Path], None]] | None = None,
    save_strategy: Literal["overwrite", "skip", "raise"] = "overwrite",
    algo: str = "blake2b",
    block_size: int = 8192,
    safe_copy: bool = False,
    companions: list[str] | None | str = "auto",
    is_equal: Callable[[Path, Path], bool] | None = None,
):
    """Context manager for deterministic, atomic, save-only-if-modified file writing.

    Yields a :class:`Saver` object.  Write your output to ``saver.path``.  On
    exit the temporary file is finalized, hashed, and compared with the
    existing destination.  The destination is replaced only when the finalized
    bytes differ.

    Args:
        path: Destination file path.
        profile: Optional named profile that selects built-in finalizers.
            Supported values: ``"zip"``, ``"xlsx"``, ``"docx"``, ``"pptx"``.
            Ignored when *finalizers* is also provided.
        finalizers: Ordered list of callables ``(Path) -> None`` that mutate
            the temporary file before hashing.  Run after the ``with`` body and
            **before** the hash comparison — this is the critical ordering that
            makes deterministic profiles work.
        save_strategy: What to do when content has **changed**.

            * ``"overwrite"`` (default) — replace the destination silently.
            * ``"raise"`` — raise :exc:`FileExistsError`.
            * ``"skip"`` — silently leave the destination untouched.
        algo: :mod:`hashlib` algorithm for content comparison. Default
            ``"blake2b"``.
        block_size: Read chunk size in bytes for hashing. Default ``8192``.
        safe_copy: When ``True``, use :func:`shutil.copyfile` (no metadata
            preserved) for the final move.  Default ``False`` (:func:`shutil.copy2`).
        companions: Controls which extra files written to the temp directory are
            moved alongside the main artifact.

            * ``"auto"`` (default) — move all extra files found in the temp dir.
            * ``[]`` / ``None`` — move only the main file.
            * ``["foo.csv", "bar.png"]`` — move only the listed filenames.
              Every name in the list **must** be written inside the ``with``
              block; if any is missing a :exc:`FileNotFoundError` is raised and
              nothing is published.  Use ``"auto"`` for optional companions.
        is_equal: Optional callable ``(new: Path, existing: Path) -> bool`` that
            decides whether the new file is *equivalent* to the existing
            destination.  When provided it replaces the byte-hash comparison
            for the **main** file only (companions still use hashes).  Useful
            for formats like GeoPackage whose bytes are non-deterministic but
            whose data content can be compared structurally.  The callable is
            only invoked when the destination already exists; a missing
            destination is always treated as changed regardless.

    Yields:
        Saver: Object with ``.path`` (write here) and result attributes
            (``.changed``, ``.saved``, ``.old_hash``, ``.new_hash``,
            ``.reason``) populated after exit.

    Raises:
        FileExistsError: If content changed and *save_strategy* is ``"raise"``.

    Example::

        with save_if_changed("report.xlsx", profile="xlsx") as saver:
            df.to_excel(saver.path, index=False)

        if saver.changed:
            print(f"Saved (old={saver.old_hash}, new={saver.new_hash})")
        else:
            print("Unchanged — destination untouched")
    """
    destination = Path(path)
    copy_fn = shutil.copyfile if safe_copy else shutil.copy2

    # Resolve finalizer list: explicit > profile > empty
    # If a profile is active and no is_equal was given, inherit the profile's comparator.
    if finalizers is None:
        if profile is not None:
            _profile = get_profile(profile)  # raises ValueError for unknown profiles
            finalizers = list(_profile.finalizers)
            if is_equal is None:
                is_equal = _profile.is_equal
        else:
            finalizers = []

    # Strategy validation
    _VALID_STRATEGIES = frozenset({"overwrite", "skip", "raise"})
    if save_strategy not in _VALID_STRATEGIES:
        raise ValueError(
            f"Unknown save_strategy {save_strategy!r}. Valid values: {sorted(_VALID_STRATEGIES)}"
        )

    # Companion resolution
    if companions is None or companions == []:
        companions_mode: str | list[str] = []
    elif companions == "auto":
        companions_mode = "auto"
    elif isinstance(companions, str):
        raise ValueError("companions must be 'auto', None, or a list of filenames")
    elif not hasattr(companions, "__iter__"):
        raise TypeError(
            f"companions must be 'auto', None, or a list of filenames, "
            f"got {type(companions).__name__!r}"
        )
    else:
        companions_list = list(companions)
        for _name in companions_list:
            if not isinstance(_name, str):
                raise TypeError(
                    f"companion filenames must be strings, got {type(_name).__name__!r}"
                )
            _validate_companion_name(_name)
        companions_mode = companions_list

    with TemporaryDirectory(prefix=".stablewrite-", suffix="") as _tmp:
        temp_dir = Path(_tmp)
        temp_path = temp_dir / destination.name
        # Pre-create as empty so file_hash always has a readable file, even if
        # the caller writes nothing inside the context block.
        temp_path.touch()
        result = SaveResult(destination=destination, hash_algo=algo)
        saver = Saver(destination=destination, path=temp_path, temp_dir=temp_dir, result=result)

        yield saver

        # --- Finalizers run BEFORE hashing (critical) ---
        for fn in finalizers:
            logger.debug("stablewrite: running finalizer %s on %s", fn.__name__, temp_path)
            fn(temp_path)

        result.new_hash = file_hash(temp_path, algo=algo, block_size=block_size)
        result.old_hash = (
            file_hash(destination, algo=algo, block_size=block_size)
            if destination.exists()
            else None
        )

        # --- Companion discovery ---
        if companions_mode == "auto":
            companion_names = [
                f.name for f in temp_dir.iterdir() if f.name != destination.name and f.is_file()
            ]
        else:
            companion_names = companions_mode
            for name in companion_names:
                if not (temp_dir / name).is_file():
                    raise FileNotFoundError(
                        f"Companion '{name}' was not written to the temporary directory. "
                        "Use companions='auto' if the companion is optional."
                    )

        # --- Change detection (main file + all companions) ---
        # Always scan every companion regardless of whether the main file changed,
        # so changed_companions is complete even when the main file also changed.
        if result.old_hash is None:
            # destination was absent — always changed
            main_changed = True
        elif is_equal is not None:
            # custom comparator: call it, then fall back to hash if dest missing
            main_changed = not is_equal(temp_path, destination)
        else:
            main_changed = result.old_hash != result.new_hash

        for name in companion_names:
            temp_companion = temp_dir / name
            if not temp_companion.exists():
                continue
            existing_companion = destination.parent / name
            existing_h = (
                file_hash(existing_companion, algo=algo, block_size=block_size)
                if existing_companion.exists()
                else None
            )
            new_h = file_hash(temp_companion, algo=algo, block_size=block_size)
            if existing_h != new_h:
                logger.info("stablewrite: companion %s has changed.", name)
                result.changed_companions.append(name)

        any_changed = main_changed or bool(result.changed_companions)
        result.changed = any_changed

        # --- Decision ---
        if result.old_hash is None:
            result.reason = "destination missing"
            logger.info(
                "stablewrite: destination missing; saving new file %s (hash: %s)",
                destination,
                result.new_hash,
            )
        elif any_changed:
            result.reason = "content changed"
            logger.info(
                "stablewrite: content changed %s (old: %s, new: %s)",
                destination,
                result.old_hash,
                result.new_hash,
            )
            if save_strategy == "raise":
                result.saved = False
                raise FileExistsError(
                    f"{destination} already exists with different content. "
                    "Use save_strategy='overwrite' to replace it."
                )
            elif save_strategy == "skip":
                result.saved = False
                result.reason = "content changed — skipped"
                logger.info(
                    "stablewrite: content changed but save_strategy='skip'; skipping %s",
                    destination,
                )
                return
        else:
            result.reason = "content unchanged"
            result.saved = False
            logger.info("stablewrite: content unchanged; skipping replace for %s", destination)
            return

        # --- Publish main file + companions to destination ---
        # Each file is copied to a destination-side temp then os.replace'd,
        # keeping the final rename same-filesystem (atomic on POSIX).
        destination.parent.mkdir(parents=True, exist_ok=True)
        _publish_file(temp_path, destination, copy_fn=copy_fn)

        if companions_mode == "auto":
            for f in temp_dir.iterdir():
                if f.name != destination.name and f.is_file():
                    dest = destination.parent / f.name
                    logger.info("stablewrite: publishing companion %s → %s", f.name, dest)
                    _publish_file(f, dest, copy_fn=copy_fn)
        else:
            for name in companion_names:
                companion_temp = temp_dir / name
                if companion_temp.exists():
                    dest = destination.parent / name
                    logger.info("stablewrite: publishing companion %s → %s", name, dest)
                    _publish_file(companion_temp, dest, copy_fn=copy_fn)

        result.saved = True


# ---------------------------------------------------------------------------
# Convenience wrappers
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def save_xlsx_if_changed(path: str | Path, **kwargs):
    """Convenience wrapper for Excel files with deterministic normalization.

    Equivalent to ``save_if_changed(path, profile="xlsx", **kwargs)``.

    Args:
        path: Destination ``.xlsx`` file path.
        **kwargs: Forwarded to :func:`save_if_changed`.

    Yields:
        Saver: See :func:`save_if_changed`.

    Example::

        with save_xlsx_if_changed("results/report.xlsx", save_strategy="overwrite") as saver:
            df.to_excel(saver.path, index=False)
    """
    with save_if_changed(path, profile="xlsx", **kwargs) as saver:
        yield saver
