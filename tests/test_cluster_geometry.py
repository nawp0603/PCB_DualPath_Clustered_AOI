"""Phase 2 gate tests — T-GEO-01..05 (Verifier, CPU-only).

Scope: geometry contract plan §2.2.5 + §6.1 for
src/cluster/dbscan_clustering.py + adaptive_cluster_cropper.py.
Phase 3 module batch_bucket.py not delivered yet → T-GEO-04/05
check Phase-2 translation property + skip letterbox with reason.
Does NOT touch Board 01/04. Does NOT modify src/ or scripts/.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.cluster import adaptive_cluster_cropper as ac  # noqa: E402
from src.cluster import dbscan_clustering as dc  # noqa: E402

IMG = (1000, 1000)  # (h, w)


def _synth_h_inv() -> tuple[np.ndarray, np.ndarray]:
    src = np.float32([[0, 0], [1000, 0], [1000, 1000], [0, 1000]])
    dst = np.float32([[50, 30], [980, 60], [960, 990], [40, 970]])
    h_fwd = cv2.getPerspectiveTransform(src, dst)  # template -> input
    h_inv = np.linalg.inv(h_fwd)  # NOTE: variable named h_fwd here is TPL->IN;
    # project_points(pts_tpl, H) with H=TPL->IN. Caller convention calls it h_inv.
    return h_fwd, np.linalg.inv(h_fwd)


# ---------------------------------------------------------------------------
# T-GEO-01 — clustering happens in Template space BEFORE projection
# ---------------------------------------------------------------------------

def test_cluster_bbox_in_template_before_project():
    # cluster_blobs must read centroid_tpl (Template) and never touch H_inv.
    src_blobs = inspect.getsource(dc.cluster_blobs)
    assert "centroid_tpl" in src_blobs
    for banned in ("H_inv", "h_inv", "project_points", "perspectiveTransform"):
        assert banned not in src_blobs, (
            f"cluster_blobs must not reference {banned} (Chot 1: cluster in Template)"
        )
    # _parse_blobs reads template fields only.
    src_parse = inspect.getsource(dc._parse_blobs)
    assert "centroid_tpl" in src_parse and "H_inv" not in src_parse
    # cropper: union box (Template) must be built BEFORE projection.
    src_c2c = inspect.getsource(ac.clusters_to_crops)
    assert src_c2c.index("union_box_tpl") < src_c2c.index("project_box_to_input"), (
        "must build Template union box BEFORE projecting via H_inv (Chot 1)"
    )
    # project_points only used inside project_box_to_input (projection point).
    src_mod = inspect.getsource(ac)
    assert "perspectiveTransform" in inspect.getsource(ac.project_points)
    # cluster_blobs output members are Template indices (no input coords).
    blobs = [
        {"centroid_tpl": [float(i * 10), 0.0], "z_diff": 5.0,
         "area": 100.0, "bbox_tpl": [float(i * 10 - 5), -5.0, float(i * 10 + 5), 5.0]}
        for i in range(3)
    ]
    clusters = dc.cluster_blobs(blobs, eps=50.0)
    assert len(clusters) > 0
    for c in clusters:
        assert "members" in c and "z_cluster" in c
        assert all(0 <= m < 3 for m in c["members"])


# ---------------------------------------------------------------------------
# T-GEO-02 — H_inv roundtrip subpixel < 1e-3 px
# ---------------------------------------------------------------------------

def test_hinv_roundtrip_subpixel():
    h_tpl2in, h_in2tpl = _synth_h_inv()
    rng = np.random.default_rng(20261023)
    pts_tpl = rng.uniform(0, 1000, size=(20, 2)).astype(np.float64)
    pts_in = ac.project_points(pts_tpl, h_tpl2in)
    back = ac.project_points(pts_in, h_in2tpl)
    err = float(np.abs(back - pts_tpl).max())
    assert err < 1e-3, f"H roundtrip max err {err} >= 1e-3"
    # 4-corner box path (project_box_to_input) invertibility on interior box.
    box_tpl = [200.0, 200.0, 400.0, 350.0]
    _, aabb = ac.project_box_to_input(box_tpl, h_tpl2in, IMG)
    # corners of Template box -> Input quad -> back to Template ≈ original.
    corners = np.float32([[box_tpl[0], box_tpl[1]], [box_tpl[2], box_tpl[1]],
                          [box_tpl[2], box_tpl[3]], [box_tpl[0], box_tpl[3]]])
    q_in = ac.project_points(corners, h_tpl2in)
    q_back = ac.project_points(q_in, h_in2tpl)
    err2 = float(np.abs(q_back - corners).max())
    assert err2 < 1e-3, f"box corner roundtrip err {err2} >= 1e-3"
    # degenerate H_inv must raise ValueError (Phase 4 catch → fallback TOP-8).
    with pytest.raises(ValueError):
        ac.project_box_to_input(box_tpl, np.zeros((3, 3)), IMG)
    with pytest.raises(ValueError):
        ac.project_box_to_input(
            box_tpl, np.full((3, 3), np.nan), IMG)


# ---------------------------------------------------------------------------
# T-GEO-03 — crop clamp SHIFT, not shrink
# ---------------------------------------------------------------------------

def test_crop_clamp_shift_not_shrink():
    h, w = IMG
    # Interior box: side preserved exactly.
    cx, cy, side = ac.squareize_box([100.0, 100.0, 300.0, 250.0], IMG)
    assert side == pytest.approx(200.0)
    x0, y0 = ac.center_origin(cx, cy, side, IMG)
    assert 0 <= x0 <= w - round(side) and 0 <= y0 <= h - round(side)
    assert x0 + round(side) <= w and y0 + round(side) <= h
    # Top-left border: shift (not shrink) — window still side×side inside.
    cx, cy, side = ac.squareize_box([0.0, 0.0, 200.0, 150.0], IMG)
    assert side == pytest.approx(200.0)
    x0, y0 = ac.center_origin(cx, cy, side, IMG)
    assert (x0, y0) == (0, 0)
    # Bottom-right border.
    cx, cy, side = ac.squareize_box([800.0, 800.0, 1000.0, 950.0], IMG)
    assert side == pytest.approx(200.0)
    x0, y0 = ac.center_origin(cx, cy, side, IMG)
    assert x0 + round(side) <= w and y0 + round(side) <= h
    assert x0 + round(side) == w  # shifted flush to edge
    # Oversize beyond image capped to min(H, W), still inside.
    cx, cy, side = ac.squareize_box([0.0, 0.0, 900.0, 900.0], (800, 800))
    assert side == pytest.approx(800.0)
    x0, y0 = ac.center_origin(cx, cy, side, (800, 800))
    assert (x0, y0) == (0, 0)
    # End-to-end: border cluster crop stays inside image with full side.
    h_tpl2in, _ = _synth_h_inv()
    blobs = [{"centroid_tpl": [10.0, 10.0], "z_diff": 9.0, "area": 120.0,
              "bbox_tpl": [0.0, 0.0, 30.0, 30.0]}]
    clusters = dc.cluster_blobs(blobs, eps=50.0)
    crops, _ = ac.clusters_to_crops(clusters, blobs, h_tpl2in, IMG)
    assert len(crops) == 1
    x0, y0 = crops[0]["origin"]
    s = crops[0]["side"]
    assert x0 >= 0 and y0 >= 0 and x0 + round(s) <= w and y0 + round(s) <= h


# ---------------------------------------------------------------------------
# T-GEO-04 — translate-only, no scale (Phase-2 part; batch_bucket = Phase 3)
# ---------------------------------------------------------------------------

def test_bucket_translate_only_no_scale():
    import importlib.util
    spec = importlib.util.find_spec("src.cluster.batch_bucket")
    if spec is None:
        pytest.skip("batch_bucket.py is Phase 3 — not delivered in Phase 2")
    from src.cluster import batch_bucket as bb
    src = inspect.getsource(bb.to_input_coords)
    # Pure translation: no * or / applied to coordinates.
    assert "*" not in src and "/" not in src, (
        "to_input_coords must be pure translation (no * or /)"
    )
    for corner in ([0.0, 0.0, 10.0, 10.0], [374.0, 374.0, 384.0, 384.0]):
        out = bb.to_input_coords(corner, (100, 200))
        assert out == pytest.approx(
            [corner[0] + 100, corner[1] + 200, corner[2] + 100, corner[3] + 200])


def test_cropper_translation_property_phase2():
    """Phase-2 counterpart: crop origin mapping is pure addition, no resize."""
    h_tpl2in, _ = _synth_h_inv()
    blobs = [{"centroid_tpl": [500.0, 500.0], "z_diff": 8.0, "area": 150.0,
              "bbox_tpl": [480.0, 480.0, 520.0, 520.0]}]
    clusters = dc.cluster_blobs(blobs, eps=50.0)
    crops, _ = ac.clusters_to_crops(clusters, blobs, h_tpl2in, IMG)
    c = crops[0]
    x0, y0 = c["origin"]
    side = c["side"]
    # Fake det at 4 crop corners → input = crop + origin (translation only).
    for bx1, by1, bx2, by2 in ([0, 0, 20, 20], [side - 20, side - 20, side, side]):
        mapped = [bx1 + x0, by1 + y0, bx2 + x0, by2 + y0]
        assert mapped[2] - mapped[0] == pytest.approx(bx2 - bx1)
        assert mapped[3] - mapped[1] == pytest.approx(by2 - by1)
    # cut helper extracts exactly side×side at origin.
    img = np.zeros((IMG[0], IMG[1], 3), dtype=np.uint8)
    cut = ac.cut_crop_from_image(img, c)
    assert cut.shape[0] == round(side) and cut.shape[1] == round(side)
    # No silent resize in cropper geometry path (actual resize calls, not words).
    src_low = inspect.getsource(ac).lower()
    assert "cv2.resize" not in src_low and ".resize(" not in src_low.replace(
        "center_origin", "").replace("squareize", ""), (
        "cropper geometry path must not call resize (scale 1:1 only)"
    )


# ---------------------------------------------------------------------------
# T-GEO-05 — letterbox unpad inverse (Phase 3 fallback B)
# ---------------------------------------------------------------------------

def test_letterbox_unpad_inverse():
    import importlib.util
    if importlib.util.find_spec("src.cluster.batch_bucket") is None:
        pytest.skip("letterbox path lives in Phase-3 batch_bucket.py")
    from src.cluster import batch_bucket as bb
    assert hasattr(bb, "letterbox_pad") and hasattr(bb, "letterbox_unpad")
    box = [100.0, 120.0, 200.0, 180.0]
    assert bb.letterbox_unpad(bb.letterbox_pad(box, 384), 384) == pytest.approx(box, abs=1e-6)
