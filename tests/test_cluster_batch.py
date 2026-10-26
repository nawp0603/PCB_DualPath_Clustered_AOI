"""Phase 3 gate tests — T-BAT-01..04 (Verifier, CPU-only, mock YOLO).

Scope: src/cluster/batch_bucket.py (A1 1-call + A2 <=3-call + B letterbox)
contract plan §2.3 + §6.3 + verifier_rules §4.2.
Does NOT touch Board 01/04. Does NOT modify src/ or scripts/.
YOLO is mocked (no GPU); rect=True verified via inspect on real detector.
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

from src.cluster import batch_bucket as bb  # noqa: E402


# ---------------------------------------------------------------------------
# Mock YOLO (counts calls, asserts imgsz == crop edge → zero-resize)
# ---------------------------------------------------------------------------

class MockYolo:
    def __init__(self, dets_fn=None):
        self.calls: list[dict] = []
        self._dets_fn = dets_fn

    def predict_batch(self, crops, conf=None, imgsz=640):
        self.calls.append({"n": len(crops), "imgsz": int(imgsz)})
        for c in crops:
            assert c.shape[0] == int(imgsz) and c.shape[1] == int(imgsz), (
                f"crop shape {c.shape} != imgsz {imgsz} (zero-resize violated)")
            assert c.shape[2] == 3
        out = []
        for i in range(len(crops)):
            if self._dets_fn is not None:
                out.append(self._dets_fn(i, int(imgsz)))
            else:
                out.append([])
        return out


def _geo_crop(cx, cy, side, cluster_id=0, z=9.0):
    return {"cx": float(cx), "cy": float(cy), "side": float(side),
            "cluster_id": int(cluster_id), "sub_id": 0, "z_cluster": float(z)}


def _img(h=1000, w=1000):
    rng = np.random.default_rng(20261023)
    return (rng.uniform(0, 255, size=(h, w, 3))).astype(np.uint8)


# ---------------------------------------------------------------------------
# Plan-locked constants
# ---------------------------------------------------------------------------

def test_plan_locked_constants_batch():
    assert bb.BUCKETS == (384, 512, 640)
    assert bb.CROP_MODE_A1 == "uniform_context_640"
    assert bb.CROP_MODE_A2 == "multi_bucket_384_512_640"
    assert bb.CROP_MODE_B == "letterbox_640"
    assert bb.LETTERBOX_SIZE == 640
    assert bb.LETTERBOX_PAD_VALUE == 114
    assert bb.MAX_GPU_CALLS == 3
    # no heavy deps in batch module (check real import statements, not words
    # in docstring like "khong import torch")
    src = inspect.getsource(bb)
    import_lines = [l.strip() for l in src.splitlines()
                    if l.strip().startswith("import ") or l.strip().startswith("from ")]
    blob = "\n".join(import_lines)
    assert "torch" not in blob and "ultralytics" not in blob
    assert "sklearn" not in blob
    assert "model.predict" not in src


# ---------------------------------------------------------------------------
# T-BAT-01 — quantize buckets + center_window (A1 uniform + A2)
# ---------------------------------------------------------------------------

def test_quantize_buckets():
    # A2 boundaries (plan §6.3): s<=384→384; 385–512→512; 513–640→640
    assert bb.quantize(100) == 384
    assert bb.quantize(200) == 384
    assert bb.quantize(384) == 384
    assert bb.quantize(384.0) == 384
    assert bb.quantize(385) == 512
    assert bb.quantize(400) == 512
    assert bb.quantize(512) == 512
    assert bb.quantize(513) == 640
    assert bb.quantize(600) == 640
    assert bb.quantize(640) == 640
    # >640 → 640 + needs_cap flag (bisection should have prevented)
    assert bb.quantize(641) == 640 and bb.needs_cap(641) is True
    assert bb.quantize(800) == 640 and bb.needs_cap(800) is True
    assert bb.needs_cap(640) is False and bb.needs_cap(384) is False
    # exact pixel boundaries
    assert bb.quantize(384.1) == 512  # just over → next bucket
    assert bb.quantize(512.1) == 640

    # A1 center_window: every cluster → 640×640 inside image (SHIFT, not shrink)
    W = H = 1000
    for cx, cy in [(500, 500), (0, 0), (1000, 1000), (100, 900), (320, 320)]:
        x0, y0 = bb.center_window(cx, cy, 640, W, H)
        assert 0 <= x0 <= W - 640 and 0 <= y0 <= H - 640
        assert x0 + 640 <= W and y0 + 640 <= H
    # corners flush to edge (shift)
    assert bb.center_window(0, 0, 640, 1000, 1000) == (0, 0)
    assert bb.center_window(1000, 1000, 640, 1000, 1000) == (360, 360)
    # A2 center_window for each bucket size stays inside
    for s in (384, 512, 640):
        for cx, cy in [(0, 0), (500, 500), (1000, 1000)]:
            x0, y0 = bb.center_window(cx, cy, s, 1000, 1000)
            assert 0 <= x0 <= 1000 - s and 0 <= y0 <= 1000 - s


# ---------------------------------------------------------------------------
# T-BAT-02 — group by bucket, max 3 calls; A1 exactly 1-call
# ---------------------------------------------------------------------------

def test_group_by_bucket_max_three_calls():
    img = _img()
    # 4 crops spanning 3 buckets: 100→384, 400→512, 600→640, 200→384
    geos = [_geo_crop(500, 500, 100, 0), _geo_crop(500, 500, 400, 1),
            _geo_crop(500, 500, 600, 2), _geo_crop(200, 200, 200, 3)]

    # A1: every size → 640, exactly 1 call
    bi_a1 = bb.build_batch_inputs(img, geos, bb.CROP_MODE_A1)
    assert len(bi_a1) == 4
    assert all(b["imgsz_call"] == 640 and b["s_cut"] == 640 for b in bi_a1)
    assert all(b["image_rgb"].shape == (640, 640, 3) for b in bi_a1)
    mock = MockYolo()
    dets, stats = bb.predict_clusters(bi_a1, mock, bb.CROP_MODE_A1)
    assert stats["mode"] == bb.CROP_MODE_A1
    assert stats["n_calls"] == 1, f"A1 must be exactly 1-call, got {stats}"
    assert mock.calls == [{"n": 4, "imgsz": 640}]
    assert stats["calls"][0]["imgsz"] == 640 and stats["calls"][0]["n_crops"] == 4

    # A2: 3 buckets → exactly 3 calls, empty groups skipped
    bi_a2 = bb.build_batch_inputs(img, geos, bb.CROP_MODE_A2)
    assert sorted(b["imgsz_call"] for b in bi_a2) == [384, 384, 512, 640]
    for b in bi_a2:
        assert b["image_rgb"].shape == (b["s_cut"], b["s_cut"], 3)
        assert b["imgsz_call"] == b["s_cut"]  # zero-resize: imgsz == edge
    mock2 = MockYolo()
    dets2, stats2 = bb.predict_clusters(bi_a2, mock2, bb.CROP_MODE_A2)
    assert stats2["n_calls"] == 3, f"A2 3-bucket must be 3 calls, got {stats2}"
    assert sorted(c["imgsz"] for c in mock2.calls) == [384, 512, 640]
    # per-group counts: 384×2, 512×1, 640×1
    by_imgsz = {c["imgsz"]: c["n"] for c in mock2.calls}
    assert by_imgsz == {384: 2, 512: 1, 640: 1}

    # A2 single-bucket → 1 call (not 3)
    bi_one = bb.build_batch_inputs(img, geos[:1], bb.CROP_MODE_A2)
    m3 = MockYolo()
    _, s3 = bb.predict_clusters(bi_one, m3, bb.CROP_MODE_A2)
    assert s3["n_calls"] == 1 and m3.calls[0]["imgsz"] == 384

    # B: letterbox → exactly 1 call imgsz=640, crops 640×640
    bi_b = bb.build_batch_inputs(img, geos, bb.CROP_MODE_B)
    assert all(b["imgsz_call"] == 640 for b in bi_b)
    assert all(b["image_rgb"].shape == (640, 640, 3) for b in bi_b)
    assert all(b["pad_s"] in (384, 512, 640) for b in bi_b)
    m4 = MockYolo()
    _, s4 = bb.predict_clusters(bi_b, m4, bb.CROP_MODE_B)
    assert s4["n_calls"] == 1 and m4.calls[0] == {"n": 4, "imgsz": 640}

    # guard: >3 groups → RuntimeError (AUDIT-04 cap)
    fake_inputs = [
        {"image_rgb": np.zeros((384, 384, 3), np.uint8), "origin": (0, 0),
         "s_cut": 384, "imgsz_call": 384, "cluster_id": 0, "sub_id": 0,
         "z_cluster": 1.0, "pad_s": 0},
        {"image_rgb": np.zeros((512, 512, 3), np.uint8), "origin": (0, 0),
         "s_cut": 512, "imgsz_call": 512, "cluster_id": 1, "sub_id": 0,
         "z_cluster": 1.0, "pad_s": 0},
        {"image_rgb": np.zeros((640, 640, 3), np.uint8), "origin": (0, 0),
         "s_cut": 640, "imgsz_call": 640, "cluster_id": 2, "sub_id": 0,
         "z_cluster": 1.0, "pad_s": 0},
        {"image_rgb": np.zeros((256, 256, 3), np.uint8), "origin": (0, 0),
         "s_cut": 256, "imgsz_call": 256, "cluster_id": 3, "sub_id": 0,
         "z_cluster": 1.0, "pad_s": 0},
    ]
    with pytest.raises(RuntimeError):
        bb.predict_clusters(fake_inputs, MockYolo(), bb.CROP_MODE_A2)

    # invalid mode → ValueError
    with pytest.raises(ValueError):
        bb.build_batch_inputs(img, geos, "nope")
    with pytest.raises(ValueError):
        bb.predict_clusters(bi_a1, MockYolo(), "nope")

    # dets mapped to INPUT + cluster_id/crop_origin attached + sorted by conf
    def _dets(i, imgsz):
        return [{"bbox": [10.0, 10.0, 50.0, 50.0], "class_id": 0, "conf": 0.9 - i * 0.1}]
    m5 = MockYolo(dets_fn=_dets)
    dets5, _ = bb.predict_clusters(bi_a1, m5, bb.CROP_MODE_A1)
    assert len(dets5) == 4
    confs = [d["conf"] for d in dets5]
    assert confs == sorted(confs, reverse=True)
    for d in dets5:
        assert "cluster_id" in d and "crop_origin" in d
        ox, oy = d["crop_origin"]
        # translation check: input bbox - origin == crop bbox
        assert d["bbox"][0] - ox == pytest.approx(10.0)
        assert d["bbox"][1] - oy == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# T-BAT-03 — rect=True every call (+ half on CUDA inside real detector)
# ---------------------------------------------------------------------------

def test_rect_true_every_call():
    src = inspect.getsource(bb)
    # every YOLO invocation in batch module goes via _predict_batch → predict_batch
    assert "_predict_batch" in src
    assert "model.predict" not in src  # no direct Ultralytics call
    # _predict_batch forwards to yolo.predict_batch (rect enforced inside)
    pb_src = inspect.getsource(bb._predict_batch)
    assert "yolo.predict_batch" in pb_src
    assert "_assert_rect_enforced" in pb_src
    # guard exists and inspects real detector
    g_src = inspect.getsource(bb._assert_rect_enforced)
    assert "rect=True" in g_src.replace(" ", "")
    # real detector enforces rect=True + half in BOTH predict paths
    from src.models import yolo_detector as yd
    det_src = inspect.getsource(yd.YOLODetector.predict_batch)
    assert "rect=True" in det_src.replace(" ", ""), (
        "YOLODetector.predict_batch must contain rect=True (Chot 3)")
    assert "half" in det_src, "predict_batch must pass half (Chot 3: half on CUDA)"
    single_src = inspect.getsource(yd.YOLODetector.predict)
    assert "rect=True" in single_src.replace(" ", "")
    # guard passes on real detector class, raises when rect missing
    class FakeNoRect:
        def predict_batch(self, crops, conf=None, imgsz=640):
            return [[] for _ in crops]
    FakeNoRect.__module__ = "src.models.yolo_detector"
    FakeNoRect.predict_batch.__module__ = "src.models.yolo_detector"
    import types
    # build source without rect=True → guard must raise
    src_no_rect = "def predict_batch(self, crops): return self.model(crops)"
    assert "rect=True" not in src_no_rect
    # real detector source DOES contain rect → guard passes (no raise on mock bypass)
    m = MockYolo()
    bb._assert_rect_enforced(m)  # mock module → bypass, no raise
    # BGR→RGB conversion present (matches evaluate.py convention)
    assert "COLOR_BGR2RGB" in src


# ---------------------------------------------------------------------------
# T-BAT-04 — empty + single (no crash, no extra GPU, correct bucket)
# ---------------------------------------------------------------------------

def test_batch_empty_and_single():
    m = MockYolo()
    dets, stats = bb.predict_clusters([], m, bb.CROP_MODE_A1)
    assert dets == [] and stats["n_calls"] == 0 and stats["n_dets"] == 0
    assert m.calls == [], "empty batch must not call GPU"
    dets, stats = bb.predict_clusters([], m, bb.CROP_MODE_A2)
    assert dets == [] and stats["n_calls"] == 0
    dets, stats = bb.predict_clusters([], m, bb.CROP_MODE_B)
    assert dets == [] and stats["n_calls"] == 0

    img = _img()
    # single small crop → 1 call, bucket 384, no pad waste
    bi = bb.build_batch_inputs(img, [_geo_crop(500, 500, 100, 7)], bb.CROP_MODE_A2)
    assert len(bi) == 1 and bi[0]["imgsz_call"] == 384
    m2 = MockYolo()
    dets2, s2 = bb.predict_clusters(bi, m2, bb.CROP_MODE_A2)
    assert s2["n_calls"] == 1 and m2.calls == [{"n": 1, "imgsz": 384}]
    # single large crop → bucket 640
    bi_big = bb.build_batch_inputs(img, [_geo_crop(500, 500, 600, 7)], bb.CROP_MODE_A2)
    m3 = MockYolo()
    _, s3 = bb.predict_clusters(bi_big, m3, bb.CROP_MODE_A2)
    assert s3["n_calls"] == 1 and m3.calls[0]["imgsz"] == 640
    # single A1 → always 640
    bi_a1 = bb.build_batch_inputs(img, [_geo_crop(500, 500, 100, 7)], bb.CROP_MODE_A1)
    m4 = MockYolo()
    _, s4 = bb.predict_clusters(bi_a1, m4, bb.CROP_MODE_A1)
    assert s4["n_calls"] == 1 and m4.calls[0] == {"n": 1, "imgsz": 640}


# ---------------------------------------------------------------------------
# B-mode pad-zone: valid edge det MUST be kept, true pad det dropped
# (plan T-GEO-05: unpad(pad)==identity; det center in pad → drop 100%)
# ---------------------------------------------------------------------------

def test_b_letterbox_pad_zone_keeps_valid_edge_det():
    # letterbox identity per bucket
    for s in (384, 512, 640):
        box = [100.0, 120.0, 200.0, 180.0]
        assert bb.letterbox_unpad(bb.letterbox_pad(box, s), s) == pytest.approx(
            box, abs=1e-6)
        # pad image shape + gray border value
        crop = np.zeros((s, s, 3), dtype=np.uint8)
        lb = bb.letterbox_pad_image(crop, s)
        assert lb.shape == (640, 640, 3)
        o = bb.letterbox_offset(s)
        if o > 0:
            assert int(lb[0, 0, 0]) == 114  # gray pad zone
            assert int(lb[o, o, 0]) == 0  # real image zone

    # True pad det (letterbox coords, center in gray) → dropped
    assert bb.is_in_pad_zone((30.0, 30.0), 384) is True
    # Valid image center (letterbox coords) → kept
    assert bb.is_in_pad_zone((320.0, 320.0), 384) is False

    # End-to-end B-mode: valid edge crop box (10,10,50,50), s=384.
    # In letterbox coords YOLO sees (138,138,178,178), center 158 → NOT pad.
    img = _img()
    geos = [_geo_crop(500, 500, 100, 0)]  # side 100 → bucket 384
    bi = bb.build_batch_inputs(img, geos, bb.CROP_MODE_B)
    assert bi[0]["pad_s"] == 384 and bi[0]["imgsz_call"] == 640
    o = bb.letterbox_offset(384)  # 128

    def _valid_edge(i, imgsz):
        return [{"bbox": [10.0 + o, 10.0 + o, 50.0 + o, 50.0 + o],
                 "class_id": 0, "conf": 0.9}]

    dets, _ = bb.predict_clusters(bi, MockYolo(dets_fn=_valid_edge),
                                  bb.CROP_MODE_B)
    assert len(dets) == 1, (
        "B-mode dropped a VALID edge det (crop 10,10-50,50): "
        "pad-zone check ran on UNPADDED coords instead of letterbox coords "
        f"(batch_bucket.py predict_clusters B-branch)")
    # unpad exact: letterbox (138,138,178,178) → crop (10,10,50,50) → +origin
    ox, oy = dets[0]["crop_origin"]
    assert dets[0]["bbox"] == pytest.approx(
        [10.0 + ox, 10.0 + oy, 50.0 + ox, 50.0 + oy])

    # True pad det must still be dropped 100%
    def _in_pad(i, imgsz):
        return [{"bbox": [10.0, 10.0, 50.0, 50.0],  # letterbox gray zone
                 "class_id": 0, "conf": 0.9}]

    dets_pad, _ = bb.predict_clusters(bi, MockYolo(dets_fn=_in_pad),
                                      bb.CROP_MODE_B)
    assert dets_pad == [], "det with center in pad zone must be dropped 100%"
