"""Phase 1 gate tests — T-CLU-01 / T-CLU-05 (Verifier, CPU-only).

Scope: validate Coder's Phase 1 deliverables (scripts/calibrate_dbscan.py,
results/calibration/calib_report.json, k_distance_val_boards05_12.png)
against plan.md §3.4 + §6.2 and verifier_rules.md §4.1.

Does NOT touch Board 01/04. Does NOT modify src/ or scripts/.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import sys
from pathlib import Path

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.calibrate_dbscan import (  # noqa: E402
    ALLOWED_CALIB_BOARDS,
    FORBIDDEN_TEST_BOARDS,
    cluster_blobs_single_linkage,
    connected_components_threshold,
    frozen_sha256_of,
    kneedle_knee,
    knn_distances,
    pipeline_hashes,
    weighted_distance_matrix,
)

CALIB_REPORT = PROJECT_ROOT / "results" / "calibration" / "calib_report.json"
KNEE_PNG = PROJECT_ROOT / "results" / "calibration" / "k_distance_val_boards05_12.png"

REQUIRED_REPORT_KEYS = {
    "eps", "min_samples", "k_raw", "knee_1nn_value", "knee_ref_k2_k3",
    "plateau_ok", "purity", "over_clustering_rate", "frag_recall_diag",
    "clustered_recall_diag", "frozen_sha256", "fallback_used",
}

# ---------------------------------------------------------------------------
# T-CLU-01 — weighted distance properties (plan §2.2.2)
# ---------------------------------------------------------------------------

def test_weighted_distance_properties():
    """d symmetric, d_ii=0, non-negative; noisy blob pushed away (AUDIT-01)."""
    rng = np.random.default_rng(20261023)
    cent = np.array([[0.0, 0.0], [100.0, 0.0], [500.0, 500.0]])
    z_good = np.array([5.0, 5.0, 5.0])
    z_noisy = np.array([5.0, -3.0, 5.0])  # blob 1 far below z0=2.0

    d_w, d_e = weighted_distance_matrix(cent, z_good)
    assert d_w.shape == (3, 3)
    # symmetry
    assert np.allclose(d_w, d_w.T), "D_w must be symmetric"
    # zero diagonal
    assert np.all(np.diag(d_w) == 0.0), "d_ii must be 0"
    # non-negativity
    assert np.all(d_w >= 0.0), "d_ij must be non-negative"
    # high-confidence pair ≈ euclidean
    assert d_w[0, 1] == pytest.approx(d_e[0, 1], rel=0.15)

    d_wn, _ = weighted_distance_matrix(cent, z_noisy)
    # noisy blob distance to a good blob must EXCEED pure euclidean
    assert d_wn[0, 1] > d_e[0, 1], (
        "Z-weighting must inflate distance to low-Z blob"
    )
    # noise-noise or noise-good never collapses below euclidean
    assert np.all(d_wn + np.eye(3) >= d_e), (
        "weighted distance must not shrink below euclidean"
    )


def test_single_linkage_no_noise_labels_and_deterministic():
    """AUDIT-01: m=1 never emits label -1; Union-Find is deterministic."""
    rng = np.random.default_rng(7)
    cent = rng.uniform(0, 2000, size=(12, 2))
    z = rng.uniform(-1, 6, size=12)
    for eps in (30.0, 50.0, 100.0, 800.0):
        lab = cluster_blobs_single_linkage(cent, z, eps)
        assert lab.shape == (12,)
        assert np.all(lab >= 0), f"m=1 must never emit -1 (eps={eps})"
        assert set(np.unique(lab)) == set(range(int(lab.max()) + 1))
    lab_a = cluster_blobs_single_linkage(cent, z, 50.0)
    lab_b = cluster_blobs_single_linkage(cent, z, 50.0)
    assert np.array_equal(lab_a, lab_b), "Single-Linkage must be deterministic"

    # synthetic T-CLU-02 geometry (2 multi-blob clusters + 1 isolated singleton)
    c = np.array([
        [0, 0], [30, 10], [10, 40], [50, 50],            # cluster A
        [400, 400], [430, 410], [410, 440],              # cluster B
        [1200, 1200],                                    # isolated singleton
    ], dtype=float)
    zz = np.full(8, 5.0)
    lab = cluster_blobs_single_linkage(c, zz, eps=50.0)
    sizes = sorted(np.bincount(lab).tolist(), reverse=True)
    assert sizes == [4, 3, 1], f"expected [4,3,1], got {sizes}"
    assert np.all(lab >= 0), "singleton must be a valid cluster, not noise"


def test_kneedle_deterministic():
    x = np.arange(50, dtype=float)
    y = np.sort(np.random.default_rng(0).uniform(0, 1000, 50))[::-1]
    assert kneedle_knee(x, y) == kneedle_knee(x, y)


# ---------------------------------------------------------------------------
# T-CLU-05 — calib_report schema, gates, frozen seal, knee figure
# ---------------------------------------------------------------------------

def _load_report() -> dict:
    assert CALIB_REPORT.exists(), f"missing frozen report: {CALIB_REPORT}"
    return json.loads(CALIB_REPORT.read_text(encoding="utf-8"))


def test_calib_report_schema_and_gates():
    r = _load_report()
    missing = REQUIRED_REPORT_KEYS - set(r.keys())
    assert not missing, f"calib_report.json missing keys: {sorted(missing)}"
    assert r["min_samples"] == 1, "Phase 1 main mode must be min_samples=1 (AUDIT-01)"
    assert isinstance(r["eps"], (int, float)) and r["eps"] > 0
    assert r["k_raw"] in (8, 12, 16), "K_raw must be one of the scanned {8,12,16}"
    # acceptance gates (plan §3.4)
    assert r["purity"] >= 0.85, f"Purity gate FAILED: {r['purity']}"
    assert r["over_clustering_rate"] <= 0.15, (
        f"Over-clustering gate FAILED: {r['over_clustering_rate']}"
    )
    # knee evidence present (main 1-NN + k=2,3 reference)
    assert np.isfinite(r["knee_1nn_value"]), "knee_1nn_value must be finite"
    assert "k2" in r["knee_ref_k2_k3"] and "k3" in r["knee_ref_k2_k3"]
    # zero-leakage seal in report
    assert r["boards"] == sorted(ALLOWED_CALIB_BOARDS), (
        f"report boards must be exactly 05-12, got {r['boards']}"
    )
    assert set(r["boards"]) & set(FORBIDDEN_TEST_BOARDS) == set()
    # frozen hash well-formed
    fh = r["frozen_sha256"]
    assert isinstance(fh, str) and len(fh) == 64
    assert all(c in "0123456789abcdef" for c in fh)


def test_frozen_sha256_recomputes():
    """Frozen seal must recompute from file content + frozen pipeline copies."""
    r = _load_report()
    pipe = pipeline_hashes()
    assert pipe == r["pipeline_files_sha256"], (
        "frozen pipeline copies drifted since freeze — re-copy or re-freeze"
    )
    assert frozen_sha256_of(r, pipe) == r["frozen_sha256"], (
        "frozen_sha256 does not match recomputation — report was hand-edited"
    )
    # each stored hash must equal the actual file bytes
    for rel, h in r["pipeline_files_sha256"].items():
        p = PROJECT_ROOT / rel
        assert p.exists(), f"frozen file missing: {rel}"
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        assert digest == h, f"hash drift in {rel}"


def test_knee_figure_exists_and_valid_png():
    assert KNEE_PNG.exists(), f"missing knee figure: {KNEE_PNG}"
    raw = KNEE_PNG.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", "knee figure is not a valid PNG"
    assert len(raw) > 10_000, f"knee PNG suspiciously small: {len(raw)} bytes"


def test_no_noise_rate_metric_used():
    """AUDIT-01: Phase 1 must not gate on any noise-rate metric, and the
    main clustering path must never emit DBSCAN noise labels (-1)."""
    r = _load_report()
    blob = json.dumps(r).lower()
    assert "noise_rate" not in blob, "noise_rate metric is banned in Phase 1 (AUDIT-01)"
    # main path: Union-Find Single-Linkage, sklearn-free, no -1 labels
    import scripts.calibrate_dbscan as calib_mod
    cc_src = inspect.getsource(calib_mod.connected_components_threshold)
    assert "-1" not in cc_src, (
        "connected_components_threshold (m=1 main path) must never emit -1"
    )
    main_src = inspect.getsource(calib_mod)
    assert "import sklearn" not in main_src and "from sklearn" not in main_src, (
        "main calibration path must be sklearn-free Union-Find (AUDIT-01)"
    )
    assert "DBSCAN(" not in main_src, (
        "no DBSCAN() call in main path — ablation m>=2 only, not the frozen path"
    )
    # behaviour: already covered by test_single_linkage_no_noise_labels...,
    # and frozen report pins min_samples=1
    assert r.get("min_samples") == 1


def test_calibrate_reproducibility_smoke():
    """Two smoke runs (max_images=1) must give bit-identical numeric fields.

    NOTE: out_dir must stay INSIDE the project (assert_writable_ok sandbox
    guard blocks /tmp on purpose) — use a scratch subdir under results/
    and remove it afterwards.
    """
    pytest.importorskip("cv2")
    from scripts.calibrate_dbscan import calibrate
    from scripts.blob_stats import LEGACY_PKU_ROOT  # noqa

    ann_root = PROJECT_ROOT.parent / "PCB_Anomaly_Inspector_Portable" / "data" / "raw" / "pku" / "Annotations"
    legacy = Path(str(LEGACY_PKU_ROOT))
    if not legacy.joinpath("Annotations").is_dir() and not ann_root.is_dir():
        pytest.skip("LEGACY PKU dataset not available on this machine")
    import shutil
    scratch = PROJECT_ROOT / "results" / "calibration" / "_verify_tmp_repro"
    outs = []
    try:
        for i in range(2):
            d = scratch / f"run{i}"
            d.mkdir(parents=True, exist_ok=True)
            body = calibrate(max_images=1, out_dir=d)
            outs.append({k: v for k, v in body.items()
                         if k not in {"calib_timestamp", "calib_elapsed_s",
                                      "smoke_mode", "frozen_sha256"}})
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    a, b = outs
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True), (
        "calibrate() is not reproducible across identical runs"
    )
