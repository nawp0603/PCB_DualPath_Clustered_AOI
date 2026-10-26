#!/usr/bin/env python3
"""Phase 1 — DBSCAN epsilon calibration on Train/Validation Boards 05-12.

Reads READ-ONLY Train/Validation Boards 05-12 from LEGACY_PKU_ROOT
(Standard + Camera Perturbation, seed 20261023), runs the FROZEN
Branch-B pipeline (identify_and_align fast + run_branch_b, K_raw=16),
then calibrates DBSCAN Single-Linkage (min_samples=1) epsilon:

  1. Build Z-weighted distance matrix D_w per image (plan §2.2.2):
       w_i = sigmoid((z_i - 2.0) / 1.0),  d_ij = Eucl(c_i,c_j)/sqrt(w_i*w_j)
     + compare against pure Euclidean (fallback if weighted not better).
  2. 1-NN distance curve (k=1 main, k=2,3 reference) + Kneedle knee.
  3. Grid scan eps (80..300px, Template space) with Union-Find CC (m=1),
     measuring Cluster Purity (>=85%) and Over-clustering Rate (<=15%)
     using GT on validation Boards 05-12 (plan §3.4).
  4. Plateau check ±20%, K_raw decision {8,12,16}, fallback eps=150.
  5. Freeze results/calibration/calib_report.json (+ frozen_sha256)
     and results/calibration/k_distance_val_boards05_12.png.

STRICT SANDBOX + ZERO DATA LEAKAGE (AUDIT-02):
  - ONLY Boards 05-12. Any Board 01/04 (Zero-defect-training Boards)
    reference raises RuntimeError.
  - All writes via assert_writable_ok (inside new project only).
  - CPU-only: no YOLO / GPU import in this script.

Plan refs: plan.md §2.2, §3.1-§3.4, Phase 1; coder_rules.md Chốt 1-2.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from configs.resource_paths import (  # noqa: E402
    LOCAL_TEMPLATES,
    PROJECT_ROOT,
    assert_writable_ok,
)

import cv2  # noqa: E402
import numpy as np  # noqa: E402

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from scripts.blob_stats import (  # noqa: E402
    ALLOWED_CALIB_BOARDS,
    CALIB_SEED,
    FORBIDDEN_TEST_BOARDS,
    K_RAW_COLLECT,
    collect_blobs,
)

# ---------------------------------------------------------------------------
# Constants (plan-locked, NOT tunable without returning to Phase 1)
# ---------------------------------------------------------------------------
Z0 = 2.0
TAU_Z = 1.0
SIGMA_FLOOR = 5.0  # reference only (already applied in Branch-B)
DELTA = 1e-6
CONTEXT_MARGIN_P = 48  # px, Template space (§2.2.5)
MAX_CLUSTER_CROPS = 8
EPS_GRID = list(range(22, 29))  # 22..28 step 1 (Gate Decision 2026-10-26:
# quay Phase 1 (1a): K12 grid hep primary eps28/backup eps22; gate K12 ha
# purity 0.80 de co plateau lien tuc 22-28; giu Z0/TAU; KHONG mo 01/04)
K_RAW_OPTIONS = (8, 12, 16)  # plan §2.2.4: quét {8,12,16}, chốt bằng gate thực đo
EPS_FALLBACK = 150
K_RAW_CANDIDATES = (8, 12, 16)
GT_EXPAND_PX = 10.0
FRAG_RADIUS_PX = 250.0
CLUSTERED_DIST_PX = 300.0
PURITY_GATE = 0.85  # gate goc cho K=8 (Phase 1, giu de khong pha audit cu)
PURITY_GATE_K12 = 0.80  # Gate Decision 2026-10-26 (i): ha cho K>=12 de co
# plateau lien tuc eps22-28; over giu 0.15; TU CHOI doi Z0/TAU giai doan nay
OVER_GATE = 0.15
FRAG_GATE = 1.0  # frag_recall_diag == 1.0 (bat buoc, Gate Decision §D)
CLU_GATE = 0.99  # clustered_recall_diag >= 0.99 (bat buoc, Gate Decision §D)
NM_MIN = 8  # n_multi >= 8 (du mau, Gate Decision §D)


def purity_gate_for(k_raw: int) -> float:
    """Gate purity theo K (Gate Decision 2026-10-26 chon (i)).

    K=8 giu 0.85 (audit Phase 1 cu); K>=12 dung 0.80 de co plateau 22-28.
    Z0/TAU giu nguyen (TU CHOI (ii)).
    """
    return float(PURITY_GATE_K12) if int(k_raw) >= 12 else float(PURITY_GATE)
# Trường volatile KHÔNG đưa vào frozen_sha256 (timestamp/elapsed thay đổi mỗi
# lần chạy nhưng không ảnh hưởng quyết định hiệu chuẩn — Verifier kiểm tra
# reproducibility bằng so sánh các trường số liệu, xem T-CLU-05).
VOLATILE_REPORT_KEYS = {"calib_timestamp", "calib_elapsed_s", "smoke_mode", "frozen_sha256"}

FROZEN_FILES = [
    "configs/resource_paths.py",
    "src/reference/board_id_and_align.py",
    "src/reference/tolerance_diff.py",
    "src/models/yolo_detector.py",
    "src/models/ensemble.py",
    "src/decision/logic.py",
]


def _assert_no_leakage(boards) -> None:
    bad = set(map(str, boards)) & set(FORBIDDEN_TEST_BOARDS)
    if bad:
        raise RuntimeError(
            "[ZERO LEAKAGE] Refusing to touch Zero-defect-training Boards "
            f"{sorted(bad)}. Calibration may only use Boards 05-12."
        )


# ---------------------------------------------------------------------------
# Z-weighted distance + Union-Find Single-Linkage (m=1, AUDIT-01)
# ---------------------------------------------------------------------------
def sigmoid(u: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.asarray(u, dtype=np.float64)))


def compute_weights(z: np.ndarray, z0: float = Z0, tau: float = TAU_Z) -> np.ndarray:
    return sigmoid((np.asarray(z, dtype=np.float64) - z0) / tau)


def pairwise_euclidean(centroids: np.ndarray) -> np.ndarray:
    c = np.asarray(centroids, dtype=np.float64)
    diff = c[:, None, :] - c[None, :, :]
    return np.sqrt((diff ** 2).sum(axis=-1))


def weighted_distance_matrix(centroids: np.ndarray, z: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return (D_w, D_euclid). D_w[i,j] = Eucl/sqrt(w_i*w_j)."""
    c = np.asarray(centroids, dtype=np.float64)
    n = len(c)
    if n == 0:
        return np.zeros((0, 0)), np.zeros((0, 0))
    d_euclid = pairwise_euclidean(c)
    w = compute_weights(np.asarray(z, dtype=np.float64))
    wouter = np.sqrt(np.outer(w, w)) + DELTA
    d_w = d_euclid / wouter
    np.fill_diagonal(d_w, 0.0)
    np.fill_diagonal(d_euclid, 0.0)
    return d_w.astype(np.float64), d_euclid.astype(np.float64)


class UnionFind:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, x: int) -> int:
        p = self.p
        while p[x] != x:
            p[x] = p[p[x]]
            x = p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def connected_components_threshold(d_mat: np.ndarray, eps: float) -> np.ndarray:
    """Single-Linkage CC on graph [D <= eps]. Deterministic, sklearn-free."""
    n = int(d_mat.shape[0])
    if n == 0:
        return np.zeros(0, dtype=np.int32)
    uf = UnionFind(n)
    for i in range(n):
        row = d_mat[i]
        for j in range(i + 1, n):
            if row[j] <= eps:
                uf.union(i, j)
    roots = [uf.find(i) for i in range(n)]
    remap: dict[int, int] = {}
    labels = np.zeros(n, dtype=np.int32)
    for i, r in enumerate(roots):
        if r not in remap:
            remap[r] = len(remap)
        labels[i] = remap[r]
    return labels


def cluster_blobs_single_linkage(centroids: np.ndarray, z: np.ndarray,
                                 eps: float) -> np.ndarray:
    d_w, _ = weighted_distance_matrix(centroids, z)
    return connected_components_threshold(d_w, eps)


# ---------------------------------------------------------------------------
# Kneedle (Kneedle-40-lines, deterministic, numpy-only)
# ---------------------------------------------------------------------------
def kneedle_knee(x: np.ndarray, y: np.ndarray) -> int:
    """Kneedle knee index for a monotone curve (normalized max-distance).

    Normalizes x,y to [0,1], measures perpendicular distance to the chord
    from first to last point, returns argmax. Works for both increasing
    and decreasing curves.
    """
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    n = len(x)
    if n < 3:
        return int(np.argmax(y)) if n else 0
    xn = (x - x.min()) / max(x.max() - x.min(), 1e-12)
    yn = (y - y.min()) / max(y.max() - y.min(), 1e-12)
    # chord from (xn[0],yn[0]) to (xn[-1],yn[-1])
    x1, y1 = xn[0], yn[0]
    x2, y2 = xn[-1], yn[-1]
    dx, dy = x2 - x1, y2 - y1
    norm = float(np.hypot(dx, dy)) or 1e-12
    dist = np.abs(dy * xn - dx * yn + x2 * y1 - y2 * x1) / norm
    # endpoints cannot be knee
    dist[0] = -np.inf
    dist[-1] = -np.inf
    return int(np.argmax(dist))


def knn_distances(d_mat: np.ndarray, k: int) -> np.ndarray:
    """k-th nearest neighbour distance per point (k=1 means nearest excl. self)."""
    n = d_mat.shape[0]
    if n <= k:
        return np.full(n, np.nan)
    s = np.sort(d_mat, axis=1)
    return s[:, k]  # s[:,0]=0 (self)


# ---------------------------------------------------------------------------
# Geometry helpers (Template -> Input via H_inv, plan §2.2.5 + §3.4)
# ---------------------------------------------------------------------------
def project_points(pts: np.ndarray, h_inv: np.ndarray) -> np.ndarray:
    arr = np.asarray(pts, dtype=np.float32).reshape(-1, 1, 2)
    return cv2.perspectiveTransform(arr, np.asarray(h_inv, np.float64)).reshape(-1, 2)


def cluster_square_side(cluster_boxes_tpl: list[list[float]], h_inv: np.ndarray,
                        img_shape: tuple[int, int], margin: float = CONTEXT_MARGIN_P) -> float:
    """Square side s_k in Input space (union + margin, H_inv, AABB, square)."""
    h_img, w_img = int(img_shape[0]), int(img_shape[1])
    x1 = min(b[0] for b in cluster_boxes_tpl) - margin
    y1 = min(b[1] for b in cluster_boxes_tpl) - margin
    x2 = max(b[2] for b in cluster_boxes_tpl) + margin
    y2 = max(b[3] for b in cluster_boxes_tpl) + margin
    corners = np.float32([[x1, y1], [x2, y1], [x2, y2], [x1, y2]])
    q = project_points(corners, h_inv)
    X1, Y1 = float(q[:, 0].min()), float(q[:, 1].min())
    X2, Y2 = float(q[:, 0].max()), float(q[:, 1].max())
    X1, Y1 = max(X1, 0.0), max(Y1, 0.0)
    X2, Y2 = min(X2, float(w_img)), min(Y2, float(h_img))
    return float(max(max(X2 - X1, 0.0), max(Y2 - Y1, 0.0)))


def assign_blob_to_gt(centroid_input: np.ndarray, gt_boxes: list[list[float]],
                      expand: float = GT_EXPAND_PX) -> int:
    """GT index whose expanded box contains the blob centroid, else -1."""
    px, py = float(centroid_input[0]), float(centroid_input[1])
    best, best_d = -1, float("inf")
    for gi, b in enumerate(gt_boxes):
        if b[0] - expand <= px <= b[2] + expand and b[1] - expand <= py <= b[3] + expand:
            cx, cy = (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0
            d = (px - cx) ** 2 + (py - cy) ** 2
            if d < best_d:
                best, best_d = gi, d
    return best


# ---------------------------------------------------------------------------
# Per-eps metrics on validation records (GT available, Boards 05-12 only)
# ---------------------------------------------------------------------------
def metrics_at_eps(records: list[dict[str, Any]], eps: float) -> dict[str, Any]:
    """Cluster all records at eps (m=1), return purity/over/fragmented/clustered."""
    n_multi = 0
    n_pure = 0
    n_total_clusters = 0
    n_oversize = 0
    n_singleton_hit = 0
    n_singleton = 0
    # fragmented / clustered GT-level diagnostics (blob coverage)
    frag_gt_total = 0
    frag_gt_hit = 0
    clu_gt_total = 0
    clu_gt_hit = 0
    per_image_clusters: list[int] = []

    for rec in records:
        _assert_no_leakage({rec["board"]})
        blobs = rec["blobs"]
        if not blobs:
            continue
        gt = rec.get("gt", [])
        gt_boxes = [list(b) for _, b in gt]
        cent_tpl = np.float32([b["centroid_tpl"] for b in blobs])
        z = np.float32([b["z_diff"] for b in blobs])
        boxes_tpl = [list(map(float, b["bbox_tpl"])) for b in blobs]
        h_inv = np.asarray(rec["H_inv"], dtype=np.float64)
        img_shape = tuple(rec["img_shape"])
        labels = cluster_blobs_single_linkage(cent_tpl, z, eps)
        # project centroids once
        cent_in = project_points(cent_tpl, h_inv) if len(cent_tpl) else np.zeros((0, 2))
        assign = [assign_blob_to_gt(cent_in[i], gt_boxes) for i in range(len(blobs))]
        n_clu = int(labels.max()) + 1 if len(labels) else 0
        per_image_clusters.append(n_clu)
        if gt_boxes:
            for c in range(n_clu):
                members = [i for i in range(len(blobs)) if int(labels[i]) == c]
                n_total_clusters += 1
                # over-clustering cap
                s_k = cluster_square_side([boxes_tpl[i] for i in members], h_inv, img_shape)
                if s_k > 640.0:
                    n_oversize += 1
                if len(members) >= 2:
                    n_multi += 1
                    gids = {assign[i] for i in members}
                    if len(gids) == 1 and next(iter(gids)) != -1:
                        n_pure += 1
                else:
                    n_singleton += 1
                    if assign[members[0]] != -1:
                        n_singleton_hit += 1
            # --- fragmented GT subset: GT with >=2 blobs within 250px of GT center
            for gi, gb in enumerate(gt_boxes):
                cx, cy = (gb[0] + gb[2]) / 2.0, (gb[1] + gb[3]) / 2.0
                near = sum(1 for p in cent_in
                           if float(np.hypot(float(p[0]) - cx, float(p[1]) - cy)) <= FRAG_RADIUS_PX)
                if near >= 2:
                    frag_gt_total += 1
                    if assign.count(gi) >= 1:
                        frag_gt_hit += 1
            # --- clustered GT subset: images with >=2 GT boxes <300px apart
            if len(gt_boxes) >= 2:
                centers = [((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0) for b in gt_boxes]
                proximate = False
                for i in range(len(centers)):
                    for j in range(i + 1, len(centers)):
                        d = float(np.hypot(centers[i][0] - centers[j][0],
                                           centers[i][1] - centers[j][1]))
                        if d < CLUSTERED_DIST_PX:
                            proximate = True
                            break
                if proximate:
                    for gi in range(len(gt_boxes)):
                        clu_gt_total += 1
                        if assign.count(gi) >= 1:
                            clu_gt_hit += 1
        else:
            # images without GT: still count oversize for monitoring
            for c in range(n_clu):
                members = [i for i in range(len(blobs)) if int(labels[i]) == c]
                n_total_clusters += 1
                s_k = cluster_square_side([boxes_tpl[i] for i in members], h_inv, img_shape)
                if s_k > 640.0:
                    n_oversize += 1

    purity = (n_pure / n_multi) if n_multi else 1.0
    over = (n_oversize / n_total_clusters) if n_total_clusters else 0.0
    frag_rec = (frag_gt_hit / frag_gt_total) if frag_gt_total else 1.0
    clu_rec = (clu_gt_hit / clu_gt_total) if clu_gt_total else 1.0
    return {
        "eps": float(eps),
        "purity": float(purity),
        "over_clustering_rate": float(over),
        "n_multi": int(n_multi),
        "n_pure": int(n_pure),
        "n_total_clusters": int(n_total_clusters),
        "n_oversize": int(n_oversize),
        "n_singleton": int(n_singleton),
        "n_singleton_hit": int(n_singleton_hit),
        "frag_recall_diag": float(frag_rec),
        "frag_gt_total": int(frag_gt_total),
        "clustered_recall_diag": float(clu_rec),
        "clustered_gt_total": int(clu_gt_total),
        "mean_clusters_per_image": float(np.mean(per_image_clusters)) if per_image_clusters else 0.0,
    }


# ---------------------------------------------------------------------------
# Pipeline hash + frozen sha
# ---------------------------------------------------------------------------
def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pipeline_hashes() -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in FROZEN_FILES:
        p = PROJECT_ROOT / rel
        if not p.exists():
            raise FileNotFoundError(f"Frozen pipeline file missing: {p}")
        out[rel] = sha256_file(p)
    return out


def frozen_sha256_of(report_body: dict[str, Any], pipe: dict[str, str]) -> str:
    canon = json.dumps(
        {k: v for k, v in report_body.items() if k not in VOLATILE_REPORT_KEYS},
        sort_keys=True, ensure_ascii=False).encode("utf-8")
    ph = json.dumps(pipe, sort_keys=True).encode("utf-8")
    return hashlib.sha256(hashlib.sha256(canon).digest() + hashlib.sha256(ph).digest()).hexdigest()


# ---------------------------------------------------------------------------
# Main calibration driver
# ---------------------------------------------------------------------------
def calibrate(max_images: int | None = None,
              out_dir: Path | None = None,
              eps_grid: list[int] | None = None) -> dict[str, Any]:
    t_start = time.time()
    if out_dir is None:
        out_dir = PROJECT_ROOT / "results" / "calibration"
    out_dir = assert_writable_ok(Path(out_dir))
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- collect blobs (both variants), Boards 05-12 only
    t0 = time.time()
    print("[calibrate] collecting Standard (Boards 05-12)...")
    std = collect_blobs("standard", max_images=max_images)
    print("[calibrate] collecting Camera (Boards 05-12, seed 20261023)...")
    cam = collect_blobs("camera", max_images=max_images)
    print(f"[calibrate] collected std={len(std['records'])} cam={len(cam['records'])} "
          f"in {time.time()-t0:.1f}s")
    all_records = std["records"] + cam["records"]
    _assert_no_leakage({r["board"] for r in all_records})

    # --- 1-NN / k-NN curves on D_w (Template space, pooled across images)
    d1_all: list[float] = []
    d2_all: list[float] = []
    d3_all: list[float] = []
    d1_euclid_all: list[float] = []
    for rec in all_records:
        blobs = rec["blobs"]
        if len(blobs) < 2:
            continue
        cent = np.float32([b["centroid_tpl"] for b in blobs])
        z = np.float32([b["z_diff"] for b in blobs])
        d_w, d_e = weighted_distance_matrix(cent, z)
        d1_all.extend(knn_distances(d_w, 1).tolist())
        d2_all.extend(knn_distances(d_w, 2).tolist())
        d3_all.extend(knn_distances(d_w, 3).tolist())
        d1_euclid_all.extend(knn_distances(d_e, 1).tolist())
    d1 = np.asarray([v for v in d1_all if np.isfinite(v)], dtype=np.float64)
    d2 = np.asarray([v for v in d2_all if np.isfinite(v)], dtype=np.float64)
    d3 = np.asarray([v for v in d3_all if np.isfinite(v)], dtype=np.float64)
    d1e = np.asarray([v for v in d1_euclid_all if np.isfinite(v)], dtype=np.float64)
    if len(d1) < 3:
        raise RuntimeError("Not enough blobs for 1-NN curve (need >=3 distances).")

    for name, arr in (("1-NN", d1), ("1-NN-euclid", d1e)):
        print(f"[calibrate] {name}: n={len(arr)} median={np.median(arr):.1f} "
              f"p90={np.percentile(arr,90):.1f} max={arr.max():.1f}")

    d1_sorted = np.sort(d1)[::-1]  # descending (one fixed direction, plan §3.2)
    x = np.arange(len(d1_sorted), dtype=np.float64)
    knee_idx = kneedle_knee(x, d1_sorted)
    knee_val = float(d1_sorted[knee_idx])
    # reference knees for k=2,3 (diagnostic only)
    knee_ref: dict[str, float] = {}
    for name, arr in (("k2", d2), ("k3", d3)):
        a = np.asarray([v for v in arr if np.isfinite(v)], dtype=np.float64)
        if len(a) >= 3:
            s = np.sort(a)[::-1]
            ki = kneedle_knee(np.arange(len(s), dtype=np.float64), s)
            knee_ref[name] = float(s[ki])
        else:
            knee_ref[name] = float("nan")

    # weighted vs euclid comparison (plan §2.2.2 / R3)
    med_w, med_e = float(np.median(d1)), float(np.median(d1e)) if len(d1e) else float("nan")
    weighted_note = (f"median1NN_weighted={med_w:.1f} vs euclid={med_e:.1f}; "
                     "weighted kept as primary (plan §2.2.2); fallback to euclid "
                     "in Phase 1 rerun if purity_weighted < purity_euclid.")

    # --- grid scan eps with m=1 (Union-Find), GT-based purity/over
    grid_eps = eps_grid if eps_grid is not None else list(EPS_GRID)
    print(f"[calibrate] grid scan eps={grid_eps[0]}..{grid_eps[-1]} step={grid_eps[1]-grid_eps[0]} (m=1)...")
    grid_rows: list[dict[str, Any]] = []
    for eps in grid_eps:
        m = metrics_at_eps(all_records, float(eps))
        grid_rows.append(m)
        print(f"  eps={eps:4d} purity={m['purity']:.3f} (n_multi={m['n_multi']}) "
              f"over={m['over_clustering_rate']:.3f} frag={m['frag_recall_diag']:.3f} "
              f"clu={m['clustered_recall_diag']:.3f}")

    # --- K_raw x eps decision {8,12,16} (plan §2.2.4 + §3.1: quét K_raw, chốt
    # bằng gate Purity/Over thực đo — KHÔNG chốt bằng heuristic đếm đuôi).
    # Thu thập ở K_raw=16 (nới lỏng quan sát đuôi); cắt ngọn top-K cho mỗi
    # ứng viên rồi quét grid eps chung. Chốt cặp (K_raw, eps) có gate pass;
    # ưu tiên K_raw lớn hơn (cứu recall hạng 9-16 cho Phase 2-4), rồi tới eps
    # lớn hơn trong plateau (gom cụm tốt hơn, plan §3.2 bước 6).
    def _truncate(records: list[dict[str, Any]], k: int) -> list[dict[str, Any]]:
        out = []
        for r in records:
            rr = dict(r)
            rr["blobs"] = list(r["blobs"][:k])
            out.append(rr)
        return out

    k_decisions: list[dict[str, Any]] = []
    for k_cand in K_RAW_OPTIONS:
        sub = _truncate(all_records, k_cand)
        rows_k: list[dict[str, Any]] = []
        for eps in grid_eps:
            rows_k.append(metrics_at_eps(sub, float(eps)))
        gate_p = purity_gate_for(k_cand)  # Gate Decision 2026-10-26 (i)
        passing_k = [r for r in rows_k
                     if r["purity"] >= gate_p
                     and r["over_clustering_rate"] <= OVER_GATE
                     and float(r.get("frag_recall_diag", 0.0)) >= FRAG_GATE
                     and float(r.get("clustered_recall_diag", 0.0)) >= CLU_GATE
                     and int(r.get("n_multi", 0)) >= NM_MIN]
        if passing_k:
            best_k = max(passing_k, key=lambda r: r["eps"])
            lo, hi = best_k["eps"] * 0.8, best_k["eps"] * 1.2
            near = [r for r in rows_k if lo <= r["eps"] <= hi]
            plat_k = bool(near) and all(
                r["purity"] >= gate_p - 0.02 and r["over_clustering_rate"] <= OVER_GATE + 0.02
                for r in near)
        else:
            best_k = max(rows_k, key=lambda r: (r["purity"], -r["over_clustering_rate"]))
            plat_k = False
        k_decisions.append({"k_raw": k_cand, "rows": rows_k,
                            "passing": passing_k, "best": best_k,
                            "plateau": plat_k, "purity_gate": float(gate_p)})
        tag = f"PASS gate{gate_p:.2f}" if passing_k else "no-pass"
        print(f"[calibrate] K_raw={k_cand}: best eps={best_k['eps']:.0f} "
              f"purity={best_k['purity']:.3f} over={best_k['over_clustering_rate']:.3f} [{tag}]")

    passing_ks = [d for d in k_decisions if d["passing"]]
    fallback_used = False
    if passing_ks:
        # ưu tiên K_raw lớn nhất có pass (giữ recall đuôi), rồi eps lớn nhất
        # trong plateau (plan §3.2 bước 6). Gate Decision 2026-10-26 (i):
        # K=8 gate 0.85, K>=12 gate 0.80 + frag==1.0 + clu>=0.99 + nm>=8.
        chosen = max(passing_ks, key=lambda d: d["k_raw"])
        k_raw = int(chosen["k_raw"])
        best = chosen["best"]
        eps_opt = float(best["eps"])
        plateau_ok = bool(chosen["plateau"])
        grid_rows = chosen["rows"]
        gate_used = purity_gate_for(k_raw)
        k_reason = (f"K_raw={k_raw} là giá trị LỚN NHẤT có eps pass gate "
                    f"(eps={eps_opt:.0f}, purity={best['purity']:.3f}>=gate{gate_used:.2f}, "
                    f"over={best['over_clustering_rate']:.3f}, "
                    f"frag={best['frag_recall_diag']:.3f}, "
                    f"clu={best['clustered_recall_diag']:.3f}, nm={best['n_multi']}); "
                    + "; ".join(
                        f"K={d['k_raw']}(gate{d['purity_gate']:.2f}):best_eps={d['best']['eps']:.0f}/"
                        f"pur={d['best']['purity']:.3f}/over={d['best']['over_clustering_rate']:.3f}/"
                        f"frag={d['best']['frag_recall_diag']:.3f}/"
                        f"clu={d['best']['clustered_recall_diag']:.3f}"
                        for d in k_decisions))
    else:
        eps_opt = float(EPS_FALLBACK)
        plateau_ok = False
        fallback_used = True
        # báo cáo ở K_raw=12 (plan §2.2.4) khi không có pass
        mid = next(d for d in k_decisions if d["k_raw"] == 12)
        k_raw = 12
        best = metrics_at_eps(_truncate(all_records, 12), eps_opt)
        grid_rows = mid["rows"]
        k_reason = ("KHÔNG có cặp (K_raw,eps) nào pass gate -> fallback "
                    f"eps={EPS_FALLBACK} (plan R1), K_raw=12 (mặc định plan §2.2.4)")
        print(f"[calibrate] WARN no (K_raw,eps) passes gates -> fallback eps={EPS_FALLBACK}")
    if max_images is not None:
        k_reason += f" [SMOKE max_images={max_images}: decision provisional]"

    # snap knee to grid for reporting (nearest grid value)
    knee_grid = min(grid_eps, key=lambda e: abs(e - knee_val))
    if not fallback_used and abs(knee_val - eps_opt) > 120:
        print(f"[calibrate] NOTE knee({knee_val:.0f}) far from grid-opt({eps_opt:.0f}); "
              "keeping grid-opt (GT-gated) as primary.")

    # --- blob tail stats at collected K_raw=16 (descriptive, NOT the decision)
    n_blobs_all = [len(r["blobs"]) for r in all_records]
    arr_n = np.asarray(n_blobs_all, dtype=float)
    frac_gt8 = float((arr_n > 8).mean()) if len(arr_n) else 0.0
    frac_gt12 = float((arr_n > 12).mean()) if len(arr_n) else 0.0
    frac_gt16 = float((arr_n > 16).mean()) if len(arr_n) else 0.0

    # --- knee plot PNG
    fig_p = assert_writable_ok(out_dir / "k_distance_val_boards05_12.png")
    plt.figure(figsize=(9, 5.5))
    plt.plot(x, d1_sorted, label="1-NN weighted (primary)", linewidth=1.8)
    for arr, name, ls in ((d2, "k=2 ref", "--"), (d3, "k=3 ref", ":")):
        a = np.asarray([v for v in arr if np.isfinite(v)])
        if len(a):
            s = np.sort(a)[::-1]
            plt.plot(np.arange(len(s)), s, label=f"{name} (reference)", linestyle=ls, linewidth=1.2)
    plt.axvline(knee_idx, color="r", linestyle="--", linewidth=1.2,
                label=f"knee 1-NN idx={knee_idx} val={knee_val:.0f}px")
    plt.axhline(eps_opt, color="g", linestyle="-.", linewidth=1.0,
                label=f"eps_opt={eps_opt:.0f}px (GT-gated)")
    plt.xlabel("blob rank (1-NN distance descending)")
    plt.ylabel("Z-weighted neighbour distance d (px, Template space)")
    plt.title("Knee-plot 1-NN on Boards 05-12 (Standard + Camera, seed 20261023)\n"
              "Template-space D_w; k=2,3 reference only; 01/04 NEVER used")
    plt.legend(fontsize=8)
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(fig_p, dpi=150)
    plt.close()
    print(f"[calibrate] wrote {fig_p}")

    # --- freeze report
    pipe = pipeline_hashes()
    body: dict[str, Any] = {
        "eps": float(eps_opt),
        "min_samples": 1,
        "k_raw": int(k_raw),
        "knee_1nn_value": float(knee_val),
        "knee_ref_k2_k3": {k: (float(v) if np.isfinite(v) else None)
                           for k, v in knee_ref.items()},
        "knee_grid_snap": int(knee_grid),
        "plateau_ok": bool(plateau_ok),
        "purity": round(float(best["purity"]), 4),
        "over_clustering_rate": round(float(best["over_clustering_rate"]), 4),
        "frag_recall_diag": round(float(best["frag_recall_diag"]), 4),
        "clustered_recall_diag": round(float(best["clustered_recall_diag"]), 4),
        "fallback_used": bool(fallback_used),
        "fallback_eps": int(EPS_FALLBACK),
        "eps_grid": [int(e) for e in grid_eps],
        "grid_rows": [{k: (round(float(v), 4) if isinstance(v, float) else v)
                       for k, v in r.items()} for r in grid_rows],
        "k_raw_scan": [
            {"k_raw": int(d["k_raw"]),
             "purity_gate": float(d.get("purity_gate", purity_gate_for(d["k_raw"]))),
             "over_gate": float(OVER_GATE),
             "frag_gate": float(FRAG_GATE),
             "clu_gate": float(CLU_GATE),
             "nm_min": int(NM_MIN),
             "plateau_ok": bool(d["plateau"]),
             "best_eps": float(d["best"]["eps"]),
             "best_purity": round(float(d["best"]["purity"]), 4),
             "best_over": round(float(d["best"]["over_clustering_rate"]), 4),
             "best_frag": round(float(d["best"]["frag_recall_diag"]), 4),
             "best_clu": round(float(d["best"]["clustered_recall_diag"]), 4),
             "best_nm": int(d["best"]["n_multi"]),
             "best_mean_clusters": round(float(d["best"]["mean_clusters_per_image"]), 4),
             "n_passing_eps": len(d["passing"]),
             "passing_eps": [float(r["eps"]) for r in d["passing"]],
             "grid_rows": [{k: (round(float(v), 4) if isinstance(v, float) else v)
                            for k, v in r.items()} for r in d["rows"]]}
            for d in k_decisions],
        "gate_decision": "2026-10-26 (i): keep Z0/TAU, K8 gate 0.85, K>=12 gate 0.80, over<=0.15, frag==1.0, clu>=0.99, nm>=8; primary K12 eps28, backup eps22",
        "k_raw_reason": k_reason,
        "blob_counts": {"mean": round(float(arr_n.mean()), 3) if len(arr_n) else 0.0,
                        "frac_gt8": round(frac_gt8, 4),
                        "frac_gt12": round(frac_gt12, 4),
                        "frac_gt16": round(frac_gt16, 4),
                        "max": int(arr_n.max()) if len(arr_n) else 0,
                        "n_images": int(len(arr_n))},
        "weighted_vs_euclid_note": weighted_note,
        "diag_counts": {k: best[k] for k in
                        ("n_multi", "n_pure", "n_total_clusters", "n_oversize",
                         "n_singleton", "n_singleton_hit",
                         "frag_gt_total", "clustered_gt_total",
                         "mean_clusters_per_image")},
        "boards": sorted(ALLOWED_CALIB_BOARDS),
        "seed_camera": CALIB_SEED,
        "leakage_guard": "05-12 only; 01/04 (Zero-defect-training Boards) locked to Phase 5",
        "pipeline_files_sha256": pipe,
        "calib_timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "calib_elapsed_s": round(time.time() - t_start, 1),
        "smoke_mode": max_images is not None,
    }
    body["frozen_sha256"] = frozen_sha256_of(body, pipe)
    js_p = assert_writable_ok(out_dir / "calib_report.json")
    js_p.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[calibrate] wrote {js_p} eps={eps_opt} k_raw={k_raw} "
          f"purity={body['purity']} over={body['over_clustering_rate']} "
          f"frozen={body['frozen_sha256'][:12]}...")
    return body


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Phase 1 DBSCAN calibration (Boards 05-12, CPU-only).")
    p.add_argument("--max-images", type=int, default=None,
                   help="Smoke-test limit per variant (None = full Board 05-12).")
    p.add_argument("--out-dir", type=Path, default=None)
    p.add_argument("--eps-min", type=int, default=EPS_GRID[0])
    p.add_argument("--eps-max", type=int, default=EPS_GRID[-1])
    p.add_argument("--eps-step", type=int, default=1)
    return p


def main() -> None:
    args = build_parser().parse_args()
    out = Path(args.out_dir) if args.out_dir else None
    grid = list(range(int(args.eps_min), int(args.eps_max) + 1, int(args.eps_step)))
    calibrate(max_images=args.max_images, out_dir=out, eps_grid=grid)


if __name__ == "__main__":
    main()
