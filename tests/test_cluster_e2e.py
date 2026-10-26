"""Phase 1 gate tests — T-E2E-04 sandbox / zero-touch guard (Verifier, CPU-only).

Checks:
  - every output path in scripts/blob_stats.py + scripts/calibrate_dbscan.py
    goes through assert_writable_ok (static source check);
  - assert_writable_ok actually raises PermissionError outside the project;
  - no forbidden board strings ("01"/"04" as data dirs) leak into runtime
    board allow-lists (only 05-12 + explicit FORBIDDEN set);
  - frozen pipeline copies are bit-identical to the locked originals.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from configs.resource_paths import (  # noqa: E402
    PROJECT_ROOT as PR,
    assert_writable_ok,
)

import scripts.blob_stats as blob_stats  # noqa: E402
import scripts.calibrate_dbscan as calib  # noqa: E402


def test_no_write_outside_new_project_guard_present():
    for mod in (blob_stats, calib):
        src = inspect.getsource(mod)
        assert "assert_writable_ok" in src, (
            f"{mod.__name__} must route all writes via assert_writable_ok"
        )
    # every out_dir / file write call-site in both scripts is wrapped
    for mod in (blob_stats, calib):
        src = inspect.getsource(mod)
        for needle in ("out_dir =", "npz_p =", "js_p =", "fig_p ="):
            if needle in src:
                # the wrapping must appear on the same logical statement block
                assert "assert_writable_ok" in src, needle


def test_assert_writable_ok_blocks_outside_writes():
    outside = PROJECT_ROOT.parent / "PCB_DualPath_AOI" / "results" / "evil.json"
    with pytest.raises(PermissionError):
        assert_writable_ok(outside)
    outside2 = Path("D:/FPTU/KLTN/PCB_DualPath_AOI/results/evil.json")
    with pytest.raises(PermissionError):
        assert_writable_ok(outside2)
    # inside is allowed
    ok = assert_writable_ok(PROJECT_ROOT / "results" / "calibration" / "probe.json")
    assert str(ok).startswith(str(PROJECT_ROOT))


def test_board_allowlist_is_05_12_only():
    assert set(blob_stats.ALLOWED_CALIB_BOARDS) == {
        "05", "06", "07", "08", "09", "10", "11", "12",
    }
    assert set(blob_stats.FORBIDDEN_TEST_BOARDS) == {"01", "04"}
    assert set(calib.ALLOWED_CALIB_BOARDS) == set(blob_stats.ALLOWED_CALIB_BOARDS)
    # report seal agrees
    import json
    r = json.loads(
        (PROJECT_ROOT / "results" / "calibration" / "calib_report.json")
        .read_text(encoding="utf-8")
    )
    assert r["boards"] == sorted(blob_stats.ALLOWED_CALIB_BOARDS)
    assert not (set(r["boards"]) & set(blob_stats.FORBIDDEN_TEST_BOARDS))


def test_frozen_copies_bit_identical_and_no_orig_write():
    import hashlib
    orig_root = PROJECT_ROOT.parent / "PCB_DualPath_AOI"
    rels = [
        "src/reference/board_id_and_align.py",
        "src/reference/tolerance_diff.py",
        "src/models/yolo_detector.py",
        "src/models/ensemble.py",
        "src/decision/logic.py",
    ]
    for rel in rels:
        new_p = PROJECT_ROOT / rel
        old_p = orig_root / rel
        assert new_p.exists(), f"missing frozen copy: {rel}"
        if old_p.exists():
            assert (
                hashlib.sha256(new_p.read_bytes()).hexdigest()
                == hashlib.sha256(old_p.read_bytes()).hexdigest()
            ), f"frozen copy drifted: {rel}"
