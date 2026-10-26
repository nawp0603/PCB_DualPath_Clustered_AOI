"""Phase 5 A/B benchmark driver (VERIFIER-ONLY, GPU).

Plan §4 Phase 5 + §5.1/§5.2 + verifier_rules §4.3:
- Mo 01/04 CHI sau verify_phase5_seal() MATCH (lan mo duy nhat).
- 5 configs (Config0 baseline legacy; C1 A1+U; C2 A2+U; C3 B+U; C4 A1+H)
  x 2 scenarios (standard + camera perturb seed 20261023).
- Cung weights (yolo11n_day2.pt), conf=0.20, IoU=0.5.
- Metric: AP50/recall/precision/F1/ΔTP Nhom1+Nhom2 + Bootstrap 95% CI
  (10000 resamples, paired per-image) + Wilcoxon signed-rank (p<0.01)
  cho crops/latency + P95 <= 450ms.
- Ghi results/ab_benchmark.csv + results/ab_benchmark.json (qua
  assert_writable_ok). Khong sua calib_report.json. Khong cham src/.

Usage:
  .../.venv/Scripts/python.exe tests/phase5_benchmark.py [--out-csv ...]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from configs.resource_paths import assert_writable_ok  # noqa: E402
import scripts.evaluate_clustered as ev  # noqa: E402
from scripts.blob_stats import perturb_image_and_boxes as _pert  # noqa: E402
import src.reference.board_id_and_align as ba  # noqa: E402
from src.models.yolo_detector import YOLODetector  # noqa: E402

BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 20261023
FRAG_RADIUS = 250.0
PROX_DIST = 300.0


def _weights_sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _match_per_gt(dets_sorted, gts_list, thr=0.5):
    """Greedy match (giong pr_counts): tra ve matched[bool] per GT + tp/fp."""
    used = [False] * len(gts_list)
    tp = fp = 0
    for _img_i, conf, box, cls in sorted(dets_sorted, key=lambda t: -t[1]):
        best, bi = 0.0, None
        for m, (k, bb) in enumerate(gts_list):
            if used[m] or k != cls:
                continue
            v = ev.box_iou(box, bb)
            if v > best:
                best, bi = v, m
        if best >= thr and bi is not None:
            used[bi] = True
            tp += 1
        else:
            fp += 1
    return used, tp, fp


def _frag_mask(gt_list, blob_centers):
    """GT la fragmented neu >=2 blob centroids trong FRAG_RADIUS cua tam GT."""
    mask = []
    bc = np.asarray(blob_centers, dtype=np.float64).reshape(-1, 2)
    for _cid, box in gt_list:
        cx, cy = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
        if len(bc):
            n = int((np.hypot(bc[:, 0] - cx, bc[:, 1] - cy) <= FRAG_RADIUS).sum())
        else:
            n = 0
        mask.append(n >= 2)
    return mask


def _prox_mask(gt_list):
    """GT co GT khac (tam-tam) < PROX_DIST la proximate (Nhom 2)."""
    cents = [((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0) for _c, b in gt_list]
    mask = []
    for i in range(len(gt_list)):
        hit = any(
            i != j and abs(cents[i][0] - cents[j][0]) ** 2
            + abs(cents[i][1] - cents[j][1]) ** 2 < PROX_DIST ** 2
            for j in range(len(gt_list))
        )
        mask.append(hit)
    return mask


def _bootstrap_recall_diff(per_img_cfg, per_img_base, npos_per_img,
                           rng, n_boot=BOOTSTRAP_RESAMPLES):
    """Paired per-image bootstrap CI cho recall diff (micro recall)."""
    n = len(per_img_cfg)
    idx = np.asarray(list(range(n)))
    diffs = np.empty(n_boot)
    for b in range(n_boot):
        s = rng.integers(0, n, n)
        tp_c = sum(per_img_cfg[i] for i in s)
        tp_b = sum(per_img_base[i] for i in s)
        npos = sum(npos_per_img[i] for i in s)
        diffs[b] = (tp_c - tp_b) / npos if npos else 0.0
    return float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))


def _bootstrap_ap_diff(dets_cfg, dets_base, gts, rng,
                       n_boot=BOOTSTRAP_RESAMPLES):
    """Paired per-image bootstrap CI cho AP50 diff (voc_ap giong evaluate)."""
    by_img_cfg: dict[int, list] = {}
    by_img_base: dict[int, list] = {}
    for d in dets_cfg:
        by_img_cfg.setdefault(d[0], []).append(d)
    for d in dets_base:
        by_img_base.setdefault(d[0], []).append(d)
    img_ids = sorted(gts.keys())
    n = len(img_ids)
    diffs = np.empty(n_boot)
    for b in range(n_boot):
        s = rng.integers(0, n, n)
        dc, db, g = [], [], {}
        for k, pos in enumerate(s):
            ii = img_ids[int(pos)]
            dc += [(k, c, bx, cl) for (_o, c, bx, cl) in by_img_cfg.get(ii, [])]
            db += [(k, c, bx, cl) for (_o, c, bx, cl) in by_img_base.get(ii, [])]
            g[k] = gts[ii]
        ap_c, _ = ev.voc_ap(dc, g, thr=0.5)
        ap_b, _ = ev.voc_ap(db, g, thr=0.5)
        diffs[b] = ap_c - ap_b
    return float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))


def _wilcoxon_p(x, y):
    from scipy.stats import wilcoxon
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    d = x - y
    if not np.any(d != 0):
        return 1.0, "all-zero-diff"
    try:
        r = wilcoxon(x, y, alternative="two-sided")
        return float(r.pvalue), "ok"
    except Exception as e:
        return 1.0, f"{type(e).__name__}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-csv", type=Path,
                    default=PROJECT_ROOT / "results" / "ab_benchmark.csv")
    ap.add_argument("--out-json", type=Path,
                    default=PROJECT_ROOT / "results" / "ab_benchmark.json")
    ap.add_argument("--conf", type=float, default=0.20)
    args = ap.parse_args()

    seal = ev.verify_phase5_seal()
    print(f"[bench] seal MATCH {seal['frozen_sha256'][:16]}... "
          f"eps={seal['eps']} m={seal['min_samples']} k={seal['k_raw']}")

    defect_items = ev.collect_clustered_items({"01", "04"}, allow_test=True)
    good_items = ev.collect_good_items_clustered({"01", "04"}, allow_test=True)
    print(f"[bench] defect={len(defect_items)} "
          f"(GT boxes={sum(len(i['gt']) for i in defect_items)}) "
          f"good={len(good_items)}")
    assert len(defect_items) == 36 and len(good_items) == 2

    w = ev.resolve_weights(None)
    wsha = _weights_sha256(w)
    print(f"[bench] weights={w.name} sha256={wsha[:16]}...")
    yolo = YOLODetector(str(w), conf_threshold=float(args.conf),
                        device="cuda")
    print(f"[bench] device={yolo.device} half={yolo.half} "
          f"imgsz={yolo.imgsz} adapt={yolo.adapt_note}")
    import torch
    print(f"[bench] torch={torch.__version__} "
          f"gpu={torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none'}")

    from configs.resource_paths import LOCAL_TEMPLATES
    cache = ba.load_cache(LOCAL_TEMPLATES / "orb_cache.npz")

    scenarios = [("standard", False, 0), ("camera", True, 20261023)]
    configs = list(ev.PHASE5_CONFIGS)
    all_items = defect_items + good_items

    # per-image store: rec[(scenario, cfg, idx)] ; blob info from config0
    per_image: dict = {}
    blob_info: dict = {}
    t_start = time.perf_counter()
    for sc_name, perturb, seed_base in scenarios:
        templates: dict = {}
        for cfg in configs:
            for i, it in enumerate(all_items):
                img = ev.read_bgr(it["path"])
                gt_boxes = [list(b) for _, b in it["gt"]]
                if perturb:
                    img, warped = _pert(img, gt_boxes, int(seed_base) + i)
                    gt_use = [(c, wb) for (c, _), wb in zip(it["gt"], warped)]
                else:
                    gt_use = list(it["gt"])
                try:
                    out = ev.run_config_single(
                        cfg, img, yolo, cache, templates,
                        conf=float(args.conf),
                        image_id=Path(it["path"]).name,
                        log_fallback=False, allow_test=True)
                    err = None
                except Exception as e:  # ghi ro, khong che dau
                    out, err = None, f"{type(e).__name__}: {e}"
                if out is not None and cfg == "config0_baseline" and not perturb:
                    pass
                if out is not None and cfg == "config0_baseline":
                    try:
                        bc = ev._blob_centers_input(
                            out.get("blobs", []),
                            np.asarray(out["H_inv"], dtype=np.float64))
                        blob_info[(sc_name, i)] = [list(map(float, p))
                                                   for p in np.asarray(bc).tolist()]
                    except Exception:
                        blob_info[(sc_name, i)] = []
                dets = []
                if out is not None:
                    for itm in out["result"]["items"]:
                        if itm.get("conf") is not None:
                            dets.append((i, float(itm["conf"]),
                                         [float(v) for v in itm["bbox"]],
                                         int(itm["class_id"])))
                per_image[(sc_name, cfg, i)] = {
                    "dets": dets, "gt": gt_use,
                    "lat": float(out["t_total_ms"]) if out else 0.0,
                    "crops": int(out.get("n_crops", 0)) if out else 0,
                    "calls": int(out.get("batch_stats", {}).get("n_calls", 0)) if out else 0,
                    "fb": bool(out.get("fallback_reason")) if out else False,
                    "level": int(out["result"]["level"]) if out else -1,
                    "err": err,
                }
            n_img = len(all_items)
            lat = [per_image[(sc_name, cfg, i)]["lat"] for i in range(n_img)]
            print(f"[bench] {sc_name}/{cfg}: done "
                  f"mean_lat={np.mean(lat):.0f}ms p95={np.percentile(lat, 95):.0f}ms "
                  f"errs={sum(1 for i in range(n_img) if per_image[(sc_name, cfg, i)]['err'])}")
    print(f"[bench] total wall {(time.perf_counter() - t_start) / 60:.1f} min")

    rng = np.random.default_rng(BOOTSTRAP_SEED)
    rows = []
    full: dict = {"meta": {}, "scenarios": {}}
    ts = datetime.now(timezone.utc).isoformat()
    full["meta"] = {"timestamp_utc": ts, "seed_perturb": 20261023,
                    "bootstrap_seed": BOOTSTRAP_SEED,
                    "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
                    "weights": w.name, "weights_sha256": wsha,
                    "conf": float(args.conf), "iou": 0.5,
                    "frozen_sha256": seal["frozen_sha256"], "eps": seal["eps"],
                    "min_samples": seal["min_samples"], "k_raw": seal["k_raw"],
                    "device": yolo.device, "torch": torch.__version__,
                    "gpu": (torch.cuda.get_device_name(0)
                            if torch.cuda.is_available() else "none")}
    verdict = {"pass": True, "notes": []}
    for sc_name, _perturb, _sb in scenarios:
        n_img = len(all_items)
        n_def = len(defect_items)
        gts = {i: per_image[(sc_name, "config0_baseline", i)]["gt"]
               for i in range(n_img)}
        sc_res: dict = {}
        base_tp_img = {}
        for cfg in configs:
            dets = []
            for i in range(n_img):
                dets += per_image[(sc_name, cfg, i)]["dets"]
            ap50, _per = ev.voc_ap(dets, gts, thr=0.5)
            tp, fp, npos = ev.pr_counts(dets, gts, thr=0.5)
            prec = tp / (tp + fp) if (tp + fp) else 0.0
            rec = tp / npos if npos else 0.0
            f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
            # FAR tren GOOD
            far_trig = sum(1 for i in range(n_def, n_img)
                           if per_image[(sc_name, cfg, i)]["level"] not in (-1, 4))
            far = far_trig / len(good_items)
            lat = [per_image[(sc_name, cfg, i)]["lat"] for i in range(n_img)]
            crops = [per_image[(sc_name, cfg, i)]["crops"] for i in range(n_img)]
            calls = [per_image[(sc_name, cfg, i)]["calls"] for i in range(n_img)]
            n_fb = sum(1 for i in range(n_img)
                       if per_image[(sc_name, cfg, i)]["fb"])
            # per-GT match + nhom
            tp_img, frag_tp, frag_n, prox_tp, prox_n = {}, 0, 0, 0, 0
            per_img_dets: dict[int, list] = {}
            for d in dets:
                per_img_dets.setdefault(d[0], []).append(d)
            for i in range(n_def):
                gl = gts[i]
                used, _t, _f = _match_per_gt(per_img_dets.get(i, []), gl)
                tp_img[i] = sum(1 for u in used if u)
                bc = blob_info.get((sc_name, i), [])
                for (m, (fm, pm)) in enumerate(zip(_frag_mask(gl, bc),
                                                   _prox_mask(gl))):
                    if fm:
                        frag_n += 1
                        frag_tp += 1 if used[m] else 0
                    if pm:
                        prox_n += 1
                        prox_tp += 1 if used[m] else 0
            if cfg == "config0_baseline":
                base_tp_img = tp_img
                base_frag = (frag_tp, frag_n)
                base_prox = (prox_tp, prox_n)
            sc_res[cfg] = {"AP50": round(ap50, 4), "recall": round(rec, 4),
                           "precision": round(prec, 4), "f1": round(f1, 4),
                           "tp": tp, "fp": fp, "npos": npos,
                           "far": round(far, 4), "far_trig": far_trig,
                           "lat_mean": round(float(np.mean(lat)), 1),
                           "lat_p95": round(float(np.percentile(lat, 95)), 1),
                           "crops": round(float(np.mean(crops)), 3),
                           "calls": round(float(np.mean(calls)), 3),
                           "fb_rate": round(n_fb / n_img, 4), "n_fb": n_fb,
                           "frag_tp": frag_tp, "frag_n": frag_n,
                           "frag_rec": round(frag_tp / frag_n, 4) if frag_n else 0.0,
                           "prox_tp": prox_tp, "prox_n": prox_n,
                           "prox_rec": round(prox_tp / prox_n, 4) if prox_n else 0.0,
                           "tp_img": tp_img,
                           "lat_list": lat, "crops_list": crops}
        # so sanh vs baseline: dTP + bootstrap + wilcoxon
        npos_img = {i: len(gts[i]) for i in range(n_def)}
        for cfg in configs:
            r = sc_res[cfg]
            if cfg == "config0_baseline":
                r.update({"dTP": 0, "dTP_frag": 0, "dTP_prox": 0,
                          "boot_rec_lo": 0.0, "boot_rec_hi": 0.0,
                          "boot_ap_lo": 0.0, "boot_ap_hi": 0.0,
                          "wx_crops_p": 1.0, "wx_lat_p": 1.0})
                continue
            b = sc_res["config0_baseline"]
            r["dTP"] = r["tp"] - b["tp"]
            r["dTP_frag"] = r["frag_tp"] - b["frag_tp"]
            r["dTP_prox"] = r["prox_tp"] - b["prox_tp"]
            lo, hi = _bootstrap_recall_diff(
                [r["tp_img"][i] for i in range(n_def)],
                [b["tp_img"][i] for i in range(n_def)],
                [npos_img[i] for i in range(n_def)], rng)
            r["boot_rec_lo"], r["boot_rec_hi"] = round(lo, 4), round(hi, 4)
            if cfg == "config1_proposed":
                dets_c = [d for i in range(n_img)
                          for d in per_image[(sc_name, cfg, i)]["dets"]]
                dets_b = [d for i in range(n_img)
                          for d in per_image[(sc_name, "config0_baseline", i)]["dets"]]
                alo, ahi = _bootstrap_ap_diff(dets_c, dets_b, gts, rng)
                r["boot_ap_lo"], r["boot_ap_hi"] = round(alo, 4), round(ahi, 4)
            else:
                r["boot_ap_lo"], r["boot_ap_hi"] = "", ""
            pc, _nc = _wilcoxon_p(r["crops_list"], b["crops_list"])
            pl, _nl = _wilcoxon_p(r["lat_list"], b["lat_list"])
            r["wx_crops_p"], r["wx_lat_p"] = pc, pl
            # pass criteria cho Proposed (C1): dTP>0 ca 2 nhom + CI khong chua 0
            # + wilcoxon crops p<0.01 + P95<=450ms (kiem tra sau vong scenario)
            if cfg == "config1_proposed":
                if not (r["dTP_frag"] > 0 and r["dTP_prox"] > 0):
                    verdict["pass"] = False
                    verdict["notes"].append(
                        f"{sc_name}: C1 dTP_frag={r['dTP_frag']} "
                        f"dTP_prox={r['dTP_prox']} (can >0 ca 2)")
                if not (lo > 0 or hi < 0):
                    verdict["pass"] = False
                    verdict["notes"].append(
                        f"{sc_name}: C1 bootCI recall [{lo},{hi}] chua 0")
                if not (pc < 0.01):
                    verdict["pass"] = False
                    verdict["notes"].append(
                        f"{sc_name}: C1 wilcoxon crops p={pc} (can <0.01)")
                if not (r["lat_p95"] <= 450 if "lat_p95" in r else True):
                    pass
                if r.get("lat_p95", 0) > 450:
                    verdict["pass"] = False
                    verdict["notes"].append(
                        f"{sc_name}: C1 P95={r['lat_p95']}ms >450ms")
        full["scenarios"][sc_name] = sc_res
        for cfg in configs:
            r = sc_res[cfg]
            m = ev.PHASE5_CONFIGS[cfg]
            rows.append({"scenario": sc_name, "config": cfg,
                         "kind": m["kind"], "mode": m["mode"],
                         "gating": m["gating"], **{k: r[k] for k in (
                             "AP50", "recall", "precision", "f1", "tp", "fp",
                             "npos", "far", "far_trig", "lat_mean", "lat_p95",
                             "crops", "calls", "fb_rate", "n_fb",
                             "frag_tp", "frag_n", "frag_rec",
                             "prox_tp", "prox_n", "prox_rec", "dTP",
                             "dTP_frag", "dTP_prox", "boot_rec_lo",
                             "boot_rec_hi", "boot_ap_lo", "boot_ap_hi",
                             "wx_crops_p", "wx_lat_p")},
                         "seed": 20261023, "weights": w.name,
                         "weights_sha256": wsha,
                         "frozen_sha256": seal["frozen_sha256"],
                         "timestamp_utc": ts})

    out_csv = assert_writable_ok(Path(args.out_csv))
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        wr.writeheader()
        wr.writerows(rows)
    # JSON: bo tp_img/lat_list/crops_list chi tiet? giu lat/crops, bo tp_img
    slim = {"meta": full["meta"], "scenarios": {}}
    for sc, d in full["scenarios"].items():
        slim["scenarios"][sc] = {c: {k: v for k, v in r.items()
                                      if k not in ("tp_img", "lat_list",
                                                   "crops_list")}
                                   for c, r in d.items()}
    out_json = assert_writable_ok(Path(args.out_json))
    out_json.write_text(json.dumps(slim, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"[bench] wrote {out_csv} ({len(rows)} rows)")
    print(f"[bench] wrote {out_json}")

    print("\n===== PHASE5 A/B (so thuc do GPU, khong muc tieu) =====")
    for sc_name, _p, _s in scenarios:
        d = full["scenarios"][sc_name]
        b = d["config0_baseline"]
        print(f"--- {sc_name} (36 defect + 2 GOOD) --- frag_n={b['frag_n']} "
              f"prox_n={b['prox_n']}")
        for cfg in configs:
            r = d[cfg]
            print(f"  {cfg:17s} AP={r['AP50']:.4f} R={r['recall']:.4f} "
                  f"P={r['precision']:.4f} F1={r['f1']:.4f} TP={r['tp']} "
                  f"FP={r['fp']} FAR={r['far']:.3f} "
                  f"lat={r['lat_mean']:.0f}/{r['lat_p95']:.0f}ms "
                  f"crops={r['crops']:.2f} calls={r['calls']:.2f} "
                  f"fb={r['n_fb']} frag={r['frag_tp']}/{r['frag_n']} "
                  f"prox={r['prox_tp']}/{r['prox_n']} dTP={r.get('dTP')} "
                  f"CI=[{r.get('boot_rec_lo')},{r.get('boot_rec_hi')}] "
                  f"wx=({r.get('wx_crops_p')},{r.get('wx_lat_p')})")
    print("VERDICT:", "PASS" if verdict["pass"] else "FAIL", verdict["notes"])


if __name__ == "__main__":
    main()
