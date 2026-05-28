"""Built-in finalizer functions for :func:`~stable_write.stablewrite.save_if_changed`.

A *finalizer* is any callable ``(Path) -> None`` that mutates a temporary
file in-place **before** hashing.  Applying the same transformations
consistently is what makes an output byte-for-byte deterministic across runs.

Built-in profiles registered by default:

* ``"zip"``  — :func:`normalize_zip_metadata`
* ``"xlsx"`` — :func:`strip_ooxml_metadata`, :func:`normalize_zip_metadata`
* ``"docx"`` — :func:`strip_ooxml_metadata`, :func:`normalize_zip_metadata`
* ``"pptx"`` — :func:`strip_ooxml_metadata`, :func:`normalize_zip_metadata`
"""

from __future__ import annotations

import contextlib
import re
import shutil
import zipfile
from logging import getLogger
from pathlib import Path
from tempfile import NamedTemporaryFile

logger = getLogger(__name__)

_NEUTRAL_DT_STR = "1980-01-01T00:00:00Z"


def normalize_zip_metadata(path: Path) -> None:
    """Rewrite a ZIP archive with deterministic metadata.

    Sorts entries alphabetically, fixes all timestamps to 1980-01-01 00:00:00,
    and clears variable ``extra`` fields so the same logical content always
    produces the same bytes.

    Args:
        path: Path to the ZIP file to normalise in-place.

    Raises:
        FileNotFoundError: If *path* does not exist.
    """
    if not path.exists():
        raise FileNotFoundError(path)

    fixed_dt = (1980, 1, 1, 0, 0, 0)

    with NamedTemporaryFile(delete=False, suffix=path.suffix) as tmp:
        tmp_path = Path(tmp.name)

    try:
        with zipfile.ZipFile(path, "r") as zin:
            with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
                for old_info in sorted(zin.infolist(), key=lambda x: x.filename):
                    new_info = zipfile.ZipInfo(filename=old_info.filename, date_time=fixed_dt)
                    new_info.compress_type = zipfile.ZIP_DEFLATED
                    new_info.comment = old_info.comment
                    new_info.extra = b""
                    new_info.create_system = old_info.create_system
                    new_info.external_attr = old_info.external_attr
                    new_info.internal_attr = old_info.internal_attr
                    if old_info.filename.endswith("/"):
                        zout.writestr(new_info, b"")
                    else:
                        with zin.open(old_info) as src, zout.open(new_info, "w") as dst:
                            shutil.copyfileobj(src, dst)

        shutil.move(tmp_path, path, copy_function=shutil.copyfile)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()


def strip_office_metadata(path: Path) -> None:
    """Remove dynamic metadata from a **generated** ``.xlsx`` workbook.

    Uses ``openpyxl`` to zero out ``creator``, ``lastModifiedBy``, and
    timestamp properties so that re-saving an unchanged workbook does not
    produce a new hash.

    .. warning::
       This function loads and re-saves the workbook through ``openpyxl``'s
       full document model.  It is intended for **generated** ``.xlsx`` files
       (e.g. the output of ``DataFrame.to_excel``).  Applying it to arbitrary
       existing workbooks may silently alter or drop unsupported features.
       For ``.docx`` / ``.pptx`` files use :func:`strip_ooxml_metadata`
       instead.

    Requires ``openpyxl``; logs a warning and returns silently if not
    installed.

    Args:
        path: Path to the ``.xlsx`` file to strip in-place.

    Raises:
        FileNotFoundError: If *path* does not exist.
    """
    try:
        from datetime import datetime

        from openpyxl import load_workbook
    except ImportError:
        logger.warning("openpyxl not available; skipping Office metadata strip for %s", path)
        return

    if not path.exists():
        raise FileNotFoundError(path)

    try:
        neutral_dt = datetime(1980, 1, 1, 0, 0, 0)
        wb = load_workbook(path)
        wb.properties.created = neutral_dt
        wb.properties.modified = neutral_dt
        wb.properties.creator = ""
        wb.properties.lastModifiedBy = ""
        wb.save(path)
    except Exception:
        logger.exception("Could not strip Office metadata from %s", path)
        raise


def _neutralize_core_xml(data: bytes) -> bytes:
    """Return *data* (``docProps/core.xml`` bytes) with author/date fields zeroed.

    Uses targeted regex substitution so that no global namespace-map state is
    mutated (``xml.etree.ElementTree.register_namespace`` has process-wide side
    effects that can interfere with openpyxl's own XML serialisation).
    """
    text = data.decode("utf-8", errors="replace")

    # Neutralise date fields (dcterms:created / dcterms:modified)
    text = re.sub(
        r"(<dcterms:(?:created|modified)(?:[^>]*)>)[^<]*(</dcterms:(?:created|modified)>)",
        r"\g<1>" + _NEUTRAL_DT_STR + r"\g<2>",
        text,
    )
    # Clear string-valued author fields
    text = re.sub(r"(<dc:creator>)[^<]*(</dc:creator>)", r"\g<1>\g<2>", text)
    text = re.sub(r"(<cp:lastModifiedBy>)[^<]*(</cp:lastModifiedBy>)", r"\g<1>\g<2>", text)

    return text.encode("utf-8")


def strip_ooxml_metadata(path: Path) -> None:
    """Remove dynamic metadata from any OOXML container (.xlsx/.docx/.pptx).

    Patches ``docProps/core.xml`` directly inside the ZIP archive, zeroing
    ``dc:creator``, ``cp:lastModifiedBy``, ``dcterms:created``, and
    ``dcterms:modified`` without loading the full document model.  Works for
    all OOXML formats and requires no third-party library.

    Args:
        path: Path to the OOXML file to strip in-place.

    Raises:
        FileNotFoundError: If *path* does not exist.
    """
    if not path.exists():
        raise FileNotFoundError(path)

    with zipfile.ZipFile(path, "r") as zin:
        if "docProps/core.xml" not in zin.namelist():
            return  # nothing to strip

    with NamedTemporaryFile(delete=False, suffix=path.suffix) as tmp:
        tmp_path = Path(tmp.name)

    try:
        with zipfile.ZipFile(path, "r") as zin:
            with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
                for info in zin.infolist():
                    with zin.open(info) as src:
                        if info.filename == "docProps/core.xml":
                            # Small XML — must be read fully to patch in memory.
                            zout.writestr(info, _neutralize_core_xml(src.read()))
                        else:
                            with zout.open(info, "w") as dst:
                                shutil.copyfileobj(src, dst)

        shutil.move(tmp_path, path, copy_function=shutil.copyfile)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
