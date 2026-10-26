"""Phase 2 gate tests — T-CLU-02..04 + T-E2E-01..03 (Verifier, CPU-only).

Scope: src/cluster/dbscan_clustering.py + adaptive_cluster_cropper.py +
cluster_gating.py contract vs plan §2.2/§2.4/§3.3 + Phase 4 hooks
(fuse/decide/attach_cluster_id). Phase-4 pipeline (evaluate_clustered)
not delivered yet → E2E-02/03 assert Phase-2 primitives + SKIP full
pipeline with reason. Does NOT touch Board 01/04. No src/scripts edits.
"""

from __future__ import annotations

import builtins
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
from src.cluster import cluster_gating as cg  # noqa: E402
from src.decision.logic import decide  # noqa: E402
from src.models import ensemble as ens  # noqa: E402

IMG = (1000, 1000)


def _h_tpl2in() -> np.ndarray:
    src = np.float32([[0, 0], [1000, 0], [1000, 1000], [0, 1000]])
    dst = np.float32([[50, 30], [980, 60], [960, 990], [40, 970]])
    return cv2.getPerspectiveTransform(src, dst)


def _blob(cx: float, cy: float, z: float, half: float = 8.0) -> dict:
    return {"centroid_tpl": [float(cx), float(cy)], "z_diff": float(z),
            "area": 120.0,
            "bbox_tpl": [float(cx - half), float(cy - half),
                         float(cx + half), float(cy + half)]}


# ---------------------------------------------------------------------------
# Plan-locked constants (App B + §2.2.2/§2.2.5/§2.4/§3.3)
# ---------------------------------------------------------------------------

def test_plan_locked_constants():
    assert dc.Z0 == 2.0 and dc.TAU_Z == 1.0 and dc.DELTA == 1e-6
    assert dc.CONTEXT_MARGIN_P == 48.0
    assert dc.MAX_CLUSTER_CROPS == 8
    assert dc.BISECT_CAP_PX == 640.0 and dc.BISECT_MAX_DEPTH == 2
    assert dc.KMEANS_RANDOM_STATE == 0
    assert cg.R_BASE == 80.0
    assert cg.CROSS_CROP_NMS_IOU == 0.50
    assert cg.MARGIN_DELTA == 40.0
    assert ac.CONTEXT_MARGIN_P == 48.0
    p = dc.load_frozen_params()
    assert p["eps"] == 28.0 and p["min_samples"] == 1 and p["k_raw"] == 12
    assert p["frozen_sha256"] == "8d4baa3ea11c683e3e18879f1e08a9dc97139e0def667ae4d8abab4830b514d9"


# ---------------------------------------------------------------------------
# T-CLU-02 — two multi-blob clusters + isolated singleton (m=1); ablation m=2
# ---------------------------------------------------------------------------

def test_dbscan_two_clusters_plus_noise():
    blobs = [_blob(x, y, 5.0) for x, y in
             ([0, 0], [30, 10], [10, 40], [50, 50],
              [400, 400], [430, 410], [410, 440],
              [1200, 1200])]
    clusters = dc.cluster_blobs(blobs, eps=50.0)
    sizes = sorted([c["size"] for c in clusters], reverse=True)
    assert sizes == [4, 3, 1], f"expected [4,3,1], got {sizes}"
    assert all(c["cluster_id"] >= 0 for c in clusters)
    # Z_cluster = max z, sorted descending
    for c in clusters:
        zs = [blobs[m]["z_diff"] for m in c["members"]]
        assert c["z_cluster"] == pytest.approx(max(zs))
    zcs = [c["z_cluster"] for c in clusters]
    assert zcs == sorted(zcs, reverse=True)
    # no noise in main path: every blob in exactly one cluster
    covered = sorted(m for c in clusters for m in c["members"])
    assert covered == list(range(8))
    # ablation m=2: singleton becomes noise (documents old paradox)
    sklearn = pytest.importorskip("sklearn")
    clusters_ab, noise = dc.cluster_blobs_ablation(
        blobs, eps=50.0, min_samples=2, return_noise=True)
    assert bool(noise[7]) is True, "ablation m=2 must mark isolated singleton as noise"
    assert all(not bool(v) for v in noise[:7])
    # Chot 3: 12 discrete blobs pairwise > eps → exactly 8 crops, top-Z kept
    H = _h_tpl2in()
    blobs12 = [_blob(i * 500.0, 0.0, float(12 - i)) for i in range(12)]
    clusters12 = dc.cluster_blobs(blobs12, eps=50.0)
    assert len(clusters12) == 12, "discrete blobs must yield 12 singletons"
    crops, dropped = ac.clusters_to_crops(clusters12, blobs12, H, (4000, 6000))
    assert len(crops) == 8, f"hard cap MAX_CLUSTER_CROPS=8, got {len(crops)}"
    assert len(dropped) == 4
    kept_z = [c["z_cluster"] for c in crops]
    assert kept_z == sorted(kept_z, reverse=True)
    assert min(kept_z) > max(d["z_cluster"] for d in dropped)
    assert all("dropped_lowz" in d["reason"] for d in dropped)


# ---------------------------------------------------------------------------
# T-CLU-03 — deterministic + sklearn-free fallback
# ---------------------------------------------------------------------------

def test_dbscan_deterministic_and_sklearn_free_fallback():
    blobs = [_blob(x, y, 5.0) for x, y in
             ([0, 0], [30, 10], [10, 40], [50, 50],
              [400, 400], [430, 410], [410, 440],
              [1200, 1200])]
    a = dc.cluster_blobs(blobs, eps=50.0)
    b = dc.cluster_blobs(blobs, eps=50.0)
    assert a == b, "cluster_blobs must be deterministic"
    cent = np.float32([bl["centroid_tpl"] for bl in blobs])
    assert np.array_equal(dc._split_two(cent), dc._split_two(cent)), (
        "KMeans bisection (seed=0) must be deterministic")
    # fallback without sklearn: longest-axis split, 2 non-empty deterministic
    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name.startswith("sklearn"):
            raise ImportError("blocked for fallback test")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = _blocked
    try:
        lab1 = dc._split_two(cent)
        lab2 = dc._split_two(cent)
    finally:
        builtins.__import__ = real_import
    assert np.array_equal(lab1, lab2)
    assert set(int(v) for v in lab1) == {0, 1}
    # main-path CC source is sklearn-free (AUDIT-01)
    assert "import sklearn" not in inspect.getsource(dc.connected_components_threshold)
    assert "from sklearn" not in inspect.getsource(dc.connected_components_threshold)


# ---------------------------------------------------------------------------
# T-CLU-04 — bisection terminates and caps
# ---------------------------------------------------------------------------

def test_bisection_terminates_and_caps():
    H = np.eye(3)
    shape = (2000, 2000)
    # diameter ~1500px cluster
    boxes = [[0.0, 900.0, 60.0, 960.0], [700.0, 900.0, 760.0, 960.0],
             [1440.0, 900.0, 1500.0, 960.0]]
    s0 = ac.cluster_square_side(boxes, H, shape)
    assert s0 > 640, f"setup must exceed cap, got {s0}"
    log: list = []
    pieces = dc.bisect_oversize_cluster([0, 1, 2], boxes, H, shape,
                                        bisect_log=log)
    assert len(log) >= 1 and len(log) <= 2, f"recursion depth <= 2, got {log}"
    assert all(d["depth"] < 2 for d in log)
    for piece in pieces:
        s = ac.cluster_square_side([boxes[i] for i in piece], H, shape)
        assert s <= 640.0, f"post-bisect piece {piece} side {s} > 640"
    # clusters_to_crops end-to-end: side capped, bisect flag set
    blobs = [_blob(30.0, 930.0, 9.0, half=30.0),
             _blob(730.0, 930.0, 8.0, half=30.0),
             _blob(1470.0, 930.0, 7.0, half=30.0)]
    clusters = dc.cluster_blobs(blobs, eps=2000.0)  # force one cluster
    assert len(clusters) == 1 and clusters[0]["size"] == 3
    crops, dropped = ac.clusters_to_crops(clusters, blobs, H, shape)
    assert len(crops) >= 1
    assert all(c["side"] <= 640.0 for c in crops)
    assert any(c["bisect_depth"] >= 1 for c in crops)
    assert all(c["truncated"] in (True, False) for c in crops)
    # degenerate member list never crashes
    assert dc.bisect_oversize_cluster([0], boxes, H, shape) == [[0]]
    # bit-identical square_side vs frozen calibrate oracle
    import scripts.calibrate_dbscan as cal_mod
    assert ac.cluster_square_side(boxes, H, shape) == \
        cal_mod.cluster_square_side(boxes, H, shape)
    # weighted distance bit-identical vs frozen oracle
    rng = np.random.default_rng(20261023)
    cent = rng.uniform(0, 2000, size=(12, 2))
    z = rng.uniform(-1, 6, size=12)
    dw_new, _ = dc.weighted_distance_matrix(cent, z)
    dw_fro, _ = cal_mod.weighted_distance_matrix(cent, z)
    assert np.array_equal(dw_new, dw_fro)


# ---------------------------------------------------------------------------
# T-E2E-01 — evidence schema compatible + cluster_id ignored by decide
# ---------------------------------------------------------------------------

def test_evidence_item_schema_compatible():
    H = _h_tpl2in()
    blobs = [_blob(500.0, 500.0, 8.0), _blob(520.0, 510.0, 7.0)]
    det = {"bbox": [540.0, 520.0, 600.0, 580.0], "class_id": 5,
           "class_name": "missing_hole", "conf": 0.9}
    items = ens.fuse([det], blobs, H)
    assert len(items) > 0
    for it in items:
        for k in ("class_id", "conf", "source", "bbox", "z_diff", "area"):
            assert k in it, f"fuse item missing {k}"
    cg.attach_cluster_id(items, 3)
    assert all(it["cluster_id"] == 3 for it in items)
    res = decide(items)  # must not crash on extra cluster_id
    assert res["conclusion"] in ("FAIL", "REWORK", "REVIEW", "PASS")
    # cropper dict carries every plan §4.2 contract field
    clusters = dc.cluster_blobs(blobs, eps=50.0)
    crops, _ = ac.clusters_to_crops(clusters, blobs, H, IMG)
    assert len(crops) >= 1
    for c in crops:
        for k in ("origin", "bucket", "cluster_id", "z_cluster", "truncated"):
            assert k in c, f"crop dict missing contract field {k}"
        img = np.zeros((IMG[0], IMG[1], 3), dtype=np.uint8)
        cut = ac.cut_crop_from_image(img, c)
        assert cut.shape[0] == round(c["side"]) and cut.shape[1] == round(c["side"])


# ---------------------------------------------------------------------------
# T-E2E-02/03 — Phase-4 fallback hooks (pipeline itself is Phase 4)
# ---------------------------------------------------------------------------

def test_fallback_single_centroid_path():
    import importlib.util
    if importlib.util.find_spec("scripts.evaluate_clustered") is None:
        pytest.skip("evaluate_clustered.py is Phase 4 — testing Phase-2 hooks only")
    # (unreachable until Phase 4 delivers the pipeline)


def test_fallback_singleton_hook_phase2():
    """Phase-2 half of E2E-02: singleton is a VALID cluster (not noise/fallback)
    and its union gate equals legacy single-centroid gate."""
    from src.models.yolo_detector import YOLODetector
    blobs = [_blob(500.0, 500.0, 9.0)]
    clusters = dc.cluster_blobs(blobs, eps=50.0)
    assert len(clusters) == 1 and clusters[0]["size"] == 1  # valid singleton
    H = _h_tpl2in()
    cx_in, cy_in = ac.project_points(
        np.array([[500.0, 500.0]]), H)[0].tolist()
    dets = [{"bbox": [float(cx_in) - 10, float(cy_in) - 10,
                      float(cx_in) + 10, float(cy_in) + 10],
             "class_id": 0, "conf": 0.9}]
    legacy = YOLODetector.spatial_center_gating(
        [dict(d) for d in dets], (float(cx_in), float(cy_in)),
        crop_origin=(0.0, 0.0))
    union = cg.union_of_disks_gate([dict(d) for d in dets],
                                   np.array([[cx_in, cy_in]]))
    assert [d["bbox"] for d in union] == [d["bbox"] for d in legacy]


def test_fallback_on_cluster_exception():
    # Degenerate H_inv raises ValueError — the signal Phase 4 catches
    # to run fallback TOP-8 + fallback_log.jsonl (§5.4.3).
    with pytest.raises(ValueError):
        ac.project_box_to_input([0.0, 0.0, 10.0, 10.0], np.zeros((3, 3)), IMG)
    # NaN metric input must not silently corrupt clustering: NaN distances
    # never satisfy (<= eps), so the blob stays an isolated singleton
    # (deterministic, no crash, no cross-contamination).
    out = dc.cluster_blobs([{"centroid_tpl": [float("nan"), 0.0],
                             "z_diff": 1.0, "area": 10.0,
                             "bbox_tpl": [0.0, 0.0, 1.0, 1.0]}], eps=50.0)
    assert len(out) == 1 and out[0]["size"] == 1
    # decide still concludes on empty/Branch-B-only evidence (fallback output).
    res = decide([{"class_id": None, "class_name": None, "conf": None,
                   "source": "branchb", "bbox": [0, 0, 160, 160],
                   "z_diff": 4.0, "area": 200.0}])
    assert res["conclusion"] in ("FAIL", "REWORK", "REVIEW", "PASS")
