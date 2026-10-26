"""Phase 2 — Union-of-Disks gating + Cross-Crop NMS (plan §2.4 + AUDIT-05).

CPU-only, numpy-only (khong torch / torchvision / ultralytics).
Tat ca toa do o he INPUT (anh goc) — tam blob da chieu qua H_inv
truoc khi goi gate (Chot 1).

Thu tu bat buoc: gating → NMS → fuse (Phase 4 goi ensemble.fuse).
"""

from __future__ import annotations

from typing import Any

import numpy as np

R_BASE = 80.0  # px, giu nguyen tu project goc (khong phat minh nguong moi)
CROSS_CROP_NMS_IOU = 0.50  # khop DEFAULT_IOU=0.5 cua evaluate.py
MARGIN_DELTA = 40.0  # le H_C cho ablation Bounding-Margin
# F3 (Verifier lan9, Coder lan10, 05-12 only): bound cung cho R_rescue.
# R_base=80 < R_rescue <= 200. Vuot bound => ValueError (khong clamp am tham).
RESCUE_R_MAX = 200.0


# ---------------------------------------------------------------------------
# IoU helpers (vectorized, khop cong thuc legacy _box_iou)
# ---------------------------------------------------------------------------
def _iou_matrix(boxes_a: np.ndarray, boxes_b: np.ndarray) -> np.ndarray:
    """IoU matrix (M, n) giong cong thuc legacy (max/min, inter, union)."""
    a = np.asarray(boxes_a, dtype=np.float64)
    b = np.asarray(boxes_b, dtype=np.float64)
    if a.size == 0 or b.size == 0:
        return np.zeros((a.shape[0] if a.size else 0,
                         b.shape[0] if b.size else 0), dtype=np.float64)
    x1 = np.maximum(a[:, None, 0], b[None, :, 0])
    y1 = np.maximum(a[:, None, 1], b[None, :, 1])
    x2 = np.minimum(a[:, None, 2], b[None, :, 2])
    y2 = np.minimum(a[:, None, 3], b[None, :, 3])
    iw = np.maximum(x2 - x1, 0.0)
    ih = np.maximum(y2 - y1, 0.0)
    inter = iw * ih
    area_a = (a[:, 2] - a[:, 0]) * (a[:, 3] - a[:, 1])
    area_b = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
    ua = area_a[:, None] + area_b[None, :] - inter
    out = np.zeros_like(inter)
    np.divide(inter, ua, out=out, where=ua > 0.0)
    return out


def _det_boxes_input(dets: list[dict[str, Any]]) -> np.ndarray:
    if not dets:
        return np.zeros((0, 4), dtype=np.float64)
    return np.asarray([[float(v) for v in d["bbox"]] for d in dets],
                      dtype=np.float64)


def _det_centers_input(dets: list[dict[str, Any]]) -> np.ndarray:
    b = _det_boxes_input(dets)
    if len(b) == 0:
        return np.zeros((0, 2), dtype=np.float64)
    return np.stack([(b[:, 0] + b[:, 2]) / 2.0,
                     (b[:, 1] + b[:, 3]) / 2.0], axis=1)


def _default_diff_boxes(centers: np.ndarray,
                        r_base: float = R_BASE) -> np.ndarray:
    c = np.asarray(centers, dtype=np.float64)
    r = float(r_base)
    return np.stack([c[:, 0] - r, c[:, 1] - r,
                     c[:, 0] + r, c[:, 1] + r], axis=1)


# ---------------------------------------------------------------------------
# Co che U — Union-of-Disks gating (duong chinh, §2.4.2)
# ---------------------------------------------------------------------------
def union_of_disks_gate(dets: list[dict[str, Any]],
                        cluster_blob_centers_input: Any,
                        r_base: float = R_BASE,
                        diff_boxes: Any | None = None,
                        require_overlap: bool = True,
                        rescue_r: float | None = None) -> list[dict[str, Any]]:
    """Giu det neu tam nam trong hop cac dia R quanh moi tam blob
    HE INPUT, HOAC IoU(det, diff_box) > 0 voi bat ky blob nao (§2.4.2).

    - dets: bbox theo HE INPUT (sau to_input_coords o Phase 4).
    - cluster_blob_centers_input: (n, 2) he Input (da chieu qua H_inv).
    - diff_boxes: (n, 4) he Input; None = o vuong 2R quanh moi tam
      (suy bien chinh xac ve spatial_center_gating khi |C| = 1).
    - require_overlap=False: chi xet dia (giong co legacy).
    - Don dieu theo cum: them blob chi giu them, khong loai (§2.4.2).
    - Vectorized < 1 ms (T-GAT-04).
    - F3 (Verifier lan9, Coder lan10, 05-12 only): rescue_r=None
      (default) => DISABLED, bit-identical baseline (tat ca dets dung
      r_base=80). Float => chi dets is_frag_rescue True dung R_rescue,
      main dets van R_base=80; IoU OR>0 giu nguyen (diff_boxes R80
      shared, khong mo rong) de han che FAR. Bound cung:
      R_base < R_rescue <= RESCUE_R_MAX (200). Vuot bound => ValueError
      (khong clamp am tham). NMS van sau gating truoc fuse (Phase 4).
    """
    if not dets:
        return []
    # F3 bound validation (khong clamp).
    if rescue_r is None:
        r_res: float | None = None
    else:
        r_res = float(rescue_r)
        if not (float(R_BASE) < r_res <= float(RESCUE_R_MAX)):
            raise ValueError(
                f"rescue_r phai thoa {float(R_BASE)} < rescue_r <= "
                f"{float(RESCUE_R_MAX)}, nhan {r_res!r}")
    centers = np.asarray(cluster_blob_centers_input, dtype=np.float64)
    if centers.size == 0:
        return []
    centers = centers.reshape(-1, 2)
    if diff_boxes is None:
        dboxes = _default_diff_boxes(centers, r_base)
    else:
        dboxes = np.asarray(diff_boxes, dtype=np.float64).reshape(-1, 4)
    p = _det_centers_input(dets)          # (M, 2)
    d = np.linalg.norm(p[:, None, :] - centers[None, :, :], axis=-1)  # (M, n)
    dmin = d.min(axis=1)
    if r_res is None:
        keep_disk = dmin <= float(r_base)
    else:
        # F3 per-det: rescue dets dung R_rescue, main van R_base=80.
        is_rescue = np.asarray(
            [bool(dd.get("is_frag_rescue", False)) for dd in dets],
            dtype=bool)
        keep_disk = np.where(is_rescue, dmin <= float(r_res),
                             dmin <= float(r_base))
    if require_overlap:
        iou = _iou_matrix(_det_boxes_input(dets), dboxes)  # (M, n)
        keep_iou = (iou > 0.0).any(axis=1)
        keep = keep_disk | keep_iou
    else:
        keep = keep_disk
    return [dets[i] for i in range(len(dets)) if bool(keep[i])]


# ---------------------------------------------------------------------------
# Tang Cross-Crop NMS — class-aware, IoU=0.50 (AUDIT-05, §2.4.4)
# ---------------------------------------------------------------------------
def _conf_of(det: dict[str, Any]) -> float:
    c = det.get("conf")
    return float(c) if c is not None else float("-inf")


def cross_crop_nms(dets_input: list[dict[str, Any]],
                   iou_thres: float = CROSS_CROP_NMS_IOU) -> list[dict[str, Any]]:
    """Gop box trung lap giua cac crop chong lan (ke ca manh bisection).

    - Class-aware: chi triet tieu trung lap CUNG class_id (short de len
      spurious_copper la 2 loi that khac nhau — khong triet lan nhau).
    - Nguong IoU = 0.50 (khop DEFAULT_IOU danh gia).
    - Giu box conf cao nhat moi nhom; chay SAU gating, TRUOC fuse.
    - O(M^2), M <= ~50 → < 0.1 ms; numpy-only.
    """
    if not dets_input:
        return []
    thr = float(iou_thres)
    # Nhom theo class_id (None thanh nhom rieng).
    groups: dict[Any, list[int]] = {}
    for i, d in enumerate(dets_input):
        groups.setdefault(d.get("class_id"), []).append(i)
    boxes = _det_boxes_input(dets_input)
    confs = np.asarray([_conf_of(d) for d in dets_input], dtype=np.float64)
    kept_idx: list[int] = []
    for _cls, idxs in groups.items():
        gi = np.asarray(idxs, dtype=np.int64)
        gb = boxes[gi]
        gc = confs[gi]
        order = np.argsort(-gc, kind="stable")  # conf giam dan
        iou_mat = _iou_matrix(gb, gb)  # (k,k) — tinh 1 lan, khong goi lai moi vong
        k = len(gi)
        suppressed = np.zeros(k, dtype=bool)
        for ii in range(k):
            oi = int(order[ii])
            if suppressed[oi]:
                continue
            kept_idx.append(int(gi[oi]))
            if ii < k - 1:
                row = iou_mat[oi]
                # Chi triet cac box dung sau (conf <=) co IoU > thr.
                later = order[ii + 1:]
                hit = later[row[later] > thr]
                suppressed[hit] = True
    kept = [dets_input[i] for i in kept_idx]
    kept.sort(key=_conf_of, reverse=True)
    return kept


# ---------------------------------------------------------------------------
# Co che H — Cluster Bounding Margin gating (ABLATION, §2.4.3)
# ---------------------------------------------------------------------------
def margin_gate_ablation(dets: list[dict[str, Any]],
                         diff_boxes: Any,
                         delta: float = MARGIN_DELTA) -> list[dict[str, Any]]:
    """Phuong an so sanh (KHONG phai duong chinh).

    H_C = AABB cua toan bo diff_box + le delta moi phia.
    Giu det neu IoU(det, H_C) > 0 HOAC tam det nam trong H_C.
    """
    if not dets:
        return []
    b = np.asarray(diff_boxes, dtype=np.float64).reshape(-1, 4)
    if b.shape[0] == 0:
        return []
    d = float(delta)
    hc = [float(b[:, 0].min()) - d, float(b[:, 1].min()) - d,
          float(b[:, 2].max()) + d, float(b[:, 3].max()) + d]
    boxes = _det_boxes_input(dets)
    centers = _det_centers_input(dets)
    iou = _iou_matrix(boxes, np.asarray([hc], dtype=np.float64))[:, 0]
    inside = ((centers[:, 0] >= hc[0]) & (centers[:, 0] <= hc[2])
              & (centers[:, 1] >= hc[1]) & (centers[:, 1] <= hc[3]))
    keep = (iou > 0.0) | inside
    return [dets[i] for i in range(len(dets)) if bool(keep[i])]


# ---------------------------------------------------------------------------
# Adapter: gan cluster_id vao evidence (backward-compatible voi decide)
# ---------------------------------------------------------------------------
def attach_cluster_id(items: list[dict[str, Any]],
                      cluster_id: int) -> list[dict[str, Any]]:
    """Them cluster_id vao moi evidence item (decide bo qua truong thua)."""
    for it in items:
        it["cluster_id"] = int(cluster_id)
    return items
