"""Phase 3 — Batch bucket inference (plan §2.3, AUDIT-04 / Chot 4).

Ba che do song song tren cung contract hinh hoc Phase 2:
  - A1 `uniform_context_640` (duong chinh khuyen nghi): moi cum s_k<=640
    thanh cua so 640x640 dong tam (SHIFT khi cham bien), cat truc tiep tu
    anh goc, dung 1 lan goi GPU predict_batch(imgsz=640).
  - A2 `multi_bucket_384_512_640`: luong tu hoa s_k theo bucket, cat SxS,
    nhom theo bucket, moi bucket 1 lan goi predict_batch(imgsz=bucket),
    toi da 3 lan goi GPU.
  - B `letterbox_640` (baseline doi chung): pad xam 114 ve 640x640,
    dung 1 lan goi predict_batch(imgsz=640), unpad + loai det tam trong pad.

Bao toan scale 1:1 (Ultralytics voi imgsz == canh crop + rect=True thi
zero-resize; voi B thi unpad truoc khi dich). Anh xa nguoc la phep tinh
tien thuan tuy to_input_coords (khong nhan chia).

CPU-only o day: YOLO object duoc inject tu ngoai (Phase 4 truyen
YOLODetector that; test truyen mock). Module nay khong import torch,
ultralytics, sklearn, khong doc bat ky Board nao (zero-leakage).
"""

from __future__ import annotations

import inspect
import time
from typing import Any

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Plan-locked constants (§2.3.5) — NOT tunable without returning to Phase 1
# ---------------------------------------------------------------------------
BUCKETS = (384, 512, 640)
CROP_MODE_A1 = "uniform_context_640"
CROP_MODE_A2 = "multi_bucket_384_512_640"
CROP_MODE_B = "letterbox_640"
CROP_MODES = (CROP_MODE_A1, CROP_MODE_A2, CROP_MODE_B)
LETTERBOX_SIZE = 640
LETTERBOX_PAD_VALUE = 114
MAX_GPU_CALLS = 3  # A2 toi da 3 lan goi (AUDIT-04)


# ---------------------------------------------------------------------------
# Cua so / luong tu hoa crop
# ---------------------------------------------------------------------------
def center_window(cx: float, cy: float, size: int,
                  W: int, H: int) -> tuple[int, int]:
    """Cua so vuong size×size dong tam (cx,cy), SHIFT (khong co) khi cham bien.

    Luon tra ve goc (x0,y0) sao cho cua so size×size nam tron trong anh.
    """
    s = int(size)
    x0 = int(round(float(cx) - s / 2.0))
    y0 = int(round(float(cy) - s / 2.0))
    x0 = min(max(x0, 0), max(int(W) - s, 0))
    y0 = min(max(y0, 0), max(int(H) - s, 0))
    return x0, y0


def quantize(s: float) -> int:
    """Luong tu hoa canh vuong he Input (chi dung cho A2).

    s <= 384 -> 384; 385..512 -> 512; 513..640 -> 640; > 640 -> 640
    (vuot cap xu ly boi bisection Phase 2 truoc khi toi day; dung
    needs_cap de phat hien truong hop lot).
    """
    v = float(s)
    if v <= 384.0:
        return 384
    if v <= 512.0:
        return 512
    return 640


def needs_cap(s: float) -> bool:
    """True neu canh vuong vuot 640 (le ra khong bao gio xay ra sau bisect)."""
    return float(s) > 640.0


# ---------------------------------------------------------------------------
# Anh xa toa do: tinh tien thuan tuy (scale 1:1)
# ---------------------------------------------------------------------------
def to_input_coords(bbox_crop: list[float],
                    origin: tuple[int, int]) -> list[float]:
    """Add crop origin to crop bbox to get input bbox.

    Pure addition of origin offsets, output keeps input size.
    """
    x0, y0 = origin
    x1, y1, x2, y2 = bbox_crop
    return [x1 + x0, y1 + y0, x2 + x0, y2 + y0]


# ---------------------------------------------------------------------------
# Letterbox B (pad xam 114 ve 640, unpad nguoc)
# ---------------------------------------------------------------------------
def letterbox_offset(s: int, size: int = LETTERBOX_SIZE) -> int:
    """Do lech pad moi phia khi dat crop s×s vao giua khung size×size."""
    return (int(size) - int(s)) // 2


def letterbox_pad(box: list[float], s: int,
                  size: int = LETTERBOX_SIZE) -> list[float]:
    """Dua bbox he crop s×s sang he letterboxed size×size (cong offset pad)."""
    o = letterbox_offset(s, size)
    x1, y1, x2, y2 = (float(box[0]), float(box[1]),
                      float(box[2]), float(box[3]))
    return [x1 + o, y1 + o, x2 + o, y2 + o]


def letterbox_unpad(box: list[float], s: int,
                    size: int = LETTERBOX_SIZE) -> list[float]:
    """Nguoc cua letterbox_pad (tru offset pad)."""
    o = letterbox_offset(s, size)
    x1, y1, x2, y2 = (float(box[0]), float(box[1]),
                      float(box[2]), float(box[3]))
    return [x1 - o, y1 - o, x2 - o, y2 - o]


def letterbox_pad_image(crop_rgb: np.ndarray, s: int,
                        size: int = LETTERBOX_SIZE,
                        pad_value: int = LETTERBOX_PAD_VALUE) -> np.ndarray:
    """Pad anh crop s×s (RGB uint8) vao giua khung size×size mau xam pad_value."""
    o = letterbox_offset(s, size)
    canvas = np.full((int(size), int(size), 3), int(pad_value), dtype=np.uint8)
    c = np.asarray(crop_rgb, dtype=np.uint8)
    h, w = c.shape[:2]
    canvas[o:o + h, o:o + w] = c[:int(size) - o, :int(size) - o]
    return canvas


def is_in_pad_zone(center_xy: tuple[float, float], s: int,
                   size: int = LETTERBOX_SIZE) -> bool:
    """True neu tam det nam trong vung pad (khong phai vung anh that)."""
    o = letterbox_offset(s, size)
    cx, cy = float(center_xy[0]), float(center_xy[1])
    return bool(cx < o or cy < o or cx >= o + s or cy >= o + s)


# ---------------------------------------------------------------------------
# Dung batch input tu geometry Phase 2 (chua goi GPU)
# ---------------------------------------------------------------------------
def build_batch_inputs(image_bgr: np.ndarray,
                       crops_geo: list[dict[str, Any]],
                       mode: str,
                       rescue_keep_res: bool = False) -> list[dict[str, Any]]:
    """Cat crop RGB + metadata cho 1 lan (A1) hoac nhieu nhom (A2, B).

    crops_geo: list dict tu clusters_to_crops (cx, cy, side, cluster_id...).
    Tra ve list dict {image_rgb, origin, s_cut, imgsz_call, cluster_id,
    sub_id, z_cluster, pad_s}. Khong goi GPU, khong doc file.
    F1 (Verifier lan9, Coder lan10, 05-12 only): rescue_keep_res=False
    (default) => bit-identical A1 1-call. True => chi rescue crops
    is_frag_rescue o mode A1 cat theo bucket (384/512/640) + imgsz=bucket
    (giu phan giai, tranh upscale 640 lam mo frag nho); main crops giu 640.
    Toi da 3 nhom GPU (AUDIT-04 preserved, 640 shared).
    A2/B + F1: giu nguyen (A2 da bucket, B la baseline ablation).
    """
    if mode not in CROP_MODES:
        raise ValueError(f"mode phai thuoc {CROP_MODES}, nhan {mode!r}")
    h, w = image_bgr.shape[:2]
    out: list[dict[str, Any]] = []
    for c in crops_geo:
        cx, cy = float(c["cx"]), float(c["cy"])
        side = float(c["side"])
        is_rescue = bool(c.get("is_frag_rescue", False))
        if mode == CROP_MODE_A1 and bool(rescue_keep_res) and is_rescue:
            # F1: rescue giu phan giai native/bucket (scale 1:1, nhu A2).
            s_cut = quantize(side)
            call = s_cut
            x0, y0 = center_window(cx, cy, s_cut, w, h)
            cut = image_bgr[y0:y0 + s_cut, x0:x0 + s_cut]
            pad_s = 0
        elif mode == CROP_MODE_A1:
            s_cut, call = LETTERBOX_SIZE, LETTERBOX_SIZE
            x0, y0 = center_window(cx, cy, s_cut, w, h)
            cut = image_bgr[y0:y0 + s_cut, x0:x0 + s_cut]
            pad_s = 0
        elif mode == CROP_MODE_A2:
            s_cut = quantize(side)
            call = s_cut
            x0, y0 = center_window(cx, cy, s_cut, w, h)
            cut = image_bgr[y0:y0 + s_cut, x0:x0 + s_cut]
            pad_s = 0
        else:  # CROP_MODE_B: cat theo bucket roi letterbox ve 640
            s_cut = quantize(side)
            call = LETTERBOX_SIZE
            x0, y0 = center_window(cx, cy, s_cut, w, h)
            raw = image_bgr[y0:y0 + s_cut, x0:x0 + s_cut]
            rgb = cv2.cvtColor(raw, cv2.COLOR_BGR2RGB)
            lb = letterbox_pad_image(rgb, s_cut)
            out.append({"image_rgb": lb, "origin": (x0, y0),
                        "s_cut": int(s_cut), "imgsz_call": int(call),
                        "cluster_id": int(c.get("cluster_id", 0)),
                        "sub_id": int(c.get("sub_id", 0)),
                        "z_cluster": float(c.get("z_cluster", 0.0)),
                        "pad_s": int(s_cut),
                        "is_frag_rescue": bool(c.get("is_frag_rescue", False))})
            continue
        rgb = cv2.cvtColor(cut, cv2.COLOR_BGR2RGB)
        out.append({"image_rgb": rgb, "origin": (x0, y0),
                    "s_cut": int(s_cut), "imgsz_call": int(call),
                    "cluster_id": int(c.get("cluster_id", 0)),
                    "sub_id": int(c.get("sub_id", 0)),
                    "z_cluster": float(c.get("z_cluster", 0.0)),
                    "pad_s": 0,
                    "is_frag_rescue": bool(c.get("is_frag_rescue", False))})
    return out


# ---------------------------------------------------------------------------
# Goi GPU (qua YOLODetector.predict_batch — ep rect=True ben trong)
# ---------------------------------------------------------------------------
def _assert_rect_enforced(yolo: Any) -> None:
    """Bao dam moi lan goi GPU ep rect=True (Chot 3 goc).

    Voi YOLODetector that: kiem bang inspect source predict_batch phai
    chua rect=True, thieu thi raise (chong regression). Voi mock trong
    test (module khac): bo qua de mock dem so lan goi.
    """
    try:
        mod = type(yolo).__module__
    except Exception:  # noqa: BLE001
        return
    if "yolo_detector" not in str(mod):
        return  # mock/fake trong test — bo qua guard
    try:
        src = inspect.getsource(type(yolo).predict_batch)
    except (OSError, TypeError):
        return
    if "rect=True" not in src.replace(" ", "").replace("\t", ""):
        raise RuntimeError(
            "YOLODetector.predict_batch mat rect=True (Chot 3) — "
            "cam goi GPU khi chua ep rect=True")


def _predict_batch(yolo: Any, crops: list[np.ndarray],
                   imgsz: int, conf: float | None = None) -> list[Any]:
    """Mot lan goi GPU predict_batch (rect=True ep ben trong YOLODetector).

    Chu thich rect=True o day de thay: moi duong goi predict trong module
    nay deu di qua YOLODetector.predict_batch voi rect=True, half theo
    thiet bi (Chot 3 goc, ke thua tu project goc).
    """
    _assert_rect_enforced(yolo)  # rect=True guard (Chot 3)
    if conf is None:
        return yolo.predict_batch(crops, imgsz=int(imgsz))  # rect=True inside
    return yolo.predict_batch(crops, conf=float(conf),
                              imgsz=int(imgsz))  # rect=True inside


def predict_clusters(batch_inputs: list[dict[str, Any]],
                     yolo: Any,
                     mode: str,
                     conf: float | None = None,
                     rescue_conf: float | None = None) -> tuple[list[dict[str, Any]],
                                                        dict[str, Any]]:
    """Chay YOLO tren batch inputs, tra dets he INPUT + stats.

    - A1: dung 1 lan goi predict_batch(imgsz=640).
    - A2: nhom theo bucket, moi bucket 1 lan goi predict_batch
      (imgsz=bucket), toi da 3 lan goi, nhom rong bo qua.
    - B: moi crop da letterbox 640, dung 1 lan goi predict_batch
      (imgsz=640), unpad + loai det tam trong vung pad.
    - Moi det crop duoc dich ve he INPUT bang to_input_coords (tinh tien
      thuan tuy, scale 1:1) truoc khi tra ve; gan them cluster_id va
      crop_origin (backward-compatible voi fuse/decide).
    - stats: {mode, n_calls, calls:[{imgsz, n_crops, ms}], n_dets}.
    - F2 frag-conf branch (Verifier conditional lan8, Coder lan9, 05-12 only):
      rescue crops (is_frag_rescue True, tu clusters_to_crops D1-sua) duoc
      chay voi nguong thap hon rescue_conf (VD 0.12-0.15), main crops giu
      baseline conf (VD 0.20). Zero extra GPU calls: chay YOLO 1 lan tai
      min(conf, rescue_conf) roi post-filter main dets ve baseline.
      rescue_conf=None (default) => DISABLED, bit-identical baseline
      (AUDIT-04 caps preserved: A1 1-call, A2 <=3-call).
    """
    if mode not in CROP_MODES:
        raise ValueError(f"mode phai thuoc {CROP_MODES}, nhan {mode!r}")
    stats: dict[str, Any] = {"mode": mode, "n_calls": 0,
                             "calls": [], "n_dets": 0}
    if not batch_inputs:
        return [], stats
    # F2: xac dinh rescue crops + conf hieu dung (khong them GPU call).
    has_rescue = bool(any(bool(b.get("is_frag_rescue", False))
                          for b in batch_inputs))
    baseline_conf = float(conf) if conf is not None else None
    rescue_c = float(rescue_conf) if rescue_conf is not None else None
    if rescue_c is not None and has_rescue and baseline_conf is not None:
        eff_conf: float | None = min(baseline_conf, rescue_c)
        need_post_filter = bool(rescue_c < baseline_conf)
    elif rescue_c is not None and has_rescue and baseline_conf is None:
        # conf=None (YOLO default): chay rescue_conf cho ca batch, khong
        # post-filter main (hiem, benchmark luon truyen conf explicit).
        eff_conf = rescue_c
        need_post_filter = False
    else:
        eff_conf = conf
        need_post_filter = False
    n_rescue_crops = int(sum(1 for b in batch_inputs
                             if bool(b.get("is_frag_rescue", False))))
    stats["conf"] = baseline_conf
    stats["rescue_conf"] = rescue_c
    stats["conf_effective"] = float(eff_conf) if eff_conf is not None else None
    stats["n_rescue_crops"] = n_rescue_crops
    stats["n_main_crops"] = int(len(batch_inputs) - n_rescue_crops)
    # Nhom theo imgsz_call (A1/B: 1 nhom; A2: toi da 3 nhom).
    groups: dict[int, list[int]] = {}
    for i, b in enumerate(batch_inputs):
        groups.setdefault(int(b["imgsz_call"]), []).append(i)
    if len(groups) > MAX_GPU_CALLS:
        raise RuntimeError(
            f"So nhom GPU {len(groups)} vuot cap {MAX_GPU_CALLS} (AUDIT-04)")
    dets_input: list[dict[str, Any]] = []
    for imgsz in sorted(groups):
        idxs = groups[imgsz]
        crops = [batch_inputs[i]["image_rgb"] for i in idxs]
        t0 = time.perf_counter()
        batch = _predict_batch(yolo, crops, imgsz, eff_conf)  # rect=True inside
        ms = (time.perf_counter() - t0) * 1000.0
        stats["calls"].append({"imgsz": int(imgsz),
                               "n_crops": len(idxs),
                               "ms": round(float(ms), 3)})
        for k, dets_one in zip(idxs, batch):
            meta = batch_inputs[k]
            ox, oy = meta["origin"]
            for d in dets_one:
                dd = dict(d)
                b = [float(v) for v in dd["bbox"]]
                if mode == CROP_MODE_B:
                    s = int(meta["pad_s"])
                    cx_lb = (b[0] + b[2]) / 2.0
                    cy_lb = (b[1] + b[3]) / 2.0
                    if is_in_pad_zone((cx_lb, cy_lb), s):
                        continue  # det tam trong vung pad -> loai 100%
                    b = letterbox_unpad(b, s)
                dd["bbox"] = to_input_coords(b, (ox, oy))
                dd["cluster_id"] = int(meta["cluster_id"])
                dd["crop_origin"] = (int(ox), int(oy))
                dd["is_frag_rescue"] = bool(meta.get("is_frag_rescue", False))
                dets_input.append(dd)
    # F2 post-filter: main crops ve baseline, rescue giu rescue_conf.
    if need_post_filter and baseline_conf is not None:
        kept: list[dict[str, Any]] = []
        for d in dets_input:
            try:
                c = float(d.get("conf", 0.0)) if d.get("conf") is not None else float("-inf")
            except Exception:
                c = float("-inf")
            if bool(d.get("is_frag_rescue", False)):
                kept.append(d)  # YOLO da loc o rescue_c
            else:
                if c >= float(baseline_conf):
                    kept.append(d)
        dets_input = kept
    dets_input.sort(key=lambda d: float(d.get("conf", 0.0)
                                       if d.get("conf") is not None
                                       else float("-inf")),
                    reverse=True)
    stats["n_calls"] = len(stats["calls"])
    stats["n_dets"] = len(dets_input)
    return dets_input, stats
