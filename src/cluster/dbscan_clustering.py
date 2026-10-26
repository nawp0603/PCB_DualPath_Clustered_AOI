"""Phase 2 — Template-space Z-weighted Single-Linkage clustering (plan §2.2).

Che do chinh: Union-Find Single-Linkage (min_samples = 1) tren do thi
[D_w <= eps] — moi blob thuoc dung 1 cum (Multi-blob Cluster |C_k| >= 2
hoac Isolated Singleton |C_k| = 1). TUYET DOI khong dung label = -1 o
che do chinh (AUDIT-01). Che do m >= 2 chi la ablation chan doan
(can sklearn, neu thieu thi bao loi ro rang).

CPU-only, numpy-only o main path (khong torch / ultralytics / sklearn).

Nguon tham so duy nhat: results/calibration/calib_report.json
(eps, min_samples, k_raw, frozen_sha256) — khong hardcode eps trong
pipeline (coder_rules §1).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from configs.resource_paths import PROJECT_ROOT

# ---------------------------------------------------------------------------
# Plan-locked constants (§2.2.2, §2.2.5, §3.3) — NOT tunable without Phase 1
# ---------------------------------------------------------------------------
Z0 = 2.0
TAU_Z = 1.0
SIGMA_FLOOR = 5.0  # reference only (da ap dung trong Branch-B extract_blobs)
DELTA = 1e-6
CONTEXT_MARGIN_P = 48.0  # px, Template space (§2.2.5 buoc 1)
MAX_CLUSTER_CROPS = 8  # tran cung so crop (AUDIT-03)
BISECT_CAP_PX = 640.0  # moi crop cuoi <= 640 (§3.3)
BISECT_MAX_DEPTH = 2
KMEANS_RANDOM_STATE = 0  # deterministic bisection

CALIB_REPORT = PROJECT_ROOT / "results" / "calibration" / "calib_report.json"


def load_frozen_params() -> dict[str, Any]:
    """Doc (eps, min_samples, k_raw, frozen_sha256) tu calib_report.json."""
    with open(CALIB_REPORT, encoding="utf-8") as f:
        r = json.load(f)
    for k in ("eps", "min_samples", "k_raw", "frozen_sha256"):
        if k not in r:
            raise KeyError(f"calib_report.json thieu truong dong bang: {k}")
    if int(r["min_samples"]) != 1:
        raise ValueError("Che do chinh bat buoc min_samples = 1 (AUDIT-01)")
    return {"eps": float(r["eps"]), "min_samples": int(r["min_samples"]),
            "k_raw": int(r["k_raw"]), "frozen_sha256": str(r["frozen_sha256"])}


# ---------------------------------------------------------------------------
# Z-weighted distance (bit-identical voi scripts/calibrate_dbscan.py)
# ---------------------------------------------------------------------------
def sigmoid(u: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.asarray(u, dtype=np.float64)))


def compute_weights(z: np.ndarray, z0: float = Z0,
                    tau: float = TAU_Z) -> np.ndarray:
    return sigmoid((np.asarray(z, dtype=np.float64) - z0) / tau)


def pairwise_euclidean(centroids: np.ndarray) -> np.ndarray:
    c = np.asarray(centroids, dtype=np.float64)
    diff = c[:, None, :] - c[None, :, :]
    return np.sqrt((diff ** 2).sum(axis=-1))


def weighted_distance_matrix(centroids: np.ndarray,
                             z: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return (D_w, D_euclid). D_w[i,j] = Eucl / sqrt(w_i*w_j)."""
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


# ---------------------------------------------------------------------------
# Union-Find Single-Linkage CC (m = 1, sklearn-free, deterministic)
# ---------------------------------------------------------------------------
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


def connected_components_threshold(d_mat: np.ndarray,
                                   eps: float) -> np.ndarray:
    """Single-Linkage CC tren do thi [D <= eps]. Khong noise, deterministic."""
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
# Contract IO chinh (plan Phase 2): cluster_blobs
# ---------------------------------------------------------------------------
def _parse_blobs(blobs: list[dict[str, Any]]) -> tuple[np.ndarray, np.ndarray]:
    cent = np.float32([b["centroid_tpl"] for b in blobs])
    z = np.float32([b["z_diff"] for b in blobs])
    return cent, z


def cluster_blobs(blobs: list[dict[str, Any]],
                  eps: float | None = None,
                  min_samples: int = 1) -> list[dict[str, Any]]:
    """Gom cum blobs he Template -> danh sach clusters (AUDIT-01).

    Input:  blobs — list dict nhu `extract_blobs` tra ve
            ({centroid_tpl:[x,y], z_diff, area, bbox_tpl, ...},
            da sort Z giam, toi da K_raw phan tu).
            eps — None = doc tu calib_report.json (frozen eps=50.0).
            min_samples — 1 (chinh). >= 2: ablation, can sklearn.
    Output: list[{cluster_id, members:[idx], z_cluster=max z, size}].
            Che do chinh: moi blob thuoc dung 1 cum, khong noise.
    """
    if not blobs:
        return []
    if min_samples != 1:
        clusters, _ = cluster_blobs_ablation(
            blobs, eps=eps, min_samples=min_samples, return_noise=True)
        return clusters
    if eps is None:
        eps = load_frozen_params()["eps"]
    cent, z = _parse_blobs(blobs)
    labels = cluster_blobs_single_linkage(cent, z, float(eps))
    clusters: list[dict[str, Any]] = []
    for c in range(int(labels.max()) + 1):
        members = [i for i in range(len(blobs)) if int(labels[i]) == c]
        zc = float(max(float(blobs[i]["z_diff"]) for i in members))
        clusters.append({"cluster_id": c, "members": members,
                         "z_cluster": zc, "size": len(members)})
    # Xep san theo z_cluster giam (Phase 2 tien ich; cat top-8 o cropper).
    clusters.sort(key=lambda d: d["z_cluster"], reverse=True)
    for new_id, d in enumerate(clusters):
        d["cluster_id"] = new_id
    return clusters


def cluster_blobs_ablation(blobs: list[dict[str, Any]],
                           eps: float | None = None,
                           min_samples: int = 2,
                           return_noise: bool = False
                           ) -> tuple[list[dict[str, Any]], np.ndarray] | list[dict[str, Any]]:
    """Ablation m >= 2 (chan doan, KHONG phai duong chinh).

    Can sklearn. Noise (label=-1) tra trong noise_mask khi return_noise.
    """
    if int(min_samples) < 2:
        raise ValueError("Ablation yeu cau min_samples >= 2")
    try:
        from sklearn.cluster import DBSCAN  # type: ignore
    except Exception as e:  # noqa: BLE001
        raise ImportError(
            "Ablation m>=2 can scikit-learn (main path m=1 sklearn-free)") from e
    if not blobs:
        empty = np.zeros(0, dtype=bool)
        return ([], empty) if return_noise else []
    if eps is None:
        eps = load_frozen_params()["eps"]
    cent, z = _parse_blobs(blobs)
    d_w, _ = weighted_distance_matrix(cent, z)
    labels = DBSCAN(eps=float(eps), min_samples=int(min_samples),
                    metric="precomputed").fit(d_w).labels_
    clusters: list[dict[str, Any]] = []
    cid = 0
    for c in sorted(set(int(v) for v in labels if int(v) != -1)):
        members = [i for i in range(len(blobs)) if int(labels[i]) == c]
        zc = float(max(float(blobs[i]["z_diff"]) for i in members))
        clusters.append({"cluster_id": cid, "members": members,
                         "z_cluster": zc, "size": len(members)})
        cid += 1
    clusters.sort(key=lambda d: d["z_cluster"], reverse=True)
    for new_id, d in enumerate(clusters):
        d["cluster_id"] = new_id
    noise_mask = np.asarray(labels, dtype=np.int32) == -1
    if return_noise:
        return clusters, noise_mask
    return clusters


# ---------------------------------------------------------------------------
# Hierarchical Bisection (§3.3): cap 640 + KMeans(k=2) toi da 2 lan
# ---------------------------------------------------------------------------
def _split_two(cent_tpl: np.ndarray,
               random_state: int = KMEANS_RANDOM_STATE) -> np.ndarray:
    """Chia thanh vien thanh 2 manh (0/1). sklearn KMeans neu co,
    fallback: cat theo truc dai (deterministic, sklearn-free)."""
    n = len(cent_tpl)
    if n < 2:
        return np.zeros(n, dtype=np.int32)
    try:
        from sklearn.cluster import KMeans  # type: ignore
        km = KMeans(n_clusters=2, n_init=10,
                    random_state=int(random_state))
        return np.asarray(km.fit_predict(
            np.asarray(cent_tpl, dtype=np.float64)), dtype=np.int32)
    except Exception:  # noqa: BLE001 — fallback sklearn-free
        c = np.asarray(cent_tpl, dtype=np.float64)
        span = c.max(axis=0) - c.min(axis=0)
        axis = int(np.argmax(span))
        order = np.argsort(c[:, axis], kind="stable")
        lab = np.zeros(n, dtype=np.int32)
        lab[order[n // 2:]] = 1
        return lab


def bisect_oversize_cluster(member_idx: list[int],
                            boxes_tpl: list[list[float]],
                            h_inv: np.ndarray,
                            img_shape: tuple[int, int],
                            max_side: float = BISECT_CAP_PX,
                            depth: int = 0,
                            max_depth: int = BISECT_MAX_DEPTH,
                            bisect_log: list[dict[str, Any]] | None = None
                            ) -> list[list[int]]:
    """De quy tach cum vuot cap 640 thanh cac manh <= cap.

    Do s_k o he Input (can boxes_tpl + h_inv + img_shape). Tra ve danh
    sach manh (list of member-lists). Ghi log moi lan tach vao bisect_log.
    """
    from src.cluster.adaptive_cluster_cropper import cluster_square_side
    s_k = cluster_square_side([boxes_tpl[i] for i in member_idx],
                              h_inv, img_shape)
    if s_k <= max_side or depth >= max_depth or len(member_idx) < 2:
        return [list(member_idx)]
    cent = np.float32([_centroid_of_box(boxes_tpl[i]) for i in member_idx])
    part = _split_two(cent)
    out: list[list[int]] = []
    for v in (0, 1):
        sub = [m for m, p in zip(member_idx, part) if int(p) == v]
        if not sub:  # tranh manh rong (KMeans rong cuc hiem) -> giu nguyen
            out.append(list(member_idx))
            break
        out.extend(bisect_oversize_cluster(
            sub, boxes_tpl, h_inv, img_shape, max_side,
            depth + 1, max_depth, bisect_log))
    else:
        if bisect_log is not None:
            bisect_log.append({"depth": depth, "n_members": len(member_idx),
                               "s_k": float(s_k)})
        return out
    return out


def _centroid_of_box(box: list[float]) -> list[float]:
    return [(float(box[0]) + float(box[2])) / 2.0,
            (float(box[1]) + float(box[3])) / 2.0]
