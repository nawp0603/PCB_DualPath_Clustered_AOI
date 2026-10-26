#!/usr/bin/env python3
"""Phase 1 — Blob statistics on Train/Validation Boards 05-12 (CPU-only).

Collects raw blobs (K_raw=16) via the FROZEN Branch-B pipeline
(identify_and_align + run_branch_b, copies frozen in src/reference/)
on READ-ONLY Train/Validation Boards 05-12 from LEGACY_PKU_ROOT,
both Standard and Camera-Perturbation variants (seed 20261023).

STRICT SANDBOX + ZERO DATA LEAKAGE:
  - ONLY boards {"05".."12"} are allowed. Any reference to Board 01/04
    (Zero-defect-training Boards, held-out test set) raises RuntimeError.
  - NEVER writes outside the new project (all outputs via assert_writable_ok).
  - CPU-only: no YOLO / GPU inference in this script.

Outputs (inside new project only):
  - results/calibration/blob_cache_<variant>.npz  (per-image blob arrays)
  - results/calibration/blob_stats.json            (counts, distributions)

Plan refs: plan.md §3.1, §4 Phase 1; coder_rules.md §3.4 (AUDIT-02).
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from configs.resource_paths import (  # noqa: E402
    LEGACY_PKU_ROOT,
    LOCAL_TEMPLATES,
    assert_writable_ok,
)

import cv2  # noqa: E402
import numpy as np  # noqa: E402

# Frozen Branch-B (local copies, never the original project).
import src.reference.board_id_and_align as ba  # noqa: E402
import src.reference.tolerance_diff as td  # noqa: E402

# ---------------------------------------------------------------------------
# Zero-leakage guards (AUDIT-02)
# ---------------------------------------------------------------------------
ALLOWED_CALIB_BOARDS = {"05", "06", "07", "08", "09", "10", "11", "12"}
FORBIDDEN_TEST_BOARDS = {"01", "04"}  # Zero-defect-training Boards — locked to Phase 5
CALIB_SEED = 20261023
K_RAW_COLLECT = 16  # collect wide, decide {8,12,16} in calibrate_dbscan.py

CLASS_ID = {
    "open_circuit": 0,
    "short": 1,
    "mouse_bite": 2,
    "spur": 3,
    "spurious_copper": 4,
    "missing_hole": 5,
}


def _assert_no_leakage(boards: set[str] | list[str]) -> None:
    bad = set(boards) & FORBIDDEN_TEST_BOARDS
    if bad:
        raise RuntimeError(
            "[ZERO LEAKAGE] Refusing to touch Zero-defect-training Boards "
            f"{sorted(bad)}. Phase 1-4 may only use Boards 05-12."
        )


def board_id_from_xml_filename(filename: str) -> str:
    return Path(filename).stem.split("_")[0].zfill(2)


def resolve_defect_image(ann_dir_name: str, xml_path: Path,
                         xml_filename_field: str) -> Path | None:
    """Resolve defect image by XML stem (global index), read-only legacy root."""
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


def collect_calib_items(boards: set[str] = ALLOWED_CALIB_BOARDS) -> list[dict[str, Any]]:
    """Collect defect items for calibration boards (READ-ONLY legacy)."""
    _assert_no_leakage(set(boards))
    unknown = set(boards) - ALLOWED_CALIB_BOARDS
    if unknown:
        raise RuntimeError(f"Unknown calibration boards: {sorted(unknown)}")
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
            _assert_no_leakage({bid})
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
    _assert_no_leakage({it["board"] for it in items})
    return items


# ---------------------------------------------------------------------------
# Camera perturbation (local copy of evaluate.perturb_image_and_boxes —
# self-contained so this script never imports the GPU/YOLO harness).
# ---------------------------------------------------------------------------
def perturb_image_and_boxes(img_bgr: np.ndarray, boxes: list[list[float]],
                            seed: int) -> tuple[np.ndarray, list[list[float]]]:
    rng = np.random.default_rng(seed)
    h, w = img_bgr.shape[:2]
    dx, dy = float(rng.uniform(-25, 25)), float(rng.uniform(-25, 25))
    ang = float(rng.uniform(-2.0, 2.0))
    cx, cy = w / 2.0, h / 2.0
    m_aff = cv2.getRotationMatrix2D((cx, cy), ang, 1.0)
    m_aff[0, 2] += dx
    m_aff[1, 2] += dy
    h_aff = np.vstack([m_aff, [0.0, 0.0, 1.0]])
    j = 0.008
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = src + rng.uniform(-j, j, size=(4, 2)).astype(np.float32) * np.float32([w, h])
    h_persp, _ = cv2.findHomography(src, dst)
    if h_persp is None:
        h_persp = np.eye(3)
    h_all = h_persp @ h_aff
    warped = cv2.warpPerspective(img_bgr, h_all, (w, h),
                                 flags=cv2.INTER_LINEAR,
                                 borderMode=cv2.BORDER_REPLICATE)
    gain = float(rng.uniform(0.85, 1.15))
    bias = float(rng.uniform(-10.0, 10.0))
    f = warped.astype(np.float32) * gain + bias
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    rx, ry = (xs - w / 2) / (w / 2), (ys - h / 2) / (h / 2)
    rr = np.sqrt(rx ** 2 + ry ** 2) / np.sqrt(2.0)
    vig = 1.0 - 0.15 * np.clip(rr, 0, 1) ** 2
    f *= vig[..., None]
    f += rng.normal(0, float(rng.uniform(3.0, 5.0)), size=f.shape)
    out = np.clip(f, 0, 255).astype(np.uint8)
    new_boxes: list[list[float]] = []
    if boxes:
        pts = []
        for b in boxes:
            pts.append([[b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]]])
        arr = np.float32(pts).reshape(-1, 1, 2)
        wpts = cv2.perspectiveTransform(arr, h_all.astype(np.float32)).reshape(-1, 4, 2)
        for q in wpts:
            x1, y1 = float(np.clip(q[:, 0].min(), 0, w - 1)), float(np.clip(q[:, 1].min(), 0, h - 1))
            x2, y2 = float(np.clip(q[:, 0].max(), 0, w - 1)), float(np.clip(q[:, 1].max(), 0, h - 1))
            if x2 - x1 >= 4 and y2 - y1 >= 4:
                new_boxes.append([x1, y1, x2, y2])
    return out, new_boxes


def read_bgr(path: str | Path) -> np.ndarray:
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(f"Cannot decode image: {path}")
    return img


def run_branch_b_on_image(img_bgr: np.ndarray, cache: dict[str, Any],
                          templates: dict[str, np.ndarray],
                          top_k: int = K_RAW_COLLECT) -> dict[str, Any]:
    """Align (fast) + Branch-B diff + blob extraction. CPU-only, no YOLO."""
    r = ba.identify_and_align(img_bgr, cache, fast=True)
    _assert_no_leakage({str(r["board_id"])})
    bid = r["board_id"]
    tpl = templates.get(bid)
    if tpl is None:
        tpl_p = LOCAL_TEMPLATES / f"{bid}.JPG"
        data = np.fromfile(str(tpl_p), dtype=np.uint8)
        tpl = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
        if tpl is None:
            raise FileNotFoundError(f"Cannot decode template: {tpl_p}")
        if tpl.shape != r["warped"].shape[:2]:
            tpl = cv2.resize(tpl, (r["warped"].shape[1], r["warped"].shape[0]),
                             interpolation=cv2.INTER_AREA)
        templates[bid] = tpl
    out = td.run_branch_b(r["warped"], tpl, top_k=top_k)
    return {"board_id": bid, "H": np.asarray(r["H"], dtype=np.float64),
            "H_inv": np.asarray(r["H_inv"], dtype=np.float64),
            "warped_shape": list(r["warped"].shape[:2]),
            "img_shape": list(img_bgr.shape[:2]),
            "blobs": out["blobs"]}


def collect_blobs(variant: str, max_images: int | None = None,
                  boards: set[str] = ALLOWED_CALIB_BOARDS) -> dict[str, Any]:
    """Collect blobs for one variant ('standard' | 'camera')."""
    _assert_no_leakage(set(boards))
    if variant not in ("standard", "camera"):
        raise ValueError("variant must be 'standard' or 'camera'")
    items = collect_calib_items(boards)
    if max_images is not None:
        items = items[: max(0, int(max_images))]
    cache = ba.load_cache(LOCAL_TEMPLATES / "orb_cache.npz")
    templates: dict[str, np.ndarray] = {}
    records: list[dict[str, Any]] = []
    for i, it in enumerate(items):
        img = read_bgr(it["path"])
        gt_boxes = [list(b) for _, b in it["gt"]]
        gt_use = list(it["gt"])
        if variant == "camera":
            img, warped = perturb_image_and_boxes(img, gt_boxes, CALIB_SEED + i)
            gt_use = [(c, wb) for (c, _), wb in zip(it["gt"], warped)]
        res = run_branch_b_on_image(img, cache, templates)
        records.append({
            "image": Path(it["path"]).name,
            "board": it["board"],
            "gt": gt_use,
            "H_inv": res["H_inv"].tolist(),
            "img_shape": res["img_shape"],
            "blobs": res["blobs"],
        })
    return {"variant": variant, "seed": CALIB_SEED if variant == "camera" else 0,
            "boards": sorted(boards), "records": records}


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    n_blobs = [len(r["blobs"]) for r in records]
    arr = np.asarray(n_blobs, dtype=np.int32) if n_blobs else np.zeros(0)
    per_board: dict[str, Any] = {}
    for b in sorted({r["board"] for r in records}):
        v = [len(r["blobs"]) for r in records if r["board"] == b]
        a = np.asarray(v, dtype=float)
        per_board[b] = {"n_images": len(v),
                        "mean_blobs": round(float(a.mean()), 3) if len(a) else 0.0,
                        "frac_gt8": round(float((a > 8).mean()), 4) if len(a) else 0.0,
                        "frac_gt12": round(float((a > 12).mean()), 4) if len(a) else 0.0,
                        "frac_gt16": round(float((a > 16).mean()), 4) if len(a) else 0.0}
    return {
        "n_images": len(records),
        "mean_blobs": round(float(arr.mean()), 3) if len(arr) else 0.0,
        "frac_images_gt8": round(float((arr > 8).mean()), 4) if len(arr) else 0.0,
        "frac_images_gt12": round(float((arr > 12).mean()), 4) if len(arr) else 0.0,
        "frac_images_gt16": round(float((arr > 16).mean()), 4) if len(arr) else 0.0,
        "max_blobs": int(arr.max()) if len(arr) else 0,
        "per_board": per_board,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 1 blob stats (Boards 05-12, CPU-only).")
    ap.add_argument("--max-images", type=int, default=None,
                    help="Smoke-test limit (None = full Board 05-12).")
    ap.add_argument("--variants", nargs="+", default=["standard", "camera"],
                    choices=["standard", "camera"])
    ap.add_argument("--out-dir", type=Path,
                    default=Path(__file__).resolve().parent.parent / "results" / "calibration")
    args = ap.parse_args()

    out_dir = assert_writable_ok(Path(args.out_dir))
    out_dir.mkdir(parents=True, exist_ok=True)
    combined: dict[str, Any] = {}
    for variant in args.variants:
        print(f"[blob_stats] variant={variant} collecting (Boards 05-12)...")
        data = collect_blobs(variant, max_images=args.max_images)
        stats = summarize(data["records"])
        combined[variant] = stats
        print(f"[blob_stats] {variant}: {stats['n_images']} images, "
              f"mean_blobs={stats['mean_blobs']}, >8={stats['frac_images_gt8']}, "
              f">12={stats['frac_images_gt12']}, max={stats['max_blobs']}")
        # Per-image blob dump (centroid_tpl, z, area, bbox) for calibration.
        rec = data["records"]
        n = len(rec)
        max_b = max([len(r["blobs"]) for r in rec] + [0])
        cent = np.full((n, max(K_RAW_COLLECT, max_b), 2), np.nan, dtype=np.float32)
        zval = np.full((n, max(K_RAW_COLLECT, max_b)), np.nan, dtype=np.float32)
        for i, r in enumerate(rec):
            for j, b in enumerate(r["blobs"][: cent.shape[1]]):
                cent[i, j] = np.float32(b["centroid_tpl"])
                zval[i, j] = np.float32(b["z_diff"])
        npz_p = assert_writable_ok(out_dir / f"blob_cache_{variant}.npz")
        np.savez_compressed(npz_p, centroids=cent, z=zval,
                            boards=np.array([r["board"] for r in rec]),
                            images=np.array([r["image"] for r in rec]))
        print(f"[blob_stats] wrote {npz_p}")
    js_p = assert_writable_ok(out_dir / "blob_stats.json")
    js_p.write_text(json.dumps({"meta": {"boards": sorted(ALLOWED_CALIB_BOARDS),
                                         "k_raw_collect": K_RAW_COLLECT,
                                         "seed_camera": CALIB_SEED,
                                         "leakage_guard": "05-12 only; 01/04 locked"},
                                "stats": combined}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"[blob_stats] wrote {js_p}")


if __name__ == "__main__":
    main()
