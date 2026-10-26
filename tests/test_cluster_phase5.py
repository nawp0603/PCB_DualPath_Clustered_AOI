"""Phase 5 gate tests — T-E2E Phase 5 (Verifier, seal-gated, 01/04 audit).

Scope: plan §4 Phase 5 + §5.1/§5.2 + verifier_rules §4.3.
- verify_phase5_seal() MATCH truoc moi thao tac 01/04.
- Ma tran 5-config dung spec (Config0 legacy; C1 A1+U; C2 A2+U; C3 B+U; C4 A1+H).
- allow_test gate lan toa (khong allow -> tu choi 01/04).
- Config0 bit-identical legacy + shape tuong thich de benchmark.
- GOOD items chi tu LOCAL_TEMPLATES (khong doc LEGACY images).
- perturb parity vs project goc (cung seed 20261023 + cung ops).
- Baseline crop/gating parity vs run_dualpath goc (evaluate.py:657-678).

Mo 01/04 o day la hop le: Phase 5 da mo test sau seal MATCH (plan §4 Phase 5),
chi doc READ-ONLY 1 anh de kiem guard + mock YOLO (khong GPU, khong calib).
Does NOT modify src/ or scripts/. Does NOT write results/.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import scripts.evaluate_clustered as ev  # noqa: E402
import scripts.blob_stats as bs  # noqa: E402
import src.reference.board_id_and_align as ba  # noqa: E402
from configs.resource_paths import LOCAL_TEMPLATES  # noqa: E402

ORIG_EVALUATE = (PROJECT_ROOT.parent / "PCB_DualPath_AOI"
                 / "scripts" / "evaluate.py")


class MockYolo:
    def predict_batch(self, crops, conf=None, imgsz=640):
        return [[] for _ in range(len(crops))]


# ---------------------------------------------------------------------------
# 1. Seal MATCH (doc lap recompute)
# ---------------------------------------------------------------------------

def test_phase5_seal_match_independent():
    import json
    from scripts.calibrate_dbscan import (
        VOLATILE_REPORT_KEYS,
        frozen_sha256_of,
        pipeline_hashes,
    )
    from src.cluster.dbscan_clustering import CALIB_REPORT
    body_full = json.loads(CALIB_REPORT.read_text(encoding="utf-8"))
    stored = str(body_full["frozen_sha256"])
    pipe = pipeline_hashes()
    body = {k: v for k, v in body_full.items()
            if k not in VOLATILE_REPORT_KEYS}
    calc = frozen_sha256_of(body, pipe)
    assert calc == stored == "8d4baa3ea11c683e3e18879f1e08a9dc97139e0def667ae4d8abab4830b514d9"
    seal = ev.verify_phase5_seal()
    assert seal["eps"] == 28.0
    assert seal["min_samples"] == 1
    assert seal["k_raw"] == 12
    assert seal["pipe_match"] is True


# ---------------------------------------------------------------------------
# 2. Ma tran 5-config dung spec plan §4 Phase 5
# ---------------------------------------------------------------------------

def test_phase5_matrix_contract():
    assert ev.GATING_MODES == ("U", "H")
    assert sorted(ev.PHASE5_CONFIGS) == [
        "config0_baseline", "config1_proposed", "config2_bucket",
        "config3_gating_h" if False else "config3_letterbox",
        "config4_gating_h",
    ]
    c0 = ev.PHASE5_CONFIGS["config0_baseline"]
    assert c0 == {"kind": "legacy", "mode": "legacy_top8",
                  "gating": "single"}
    assert ev.PHASE5_CONFIGS["config1_proposed"]["mode"] == "uniform_context_640"
    assert ev.PHASE5_CONFIGS["config1_proposed"]["gating"] == "U"
    assert ev.PHASE5_CONFIGS["config2_bucket"]["mode"] == "multi_bucket_384_512_640"
    assert ev.PHASE5_CONFIGS["config2_bucket"]["gating"] == "U"
    assert ev.PHASE5_CONFIGS["config3_letterbox"]["mode"] == "letterbox_640"
    assert ev.PHASE5_CONFIGS["config3_letterbox"]["gating"] == "U"
    assert ev.PHASE5_CONFIGS["config4_gating_h"]["mode"] == "uniform_context_640"
    assert ev.PHASE5_CONFIGS["config4_gating_h"]["gating"] == "H"
    # Moi mode trong CROP_MODES (tru legacy_top8 cua baseline)
    from src.cluster.batch_bucket import CROP_MODES
    for name, cfg in ev.PHASE5_CONFIGS.items():
        if cfg["kind"] == "legacy":
            continue
        assert cfg["mode"] in CROP_MODES, name
        assert cfg["gating"] in ev.GATING_MODES, name
    # Dispatcher tu choi config la + gating la
    with pytest.raises(ValueError):
        ev.run_config_single("configX", np.zeros((64, 64, 3), np.uint8),
                             MockYolo(), {}, {})
    with pytest.raises(ValueError):
        ev.run_clustered_single(np.zeros((64, 64, 3), np.uint8),
                                MockYolo(), {}, {}, gating="X")


# ---------------------------------------------------------------------------
# 3. allow_test gate lan toa — khong allow thi tu choi 01/04
# ---------------------------------------------------------------------------

def test_phase5_leak_gate_without_allow():
    # collect mat dinh (allow_test=False) tu choi 01/04, chap nhan 07
    with pytest.raises(RuntimeError):
        ev.collect_clustered_items({"01"})
    with pytest.raises(RuntimeError):
        ev.collect_clustered_items({"07", "04"})
    ok = ev.collect_clustered_items({"07"})
    assert len(ok) > 0 and {i["board"] for i in ok} == {"07"}
    with pytest.raises(RuntimeError):
        ev.collect_good_items_clustered({"01"})
    # khong mo duoc test khi seal vo: monkeypatch seal -> MISMATCH
    import scripts.evaluate_clustered as _ev
    orig = _ev.verify_phase5_seal
    try:
        def _boom():
            raise RuntimeError("[FROZEN SEAL] MISMATCH (test)")
        _ev.verify_phase5_seal = _boom
        with pytest.raises(RuntimeError):
            _ev.collect_clustered_items({"01", "04"}, allow_test=True)
    finally:
        _ev.verify_phase5_seal = orig


def test_phase5_pipeline_refuses_01_without_allow():
    # 1 anh 01/04 that READ-ONLY + mock YOLO: pipeline phai raise
    # [ZERO LEAKAGE] khi allow_test=False (kiem guard sau align that).
    seal = ev.verify_phase5_seal()
    assert seal["pipe_match"] is True
    items = ev.collect_clustered_items({"01", "04"}, allow_test=True)
    assert len(items) == 36, f"01/04 phai co 36 anh, got {len(items)}"
    img = ev.read_bgr(items[0]["path"])
    cache = ba.load_cache()
    with pytest.raises(RuntimeError, match="ZERO LEAKAGE"):
        ev.run_config_single("config1_proposed", img, MockYolo(), cache, {},
                             conf=0.20, image_id="guard01",
                             log_fallback=False, allow_test=False)


# ---------------------------------------------------------------------------
# 4. Config0 baseline: bit-identical legacy + shape tuong thich benchmark
# ---------------------------------------------------------------------------

def test_baseline_bit_identical_and_shape():
    items = ev.collect_clustered_items({"07"})
    img = ev.read_bgr(items[0]["path"])
    cache = ba.load_cache()
    yolo = MockYolo()
    base = ev.run_baseline_single(img, yolo, cache, {}, conf=0.20,
                                  image_id="bshape", log_fallback=False)
    assert base["mode"] == "legacy_top8" and base["gating"] == "single"
    assert base["board_id"] == "07"
    # shape tuong thich run_clustered_single (de benchmark so sanh duoc)
    clu = ev.run_clustered_single(img, MockYolo(), cache, {}, conf=0.20,
                                  image_id="cshape", log_fallback=False)
    for k in ("board_id", "blobs", "n_blobs", "batch_stats", "n_crops",
              "yolo_dets", "result", "t_total_ms", "n_dets_before_gating",
              "n_dets_after_gating", "n_dets_after_nms"):
        assert k in base and k in clu, f"thieu key {k}"
    assert base["result"]["conclusion"] in ("FAIL", "REWORK", "REVIEW", "PASS")
    # bit-identical vs duong legacy truc tiep (cung blobs + H_inv)
    import numpy as _np
    h_inv = _np.asarray(base["H_inv"], dtype=_np.float64)
    yolo2 = MockYolo()
    legacy = ev.run_legacy_branch_b_path(img, base["blobs"], h_inv, yolo2,
                                         conf=0.20)
    assert base["yolo_dets"] == legacy
    assert base["n_crops"] == min(len(base["blobs"]), 8)


def test_config_dispatcher_all_five_mock():
    items = ev.collect_clustered_items({"07"})
    img = ev.read_bgr(items[0]["path"])
    cache = ba.load_cache()
    expect = {
        "config0_baseline": ("legacy_top8", "single"),
        "config1_proposed": ("uniform_context_640", "U"),
        "config2_bucket": ("multi_bucket_384_512_640", "U"),
        "config3_letterbox": ("letterbox_640", "U"),
        "config4_gating_h": ("uniform_context_640", "H"),
    }
    for cfg, (mode, gating) in expect.items():
        out = ev.run_config_single(cfg, img, MockYolo(), cache, {},
                                   conf=0.20, image_id=f"d{cfg}",
                                   log_fallback=False)
        assert out["mode"] == mode, cfg
        assert out["gating"] == gating, cfg
        assert out["result"]["conclusion"] in ("FAIL", "REWORK",
                                               "REVIEW", "PASS"), cfg
        assert out["n_crops"] <= 10, (
            f"cap 8 + rescue 2 (D1-sua), got {out['n_crops']}", cfg)
        assert int(out["batch_stats"].get("n_calls", 0)) <= 3, cfg


# ---------------------------------------------------------------------------
# 5. GOOD items chi tu LOCAL_TEMPLATES (khong doc LEGACY images)
# ---------------------------------------------------------------------------

def test_good_items_local_templates_only():
    goods = ev.collect_good_items_clustered({"01", "04"}, allow_test=True)
    assert len(goods) == 2, f"GOOD 01+04 phai co 2, got {len(goods)}"
    for g in goods:
        assert g["gt"] == []
        p = str(Path(g["path"]).resolve())
        assert str(LOCAL_TEMPLATES.resolve()) in p, f"GOOD phai tu LOCAL_TEMPLATES: {p}"
        assert "pku" not in p.lower() and "images" not in p.lower().split("templates")[0]


# ---------------------------------------------------------------------------
# 6. perturb parity vs project goc (cung seed -> cung ops/hang so)
# ---------------------------------------------------------------------------

def test_perturb_parity_with_origin():
    assert ORIG_EVALUATE.exists(), "can project goc de doi chieu READ-ONLY"
    orig_src = ORIG_EVALUATE.read_text(encoding="utf-8")
    new_src = inspect.getsource(bs.perturb_image_and_boxes)
    # Cung hang so then chot (thuat toan mo phong camera)
    for lit in ("0.008", "0.85", "1.15", "-10.0", "10.0", "0.15",
                "3.0", "5.0", "-25, 25", "-2.0", "2.0",
                "BORDER_REPLICATE", "INTER_LINEAR", "default_rng",
                "w - 1", "h - 1"):
        assert lit in orig_src, f"goc thieu {lit}"
        assert lit in new_src, f"moi thieu {lit} (lech goc)"
    # Cung thu tu rng calls: dx/dy -> ang -> perspective -> gain -> bias -> noise
    order = ["uniform(-25, 25)", "uniform(-2.0, 2.0)", "uniform(-j, j",
             "uniform(0.85, 1.15)", "uniform(-10.0, 10.0)",
             "uniform(3.0, 5.0)"]
    for src, tag in ((orig_src, "goc"), (new_src, "moi")):
        pos = [src.find(p) for p in order]
        assert all(v >= 0 for v in pos), f"{tag} thieu op: {pos}"
        assert pos == sorted(pos), f"{tag} sai thu tu rng: {pos}"
    # Cung nguong box toi thieu 4px
    assert ">= 4" in orig_src and ">= 4" in new_src


# ---------------------------------------------------------------------------
# 7. Baseline crop/gating parity vs run_dualpath goc (evaluate.py:657-678)
# ---------------------------------------------------------------------------

def test_baseline_crop_gating_parity_with_origin():
    assert ORIG_EVALUATE.exists()
    orig_src = ORIG_EVALUATE.read_text(encoding="utf-8")
    new_src = inspect.getsource(ev.run_legacy_branch_b_path)
    # Cung hang so crop-verify goc: clip tam-320, max(W-640,0), BGR2RGB,
    # spatial_center_gating R80, +x0/+y0 ve he goc
    for lit in ("320", "640", "COLOR_BGR2RGB", "spatial_center_gating",
                "80.0", "+ x0", "+ y0"):
        assert lit in orig_src, f"goc thieu {lit}"
        assert lit in new_src, f"moi thieu {lit} (lech goc)"
    # Cung cong thuc clip goc crop
    assert "max(W - 640, 0)" in orig_src or "max(W-640,0)" in orig_src.replace(" ", "")
    assert "max(w_img - 640, 0)" in new_src
