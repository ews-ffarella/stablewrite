"""Geopandas / shapefile integration tests for stable_write.save_if_changed.

A shapefile is the canonical "companion file bundle":
  .shp  — geometry bytes          (changes when geometry changes)
  .dbf  — attribute table         (changes when attribute columns change)
  .shx  — index                   (usually changes with geometry)
  .prj  — CRS string              (stable as long as CRS is the same)
  .cpg  — encoding declaration    (static)

These tests verify:
  - first write: all companion files land next to the main .shp
  - unchanged re-save: nothing is touched (mtime preserved)
  - geometry change: main file (.shp) detected as changed
  - attribute-only change: only .dbf changes → companion change triggers a
    full re-save even though the main .shp bytes are identical
"""

from typing import List, Optional

import pytest

geopandas = pytest.importorskip("geopandas")
shapely = pytest.importorskip("shapely")

from pathlib import Path  # noqa: E402

import geopandas as gpd  # noqa: E402
from shapely.geometry import Point  # noqa: E402

from stable_write import save_if_changed  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SHAPEFILE_EXTS = {".shp", ".dbf", ".shx", ".prj", ".cpg"}


def _make_gdf(values: list, points: Optional[List[tuple]] = None) -> gpd.GeoDataFrame:
    if points is None:
        points = [(float(i), float(i)) for i in range(len(values))]
    return gpd.GeoDataFrame(
        {"val": values, "geometry": [Point(x, y) for x, y in points]},
        crs="EPSG:4326",
    )


def _write(
    gdf: gpd.GeoDataFrame,
    tmp_path: Path,
    save_strategy: str = "overwrite",
):
    """Save *gdf* to ``tmp_path/data.shp`` via save_if_changed."""
    dest = tmp_path / "data.shp"
    with save_if_changed(dest, companions="auto", save_strategy=save_strategy) as saver:
        gdf.to_file(saver.path)
    return saver


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestShapefileFirstWrite:
    def test_all_companion_files_created(self, tmp_path):
        gdf = _make_gdf([1, 2])
        _write(gdf, tmp_path)
        present = {f.suffix for f in tmp_path.iterdir()}
        assert _SHAPEFILE_EXTS.issubset(present)

    def test_saver_saved_true(self, tmp_path):
        gdf = _make_gdf([1, 2])
        saver = _write(gdf, tmp_path)
        assert saver.saved is True
        assert saver.changed is True
        assert saver.reason == "destination missing"

    def test_main_file_readable(self, tmp_path):
        gdf = _make_gdf([10, 20, 30])
        _write(gdf, tmp_path)
        result = gpd.read_file(tmp_path / "data.shp")
        assert list(result["val"]) == [10, 20, 30]


class TestShapefileUnchanged:
    def test_unchanged_resave_skipped(self, tmp_path):
        gdf = _make_gdf([1, 2])
        _write(gdf, tmp_path)
        mtime_before = (tmp_path / "data.shp").stat().st_mtime_ns

        saver = _write(gdf, tmp_path)

        assert saver.saved is False
        assert saver.changed is False
        assert saver.reason == "content unchanged"
        assert (tmp_path / "data.shp").stat().st_mtime_ns == mtime_before

    def test_unchanged_dbf_not_touched(self, tmp_path):
        gdf = _make_gdf([1, 2])
        _write(gdf, tmp_path)
        mtime_dbf = (tmp_path / "data.dbf").stat().st_mtime_ns

        _write(gdf, tmp_path)

        assert (tmp_path / "data.dbf").stat().st_mtime_ns == mtime_dbf


class TestShapefileGeometryChanged:
    def test_geometry_change_detected(self, tmp_path):
        _write(_make_gdf([1, 2], [(0.0, 0.0), (1.0, 1.0)]), tmp_path)
        saver = _write(_make_gdf([1, 2], [(0.0, 0.0), (9.0, 9.0)]), tmp_path)

        assert saver.saved is True
        assert saver.changed is True

    def test_geometry_change_persisted(self, tmp_path):
        _write(_make_gdf([1, 2], [(0.0, 0.0), (1.0, 1.0)]), tmp_path)
        _write(_make_gdf([1, 2], [(0.0, 0.0), (9.0, 9.0)]), tmp_path)

        result = gpd.read_file(tmp_path / "data.shp")
        assert pytest.approx(result.geometry.iloc[1].x) == 9.0


class TestShapefileAttributeOnlyChanged:
    """Key case: geometry unchanged but attribute column changed.

    The .shp bytes are identical; only .dbf changes.  The companion change
    detection must catch this and trigger a full re-save even though the main
    .shp bytes are identical.
    """

    def test_attribute_change_detected_via_companion(self, tmp_path):
        pts = [(0.0, 0.0), (1.0, 1.0)]
        _write(_make_gdf([1, 2], pts), tmp_path)
        saver = _write(_make_gdf([99, 100], pts), tmp_path)  # same geometry, new values

        assert saver.saved is True
        assert saver.changed is True

    def test_attribute_change_persisted(self, tmp_path):
        pts = [(0.0, 0.0), (1.0, 1.0)]
        _write(_make_gdf([1, 2], pts), tmp_path)
        _write(_make_gdf([99, 100], pts), tmp_path)

        result = gpd.read_file(tmp_path / "data.shp")
        assert list(result["val"]) == [99, 100]

    def test_raises_without_overwrite(self, tmp_path):
        pts = [(0.0, 0.0), (1.0, 1.0)]
        _write(_make_gdf([1, 2], pts), tmp_path)

        with pytest.raises(FileExistsError):
            _write(_make_gdf([99, 100], pts), tmp_path, save_strategy="raise")

    def test_original_preserved_when_raise_strategy(self, tmp_path):
        pts = [(0.0, 0.0), (1.0, 1.0)]
        _write(_make_gdf([1, 2], pts), tmp_path)

        with pytest.raises(FileExistsError):
            _write(_make_gdf([99, 100], pts), tmp_path, save_strategy="raise")

        result = gpd.read_file(tmp_path / "data.shp")
        assert list(result["val"]) == [1, 2]  # original untouched

    def test_attribute_only_change_reports_dbf_as_changed_companion(self, tmp_path):
        pts = [(0.0, 0.0), (1.0, 1.0)]
        _write(_make_gdf([1, 2], pts), tmp_path)
        saver = _write(_make_gdf([99, 100], pts), tmp_path)

        assert saver.changed_companions == ["data.dbf"]


# ===========================================================================
# GeoPackage (.gpkg) — single-file, SQLite-based format
#
# GeoPackage embeds creation/modification timestamps in the SQLite file header
# and in the gpkg_contents table.  This means two independent writes of
# identical data will produce *different* byte sequences, so naive hash
# comparison always reports "changed".
#
# These tests verify the basic read/write/change-detection behaviour:
# - a changed file is always detected (different content)
# - an unchanged re-save is skipped when the caller replays the exact bytes
#
# A determinism xfail documents the known metadata problem.
# If you need semantic (non-byte) comparison, pass is_equal= with your own
# comparator (e.g. load both files with geopandas and compare geometries/CRS).
# ===========================================================================


def _write_gpkg(
    gdf: gpd.GeoDataFrame,
    tmp_path: Path,
    save_strategy: str = "overwrite",
):
    """Save *gdf* to ``tmp_path/data.gpkg`` via save_if_changed (no is_equal)."""
    dest = tmp_path / "data.gpkg"
    with save_if_changed(dest, save_strategy=save_strategy) as saver:
        # fiona/GPKG driver refuses to write to a pre-existing file; remove the
        # empty placeholder that stablewrite creates before handing the path over.
        saver.path.unlink()
        gdf.to_file(saver.path, driver="GPKG")
    return saver


class TestGeopackageFirstWrite:
    def test_file_created(self, tmp_path):
        saver = _write_gpkg(_make_gdf([1, 2]), tmp_path)
        assert (tmp_path / "data.gpkg").exists()
        assert saver.saved is True
        assert saver.changed is True
        assert saver.reason == "destination missing"

    def test_file_readable(self, tmp_path):
        _write_gpkg(_make_gdf([10, 20, 30]), tmp_path)
        result = gpd.read_file(tmp_path / "data.gpkg")
        assert sorted(result["val"].tolist()) == [10, 20, 30]


class TestGeopackageUnchanged:
    def test_unchanged_resave_skipped(self, tmp_path):
        """Re-saving an identical GeoDataFrame must not touch the file.

        Note: this only works because save_if_changed hashes the *new* temp
        file against the *existing* destination.  Two independent writes of
        the same GDF will differ in their SQLite timestamps, so this test
        writes via a second context manager reusing the *same* destination
        content — i.e., it verifies save_if_changed skips when the new bytes
        equal the old bytes, which is only the case when the caller provides
        exactly the same bytes.
        """
        gdf = _make_gdf([1, 2])
        _write_gpkg(gdf, tmp_path)
        # Read back the raw bytes and write them verbatim to check skip logic
        raw = (tmp_path / "data.gpkg").read_bytes()
        mtime_before = (tmp_path / "data.gpkg").stat().st_mtime_ns

        dest = tmp_path / "data.gpkg"
        with save_if_changed(dest) as saver:
            saver.path.write_bytes(raw)

        assert saver.saved is False
        assert (tmp_path / "data.gpkg").stat().st_mtime_ns == mtime_before

    def test_changed_content_detected(self, tmp_path):
        _write_gpkg(_make_gdf([1, 2]), tmp_path)
        saver = _write_gpkg(_make_gdf([9, 8]), tmp_path)

        assert saver.saved is True
        assert saver.changed is True


class TestGeopackageDeterminism:
    @pytest.mark.xfail(
        reason=(
            "GeoPackage (SQLite) embeds non-deterministic timestamps in the file "
            "header and gpkg_contents table, so two independent writes of identical "
            "data produce different byte sequences.  Pass is_equal= with a custom "
            "comparator (e.g. geopandas-based) if semantic equivalence is required."
        ),
        strict=True,
    )
    def test_raw_writes_are_not_deterministic(self, tmp_path):
        """Documents the known non-determinism of raw GPKG byte hashes.

        Expected to FAIL (xfail/strict) — use is_equal= for semantic comparison.
        """
        from stable_write import file_hash

        gdf = _make_gdf([1, 2])

        dest1 = tmp_path / "a.gpkg"
        with save_if_changed(dest1) as saver:
            saver.path.unlink()
            gdf.to_file(saver.path, driver="GPKG")
        h1 = file_hash(dest1)

        dest2 = tmp_path / "a.gpkg"
        with save_if_changed(dest2) as saver:
            saver.path.unlink()
            gdf.to_file(saver.path, driver="GPKG")
        h2 = file_hash(dest2)

        assert h1 == h2


# ===========================================================================
# GeoPackage — custom is_equal example using geopandas
# ===========================================================================


def _gpkg_is_equal(new: Path, existing: Path) -> bool:
    """Semantic GeoPackage comparator: CRS + geometry + attributes must match."""
    import pandas as pd

    a = gpd.read_file(new).reset_index(drop=True)
    b = gpd.read_file(existing).reset_index(drop=True)

    if a.crs != b.crs:
        return False

    if len(a) != len(b):
        return False

    a_geom_col = a.geometry.name
    b_geom_col = b.geometry.name

    geometries_equal = a.geometry.geom_equals(b.geometry).fillna(False).all()

    attrs_equal = pd.DataFrame(a.drop(columns=a_geom_col)).equals(
        pd.DataFrame(b.drop(columns=b_geom_col))
    )

    return bool(geometries_equal and attrs_equal)


def _write_gpkg_compared(
    gdf: gpd.GeoDataFrame,
    tmp_path: Path,
    save_strategy: str = "overwrite",
):
    dest = tmp_path / "data.gpkg"
    with save_if_changed(dest, is_equal=_gpkg_is_equal, save_strategy=save_strategy) as saver:
        saver.path.unlink()
        gdf.to_file(saver.path, driver="GPKG")
    return saver


class TestGeopackageIsEqual:
    """Shows how to use is_equal= with a geopandas-based comparator so that
    two independent writes of the same geodata are treated as unchanged despite
    the raw bytes differing due to SQLite timestamps.
    """

    def test_unchanged_resave_skipped(self, tmp_path):
        gdf = _make_gdf([1, 2])
        _write_gpkg_compared(gdf, tmp_path)
        mtime_before = (tmp_path / "data.gpkg").stat().st_mtime_ns

        saver = _write_gpkg_compared(gdf, tmp_path)

        assert saver.saved is False
        assert saver.changed is False
        assert (tmp_path / "data.gpkg").stat().st_mtime_ns == mtime_before

    def test_changed_data_detected(self, tmp_path):
        _write_gpkg_compared(_make_gdf([1, 2]), tmp_path)
        saver = _write_gpkg_compared(_make_gdf([9, 8]), tmp_path)

        assert saver.saved is True
        assert saver.changed is True

    def test_changed_crs_detected(self, tmp_path):
        gdf_4326 = _make_gdf([1, 2])
        _write_gpkg_compared(gdf_4326, tmp_path)

        gdf_3857 = gdf_4326.to_crs("EPSG:3857")
        saver = _write_gpkg_compared(gdf_3857, tmp_path)

        assert saver.saved is True
        assert saver.changed is True
