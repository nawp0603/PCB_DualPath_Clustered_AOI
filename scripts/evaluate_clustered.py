#!/usr/bin/env python3
"""Phase 4 — Dual-Path Clustered pipeline (plan §4 Phase 4, AUDIT-01/03/04/05).

Fork `run_dualpath` goc (scripts/evaluate.py): giu nguyen Nhanh A +
Box Fusion + luat 4 muc, thay doan Branch-B
"cents -> 8 crop 640 -> 1 batch -> gating don" bang pipeline cum:

  blobs (K_raw frozen) -> DBSCAN(m=1, eps frozen) -> Z_cluster top-8
  -> adaptive crops (mode A1/A2/B qua --crop-mode)
  -> bucket batches (predict_clusters) -> union_of_disks_gate (U, R=80)
  -> cross_crop_nms (IoU=0.50, class-aware) -> fuse_and_decide
  + fallback_single (1 blob) + fallback TOP-8 khi exception cluster.

Thu tu tich hop an toan (plan §4 Phase 4):
  (1) Nhanh B goc trong project moi (sanity blob);
  (2) DBSCAN(m=1, eps frozen) -> so crop (<=8) vs 8;
  (3) batch A1 -> A2 -> B (--crop-mode);
  (4) union-gating -> NMS -> fusion + luat 4 muc.

CPU-only o day ngoai tru YOLO inject tu ngoai (Phase 4 truyen
YOLODetector that; test truyen mock). Module nay khong doc Board 01/04
(Zero-defect-training Boards) — chi Board 05-12, dac biet Board 07.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from configs.resource_paths import (  # noqa: E402
    LEGACY_PKU_ROOT,
    LOCAL_TEMPLATES,
    PROJECT_ROOT,
    assert_writable_ok,
)

import cv2  # noqa: E402
import numpy as np  # noqa: E402

import src.reference.board_id_and_align as ba  # noqa: E402
import src.reference.tolerance_diff as td  # noqa: E402
from src.cluster.adaptive_cluster_cropper import (  # noqa: E402
    clusters_to_crops,
)
from src.cluster.batch_bucket import (  # noqa: E402
    CROP_MODE_A1,
    CROP_MODE_A2,
    CROP_MODE_B,
    CROP_MODES,
    build_batch_inputs,
    predict_clusters,
)
from src.cluster.cluster_gating import (  # noqa: E402
    CROSS_CROP_NMS_IOU,
    MARGIN_DELTA,
    R_BASE,
    RESCUE_R_MAX,
    cross_crop_nms,
    margin_gate_ablation,
    union_of_disks_gate,
)
from src.cluster.dbscan_clustering import (  # noqa: E402
    cluster_blobs,
    load_frozen_params,
)
from src.models.ensemble import fuse_and_decide  # noqa: E402
from src.models.yolo_detector import YOLODetector  # noqa: E402

# ---------------------------------------------------------------------------
# Zero-leakage guards (AUDIT-02) — giong scripts/blob_stats.py
# ---------------------------------------------------------------------------
ALLOWED_CALIB_BOARDS = {"05", "06", "07", "08", "09", "10", "11", "12"}
FORBIDDEN_TEST_BOARDS = {"01", "04"}  # Zero-defect-training Boards — locked to Phase 5
CALIB_SEED = 20261023

FALLBACK_LOG = PROJECT_ROOT / "results" / "fallback_log.jsonl"

CLASS_ID = {
    "open_circuit": 0,
    "short": 1,
    "mouse_bite": 2,
    "spur": 3,
    "spurious_copper": 4,
    "missing_hole": 5,
}

DEFAULT_CONF = 0.20
DEFAULT_IOU = 0.5
DEFAULT_CROP_MODE = CROP_MODE_A1


def _assert_no_leakage(boards, allow_test: bool = False) -> None:
    # Phase 4 default: strict — 01/04 locked. Phase 5 opt-in: caller passes
    # allow_test=True ONLY after verify_phase5_seal() MATCH (plan §4 Phase 5).
    if allow_test:
        return
    bad = set(map(str, boards)) & FORBIDDEN_TEST_BOARDS
    if bad:
        raise RuntimeError(
            "[ZERO LEAKAGE] Refusing to touch Zero-defect-training Boards "
            f"{sorted(bad)}. Phase 4 may only use Boards 05-12."
        )


# ---------------------------------------------------------------------------
# Phase 5 gate: open 01/04 ONLY after frozen seal MATCH (plan §4 Phase 5)
# ---------------------------------------------------------------------------
PHASE5_TEST_BOARDS = {"01", "04"}  # Zero-defect-training Boards — Phase 5 only
PHASE5_SEED = 20261023

# Phase 5 matrix (plan §4 Phase 5, §5.1-§5.2): Config0 baseline legacy TOP-8,
# Config1-3 cluster A1/A2/B + U+NMS, Config4 cluster A1 + H ablation.
# Coder scope: prepare scripts + CPU-light smoke only; full GPU benchmark
# + Bootstrap/Wilcoxon + P95 belongs to Verifier (verifier_rules §4.3).
GATING_MODES = ("U", "H")
PHASE5_CONFIGS: dict[str, dict[str, str]] = {
    "config0_baseline": {"kind": "legacy", "mode": "legacy_top8",
                         "gating": "single"},
    "config1_proposed": {"kind": "clustered", "mode": CROP_MODE_A1,
                         "gating": "U"},
    "config2_bucket": {"kind": "clustered", "mode": CROP_MODE_A2,
                       "gating": "U"},
    "config3_letterbox": {"kind": "clustered", "mode": CROP_MODE_B,
                          "gating": "U"},
    "config4_gating_h": {"kind": "clustered", "mode": CROP_MODE_A1,
                         "gating": "H"},
}


def verify_phase5_seal() -> dict[str, Any]:
    """Recompute frozen_sha256 (2-arg) + pipeline_hashes; raise if MISMATCH.

    Coder/Verifier must call this BEFORE any 01/04 read. Returns frozen params
    dict on MATCH. Uses LEGACY venv python semantics: 2-arg
    frozen_sha256_of(body, pipe) + exclude all VOLATILE keys + utf-8.
    """
    import json as _json
    from scripts.calibrate_dbscan import (  # lazy: keep main path light
        VOLATILE_REPORT_KEYS,
        frozen_sha256_of,
        pipeline_hashes,
    )
    from src.cluster.dbscan_clustering import CALIB_REPORT as _CR

    body_full = _json.loads(_CR.read_text(encoding="utf-8"))
    stored = str(body_full.get("frozen_sha256", ""))
    pipe = pipeline_hashes()
    body = {k: v for k, v in body_full.items() if k not in VOLATILE_REPORT_KEYS}
    calc = frozen_sha256_of(body, pipe)
    if calc != stored:
        raise RuntimeError(
            f"[FROZEN SEAL] MISMATCH: stored={stored} calc={calc}. "
            "Refusing to open 01/04 — return to Phase 1."
        )
    fp = load_frozen_params()  # enforces m==1 + required keys
    return {"eps": float(fp["eps"]), "min_samples": int(fp["min_samples"]),
            "k_raw": int(fp["k_raw"]), "frozen_sha256": str(fp["frozen_sha256"]),
            "pipe_match": True}


# ---------------------------------------------------------------------------
# IO helpers (READ-ONLY legacy, Unicode-safe)
# ---------------------------------------------------------------------------
def read_bgr(path: str | Path) -> np.ndarray:
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(f"Cannot decode image: {path}")
    return img


def board_id_from_xml_filename(filename: str) -> str:
    return Path(filename).stem.split("_")[0].zfill(2)


def resolve_defect_image(ann_dir_name: str, xml_path: Path,
                         xml_filename_field: str) -> Path | None:
    stem = xml_path.stem
    if stem.endswith("_annotation"):
        stem = stem[: -len("_annotation")]
    roots = [LEGACY_PKU_ROOT]
    candidates = [stem + ".jpg", stem + ".JPG", xml_filename_field,
                  Path(xml_filename_field).stem + ".jpg"]
    for root in roots:
        try:
            if not root.is_dir():
                continue
        except Exception:
            continue
        for cand in candidates:
            q = root / "images" / ann_dir_name / cand
            if q.exists():
                return q
    return None


def collect_clustered_items(boards: set[str] = {"07"},
                            allow_test: bool = False) -> list[dict[str, Any]]:
    """Collect defect items READ-ONLY tu LEGACY_PKU_ROOT.

    Phase 4 default: chi Board 05-12. Phase 5: allow_test=True (chi sau khi
    verify_phase5_seal() MATCH) moi duoc mo Zero-defect-training Boards 01/04.
    """
    if allow_test:
        # Phase 5 gate (plan §4 Phase 5): mo 01/04 CHI sau khi seal MATCH.
        # Raise RuntimeError neu MISMATCH — khong mo test khi seal vo.
        verify_phase5_seal()
    _assert_no_leakage(set(boards), allow_test=allow_test)
    allowed = set(ALLOWED_CALIB_BOARDS)
    if allow_test:
        allowed |= set(PHASE5_TEST_BOARDS)
    unknown = set(boards) - allowed
    if unknown:
        raise RuntimeError(f"Unknown boards (got {sorted(unknown)}). "
                           f"Allowed={sorted(allowed)}.")
    ann_root = LEGACY_PKU_ROOT / "Annotations"
    if not ann_root.is_dir():
        raise FileNotFoundError(f"Missing Annotations: {ann_root}")
    items: list[dict[str, Any]] = []
    for ann_dir in sorted(p for p in ann_root.iterdir() if p.is_dir()):
        for xp in sorted(ann_dir.glob("*_annotation.xml")):
            try:
                r = ET.parse(xp).getroot()
            except ET.ParseError:
                continue
            fn_el = r.find("filename")
            if fn_el is None or not fn_el.text:
                continue
            fname = fn_el.text.strip()
            bid = board_id_from_xml_filename(fname)
            if bid not in boards:
                continue
            _assert_no_leakage({bid}, allow_test=allow_test)
            img_p = resolve_defect_image(ann_dir.name, xp, fname)
            if img_p is None:
                continue
            gt: list[tuple[int, list[float]]] = []
            for o in r.findall("object"):
                n = o.find("name")
                b = o.find("bndbox")
                if n is None or b is None or not n.text:
                    continue
                cid = CLASS_ID.get(n.text.strip().lower())
                if cid is None:
                    continue
                try:
                    box = [float(b.find("xmin").text), float(b.find("ymin").text),
                           float(b.find("xmax").text), float(b.find("ymax").text)]
                except (TypeError, ValueError):
                    continue
                gt.append((cid, box))
            if gt:
                items.append({"path": str(img_p), "board": bid,
                              "xml": str(xp), "gt": gt})
    items.sort(key=lambda d: d["path"])
    _assert_no_leakage({it["board"] for it in items}, allow_test=allow_test)
    return items


def collect_good_items_clustered(boards: set[str] = {"01", "04"},
                                 allow_test: bool = False) -> list[dict[str, Any]]:
    """GOOD items = Golden Templates 01/04 (READ-ONLY LOCAL_TEMPLATES).

    Phase 5 only (allow_test=True AFTER verify_phase5_seal() MATCH).
    Mirrors evaluate.py::collect_good_items for FAR. No LEGACY image read.
    """
    if allow_test:
        verify_phase5_seal()
    _assert_no_leakage(set(boards), allow_test=allow_test)
    allowed = set(ALLOWED_CALIB_BOARDS)
    if allow_test:
        allowed |= set(PHASE5_TEST_BOARDS)
    unknown = set(boards) - allowed
    if unknown:
        raise RuntimeError(f"Unknown boards (got {sorted(unknown)}). "
                           f"Allowed={sorted(allowed)}.")
    items: list[dict[str, Any]] = []
    for bid in sorted(set(map(str, boards))):
        p = LOCAL_TEMPLATES / f"{bid}.JPG"
        try:
            if p.is_file():
                items.append({"path": str(p), "board": bid, "gt": []})
        except Exception:
            continue
    return items


def six_class_samples(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chon 6 anh dai dien du 6 lop (uu tien anh dau tien moi lop)."""
    seen: dict[int, dict[str, Any]] = {}
    for it in items:
        for cid, _box in it["gt"]:
            if cid not in seen:
                seen[cid] = it
    return [seen[k] for k in sorted(seen)]


# ---------------------------------------------------------------------------
# Weights (READ-ONLY): assets trong project moi truoc, fallback sang goc
# ---------------------------------------------------------------------------
def resolve_weights(preferred: str | Path | None = None) -> Path:
    from configs.resource_paths import LOCAL_BASE_PT, LOCAL_DAY2_PT
    cands: list[Path] = []
    if preferred is not None:
        cands.append(Path(preferred))
    cands += [LOCAL_DAY2_PT, LOCAL_BASE_PT]
    # Fallback READ-ONLY sang project goc (plan §STRICT SANDBOX cho phep
    # doc tham khao assets/weights/*.pt; KHONG ghi sang do).
    orig = PROJECT_ROOT.parent / "PCB_DualPath_AOI" / "assets" / "weights"
    cands += [orig / "yolo11n_day2.pt", orig / "yolo11n_base.pt",
              orig / "best.pt"]
    for p in cands:
        try:
            if p.is_file():
                return p
        except Exception:
            continue
    raise FileNotFoundError(
        "Thieu weights YOLO (tim assets/weights trong project moi + goc "
        "PCB_DualPath_AOI deu khong thay).")


# ---------------------------------------------------------------------------
# Fallback log (§5.4): moi dong {image_id, reason, n_blobs}
# ---------------------------------------------------------------------------
def append_fallback_log(image_id: str, reason: str, n_blobs: int,
                        log_path: Path | None = None) -> Path:
    p = assert_writable_ok(Path(log_path) if log_path is not None else FALLBACK_LOG)
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"image_id": str(image_id), "reason": str(reason),
           "n_blobs": int(n_blobs)}
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return p


# ---------------------------------------------------------------------------
# Hinh hoc Input: tam blob + diff_box he anh goc (Chot 1)
# ---------------------------------------------------------------------------
def _blob_centers_input(blobs: list[dict[str, Any]],
                        h_inv: np.ndarray) -> np.ndarray:
    if not blobs:
        return np.zeros((0, 2), dtype=np.float64)
    tpl = np.float32([b["centroid_tpl"] for b in blobs])
    pts = ba.project_points(tpl, np.asarray(h_inv, dtype=np.float64))
    return np.asarray(pts, dtype=np.float64)


def _diff_boxes_input(centers_input: np.ndarray,
                      r_base: float = R_BASE) -> np.ndarray:
    c = np.asarray(centers_input, dtype=np.float64).reshape(-1, 2)
    if c.shape[0] == 0:
        return np.zeros((0, 4), dtype=np.float64)
    r = float(r_base)
    return np.stack([c[:, 0] - r, c[:, 1] - r,
                     c[:, 0] + r, c[:, 1] + r], axis=1)


def _attach_cluster_ids(items: list[dict[str, Any]],
                         dets_nms: list[dict[str, Any]],
                         blobs: list[dict[str, Any]],
                         clusters: list[dict[str, Any]],
                         centers_input: np.ndarray) -> list[dict[str, Any]]:
    """Gan cluster_id vao moi evidence item (backward-compatible, §2.4.4).

    - fused/yolo (co bbox det): map theo bbox khop det trong dets_nms.
    - branchb (khong det): map theo centroid_input gan nhat -> blob ->
      cum chua blob do. Khong tim thay -> -1. decide() bo qua truong thua.
    """
    # bbox -> cluster_id (bbox da he INPUT, khop chinh xac float).
    bbox2cid: dict[tuple, int] = {}
    for d in dets_nms:
        try:
            key = tuple(float(v) for v in d["bbox"])
        except Exception:
            continue
        bbox2cid[key] = int(d.get("cluster_id", -1))
    # blob idx -> cluster_id.
    blob2cid: dict[int, int] = {}
    for c in clusters:
        for m in list(c.get("members", [])):
            blob2cid[int(m)] = int(c.get("cluster_id", -1))
    ci = np.asarray(centers_input, dtype=np.float64).reshape(-1, 2) \
        if centers_input is not None else np.zeros((0, 2))
    for it in items:
        if it.get("conf") is not None and it.get("bbox") is not None:
            try:
                key = tuple(float(v) for v in it["bbox"])
            except Exception:
                key = None
            if key is not None and key in bbox2cid:
                it["cluster_id"] = int(bbox2cid[key])
                continue
            # det du (fuse giu det ngoai gate): gan cum co tam gan tam det nhat.
            try:
                b = [float(v) for v in it["bbox"]]
                pcx, pcy = (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0
                if len(ci):
                    dd = np.hypot(ci[:, 0] - pcx, ci[:, 1] - pcy)
                    bi = int(np.argmin(dd))
                    it["cluster_id"] = int(blob2cid.get(bi, -1))
                else:
                    it["cluster_id"] = -1
            except Exception:
                it["cluster_id"] = -1
        else:
            # branchb: map theo centroid_input (fuse tinh cung cach).
            try:
                cc = it.get("centroid_input")
                if cc is not None and len(ci):
                    dd = np.hypot(ci[:, 0] - float(cc[0]),
                                  ci[:, 1] - float(cc[1]))
                    bi = int(np.argmin(dd))
                    it["cluster_id"] = int(blob2cid.get(bi, -1))
                else:
                    it["cluster_id"] = -1
            except Exception:
                it["cluster_id"] = -1
    return items


# ---------------------------------------------------------------------------
# Duong TOP-8 goc (fallback bit-identical, fork run_dualpath evaluate.py)
# ---------------------------------------------------------------------------
def run_legacy_branch_b_path(image_bgr: np.ndarray,
                             blobs: list[dict[str, Any]],
                             h_inv: np.ndarray,
                             yolo: Any,
                             conf: float | None = None) -> list[dict[str, Any]]:
    """Crop 640 quanh tam blob (he Input) -> predict_batch 1-call -> gating don.

    Copy nguyen van logic run_dualpath goc (evaluate.py:657-678) de dam bao
    bit-identical khi fallback. BGR->RGB nhu ban goc.
    """
    h_img, w_img = image_bgr.shape[:2]
    if blobs:
        cents = ba.project_points(
            np.float32([b["centroid_tpl"] for b in blobs]),
            np.asarray(h_inv, dtype=np.float64))
    else:
        cents = np.zeros((0, 2), dtype=np.float32)
    crops, origins = [], []
    for (px, py) in cents:
        x0 = int(np.clip(float(px) - 320, 0, max(w_img - 640, 0)))
        y0 = int(np.clip(float(py) - 320, 0, max(h_img - 640, 0)))
        crops.append(cv2.cvtColor(image_bgr[y0:y0 + 640, x0:x0 + 640],
                                  cv2.COLOR_BGR2RGB))
        origins.append((x0, y0))
    batch = yolo.predict_batch(crops) if crops else []
    if conf is not None and hasattr(yolo, "predict_batch"):
        # predict_batch da nhan conf ben trong neu can; giu call goc
        # (khong goi lai) de bit-identical — conf chi dung o duong cum.
        pass
    yolo_dets_all: list[dict[str, Any]] = []
    for dets_one, (px, py), (x0, y0) in zip(batch, cents, origins):
        kept = YOLODetector.spatial_center_gating(
            dets_one, center_xy=(float(px), float(py)),
            radius=80.0, crop_origin=(float(x0), float(y0)))
        for d in kept:
            dd = dict(d)
            dd["bbox"] = [d["bbox"][0] + x0, d["bbox"][1] + y0,
                          d["bbox"][2] + x0, d["bbox"][3] + y0]
            dd["cluster_id"] = -1  # fallback TOP-8: khong co cum
            dd["crop_origin"] = (int(x0), int(y0))
            yolo_dets_all.append(dd)
    return yolo_dets_all


# ---------------------------------------------------------------------------
# Phase 5 Config0 — Baseline legacy TOP-8 single-image (plan §4 Phase 5)
# ---------------------------------------------------------------------------
def run_baseline_single(image_bgr: np.ndarray,
                        yolo: Any,
                        cache: dict[str, Any],
                        templates: dict[str, np.ndarray],
                        conf: float | None = None,
                        image_id: str = "",
                        log_fallback: bool = False,
                        fallback_log_path: Path | None = None,
                        allow_test: bool = False) -> dict[str, Any]:
    """Config0 Baseline: align + Branch-B (K_raw frozen) + TOP-8 crop640 1-call
    + single-centroid gating + fuse_and_decide (bit-identical run_dualpath goc).

    allow_test=True ONLY after verify_phase5_seal() MATCH (mo 01/04).
    Tra ve cung shape dict nhu run_clustered_single de benchmark so sanh duoc.
    """
    if allow_test:
        # Phase 5 gate: mo 01/04 CHI sau khi seal MATCH.
        verify_phase5_seal()
    fp = load_frozen_params()  # eps/k_raw/frozen — khong hardcode
    k_raw = int(fp["k_raw"])
    t_total = time.perf_counter()
    out: dict[str, Any] = {"mode": "legacy_top8", "gating": "single",
                            "fallback_single": False, "fallback_reason": None}
    r = ba.identify_and_align(image_bgr, cache, fast=True)
    t_align = 0.0  # giu nhe cho baseline smoke; chi tiet latency do Verifier GPU
    _assert_no_leakage({str(r["board_id"])}, allow_test=allow_test)
    tpl = templates.get(r["board_id"])
    if tpl is None:
        tpl_p = LOCAL_TEMPLATES / f"{r['board_id']}.JPG"
        data = np.fromfile(str(tpl_p), dtype=np.uint8)
        tpl = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
        if tpl is None:
            raise FileNotFoundError(f"Cannot decode template: {tpl_p}")
        if tpl.shape != r["warped"].shape[:2]:
            tpl = cv2.resize(tpl, (r["warped"].shape[1], r["warped"].shape[0]),
                             interpolation=cv2.INTER_AREA)
        templates[r["board_id"]] = tpl
    bb = td.run_branch_b(r["warped"], tpl, top_k=int(k_raw),
                         valid_mask=r.get("valid_mask"))
    blobs_all = list(bb["blobs"])
    # Baseline TOP-8 co dinh (plan §4 Phase 5 Config0): cat cung 8 blob Z cao nhat.
    blobs = blobs_all[: min(8, max(1, int(k_raw)))] if blobs_all else []
    h_inv = np.asarray(r["H_inv"], dtype=np.float64)
    out.update({"board_id": r["board_id"], "inliers": r["inliers"],
                "H": r["H"], "H_inv": r["H_inv"],
                "warped": r.get("warped"), "diff_map": bb.get("diff_map"),
                "structure_mask": bb.get("structure_mask"),
                "blobs": blobs, "n_blobs": len(blobs),
                "t_align_ms": round(float(t_align), 3),
                "t_diff_ms": 0.0})
    if len(blobs) == 0:
        res0 = fuse_and_decide([], [], h_inv)
        out.update({"clusters": [], "crops_geo": [], "dropped_log": [],
                    "batch_stats": {"mode": "legacy_top8", "n_calls": 0,
                                    "calls": [], "n_dets": 0},
                    "n_crops": 0, "dets_raw": [],
                    "n_dets_before_gating": 0, "n_dets_after_gating": 0,
                    "n_dets_after_nms": 0, "yolo_dets": [],
                    "result": res0, "t_roi_ms": 0.0,
                    "t_total_ms": round((time.perf_counter() - t_total) * 1000.0, 3)})
        return out
    t0 = time.perf_counter()
    yd = run_legacy_branch_b_path(image_bgr, blobs, h_inv, yolo, conf)
    t_roi = (time.perf_counter() - t0) * 1000.0
    res = fuse_and_decide(yd, blobs, h_inv)
    for it in res.get("items", []):
        it.setdefault("cluster_id", -1)
    out.update({"clusters": [], "crops_geo": [], "dropped_log": [],
                "batch_stats": {"mode": "legacy_top8",
                                "n_calls": 1 if yd else 0,
                                "calls": [], "n_dets": len(yd)},
                "n_crops": min(len(blobs), 8),
                "n_gpu_calls": 1 if yd else 0,
                "dets_raw": yd, "n_dets_before_gating": len(yd),
                "n_dets_after_gating": len(yd),
                "n_dets_after_nms": len(yd),
                "yolo_dets": yd, "result": res,
                "t_roi_ms": round(float(t_roi), 3),
                "t_total_ms": round((time.perf_counter() - t_total) * 1000.0, 3)})
    return out


def run_config_single(config_name: str,
                      image_bgr: np.ndarray,
                      yolo: Any,
                      cache: dict[str, Any],
                      templates: dict[str, np.ndarray],
                      conf: float | None = None,
                      image_id: str = "",
                      log_fallback: bool = True,
                      fallback_log_path: Path | None = None,
                      allow_test: bool = False,
                      rescue_conf: float | None = None,
                      rescue_keep_res: bool = False,
                      rescue_r: float | None = None) -> dict[str, Any]:
    """Dispatcher 5-config Phase 5 (plan §4 Phase 5 ma tran Config0-4).

    - config0_baseline -> run_baseline_single (legacy TOP-8 + single).
    - config1_proposed (A1+U), config2_bucket (A2+U), config3_letterbox (B+U),
      config4_gating_h (A1+H) -> run_clustered_single tuong ung.
    allow_test=True ONLY after verify_phase5_seal() MATCH.
    F2 rescue_conf=None (default) => DISABLED, bit-identical baseline.
    F1 rescue_keep_res=False (default) => DISABLED bit-identical;
    True => rescue crops A1 giu phan giai bucket (05-12 only).
    F3 rescue_r=None (default) => DISABLED bit-identical;
    float => rescue dets dung R_rescue bounded (80,200] (05-12 only).
    """
    if config_name not in PHASE5_CONFIGS:
        raise ValueError(f"config phai thuoc {sorted(PHASE5_CONFIGS)}, nhan {config_name!r}")
    if allow_test:
        verify_phase5_seal()
    cfg = PHASE5_CONFIGS[config_name]
    if cfg.get("kind") == "legacy":
        return run_baseline_single(
            image_bgr, yolo, cache, templates, conf=conf,
            image_id=image_id, log_fallback=log_fallback,
            fallback_log_path=fallback_log_path, allow_test=allow_test)
    return run_clustered_single(
        image_bgr, yolo, cache, templates, mode=str(cfg["mode"]),
        conf=conf, image_id=image_id, log_fallback=log_fallback,
        fallback_log_path=fallback_log_path,
        gating=str(cfg.get("gating", "U")), allow_test=allow_test,
        rescue_conf=rescue_conf, rescue_keep_res=rescue_keep_res,
        rescue_r=rescue_r)


# ---------------------------------------------------------------------------
# Pipeline 1 anh (dung cho UI + smoke + benchmark)
# ---------------------------------------------------------------------------
def run_clustered_single(image_bgr: np.ndarray,
                         yolo: Any,
                         cache: dict[str, Any],
                         templates: dict[str, np.ndarray],
                         mode: str = DEFAULT_CROP_MODE,
                         conf: float | None = None,
                         image_id: str = "",
                         log_fallback: bool = True,
                         fallback_log_path: Path | None = None,
                         gating: str = "U",
                         allow_test: bool = False,
                         rescue_conf: float | None = None,
                         rescue_keep_res: bool = False,
                         rescue_r: float | None = None) -> dict[str, Any]:
    """Chay Dual-Path Clustered tren 1 anh BGR. Tra ve dict day du cho UI/ve.

    Contract (plan §4 Phase 4 + §5.4 + Phase 5 Config4-H):
      - fallback_single=True + duong TOP-8 bit-identical khi anh chi 1 blob.
      - moi exception cluster -> fallback TOP-8 + dong fallback_log.jsonl.
      - NMS chen sau gating, truoc fuse (log n truoc/sau NMS).
      - toa do det cuoi o he anh goc.
      - gating="U" (default, Phase 4): union_of_disks_gate + NMS.
      - gating="H" (Phase 5 Config4 ablation): margin_gate_ablation + NMS.
      - allow_test=True ONLY after verify_phase5_seal() MATCH (Phase 5 01/04).
      - F2 rescue_conf (Verifier lan8, Coder lan9, 05-12 only): None (default)
        => DISABLED, bit-identical baseline. Float (VD 0.12-0.15) => rescue
        crops is_frag_rescue chay nguong thap, main giu baseline conf.
        Zero extra GPU calls (AUDIT-04 preserved).
      - F1 rescue_keep_res (Verifier lan9, Coder lan10, 05-12 only): False
        (default) => DISABLED, A1 bit-identical 1-call. True => chi rescue
        crops A1 cat theo bucket + imgsz=bucket (giu phan giai, tranh
        upscale 640 lam mo frag nho); main van 640. Toi da 3 nhom GPU,
        640 shared (AUDIT-04 preserved). A2/B + F1: giu nguyen.
      - F3 rescue_r (Verifier lan9, Coder lan10, 05-12 only): None
        (default) => DISABLED, tat ca dets R_base=80 bit-identical.
        Float => rescue dets dung R_rescue, main van 80; IoU OR>0 giu
        nguyen (diff_boxes R80 shared) de han che FAR. Bound cung:
        80 < R_rescue <= 200 (RESCUE_R_MAX), vuot => ValueError.
        NMS van sau gating truoc fuse.
    """
    if mode not in CROP_MODES:
        raise ValueError(f"mode phai thuoc {CROP_MODES}, nhan {mode!r}")
    if gating not in GATING_MODES:
        raise ValueError(f"gating phai thuoc {GATING_MODES}, nhan {gating!r}")
    # F1/F3 fail-fast (05-12 only, truoc moi viec nang): bound cung,
    # khong clamp am tham; F3 chi ho tro gating U (H ablation khong
    # dung union gate -> tu choi thay vi bo qua am tham).
    if rescue_r is not None:
        _rr = float(rescue_r)
        if not (float(R_BASE) < _rr <= float(RESCUE_R_MAX)):
            raise ValueError(
                f"rescue_r phai thoa {float(R_BASE)} < rescue_r <= "
                f"{float(RESCUE_R_MAX)}, nhan {_rr!r}")
        if gating != "U":
            raise ValueError(
                "rescue_r chi ho tro gating='U' (H ablation khong dung "
                "union_of_disks_gate)")
    fp = load_frozen_params()  # eps/k_raw/frozen — khong hardcode
    eps, k_raw = float(fp["eps"]), int(fp["k_raw"])
    if allow_test:
        # Phase 5 gate: mo 01/04 CHI sau khi seal MATCH (plan §4 Phase 5).
        verify_phase5_seal()
    t_total = time.perf_counter()
    out: dict[str, Any] = {"mode": mode, "gating": gating,
                            "fallback_single": False,
                            "fallback_reason": None}
    # (1) Align + Branch-B goc (sanity, giu nguyen nhu baseline).
    t0 = time.perf_counter()
    r = ba.identify_and_align(image_bgr, cache, fast=True)
    t_align = (time.perf_counter() - t0) * 1000.0
    _assert_no_leakage({str(r["board_id"])}, allow_test=allow_test)
    tpl = templates.get(r["board_id"])
    if tpl is None:
        tpl_p = LOCAL_TEMPLATES / f"{r['board_id']}.JPG"
        data = np.fromfile(str(tpl_p), dtype=np.uint8)
        tpl = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
        if tpl is None:
            raise FileNotFoundError(f"Cannot decode template: {tpl_p}")
        if tpl.shape != r["warped"].shape[:2]:
            tpl = cv2.resize(tpl, (r["warped"].shape[1], r["warped"].shape[0]),
                             interpolation=cv2.INTER_AREA)
        templates[r["board_id"]] = tpl
    t0 = time.perf_counter()
    bb = td.run_branch_b(r["warped"], tpl, top_k=int(k_raw),
                         valid_mask=r.get("valid_mask"))
    t_diff = (time.perf_counter() - t0) * 1000.0
    blobs_all = list(bb["blobs"])
    blobs = blobs_all[: max(1, int(k_raw))]
    h_inv = np.asarray(r["H_inv"], dtype=np.float64)
    img_shape = (int(image_bgr.shape[0]), int(image_bgr.shape[1]))

    out.update({"board_id": r["board_id"], "inliers": r["inliers"],
                "H": r["H"], "H_inv": r["H_inv"],
                "warped": r.get("warped"), "diff_map": bb.get("diff_map"),
                "structure_mask": bb.get("structure_mask"),
                "blobs": blobs, "n_blobs": len(blobs),
                "t_align_ms": round(float(t_align), 3),
                "t_diff_ms": round(float(t_diff), 3)})

    def _do_legacy(reason: str, n_blobs: int) -> dict[str, Any]:
        t0l = time.perf_counter()
        yd = run_legacy_branch_b_path(image_bgr, blobs, h_inv, yolo, conf)
        t_roi = (time.perf_counter() - t0l) * 1000.0
        res = fuse_and_decide(yd, blobs, h_inv)
        for it in res.get("items", []):
            it.setdefault("cluster_id", -1)
        out.update({"clusters": [], "crops_geo": [], "dropped_log": [],
                    "batch_stats": {"mode": "legacy_top8_fallback",
                                    "n_calls": 1 if yd else 0,
                                    "calls": [], "n_dets": len(yd)},
                    "n_crops": min(len(blobs), 8),
                    "dets_raw": yd, "n_dets_before_gating": len(yd),
                    "n_dets_after_gating": len(yd),
                    "n_dets_after_nms": len(yd),
                    "yolo_dets": yd, "result": res,
                    "fallback_single": bool(reason == "fallback_single_1blob"),
                    "fallback_reason": reason,
                    "t_roi_ms": round(float(t_roi), 3),
                    "t_total_ms": round((time.perf_counter() - t_total) * 1000.0, 3)})
        if log_fallback:
            try:
                append_fallback_log(image_id or "unknown", reason, int(n_blobs),
                                    fallback_log_path)
            except Exception:
                pass
        return out

    # Khong blob -> khong YOLO call, fuse rong -> decide PASS.
    if len(blobs) == 0:
        res0 = fuse_and_decide([], [], h_inv)
        out.update({"clusters": [], "crops_geo": [], "dropped_log": [],
                    "batch_stats": {"mode": mode, "n_calls": 0,
                                    "calls": [], "n_dets": 0},
                    "n_crops": 0, "dets_raw": [],
                    "n_dets_before_gating": 0, "n_dets_after_gating": 0,
                    "n_dets_after_nms": 0, "yolo_dets": [],
                    "result": res0,
                    "t_roi_ms": 0.0,
                    "t_total_ms": round((time.perf_counter() - t_total) * 1000.0, 3)})
        return out

    # fallback_single: anh chi 1 blob -> duong tat TOP-8 bit-identical.
    if len(blobs) == 1:
        return _do_legacy("fallback_single_1blob", len(blobs))

    # (2)-(4) Duong cum: DBSCAN -> crops -> batch -> gating -> NMS -> fuse.
    try:
        t0 = time.perf_counter()
        clusters = cluster_blobs(blobs, eps=float(eps), min_samples=1)
        crops_geo, dropped_log = clusters_to_crops(
            clusters, blobs, h_inv, img_shape, max_cluster_crops=8)
        t_clu = (time.perf_counter() - t0) * 1000.0
    except Exception as e:  # §5.4(3): moi exception cluster -> fallback TOP-8
        if log_fallback:
            try:
                append_fallback_log(image_id or "unknown",
                                    f"cluster_exception_{type(e).__name__}",
                                    len(blobs), fallback_log_path)
            except Exception:
                pass
        return _do_legacy(f"cluster_exception_{type(e).__name__}", len(blobs))

    try:
        batch_inputs = build_batch_inputs(
            image_bgr, crops_geo, mode,
            rescue_keep_res=bool(rescue_keep_res))
        t0 = time.perf_counter()
        if conf is None:
            dets_input, stats = predict_clusters(
                batch_inputs, yolo, mode, rescue_conf=rescue_conf)
        else:
            dets_input, stats = predict_clusters(
                batch_inputs, yolo, mode,
                float(conf), rescue_conf=rescue_conf)
        t_roi = (time.perf_counter() - t0) * 1000.0
        n_before = len(dets_input)
        centers_input = _blob_centers_input(blobs, h_inv)
        diff_boxes = _diff_boxes_input(centers_input, R_BASE)
        if gating == "H":
            # Phase 5 Config4 ablation (plan §2.4.3): Bounding-Margin H + NMS.
            dets_gated = margin_gate_ablation(
                dets_input, diff_boxes, delta=float(MARGIN_DELTA))
        else:
            dets_gated = union_of_disks_gate(
                dets_input, centers_input, r_base=float(R_BASE),
                diff_boxes=diff_boxes, require_overlap=True,
                rescue_r=rescue_r)
        n_gated = len(dets_gated)
        dets_nms = cross_crop_nms(dets_gated, iou_thres=float(CROSS_CROP_NMS_IOU))
        n_nms = len(dets_nms)
        res = fuse_and_decide(dets_nms, blobs, h_inv)
        _attach_cluster_ids(res.get("items", []), dets_nms, blobs,
                             clusters, centers_input)
        out.update({"clusters": clusters, "crops_geo": crops_geo,
                    "dropped_log": dropped_log,
                    "batch_stats": stats, "n_crops": len(crops_geo),
                    "n_gpu_calls": int(stats.get("n_calls", 0)),
                    "dets_raw": dets_input,
                    "n_dets_before_gating": int(n_before),
                    "n_dets_after_gating": int(n_gated),
                    "n_dets_after_nms": int(n_nms),
                    "yolo_dets": dets_nms, "result": res,
                    "centroids_input": [(float(x), float(y))
                                        for x, y in centers_input.tolist()] if len(centers_input) else [],
                    "t_cluster_ms": round(float(t_clu), 3),
                    "t_roi_ms": round(float(t_roi), 3),
                    "t_total_ms": round((time.perf_counter() - t_total) * 1000.0, 3)})
        return out
    except Exception as e:  # loi batch/gating/NMS -> fallback TOP-8
        if log_fallback:
            try:
                append_fallback_log(image_id or "unknown",
                                    f"cluster_exception_{type(e).__name__}",
                                    len(blobs), fallback_log_path)
            except Exception:
                pass
        return _do_legacy(f"cluster_exception_{type(e).__name__}", len(blobs))


# ---------------------------------------------------------------------------
# Driver nhieu anh (mirror run_dualpath: AP50/Recall/FAR/latency + cluster)
# ---------------------------------------------------------------------------
def box_iou(a: list[float], b: list[float]) -> float:
    x1, y1 = max(float(a[0]), float(b[0])), max(float(a[1]), float(b[1]))
    x2, y2 = min(float(a[2]), float(b[2])), min(float(a[3]), float(b[3]))
    iw, ih = max(0.0, x2 - x1), max(0.0, y2 - y1)
    inter = iw * ih
    if inter <= 0.0:
        return 0.0
    ua = (float(a[2]) - float(a[0])) * (float(a[3]) - float(a[1])) \
        + (float(b[2]) - float(b[0])) * (float(b[3]) - float(b[1])) - inter
    return inter / ua if ua > 0 else 0.0


def voc_ap(dets, gts, thr: float = 0.5):
    by_cls: dict[int, list] = {}
    for img_i, conf, box, cls in dets:
        by_cls.setdefault(cls, []).append((conf, img_i, box))
    gt_cls: dict[int, dict[int, list]] = {}
    for img_i, lst in gts.items():
        for cls, box in lst:
            gt_cls.setdefault(cls, {}).setdefault(img_i, []).append([box, False])
    aps: list[float] = []
    per: dict[str, float] = {}
    from src.models.yolo_detector import CLASS_NAMES as _CN
    for cls in sorted(gt_cls):
        d = sorted(by_cls.get(cls, []), key=lambda t: -t[0])
        npos = sum(len(v) for v in gt_cls[cls].values())
        if npos == 0:
            continue
        tp = np.zeros(len(d))
        fp = np.zeros(len(d))
        for j, (_, img_i, box) in enumerate(d):
            cands = gt_cls[cls].get(img_i, [])
            best, bi = 0.0, -1
            for m, cd in enumerate(cands):
                if cd[1]:
                    continue
                v = box_iou(box, cd[0])
                if v > best:
                    best, bi = v, m
            if best >= thr and bi >= 0:
                cands[bi][1] = True
                tp[j] = 1
            else:
                fp[j] = 1
        ctp, cfp = np.cumsum(tp), np.cumsum(fp)
        rec = ctp / max(npos, 1)
        prec = ctp / np.maximum(ctp + cfp, 1e-9)
        mrec = np.concatenate(([0.0], rec, [1.0]))
        mpre = np.concatenate(([0.0], prec, [0.0]))
        for q in range(len(mpre) - 2, -1, -1):
            mpre[q] = max(mpre[q], mpre[q + 1])
        idx = np.where(mrec[1:] != mrec[:-1])[0]
        ap = float(np.sum((mrec[idx + 1] - mrec[idx]) * mpre[idx + 1]))
        aps.append(ap)
        per[_CN.get(cls, str(cls))] = round(ap, 4)
    return (float(np.mean(aps)) if aps else 0.0), per


def pr_counts(dets, gts, thr: float = 0.5):
    npos = sum(len(v) for v in gts.values())
    used: set = set()
    tp = fp = 0
    for img_i, _, box, cls in sorted(dets, key=lambda t: -t[1]):
        cands = [(m, bb) for m, (kk, bb) in enumerate(gts.get(img_i, [])) if kk == cls]
        best, bi = 0.0, None
        for m, bb in cands:
            key = (img_i, cls, m)
            if key in used:
                continue
            v = box_iou(box, bb)
            if v > best:
                best, bi = v, key
        if best >= thr and bi is not None:
            used.add(bi)
            tp += 1
        else:
            fp += 1
    return tp, fp, npos


def run_dualpath_clustered(defect_items: list[dict[str, Any]],
                           good_items: list[dict[str, Any]],
                           yolo: Any,
                           mode: str = DEFAULT_CROP_MODE,
                           perturb: bool = False,
                           seed_base: int = 0,
                           cache: dict[str, Any] | None = None,
                           conf: float | None = None,
                           log_fallback: bool = True,
                           gating: str = "U",
                           allow_test: bool = False,
                           rescue_conf: float | None = None,
                           rescue_keep_res: bool = False,
                           rescue_r: float | None = None) -> dict[str, Any]:
    """Chay pipeline cum tren 1 scenario (mirror run_dualpath goc + cluster).

    Tra ve AP50/Recall/Precision/FAR/latency + cluster stats
    (crops/anh, GPU-calls/anh, fallback rate, dets truoc/sau NMS).

    Phase 4 default: gating="U", allow_test=False (chi 05-12).
    Phase 5: gating="H" cho Config4 ablation; allow_test=True CHI sau khi
    verify_phase5_seal() MATCH (mo 01/04).
    F2 rescue_conf=None (default) => DISABLED, bit-identical baseline.
    F1 rescue_keep_res=False (default) => DISABLED bit-identical;
    True => rescue crops A1 giu phan giai bucket (05-12 only).
    F3 rescue_r=None (default) => DISABLED; float => rescue dets R_rescue
    bounded (80,200] (05-12 only), IoU OR>0 giu nguyen, NMS sau gating.
    """
    if mode not in CROP_MODES:
        raise ValueError(f"mode phai thuoc {CROP_MODES}, nhan {mode!r}")
    if gating not in GATING_MODES:
        raise ValueError(f"gating phai thuoc {GATING_MODES}, nhan {gating!r}")
    if allow_test:
        # Phase 5 gate: mo 01/04 CHI sau khi seal MATCH.
        verify_phase5_seal()
    _assert_no_leakage({it.get("board", "?") for it in defect_items + good_items},
                       allow_test=allow_test)
    if cache is None:
        cache = ba.load_cache(LOCAL_TEMPLATES / "orb_cache.npz")
    templates: dict[str, np.ndarray] = {}
    # Lazy import de tranh phu thuoc nang khi test mock.
    try:
        from scripts.blob_stats import perturb_image_and_boxes as _pert
    except Exception:
        _pert = None

    dets: list = []
    gts: dict[int, list] = {}
    far_triggers = 0
    far_total = len(good_items)
    lats: list[float] = []
    levels: dict[int, int] = {1: 0, 2: 0, 3: 0, 4: 0}
    scenario_error: str | None = None
    crops_list: list[int] = []
    calls_list: list[int] = []
    n_fallback = 0
    n_before_nms = 0
    n_after_nms = 0

    for i, it in enumerate(defect_items + good_items):
        img = read_bgr(it["path"])
        gt_boxes = [list(b) for _, b in it["gt"]]
        if perturb:
            if _pert is None:
                raise RuntimeError("Thieu perturb_image_and_boxes cho scenario camera")
            img, warped = _pert(img, gt_boxes, int(seed_base) + i)
            gt_use = [(c, wb) for (c, _), wb in zip(it["gt"], warped)]
        else:
            gt_use = list(it["gt"])
        gts[i] = gt_use
        try:
            one = run_clustered_single(
                img, yolo, cache, templates, mode=mode, conf=conf,
                image_id=Path(it["path"]).name, log_fallback=log_fallback,
                gating=gating, allow_test=allow_test,
                rescue_conf=rescue_conf,
                rescue_keep_res=rescue_keep_res,
                rescue_r=rescue_r)
            for itm in one["result"]["items"]:
                if itm.get("conf") is not None:
                    dets.append((i, float(itm["conf"]),
                                 [float(v) for v in itm["bbox"]],
                                 int(itm["class_id"])))
            levels[one["result"]["level"]] += 1
            crops_list.append(int(one.get("n_crops", 0)))
            bs = one.get("batch_stats", {})
            calls_list.append(int(bs.get("n_calls", 0)))
            if one.get("fallback_reason"):
                n_fallback += 1
            n_before_nms += int(one.get("n_dets_before_gating", 0))
            n_after_nms += int(one.get("n_dets_after_nms", 0))
            lats.append(float(one.get("t_total_ms", 0.0)))
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            if scenario_error is None:
                scenario_error = err
            lats.append(0.0)
            crops_list.append(0)
            calls_list.append(0)

        if not it["gt"]:
            try:
                if one is not None and one["result"]["level"] < 4:
                    far_triggers += 1
            except Exception:
                pass

    ap, per = voc_ap(dets, gts, thr=DEFAULT_IOU)
    tp, fp, npos = pr_counts(dets, gts, thr=DEFAULT_IOU)
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / npos if npos else 0.0
    far = far_triggers / far_total if far_total else 0.0
    n_img = len(defect_items + good_items)
    return {"tag": f"Dual-Path Clustered (mode={mode},gating={gating})",
            "mode": mode, "gating": gating,
            "AP50": round(ap, 4), "recall": round(rec, 4),
            "precision": round(prec, 4),
            "tp": tp, "fp": fp, "npos": npos, "n_det": len(dets),
            "far": round(far, 4), "far_triggers": far_triggers,
            "far_total": far_total,
            "latency_ms": round(float(np.mean(lats)), 1) if lats else 0.0,
            "num_images": n_img,
            "num_defect": len(defect_items), "num_good": len(good_items),
            "per_class": per, "levels": levels,
            "crops_per_image": round(float(np.mean(crops_list)), 3) if crops_list else 0.0,
            "gpu_calls_per_image": round(float(np.mean(calls_list)), 3) if calls_list else 0.0,
            "fallback_rate": round(n_fallback / n_img, 4) if n_img else 0.0,
            "n_fallback": n_fallback,
            "n_dets_before_nms": n_before_nms, "n_dets_after_nms": n_after_nms,
            "error": scenario_error}


# ---------------------------------------------------------------------------
# CLI (smoke Board 07 — KHONG cham 01/04)
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Phase 4: Dual-Path Clustered (Board 05-12 only).")
    p.add_argument("--crop-mode", choices=list(CROP_MODES), default=DEFAULT_CROP_MODE,
                   help="A1 uniform_context_640 / A2 multi_bucket / B letterbox_640.")
    p.add_argument("--boards", nargs="+", default=["07"],
                   help="Chi Board 05-12 (mac dinh 07). Cam 01/04.")
    p.add_argument("--max-images", type=int, default=6,
                   help="Gioi han so anh loi (smoke 6 anh du 6 lop). None = full boards.")
    p.add_argument("--weights", type=Path, default=None,
                   help="YOLO .pt (mac dinh: assets moi -> goc READ-ONLY).")
    p.add_argument("--conf", type=float, default=DEFAULT_CONF)
    p.add_argument("--rescue-conf", type=float, default=None,
                   help="F2 frag-conf branch (VD 0.12-0.15) tren rescue crops only; "
                        "None (default) = DISABLED, bit-identical baseline. 05-12 only.")
    p.add_argument("--rescue-keep-res", action="store_true",
                   help="F1 (05-12 only): rescue crops A1 giu phan giai bucket "
                        "(imgsz=bucket) thay vi upscale 640; default OFF = "
                        "DISABLED bit-identical 1-call.")
    p.add_argument("--rescue-r", type=float, default=None,
                   help="F3 (05-12 only): rescue dets dung R_rescue bounded "
                        "(80,200]; None (default) = DISABLED bit-identical R80.")
    p.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    p.add_argument("--no-save", action="store_true")
    p.add_argument("--out-json", type=Path,
                   default=PROJECT_ROOT / "results" / "clustered_smoke_board07.json")
    return p


def main() -> None:
    args = build_parser().parse_args()
    _assert_no_leakage(set(args.boards))
    unknown = set(args.boards) - ALLOWED_CALIB_BOARDS
    if unknown:
        raise SystemExit(f"Cam Board {sorted(unknown)} (Phase 4 chi 05-12).")
    w = resolve_weights(args.weights)
    print(f"[clustered] weights: {w}")
    from src.models.yolo_detector import YOLODetector as _YD
    device = None if args.device == "auto" else args.device
    yolo = _YD(str(w), conf_threshold=float(args.conf), device=device)
    cache = ba.load_cache(LOCAL_TEMPLATES / "orb_cache.npz")
    items = collect_clustered_items(set(args.boards))
    if args.max_images is not None:
        # Uu tien du 6 lop: lay 6 mau moi lop 1 anh truoc, roi moi cat.
        six = six_class_samples(items)
        if len(six) >= 6 and int(args.max_images) <= 6:
            items = six[: int(args.max_images)]
        else:
            items = items[: max(0, int(args.max_images))]
    print(f"[clustered] Board {sorted(args.boards)}: {len(items)} anh loi "
          f"(mode={args.crop_mode}).")
    res = run_dualpath_clustered(items, [], yolo, mode=args.crop_mode,
                                 perturb=False, seed_base=0, cache=cache,
                                 conf=float(args.conf), log_fallback=True,
                                 rescue_conf=args.rescue_conf,
                                 rescue_keep_res=bool(args.rescue_keep_res),
                                 rescue_r=args.rescue_r)
    print(f"[clustered] AP50={res['AP50']} R={res['recall']} P={res['precision']} "
          f"TP={res['tp']} FP={res['fp']} FAR={res['far']} "
          f"{res['latency_ms']:.0f}ms/anh levels={res['levels']}")
    print(f"[clustered] crops/anh={res['crops_per_image']} "
          f"gpu-calls/anh={res['gpu_calls_per_image']} "
          f"fallback={res['n_fallback']}/{res['num_images']} "
          f"dets {res['n_dets_before_nms']}->{res['n_dets_after_nms']} (NMS)")
    if not args.no_save:
        out = assert_writable_ok(Path(args.out_json))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[clustered] Da ghi JSON: {out}")
    print("[clustered] DONE (KHONG cham Board 01/04).")


if __name__ == "__main__":
    main()
