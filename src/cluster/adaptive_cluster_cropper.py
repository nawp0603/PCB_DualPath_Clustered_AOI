"""Phase 2 — Adaptive cluster cropper (plan §2.2.5 + §3.3).

Hinh hoc thuan, CPU-only, numpy + cv2 (perspectiveTransform) only.
Khong torch / ultralytics / sklearn o main path (bisect fallback
sklearn-free khi thieu sklearn).

Quy uoc toa do (Chot 1):
  - Gom cum HOAN TOAN o he Template (xem dbscan_clustering.py).
  - Chi sau khi co hop bao cum + le p=48 (he Template) moi chieu 4 goc
    qua H_inv ve he Input, lay AABB, clamp, vuong hoa bang SHIFT (khong co).

Contract:
  clusters_to_crops(clusters, blobs, h_inv, img_shape,
                    max_cluster_crops=8)
    -> (crops, dropped_log)
    crops: list[dict](sorted Z_cluster giam, toi da 8) moi dict:
      {cluster_id, members, z_cluster, size, box_tpl, quad_input,
       box_input, cx, cy, side, origin, truncated, bisect_depth}
    dropped_log: list[dict] cac cum hang 9+ bi loai (AUDIT-03).
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from src.cluster.dbscan_clustering import (
    BISECT_CAP_PX,
    BISECT_MAX_DEPTH,
    CONTEXT_MARGIN_P,
    MAX_CLUSTER_CROPS,
    bisect_oversize_cluster,
)

TRUNCATED_FLAG = True


# ---------------------------------------------------------------------------
# Hinh hoc Template -> Input (Chot 1: chi chieu khi da co thuc the hinh hoc)
# ---------------------------------------------------------------------------
def project_points(pts: np.ndarray, h_inv: np.ndarray) -> np.ndarray:
    arr = np.asarray(pts, dtype=np.float32).reshape(-1, 1, 2)
    return cv2.perspectiveTransform(
        arr, np.asarray(h_inv, dtype=np.float64)).reshape(-1, 2)


def union_box_tpl(boxes_tpl: list[list[float]],
                  margin: float = CONTEXT_MARGIN_P) -> list[float]:
    """Hop bao he Template + le ngu canh p (moi phia)."""
    x1 = min(float(b[0]) for b in boxes_tpl) - float(margin)
    y1 = min(float(b[1]) for b in boxes_tpl) - float(margin)
    x2 = max(float(b[2]) for b in boxes_tpl) + float(margin)
    y2 = max(float(b[3]) for b in boxes_tpl) + float(margin)
    return [x1, y1, x2, y2]


def project_box_to_input(box_tpl: list[float], h_inv: np.ndarray,
                         img_shape: tuple[int, int]) -> tuple[np.ndarray, list[float]]:
    """Chieu 4 goc hop bao Template qua H_inv -> (quad_input, aabb_clamped)."""
    h_img, w_img = int(img_shape[0]), int(img_shape[1])
    x1, y1, x2, y2 = (float(box_tpl[0]), float(box_tpl[1]),
                      float(box_tpl[2]), float(box_tpl[3]))
    corners = np.float32([[x1, y1], [x2, y1], [x2, y2], [x1, y2]])
    h = np.asarray(h_inv, dtype=np.float64)
    if h.shape != (3, 3) or not bool(np.all(np.isfinite(h))):
        raise ValueError(
            "H_inv khong hop le (khong phai 3x3 huu han) "
            "(Phase 4 catch -> fallback TOP-8, plan §5.4 muc 3)")
    if abs(float(np.linalg.det(h))) < 1e-12:
        raise ValueError(
            "H_inv suy bien (|det| ~ 0) "
            "(Phase 4 catch -> fallback TOP-8, plan §5.4 muc 3)")
    q = project_points(corners, h)
    if not bool(np.all(np.isfinite(q))):
        raise ValueError(
            "H_inv suy bien: quad_input chua NaN/inf "
            "(Phase 4 catch -> fallback TOP-8, plan §5.4 muc 3)")
    X1, Y1 = float(q[:, 0].min()), float(q[:, 1].min())
    X2, Y2 = float(q[:, 0].max()), float(q[:, 1].max())
    X1, Y1 = max(X1, 0.0), max(Y1, 0.0)
    X2, Y2 = min(X2, float(w_img)), min(Y2, float(h_img))
    return q, [X1, Y1, X2, Y2]


def squareize_box(box_input: list[float],
                  img_shape: tuple[int, int]) -> tuple[float, float, float]:
    """Vuong hoa AABB he Input bang SHIFT (khong co).

    Return (cx, cy, side) voi side = max(dx, dy). Tam la tam AABB.
    Clamp/shift tam de cua so side×side nam tron trong anh (T-GEO-03).
    Neu side > kich thuoc anh thi cap ve min(H, W) (truong hop cum
    khong lo, bao ve bien).
    """
    h_img, w_img = int(img_shape[0]), int(img_shape[1])
    X1, Y1, X2, Y2 = (float(box_input[0]), float(box_input[1]),
                      float(box_input[2]), float(box_input[3]))
    dx, dy = max(X2 - X1, 0.0), max(Y2 - Y1, 0.0)
    side = float(max(dx, dy))
    side = min(side, float(min(h_img, w_img)))
    cx, cy = (X1 + X2) / 2.0, (Y1 + Y2) / 2.0
    half = side / 2.0
    # Shift (khong co): giu nguyen side, truot tam vao trong anh.
    cx = min(max(cx, half), float(w_img) - half) if side < w_img else float(w_img) / 2.0
    cy = min(max(cy, half), float(h_img) - half) if side < h_img else float(h_img) / 2.0
    return float(cx), float(cy), float(side)


def cluster_square_side(cluster_boxes_tpl: list[list[float]],
                        h_inv: np.ndarray,
                        img_shape: tuple[int, int],
                        margin: float = CONTEXT_MARGIN_P) -> float:
    """Canh vuong s_k he Input (union + margin, H_inv, AABB, square).

    Bit-identical voi scripts/calibrate_dbscan.py (dung cho bisect).
    """
    box = union_box_tpl(cluster_boxes_tpl, margin)
    _, aabb = project_box_to_input(box, h_inv, img_shape)
    dx = max(aabb[2] - aabb[0], 0.0)
    dy = max(aabb[3] - aabb[1], 0.0)
    return float(max(dx, dy))


def center_origin(cx: float, cy: float, side: float,
                  img_shape: tuple[int, int]) -> tuple[int, int]:
    """Goc (x0, y0) cua so side×side dong tam (cx,cy), SHIFT khi cham bien."""
    h_img, w_img = int(img_shape[0]), int(img_shape[1])
    half = float(side) / 2.0
    x0 = int(round(float(cx) - half))
    y0 = int(round(float(cy) - half))
    x0 = min(max(x0, 0), max(int(w_img - round(side)), 0))
    y0 = min(max(y0, 0), max(int(h_img - round(side)), 0))
    return x0, y0


# ---------------------------------------------------------------------------
# Rank-guard (Gate Decision 2026-10-26 (i), Phase 1 return 1a)
# + D1-sua frag-aware rescue (Verifier Decision D1-D4 2026-10-26, Phase 5 lan4)
# ---------------------------------------------------------------------------
RANK_TOP_N = 8  # TOP-8 baseline threshold (AUDIT-03): rank9+ = idx >= 8
FRAG_RADIUS_PX = 250.0  # CHOT Verifier D1-sua: cung dinh nghia metrics_at_eps
# FRAG_RADIUS (plan §3.4). Proxy frag: 1 GT vo >=2 manh cach nhau <=250px Input.
MAX_FRAG_RESCUE_SLOTS = 2  # toi da 2 slot/anh cho frag rescue (Verifier §D)


def is_span_cluster(members: list[int], top_n: int = RANK_TOP_N) -> bool:
    """Span-cutoff cluster: chua ca thanh vien top8 (idx<8) va rank9+ (idx>=8).

    Dung de nhan dien cum cuu slot rank9-12 (S5.1 Nhom 1). members la chi so
    trong blobs list (da sort Z giam, top-K). K=8 -> luon False (khong span).
    K=12 -> idx 8-11 la rank9-12; K=16 -> idx 8-15 la rank9+.
    """
    if len(list(members)) < 2:
        return False
    has_low = any(int(m) < int(top_n) for m in members)
    has_high = any(int(m) >= int(top_n) for m in members)
    return bool(has_low and has_high)


def _blob_centers_input(blobs: list[dict[str, Any]],
                        h_inv: np.ndarray) -> np.ndarray:
    """Tam blob he Input (chieu centroid_tpl qua H_inv). D1-sua dung do khoang
    cach dropped<->dropped he Input (proxy frag, khong can GT luc inference)."""
    if not blobs:
        return np.zeros((0, 2), dtype=np.float64)
    tpl = np.float32([b["centroid_tpl"] for b in blobs])
    return np.asarray(project_points(tpl, np.asarray(h_inv, dtype=np.float64)),
                       dtype=np.float64)


def find_frag_rescue_pairs(
        dropped: list[dict[str, Any]],
        centers_input: np.ndarray,
        frag_radius_px: float = FRAG_RADIUS_PX,
        max_slots: int = MAX_FRAG_RESCUE_SLOTS,
        top_n: int = RANK_TOP_N,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Tim cap singleton rank9-12 dropped cach nhau <= frag_radius (D1-sua).

    - Chi xet cum singleton (size==1) co member idx>=top_n (rank9+). Cum
      multi-blob / idx<8 khong thuoc dien rescue (span-guard da xu ly).
    - Khoang cach do he INPUT (centers_input, Euclidean) — proxy suy doan
      frag: 1 GT vo >=2 manh (Verifier Decision D1-D4 §D: board12 blob8<->10
      247.5px). D1-cu (kept<->dropped) FAILED vi blob lost cach kept 669/886px.
    - Toi da max_slots slot/anh (doi thanh toi da max_slots//2 cap, tham lam
      khong trung lap), uu tien cap tong Z cao nhat (deterministic tie-break
      theo min member idx). K=8 -> rong (khong co idx>=8).
    """
    max_pairs = max(0, int(max_slots) // 2)
    if max_pairs <= 0 or not dropped:
        return []
    ci = np.asarray(centers_input, dtype=np.float64).reshape(-1, 2)
    cands = [d for d in list(dropped)
             if int(len(list(d.get("members", [])))) == 1
             and int(list(d.get("members", []))[0]) >= int(top_n)]
    if len(cands) < 2:
        return []
    scored: list[tuple[float, int, dict[str, Any], dict[str, Any]]] = []
    for i in range(len(cands)):
        mi = int(list(cands[i].get("members", []))[0])
        if mi >= len(ci):
            continue
        for j in range(i + 1, len(cands)):
            mj = int(list(cands[j].get("members", []))[0])
            if mj >= len(ci):
                continue
            dist = float(np.hypot(float(ci[mi, 0]) - float(ci[mj, 0]),
                                  float(ci[mi, 1]) - float(ci[mj, 1])))
            if dist <= float(frag_radius_px):
                s = float(cands[i].get("z_cluster", 0.0)) \
                    + float(cands[j].get("z_cluster", 0.0))
                scored.append((s, min(mi, mj), cands[i], cands[j]))
    scored.sort(key=lambda t: (-t[0], t[1]))
    picked: list[tuple[dict[str, Any], dict[str, Any]]] = []
    used: set[int] = set()
    for s, _, a, b in scored:
        if len(picked) >= max_pairs:
            break
        ma = int(list(a.get("members", []))[0])
        mb = int(list(b.get("members", []))[0])
        if ma in used or mb in used:
            continue
        picked.append((a, b))
        used.add(ma)
        used.add(mb)
    return picked


# ---------------------------------------------------------------------------
# Contract chinh: clusters -> crops (geometry only, chua cat anh)
# ---------------------------------------------------------------------------
def clusters_to_crops(clusters: list[dict[str, Any]],
                      blobs: list[dict[str, Any]],
                      h_inv: np.ndarray,
                      img_shape: tuple[int, int],
                      max_cluster_crops: int = MAX_CLUSTER_CROPS,
                      margin: float = CONTEXT_MARGIN_P,
                      cap_px: float = BISECT_CAP_PX,
                      max_depth: int = BISECT_MAX_DEPTH,
                      frag_radius_px: float = FRAG_RADIUS_PX,
                      max_frag_slots: int = MAX_FRAG_RESCUE_SLOTS,
                      ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Cum -> crops hinh hoc (rank-guard + Z_cluster, cat cung top-8, bisect).

    - Danh dau span (is_span) cho moi cum: cum multi-blob chua ca idx<8 va
      idx>=8 duoc uu tien giu de cuu slot rank9-12 (Gate Decision 2026-10-26).
    - Sap xep theo (is_span, Z_cluster) giam: span truoc, roi Z cao truoc.
      K=8 / discrete blobs (khong span) suy bien ve sort Z thuan (AUDIT-03).
    - Cat cung toi da `max_cluster_crops` cum (AUDIT-03, = TOP-8 baseline).
    - D1-sua frag rescue (Verifier Decision D1-D4 2026-10-26, Phase 5 lan4):
      sau cat top-8, tim cap singleton rank9-12 trong dropped cach nhau
      <= frag_radius_px he Input (dropped<->dropped, proxy frag 1 GT vo >=2
      manh), toi da max_frag_slots slot/anh (uu tien tong Z cao nhat), gop
      vao kept (tong toi da 8+2). D1-cu (kept<->dropped) FAILED vi blob lost
      cach kept 669/886px (board12). Radius 250px CHOT (plan §3.4).
    - Cum vuot cap 640 (s_k > cap): Hierarchical Bisection toi da 2 lan;
      moi manh thanh 1 crop doc lap; manh van > cap -> crop tai tam +
      truncated=True (§3.3).
    - Tra ve (crops, dropped_log). Khong doc/ghi file, khong YOLO.
    """
    if not clusters:
        return [], []
    for _c in list(clusters):
        _c["_is_span"] = is_span_cluster(_c.get("members", []))
        _c["_is_frag_rescue"] = False
    try:
        ranked = sorted(list(clusters),
                        key=lambda d: (bool(d.get("_is_span", False)),
                                       float(d["z_cluster"])), reverse=True)
        cap = max(0, int(max_cluster_crops))
        kept_in = ranked[:cap]
        dropped = ranked[cap:]

        # D1-sua: frag rescue tren dropped (dropped<->dropped, Input-space).
        rescued: list[dict[str, Any]] = []
        try:
            centers_in = _blob_centers_input(
                list(blobs), np.asarray(h_inv, dtype=np.float64))
            pairs = find_frag_rescue_pairs(
                list(dropped), centers_in,
                frag_radius_px=float(frag_radius_px),
                max_slots=int(max_frag_slots))
            rescue_ids: set[int] = set()
            for _a, _b in pairs:
                rescue_ids.add(id(_a))
                rescue_ids.add(id(_b))
            for _d in list(dropped):
                if id(_d) in rescue_ids:
                    _d["_is_frag_rescue"] = True
                    rescued.append(_d)
            if rescued:
                kept_in = list(kept_in) + list(rescued)
                dropped = [d for d in list(dropped) if id(d) not in rescue_ids]
        except Exception:
            # Rescue la co che phu: loi hinh hoc khong duoc pha duong chinh.
            rescued = []
            for _c in list(clusters):
                _c["_is_frag_rescue"] = False
            kept_in = ranked[:cap]
            dropped = ranked[cap:]

        boxes_tpl_all = [list(map(float, b["bbox_tpl"])) for b in blobs]
        crops: list[dict[str, Any]] = []
        for rank, c in enumerate(kept_in):
            members = list(c["members"])
            zc = float(c["z_cluster"])
            # Bisection neu vuot cap (do s_k o he Input).
            bisect_log: list[dict[str, Any]] = []
            pieces = bisect_oversize_cluster(
                members, boxes_tpl_all, np.asarray(h_inv, dtype=np.float64),
                tuple(img_shape), max_side=float(cap_px),
                depth=0, max_depth=int(max_depth), bisect_log=bisect_log)
            for pi, piece in enumerate(pieces):
                pboxes = [boxes_tpl_all[i] for i in piece]
                box_tpl = union_box_tpl(pboxes, margin)
                quad, aabb = project_box_to_input(
                    box_tpl, np.asarray(h_inv, dtype=np.float64),
                    tuple(img_shape))
                cx, cy, side = squareize_box(aabb, tuple(img_shape))
                # Manh van > cap sau 2 lan tach -> cat tai tam + co truncated.
                truncated = bool(side >= float(cap_px) and
                                 cluster_square_side(
                                     pboxes, np.asarray(h_inv, dtype=np.float64),
                                     tuple(img_shape), margin) > float(cap_px))
                if truncated:
                    side = min(float(side), float(cap_px))
                    cx, cy, _ = squareize_box(
                        [cx - side / 2.0, cy - side / 2.0,
                         cx + side / 2.0, cy + side / 2.0], tuple(img_shape))
                x0, y0 = center_origin(cx, cy, side, tuple(img_shape))
                crops.append({
                    "cluster_id": int(c.get("cluster_id", rank)),
                    "sub_id": int(pi),
                    "members": list(piece),
                    "z_cluster": float(zc),
                    "size": len(piece),
                    "box_tpl": [float(v) for v in box_tpl],
                    "quad_input": np.asarray(quad, dtype=np.float64),
                    "box_input": [float(v) for v in aabb],
                    "cx": float(cx), "cy": float(cy),
                    "side": float(side),
                    "origin": (int(x0), int(y0)),
                    "bucket": int(round(float(side))),
                    "truncated": bool(truncated),
                    "bisect_depth": int(len(bisect_log) > 0),
                    "is_span": bool(c.get("_is_span", False)),
                    "is_frag_rescue": bool(c.get("_is_frag_rescue", False)),
                })
        # Sap crops theo (span, Z_cluster) giam (manh cua cung cum giu thu tu sub_id).
        # D1-sua: rescue giu nguyen thu tu Z (Z thap → cuoi, khong chen span/Z cao).
        crops.sort(key=lambda d: (bool(d.get("is_span", False)),
                                  float(d["z_cluster"]), -int(d["sub_id"])),
                   reverse=True)
        dropped_log = [{"cluster_id": int(d.get("cluster_id", -1)),
                        "z_cluster": float(d["z_cluster"]),
                        "size": int(len(d["members"])),
                        "is_span": bool(d.get("_is_span", False)),
                        "is_frag_rescue": False,
                        "reason": "dropped_lowz_clusters rank>8 (AUDIT-03)"}
                       for d in dropped]
        return crops, dropped_log
    finally:
        for _c in list(clusters):
            _c.pop("_is_span", None)
            _c.pop("_is_frag_rescue", None)


def cut_crop_from_image(image_bgr: np.ndarray,
                        crop: dict[str, Any]) -> np.ndarray:
    """Cat crop vuong side×side tai origin tu anh Input (BGR uint8).

    Tien ich cho Phase 4 (khong bat buoc o Phase 2). Khong resize.
    """
    x0, y0 = int(crop["origin"][0]), int(crop["origin"][1])
    side = int(round(float(crop["side"])))
    h, w = image_bgr.shape[:2]
    x1, y1 = min(x0 + side, w), min(y0 + side, h)
    return image_bgr[y0:y1, x0:x1].copy()
