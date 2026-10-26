"""Phase 2 gate tests — T-GAT-01..06 (Verifier, CPU-only).

Scope: src/cluster/cluster_gating.py (Union-of-Disks + Cross-Crop NMS
+ Margin ablation) vs frozen legacy YOLODetector.spatial_center_gating.
Does NOT touch Board 01/04. Does NOT modify src/ or scripts/.
"""

from __future__ import annotations

import inspect
import sys
import time
from pathlib import Path

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.cluster import cluster_gating as cg  # noqa: E402
from src.models.yolo_detector import (  # noqa: E402
    CENTER_GATE_RADIUS,
    YOLODetector,
)

RNG = np.random.default_rng(20261023)


def _det(cx: float, cy: float, half: float = 10.0,
         conf: float = 0.9, class_id: int = 0) -> dict:
    return {"bbox": [cx - half, cy - half, cx + half, cy + half],
            "class_id": class_id, "conf": conf}


# ---------------------------------------------------------------------------
# T-GAT-01 — singleton union == legacy bit-identical
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("require_overlap", [True, False])
def test_union_single_blob_equals_legacy(require_overlap: bool):
    assert cg.R_BASE == CENTER_GATE_RADIUS == 80.0
    rng = np.random.default_rng(20261023 + int(require_overlap))
    for _ in range(20):
        cx, cy = rng.uniform(100, 900, size=2)
        dets = [
            {"bbox": [float(x1), float(y1), float(x2), float(y2)],
             "class_id": int(rng.integers(0, 6)), "conf": float(rng.uniform(0.2, 0.99))}
            for x1, y1, x2, y2 in
            (lambda b: b)(np.column_stack([
                rng.uniform(0, 980, size=8), rng.uniform(0, 980, size=8),
                rng.uniform(0, 980, size=8), rng.uniform(0, 980, size=8)]))
        ]
        # normalize x1<x2, y1<y2, min size 5px
        for d in dets:
            x1, y1, x2, y2 = d["bbox"]
            if x2 < x1:
                x1, x2 = x2, x1
            if y2 < y1:
                y1, y2 = y2, y1
            if x2 - x1 < 5:
                x2 = x1 + 5
            if y2 - y1 < 5:
                y2 = y1 + 5
            d["bbox"] = [x1, y1, x2, y2]
        legacy = YOLODetector.spatial_center_gating(
            [dict(dd) for dd in dets], (float(cx), float(cy)),
            radius=80.0, crop_origin=(0.0, 0.0),
            require_overlap=require_overlap, diff_box=None)
        union = cg.union_of_disks_gate(
            [dict(dd) for dd in dets], np.array([[cx, cy]]),
            r_base=80.0, diff_boxes=None, require_overlap=require_overlap)
        assert [d["bbox"] for d in union] == [d["bbox"] for d in legacy], (
            f"singleton union must be bit-identical to legacy "
            f"(require_overlap={require_overlap})"
        )
    # with explicit diff_box passed both sides
    dets = [_det(500, 500), _det(700, 700), _det(505, 505, half=100)]
    db = [420.0, 420.0, 580.0, 580.0]
    legacy = YOLODetector.spatial_center_gating(
        [dict(d) for d in dets], (500.0, 500.0), crop_origin=(0.0, 0.0),
        require_overlap=True, diff_box=list(db))
    union = cg.union_of_disks_gate(
        [dict(d) for d in dets], np.array([[500.0, 500.0]]),
        diff_boxes=np.array([db]), require_overlap=True)
    assert [d["bbox"] for d in union] == [d["bbox"] for d in legacy]


# ---------------------------------------------------------------------------
# T-GAT-02 — union rescues edge detection dropped by single-centroid
# ---------------------------------------------------------------------------

def test_union_rescues_edge_detection():
    centers = np.array([[0.0, 0.0], [120.0, 0.0], [240.0, 0.0]])
    det_edge = _det(240.0, 0.0, half=10.0)
    det_far = _det(900.0, 900.0, half=10.0)
    dets = [dict(det_edge), dict(det_far)]
    # single-centroid at cluster mean (120,0), R=80 → drops edge det
    single = YOLODetector.spatial_center_gating(
        [dict(d) for d in dets], (120.0, 0.0), crop_origin=(0.0, 0.0),
        require_overlap=True)
    assert [d["bbox"] for d in single] == [], (
        "single-centroid gate must DROP the edge det (repro of §2.4.1 bug)")
    kept = cg.union_of_disks_gate([dict(d) for d in dets], centers)
    kept_boxes = [d["bbox"] for d in kept]
    assert det_edge["bbox"] in kept_boxes, "union gate must RESCUE edge det"
    assert det_far["bbox"] not in kept_boxes, "union gate must still drop far FP"


# ---------------------------------------------------------------------------
# T-GAT-03 — monotonicity: adding blobs never drops a kept det (fuzz 100)
# ---------------------------------------------------------------------------

def test_union_monotonic_no_new_fn():
    rng = np.random.default_rng(0)
    for t in range(100):
        n = int(rng.integers(1, 6))
        m = int(rng.integers(1, 20))
        centers = rng.uniform(0, 1000, size=(n, 2))
        extra = rng.uniform(0, 1000, size=(int(rng.integers(1, 4)), 2))
        dets = []
        for _ in range(m):
            cx, cy = rng.uniform(-50, 1050, size=2)
            hw, hh = rng.uniform(5, 60, size=2)
            dets.append({"bbox": [cx - hw, cy - hh, cx + hw, cy + hh],
                         "class_id": int(rng.integers(0, 6)),
                         "conf": float(rng.uniform(0.2, 0.99))})
        small = {tuple(d["bbox"]) for d in
                 cg.union_of_disks_gate([dict(d) for d in dets], centers)}
        big = {tuple(d["bbox"]) for d in
               cg.union_of_disks_gate([dict(d) for d in dets],
                                      np.vstack([centers, extra]))}
        assert small <= big, f"monotonicity violated at fuzz case {t}"


# ---------------------------------------------------------------------------
# T-GAT-04 — micro-benchmark gating + NMS P95 < 1 ms (M=50 × n=12 × 200)
# ---------------------------------------------------------------------------

def test_gating_latency_under_1ms():
    rng = np.random.default_rng(20261023)
    centers = rng.uniform(0, 3000, size=(12, 2))
    dets = []
    for _ in range(50):
        cx, cy = rng.uniform(0, 3000, size=2)
        dets.append({"bbox": [cx - 15, cy - 15, cx + 15, cy + 15],
                     "class_id": int(rng.integers(0, 6)),
                     "conf": float(rng.uniform(0.2, 0.99))})
    dboxes = np.stack([centers[:, 0] - 80, centers[:, 1] - 80,
                       centers[:, 0] + 80, centers[:, 1] + 80], axis=1)
    for _ in range(10):  # warmup
        cg.union_of_disks_gate(dets, centers, diff_boxes=dboxes)
        cg.cross_crop_nms(dets)
    tg, tn = [], []
    for _ in range(200):
        t0 = time.perf_counter()
        cg.union_of_disks_gate(dets, centers, diff_boxes=dboxes)
        tg.append((time.perf_counter() - t0) * 1000.0)
        t0 = time.perf_counter()
        cg.cross_crop_nms(dets)
        tn.append((time.perf_counter() - t0) * 1000.0)
    p95g, p95n = float(np.percentile(tg, 95)), float(np.percentile(tn, 95))
    print(f"\n[GAT-04] gating p95={p95g:.3f}ms mean={np.mean(tg):.3f}ms | "
          f"nms p95={p95n:.3f}ms mean={np.mean(tn):.3f}ms")
    assert p95g < 1.0, f"gating P95 {p95g:.3f}ms >= 1ms"
    assert p95n < 1.0, f"NMS P95 {p95n:.3f}ms >= 1ms"


# ---------------------------------------------------------------------------
# T-GAT-05 — margin-gate ablation follows H_C + Δ=40 formula
# ---------------------------------------------------------------------------

def test_margin_gate_ablation_exists():
    assert cg.MARGIN_DELTA == 40.0
    src = inspect.getsource(cg.margin_gate_ablation)
    assert "MARGIN_DELTA" in inspect.getsource(cg) or "delta" in src
    dboxes = np.array([[0.0, 0.0, 10.0, 10.0], [100.0, 100.0, 120.0, 110.0]])
    # H_C = [-40,-40,160,150]
    inside = {"bbox": [0.0, 0.0, 5.0, 5.0], "class_id": 0, "conf": 0.9}
    edge = {"bbox": [150.0, 140.0, 170.0, 160.0],
            "class_id": 0, "conf": 0.9}  # IoU>0 with H_C, center outside
    outside = {"bbox": [500.0, 500.0, 520.0, 520.0],
               "class_id": 0, "conf": 0.9}
    kept = cg.margin_gate_ablation(
        [dict(inside), dict(edge), dict(outside)], dboxes)
    kb = [d["bbox"] for d in kept]
    assert inside["bbox"] in kb and edge["bbox"] in kb
    assert outside["bbox"] not in kb


# ---------------------------------------------------------------------------
# T-GAT-06 — cross-crop NMS dedups bisected overlap (IoU=0.75 → 1 box)
# ---------------------------------------------------------------------------

def test_cross_crop_nms_deduplicates_bisected_overlap():
    assert cg.CROSS_CROP_NMS_IOU == 0.50
    # IoU exactly 0.75 pair: (100-x)/(100+x) = 0.75 → x = 25/1.75
    x = 25.0 / 1.75
    a = {"bbox": [0.0, 0.0, 100.0, 100.0], "class_id": 0, "conf": 0.9}
    b = {"bbox": [x, 0.0, 100.0 + x, 100.0], "class_id": 0, "conf": 0.7}
    iou = cg._iou_matrix(np.array([a["bbox"]]), np.array([b["bbox"]]))[0, 0]
    assert iou == pytest.approx(0.75, abs=1e-6)
    out = cg.cross_crop_nms([dict(a), dict(b)], iou_thres=0.50)
    assert len(out) == 1 and out[0]["conf"] == pytest.approx(0.9)
    # class-aware: different class → NOT merged
    b2 = dict(b)
    b2["class_id"] = 4
    out2 = cg.cross_crop_nms([dict(a), b2], iou_thres=0.50)
    assert len(out2) == 2, "NMS must be class-aware (no cross-class kill)"
    # non-overlapping same class → both kept
    c = {"bbox": [500.0, 500.0, 560.0, 560.0], "class_id": 0, "conf": 0.8}
    out3 = cg.cross_crop_nms([dict(a), dict(c)], iou_thres=0.50)
    assert len(out3) == 2
    # empty → empty, no crash
    assert cg.cross_crop_nms([]) == []
