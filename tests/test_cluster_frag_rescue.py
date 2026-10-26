"""Phase 5 lan4 — T-CLU-FRG tests for D1-sua frag rescue (Coder, CPU-only).

Scope: src/cluster/adaptive_cluster_cropper.py
(find_frag_rescue_pairs + clusters_to_crops rescue path) vs Verifier
Decision D1-D4 2026-10-26 §D (dropped<->dropped proximity, FRAG_RADIUS 250px
Input-space, toi da 2 slot/anh, uu tien tong Z cao nhat).

Does NOT touch Board 01/04. Does NOT modify calib_report.json / frozen.
CPU-only, numpy + cv2 only (no torch / YOLO / GPU).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.cluster import adaptive_cluster_cropper as ac  # noqa: E402


def _mk_blobs(n: int, z_top: float = 12.0) -> list[dict]:
    """n discrete blobs, Z giam dan (da sort Z giam, top-K)."""
    return [{"centroid_tpl": [float(i * 500.0), 0.0],
             "z_diff": float(z_top - i),
             "area": 120.0,
             "bbox_tpl": [float(i * 500.0 - 8), -8.0,
                          float(i * 500.0 + 8), 8.0]}
            for i in range(n)]


def _mk_clusters_discrete(n: int, z_top: float = 12.0) -> list[dict]:
    return [{"cluster_id": i, "members": [i],
             "z_cluster": float(z_top - i), "size": 1}
            for i in range(n)]


# ---------------------------------------------------------------------------
# T-CLU-FRG-01 — plan-locked constants (Verifier §D CHOT, khong tune)
# ---------------------------------------------------------------------------

def test_frag_rescue_constants_pinned():
    assert ac.RANK_TOP_N == 8
    assert ac.FRAG_RADIUS_PX == 250.0
    assert ac.MAX_FRAG_RESCUE_SLOTS == 2
    assert ac.MAX_CLUSTER_CROPS == 8


# ---------------------------------------------------------------------------
# T-CLU-FRG-02 — synthetic board12-like: cap gan (247.5px) duoc cuu,
# cap xa (>250) khong cuu; D1-cu (kept<->dropped) khong duoc dung
# ---------------------------------------------------------------------------

def test_find_pairs_close_dropped_rescued_far_not():
    # blobs idx 0..11; kept 0..7 far; dropped 8..11 trong do 8<->10 gan
    ci = np.zeros((12, 2), dtype=np.float64)
    ci[0:8, 0] = np.arange(8) * 100.0  # kept xa goc
    ci[8] = [1500.0, 1500.0]
    ci[9] = [3000.0, 3000.0]   # xa moi cap
    ci[10] = [1540.0, 1560.0]  # cach blob8 ~72px (<=250)
    ci[11] = [4000.0, 4000.0]  # xa moi cap
    dropped = [{"cluster_id": 8, "members": [8], "z_cluster": 4.78},
               {"cluster_id": 9, "members": [9], "z_cluster": 4.61},
               {"cluster_id": 10, "members": [10], "z_cluster": 4.53},
               {"cluster_id": 11, "members": [11], "z_cluster": 3.0}]
    pairs = ac.find_frag_rescue_pairs(dropped, ci)
    assert len(pairs) == 1  # toi da 1 cap (2 slot)
    (a, b) = pairs[0]
    got = sorted([int(list(a["members"])[0]), int(list(b["members"])[0])])
    assert got == [8, 10], f"phai cuu cap gan 8<->10, got {got}"


def test_find_pairs_all_far_no_rescue():
    ci = np.zeros((12, 2), dtype=np.float64)
    for i in range(12):
        ci[i] = [float(i * 2000.0), 0.0]  # moi cap cach nhau >=2000px
    dropped = [{"cluster_id": 8, "members": [8], "z_cluster": 4.78},
               {"cluster_id": 9, "members": [9], "z_cluster": 4.61},
               {"cluster_id": 10, "members": [10], "z_cluster": 4.53},
               {"cluster_id": 11, "members": [11], "z_cluster": 3.0}]
    assert ac.find_frag_rescue_pairs(dropped, ci) == []


# ---------------------------------------------------------------------------
# T-CLU-FRG-03 — K=8 suy bien: khong co idx>=8 -> khong rescue
# ---------------------------------------------------------------------------

def test_k8_no_rescue():
    ci = np.zeros((8, 2), dtype=np.float64)
    ci[:, 0] = np.arange(8) * 10.0  # tat ca gan nhau nhung khong co rank9+
    dropped = [{"cluster_id": i, "members": [i],
                "z_cluster": float(8 - i)} for i in range(8)]
    assert ac.find_frag_rescue_pairs(dropped, ci) == []
    # end-to-end: 8 discrete -> 8 crops, 0 dropped, khong co rescue flag
    blobs = _mk_blobs(8)
    clusters = _mk_clusters_discrete(8)
    crops, dlog = ac.clusters_to_crops(clusters, blobs, np.eye(3), (1000, 1000))
    assert len(crops) == 8 and len(dlog) == 0
    assert all(c.get("is_frag_rescue") is False for c in crops)


# ---------------------------------------------------------------------------
# T-CLU-FRG-04 — gioi han slot + uu tien tong Z + khong trung lap
# ---------------------------------------------------------------------------

def test_max_slots_priority_no_overlap():
    # 4 singleton dropped tao 2 cap gan: (8,9) tong Z=9.0 vs (10,11) tong=5.0
    ci = np.zeros((12, 2), dtype=np.float64)
    ci[8] = [0.0, 0.0]
    ci[9] = [50.0, 0.0]      # 8<->9 ~50px
    ci[10] = [5000.0, 0.0]
    ci[11] = [5050.0, 0.0]   # 10<->11 ~50px
    dropped = [{"cluster_id": 8, "members": [8], "z_cluster": 5.0},
               {"cluster_id": 9, "members": [9], "z_cluster": 4.0},
               {"cluster_id": 10, "members": [10], "z_cluster": 3.0},
               {"cluster_id": 11, "members": [11], "z_cluster": 2.0}]
    pairs = ac.find_frag_rescue_pairs(dropped, ci, max_slots=2)
    assert len(pairs) == 1  # chi 1 cap (2 slot)
    (a, b) = pairs[0]
    got = sorted([int(list(a["members"])[0]), int(list(b["members"])[0])])
    assert got == [8, 9], f"uu tien tong Z cao nhat, got {got}"
    # overlapping candidates: chi 1 cap khong trung lap
    ci2 = np.zeros((12, 2), dtype=np.float64)
    ci2[8] = [0.0, 0.0]
    ci2[9] = [40.0, 0.0]
    ci2[10] = [80.0, 0.0]  # 8-9-10 doi mot <=250
    ci2[11] = [9000.0, 0.0]
    dropped2 = [{"cluster_id": 8, "members": [8], "z_cluster": 5.0},
                {"cluster_id": 9, "members": [9], "z_cluster": 4.9},
                {"cluster_id": 10, "members": [10], "z_cluster": 4.8},
                {"cluster_id": 11, "members": [11], "z_cluster": 1.0}]
    pairs2 = ac.find_frag_rescue_pairs(dropped2, ci2, max_slots=2)
    assert len(pairs2) == 1
    used = [int(list(a["members"])[0]) for a, b in pairs2 for a in (a, b)]
    assert len(set(used)) == 2, "cap rescue khong duoc trung lap blob"


def test_multiblob_dropped_not_eligible():
    ci = np.array([[0.0, 0.0], [30.0, 0.0], [5000.0, 5000.0]])
    dropped = [{"cluster_id": 8, "members": [8, 9], "z_cluster": 6.0},
               {"cluster_id": 10, "members": [10], "z_cluster": 5.0}]
    # multi-blob du gan cung khong duoc rescue (span-guard xu ly rieng)
    assert ac.find_frag_rescue_pairs(dropped, ci) == []


# ---------------------------------------------------------------------------
# T-CLU-FRG-05 — end-to-end clusters_to_crops: rescue gop vao kept (8+2),
# Z thap cuoi danh sach, don temp field, deterministic
# ---------------------------------------------------------------------------

def test_clusters_to_crops_rescue_end_to_end():
    blobs = _mk_blobs(12)
    clusters = _mk_clusters_discrete(12)
    # H_inv dong nhat: tam Input = centroid_tpl; dat cap dropped 8<->10 gan
    # (Input-space) con cac cap khac xa -> chi cuu dung 1 cap.
    h = np.eye(3)
    # Ghi de centroid de tao khoang cach mong muon he Input (= Template).
    for i, x in enumerate([0, 1000, 2000, 3000, 4000, 5000, 6000, 7000,
                           10000, 30000, 10100, 40000]):
        blobs[i]["centroid_tpl"] = [float(x), 0.0]
        blobs[i]["bbox_tpl"] = [float(x - 8), -8.0, float(x + 8), 8.0]
    crops1, dlog1 = ac.clusters_to_crops(clusters, blobs, h, (1000, 1000))
    # kept 8 + rescue 2 = 10 crops; dropped 4-2 = 2
    assert len(crops1) == 10, f"8 kept + 2 rescue = 10, got {len(crops1)}"
    assert len(dlog1) == 2
    rescued = [c for c in crops1 if c.get("is_frag_rescue") is True]
    assert len(rescued) == 2
    got_members = sorted(m for c in rescued for m in c["members"])
    assert got_members == [8, 10], f"cuu dung cap 8<->10, got {got_members}"
    # rescue Z thap nam cuoi (khong chen span/Z cao)
    zs = [c["z_cluster"] for c in crops1]
    assert zs == sorted(zs, reverse=True)
    # dropped_log khong mang rescue flag
    assert all(d.get("is_frag_rescue") is False for d in dlog1)
    # temp fields duoc don (khong leak _is_* ra ngoai)
    for c in clusters:
        assert "_is_span" not in c and "_is_frag_rescue" not in c
    # deterministic: chay lai cho ket qua giong het
    clusters_b = _mk_clusters_discrete(12)
    crops2, dlog2 = ac.clusters_to_crops(clusters_b, blobs, h, (1000, 1000))
    assert [(c["members"], c["z_cluster"]) for c in crops1] == \
           [(c["members"], c["z_cluster"]) for c in crops2]
    assert dlog1 == dlog2


def test_rescue_does_not_break_span_priority():
    # span cluster (members [0,8]) phai duoc giu truoc ca khi co rescue
    blobs = _mk_blobs(12)
    for i in range(12):
        blobs[i]["centroid_tpl"] = [float(i * 500.0), 0.0]
        blobs[i]["bbox_tpl"] = [float(i * 500.0 - 8), -8.0,
                                float(i * 500.0 + 8), 8.0]
    clusters = [{"cluster_id": 0, "members": [0, 8], "z_cluster": 3.0}]
    clusters += [{"cluster_id": i + 1, "members": [i + 1 if i + 1 != 8 else 9],
                  "z_cluster": float(11 - i)} for i in range(11)]
    crops, _ = ac.clusters_to_crops(clusters, blobs, np.eye(3), (1000, 1000))
    assert crops[0]["is_span"] is True
    assert crops[0]["cluster_id"] == 0
