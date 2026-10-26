"""Phase 4 gate tests — T-E2E-02/03/05 real pipeline (Verifier, CPU-only).

Scope: scripts/evaluate_clustered.py contract plan §4 Phase 4 + §5.4
(fallback_single bit-identical, exception → TOP-8 + fallback_log.jsonl,
NMS after gating before fuse, six-class Board07 smoke, zero-leakage,
GOOD 01/04 display-only).
Does NOT touch Board 01/04. Does NOT modify src/ or scripts/.
YOLO is mocked (no GPU); Board07 images are READ-ONLY legacy.
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import scripts.evaluate_clustered as ev  # noqa: E402
import src.reference.board_id_and_align as ba  # noqa: E402
from configs.resource_paths import assert_writable_ok  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockYolo:
    """Minimal YOLO double: counts calls, returns canned dets per crop."""

    def __init__(self, dets_fn=None):
        self.calls: list[dict] = []
        self._dets_fn = dets_fn

    def predict_batch(self, crops, conf=None, imgsz=640):
        self.calls.append({"n": len(crops), "imgsz": int(imgsz)})
        out = []
        for i in range(len(crops)):
            if self._dets_fn is not None:
                out.append(self._dets_fn(i, int(imgsz)))
            else:
                out.append([])
        return out


def _img(h=1000, w=1000):
    rng = np.random.default_rng(20261023)
    return (rng.uniform(0, 255, size=(h, w, 3))).astype(np.uint8)


def _blob(cx, cy, z, half=10.0):
    return {"centroid_tpl": [float(cx), float(cy)], "z_diff": float(z),
            "area": 200.0,
            "bbox_tpl": [float(cx - half), float(cy - half),
                         float(cx + half), float(cy + half)]}


def _fake_align_factory(warped_shape=(800, 800)):
    h, w = warped_shape
    warped = np.zeros((h, w, 3), dtype=np.uint8)

    def _fake(image_bgr, cache=None, fast=True):
        return {"board_id": "07", "H": np.eye(3), "H_inv": np.eye(3),
                "inliers": 100, "warped": warped,
                "valid_mask": None, "mode": "fast"}

    return _fake


def _fake_branch_factory(blobs):
    def _fake(warped, tpl, top_k=8, valid_mask=None):
        return {"blobs": list(blobs),
                "diff_map": np.zeros((800, 800), dtype=np.float32),
                "structure_mask": np.zeros((800, 800), dtype=bool)}
    return _fake


# ---------------------------------------------------------------------------
# T-E2E-02 — fallback_single bit-identical vs legacy TOP-8
# ---------------------------------------------------------------------------

def test_e2e02_fallback_single_bit_identical(monkeypatch):
    blobs = [_blob(400.0, 400.0, 9.0)]
    monkeypatch.setattr(ev.ba, "identify_and_align",
                        _fake_align_factory())
    monkeypatch.setattr(ev.td, "run_branch_b",
                        _fake_branch_factory(blobs))

    def _center_det(i, imgsz):
        # det centred at blob in crop coords: crop origin for (400,400)
        # on 1000x1000 is (80,80); crop-centre (320,320) == input (400,400)
        return [{"bbox": [310.0, 310.0, 330.0, 330.0],
                 "class_id": 0, "class_name": "open_circuit", "conf": 0.9}]

    yolo = MockYolo(dets_fn=_center_det)
    image = _img()
    out = ev.run_clustered_single(image, yolo, {}, {}, mode="uniform_context_640",
                                  conf=0.20, image_id="e2e02_single",
                                  log_fallback=False)
    assert out["fallback_single"] is True
    assert out["fallback_reason"] == "fallback_single_1blob"
    assert out["n_blobs"] == 1 and out["n_crops"] == 1
    # bit-identical vs direct legacy path on same blobs/H_inv
    h_inv = np.eye(3)
    yolo2 = MockYolo(dets_fn=_center_det)
    legacy = ev.run_legacy_branch_b_path(image, out["blobs"], h_inv, yolo2,
                                         conf=0.20)
    assert out["yolo_dets"] == legacy, (
        "fallback_single must be bit-identical to run_legacy_branch_b_path")
    # legacy dets carry cluster_id -1 + crop_origin, coords in Input space
    H, W = image.shape[:2]
    for d in out["yolo_dets"]:
        assert d["cluster_id"] == -1
        assert "crop_origin" in d
        x1, y1, x2, y2 = d["bbox"]
        assert 0 <= x1 < x2 <= W and 0 <= y1 < y2 <= H
    # conclusion still in 4 levels
    assert out["result"]["conclusion"] in ("FAIL", "REWORK", "REVIEW", "PASS")


# ---------------------------------------------------------------------------
# T-E2E-03 — cluster exception → fallback TOP-8 + log schema
# ---------------------------------------------------------------------------

def test_e2e03_cluster_exception_fallback_and_log_schema(monkeypatch, tmp_path=None):
    blobs = [_blob(100.0, 100.0, 9.0), _blob(300.0, 300.0, 8.0)]
    monkeypatch.setattr(ev.ba, "identify_and_align",
                        _fake_align_factory())
    monkeypatch.setattr(ev.td, "run_branch_b",
                        _fake_branch_factory(blobs))

    def _boom(*a, **k):
        raise RuntimeError("boom-cluster")

    monkeypatch.setattr(ev, "cluster_blobs", _boom)
    yolo = MockYolo()
    image = _img()
    scratch = PROJECT_ROOT / "results" / "calibration" / "_verify_tmp_p4" / "fb.jsonl"
    if scratch.exists():
        scratch.unlink()
    out = ev.run_clustered_single(image, yolo, {}, {}, mode="uniform_context_640",
                                  conf=0.20, image_id="e2e03_exc",
                                  log_fallback=True,
                                  fallback_log_path=scratch)
    assert out["fallback_single"] is False
    assert str(out["fallback_reason"]).startswith("cluster_exception_RuntimeError")
    assert out["result"]["conclusion"] in ("FAIL", "REWORK", "REVIEW", "PASS")
    # log schema {image_id, reason, n_blobs}
    assert scratch.exists(), "fallback_log.jsonl must be written on exception"
    rec = json.loads(scratch.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert set(rec.keys()) == {"image_id", "reason", "n_blobs"}
    assert rec["image_id"] == "e2e03_exc"
    assert "RuntimeError" in rec["reason"]
    assert rec["n_blobs"] == 2
    # cleanup scratch (keep results/ clean)
    try:
        scratch.unlink()
        parent = scratch.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Order: gating → NMS → fuse on true cluster path (not _do_legacy def)
# ---------------------------------------------------------------------------

def test_e2e_order_gating_nms_fuse_on_cluster_path():
    src = inspect.getsource(ev.run_clustered_single)
    cut = src[src.find("# (2)-(4)"):]
    assert "# (2)-(4)" in src, "cluster-path marker missing"
    i_clu = cut.find("cluster_blobs")
    i_build = cut.find("build_batch_inputs")
    i_pred = cut.find("predict_clusters")
    i_union = cut.find("union_of_disks_gate")
    i_nms = cut.find("cross_crop_nms")
    i_fuse = cut.find("fuse_and_decide")
    i_attach = cut.find("_attach_cluster_ids")
    assert all(v >= 0 for v in (i_clu, i_build, i_pred, i_union, i_nms, i_fuse, i_attach))
    assert i_clu < i_build < i_pred < i_union < i_nms < i_fuse < i_attach, (
        "true cluster path must be cluster→build→predict→U-gate→NMS→fuse→attach")
    # require_overlap=True on union gate (plan §2.4.2 + T3 adapter)
    assert "require_overlap=True" in cut
    # frozen params (no hardcode) + caps
    assert "load_frozen_params" in src
    assert "max_cluster_crops=8" in src


# ---------------------------------------------------------------------------
# Leakage + sandbox guards for Phase-4 pipeline
# ---------------------------------------------------------------------------

def test_e2e_phase4_no_leakage_and_sandbox_guards():
    src = inspect.getsource(ev)
    assert "unseen" not in src.lower(), "banned term 'unseen' in pipeline"
    for banned in ("import torch", "from ultralytics", "import ultralytics",
                   "CUDA", "model.predict"):
        assert banned not in src, f"pipeline must not contain {banned!r}"
    assert ev.FORBIDDEN_TEST_BOARDS == {"01", "04"}
    assert ev.ALLOWED_CALIB_BOARDS == {"05", "06", "07", "08",
                                       "09", "10", "11", "12"}
    assert "assert_writable_ok" in src
    assert str(ev.FALLBACK_LOG).startswith(str(PROJECT_ROOT))
    # guards refuse 01/04
    with pytest.raises(RuntimeError):
        ev._assert_no_leakage({"01"})
    with pytest.raises(RuntimeError):
        ev._assert_no_leakage({"07", "04"})
    ev._assert_no_leakage({"07"})
    ev._assert_no_leakage({"05", "12"})
    # writes blocked outside project
    with pytest.raises(PermissionError):
        assert_writable_ok(PROJECT_ROOT.parent / "PCB_DualPath_AOI" / "evil.json")


# ---------------------------------------------------------------------------
# UI GOOD 01/04 display-only guard (no pipeline on Zero-defect-training Boards)
# ---------------------------------------------------------------------------

def test_e2e_ui_good_templates_display_only():
    import src.ui.app_clustered as app
    src = inspect.getsource(app)
    assert "is_good_template" in src
    assert "validate_input_image" in src
    # GOOD branch returns before pipeline entry point (call site in main,
    # not the `def run_clustered_demo(` line at top of file)
    i_guard = src.find("if is_good_template")
    i_run = src.find("out = run_clustered_demo(")
    assert 0 <= i_guard < i_run, (
        "GOOD 01/04 must return before run_clustered_demo (display-only)")
    tail = src[i_guard:i_run]
    assert "return" in tail
    # sample list keeps 6 Board07 + 2 GOOD display names
    assert "01.JPG" in src and "04.JPG" in src
    # resolve maps 01/04 to LOCAL_TEMPLATES (never LEGACY images)
    rsrc = inspect.getsource(app.resolve_sample_board07)
    assert "LOCAL_TEMPLATES" in rsrc


# ---------------------------------------------------------------------------
# T-E2E-05 — six-class Board07 smoke (real align+diff, mock YOLO, no 01/04)
# ---------------------------------------------------------------------------

def test_e2e05_six_class_board07_smoke():
    cache = ba.load_cache()
    items = ev.collect_clustered_items({"07"})
    assert len(items) >= 6, f"Board07 items too few: {len(items)}"
    assert {it["board"] for it in items} == {"07"}
    six = ev.six_class_samples(items)
    assert len(six) == 6, f"six_class_samples must cover 6 classes, got {len(six)}"
    got_classes = sorted({c for s in six for c, _ in s["gt"]})
    assert got_classes == [0, 1, 2, 3, 4, 5], f"must cover 6 classes, got {got_classes}"

    def _centre(i, imgsz):
        c = float(imgsz) / 2.0
        return [{"bbox": [c - 20, c - 20, c + 20, c + 20],
                 "class_id": 5, "class_name": "missing_hole", "conf": 0.9}]

    templates: dict = {}
    for s in six:
        img = ev.read_bgr(s["path"])
        H, W = img.shape[:2]
        yolo = MockYolo(dets_fn=_centre)
        out = ev.run_clustered_single(img, yolo, cache, templates,
                                      mode="uniform_context_640",
                                      conf=0.20,
                                      image_id=Path(s["path"]).name,
                                      log_fallback=False)
        assert out["board_id"] == "07"
        assert out["result"]["conclusion"] in ("FAIL", "REWORK", "REVIEW", "PASS")
        assert out["n_crops"] <= 10, (
            f"cap MAX_CLUSTER_CROPS=8 + MAX_FRAG_RESCUE_SLOTS=2 (D1-sua), "
            f"got {out['n_crops']}")
        bs = out.get("batch_stats", {})
        assert int(bs.get("n_calls", 0)) <= 3, f"GPU calls cap 3, got {bs}"
        # Input-space coords for every det
        for d in out.get("yolo_dets", []):
            x1, y1, x2, y2 = [float(v) for v in d["bbox"]]
            assert 0 <= x1 < x2 <= W and 0 <= y1 < y2 <= H, (
                f"det out of Input bounds: {d['bbox']} on {W}x{H}")
            assert "cluster_id" in d
        # NMS never inflates count
        assert out["n_dets_after_nms"] <= out["n_dets_before_gating"]
