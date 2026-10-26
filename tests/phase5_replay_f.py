"""Phase 5 F.3 GPU replay (VERIFIER-ONLY).

Spec F.3 (lan22-25, khong doi): C1 A1+U baseline (tat ca DISABLED) + F2
(rescue_conf 0.15/0.12) + F1 (rescue_keep_res) + F3 (rescue_r 120/150/200),
x2 scenarios (standard + camera perturb seed 20261023), 01/04 ONLY sau
verify_phase5_seal() MATCH. conf=0.20 IoU=0.5 weights yolo11n_day2.pt.
Metric giong het tests/phase5_benchmark.py (Bootstrap 10000 paired
per-image + Wilcoxon p<0.01 + P95<=450). Ghi results/phase5_replay_f.csv
+ .json (FILE MOI — sealed ab_benchmark.* NGUYEN). Khong sua src/scripts.

Usage:
  smoke:  .../.venv/Scripts/python.exe tests/phase5_replay_f.py --smoke
  full:   .../.venv/Scripts/python.exe tests/phase5_replay_f.py
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from configs.resource_paths import assert_writable_ok  # noqa: E402
from configs.resource_paths import LOCAL_TEMPLATES  # noqa: E402
import scripts.evaluate_clustered as ev  # noqa: E402
from scripts.blob_stats import perturb_image_and_boxes as _pert  # noqa: E402
import src.reference.board_id_and_align as ba  # noqa: E402
from src.models.yolo_detector import YOLODetector  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "p5bench", PROJECT_ROOT / "tests" / "phase5_benchmark.py")
P5 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P5)

# (variant, config_name, rescue_kwargs) — tat ca F deu tren A1+U (C1).
VARIANTS: list[tuple[str, str, dict]] = [
    ("C0_ref", "config0_baseline", {}),
    ("C1_base", "config1_proposed", {}),
    ("F2_015", "config1_proposed", {"rescue_conf": 0.15}),
    ("F2_012", "config1_proposed", {"rescue_conf": 0.12}),
    ("F1_keep", "config1_proposed", {"rescue_keep_res": True}),
    ("F3_120", "config1_proposed", {"rescue_r": 120.0}),
    ("F3_150", "config1_proposed", {"rescue_r": 150.0}),
    ("F3_200", "config1_proposed", {"rescue_r": 200.0}),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-csv", type=Path,
                    default=PROJECT_ROOT / "results" / "phase5_replay_f.csv")
    ap.add_argument("--out-json", type=Path,
                    default=PROJECT_ROOT / "results" / "phase5_replay_f.json")
    ap.add_argument("--conf", type=float, default=0.20)
    ap.add_argument("--smoke", action="store_true",
                    help="probe nhanh: 3 anh std x 3 variants (C0,C1,F2_012)")
    ap.add_argument("--n-boot", type=int, default=10_000)
    args = ap.parse_args()

    seal = ev.verify_phase5_seal()
    print(f"[replay] seal MATCH {seal['frozen_sha256'][:16]}... "
          f"eps={seal['eps']} m={seal['min_samples']} k={seal['k_raw']}")

    defect_items = ev.collect_clustered_items({"01", "04"}, allow_test=True)
    good_items = ev.collect_good_items_clustered({"01", "04"}, allow_test=True)
    print(f"[replay] defect={len(defect_items)} "
          f"(GT boxes={sum(len(i['gt']) for i in defect_items)}) "
          f"good={len(good_items)}")

    variants = list(VARIANTS)
    scenarios = [("standard", False, 0), ("camera", True, 20261023)]
    if args.smoke:
        variants = [v for v in variants
                    if v[0] in ("C0_ref", "C1_base", "F2_012")]
        scenarios = [("standard", False, 0)]
        defect_items = defect_items[:3]
        good_items = []
    else:
        assert len(defect_items) == 36 and len(good_items) == 2
    all_items = defect_items + good_items
    n_def = len(defect_items)

    w = ev.resolve_weights(None)
    wsha = P5._weights_sha256(w)
    print(f"[replay] weights={w.name} sha256={wsha[:16]}...")
    yolo = YOLODetector(str(w), conf_threshold=float(args.conf),
                        device="cuda")
    print(f"[replay] device={yolo.device} half={yolo.half}")
    import torch
    print(f"[replay] torch={torch.__version__} "
          f"gpu={torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none'}")

    cache = ba.load_cache(LOCAL_TEMPLATES / "orb_cache.npz")

    per_image: dict = {}
    blob_info: dict = {}
    t_start = time.perf_counter()
    for sc_name, perturb, seed_base in scenarios:
        templates: dict = {}
        for vname, cfg, rkw in variants:
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
                        log_fallback=False, allow_test=True, **rkw)
                    err = None
                except Exception as e:
                    out, err = None, f"{type(e).__name__}: {e}"
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
                per_image[(sc_name, vname, i)] = {
                    "dets": dets, "gt": gt_use,
                    "lat": float(out["t_total_ms"]) if out else 0.0,
                    "crops": int(out.get("n_crops", 0)) if out else 0,
                    "calls": int(out.get("batch_stats", {}).get("n_calls", 0)) if out else 0,
                    "fb": bool(out.get("fallback_reason")) if out else False,
                    "level": int(out["result"]["level"]) if out else -1,
                    "err": err,
                }
            n_img = len(all_items)
            lat = [per_image[(sc_name, vname, i)]["lat"] for i in range(n_img)]
            errs = sum(1 for i in range(n_img)
                       if per_image[(sc_name, vname, i)]["err"])
            print(f"[replay] {sc_name}/{vname}: done "
                  f"mean_lat={np.mean(lat):.0f}ms p95={np.percentile(lat, 95):.0f}ms "
                  f"errs={errs}", flush=True)
    print(f"[replay] total wall {(time.perf_counter() - t_start) / 60:.1f} min")

    rng = np.random.default_rng(P5.BOOTSTRAP_SEED)
    rows = []
    full: dict = {"meta": {}, "scenarios": {}}
    ts = datetime.now(timezone.utc).isoformat()
    full["meta"] = {"timestamp_utc": ts, "seed_perturb": 20261023,
                    "bootstrap_seed": P5.BOOTSTRAP_SEED,
                    "bootstrap_resamples": int(args.n_boot),
                    "weights": w.name, "weights_sha256": wsha,
                    "conf": float(args.conf), "iou": 0.5,
                    "frozen_sha256": seal["frozen_sha256"], "eps": seal["eps"],
                    "min_samples": seal["min_samples"], "k_raw": seal["k_raw"],
                    "device": yolo.device, "torch": torch.__version__,
                    "gpu": (torch.cuda.get_device_name(0)
                            if torch.cuda.is_available() else "none"),
                    "spec": "F.3 lan22-25 (C1 + F2x2 + F1 + F3x3)",
                    "smoke": bool(args.smoke)}
    verdict = {"pass": True, "notes": []}
    for sc_name, _perturb, _sb in scenarios:
        n_img = len(all_items)
        gts = {i: per_image[(sc_name, "C0_ref", i)]["gt"]
               for i in range(n_img)}
        sc_res: dict = {}
        for vname, cfg, rkw in variants:
            dets = []
            for i in range(n_img):
                dets += per_image[(sc_name, vname, i)]["dets"]
            ap50, _per = ev.voc_ap(dets, gts, thr=0.5)
            tp, fp, npos = ev.pr_counts(dets, gts, thr=0.5)
            prec = tp / (tp + fp) if (tp + fp) else 0.0
            rec = tp / npos if npos else 0.0
            f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
            far_trig = sum(1 for i in range(n_def, n_img)
                           if per_image[(sc_name, vname, i)]["level"] not in (-1, 4))
            far = far_trig / len(good_items) if good_items else 0.0
            lat = [per_image[(sc_name, vname, i)]["lat"] for i in range(n_img)]
            crops = [per_image[(sc_name, vname, i)]["crops"] for i in range(n_img)]
            calls = [per_image[(sc_name, vname, i)]["calls"] for i in range(n_img)]
            n_fb = sum(1 for i in range(n_img)
                       if per_image[(sc_name, vname, i)]["fb"])
            tp_img, frag_tp, frag_n, prox_tp, prox_n = {}, 0, 0, 0, 0
            per_img_dets: dict[int, list] = {}
            for d in dets:
                per_img_dets.setdefault(d[0], []).append(d)
            for i in range(n_def):
                gl = gts[i]
                used, _t, _f = P5._match_per_gt(per_img_dets.get(i, []), gl)
                tp_img[i] = sum(1 for u in used if u)
                bc = blob_info.get((sc_name, i), [])
                for (m, (fm, pm)) in enumerate(zip(P5._frag_mask(gl, bc),
                                                   P5._prox_mask(gl))):
                    if fm:
                        frag_n += 1
                        frag_tp += 1 if used[m] else 0
                    if pm:
                        prox_n += 1
                        prox_tp += 1 if used[m] else 0
            sc_res[vname] = {"AP50": round(ap50, 4), "recall": round(rec, 4),
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
                             "lat_list": lat, "crops_list": crops,
                             "config": cfg, "rescue": dict(rkw)}
        npos_img = {i: len(gts[i]) for i in range(n_def)}
        for vname, cfg, rkw in variants:
            r = sc_res[vname]
            if vname == "C0_ref":
                r.update({"dTP": 0, "dTP_frag": 0, "dTP_prox": 0,
                          "boot_rec_lo": 0.0, "boot_rec_hi": 0.0,
                          "boot_ap_lo": 0.0, "boot_ap_hi": 0.0,
                          "wx_crops_p": 1.0, "wx_lat_p": 1.0})
                continue
            b = sc_res["C0_ref"]
            r["dTP"] = r["tp"] - b["tp"]
            r["dTP_frag"] = r["frag_tp"] - b["frag_tp"]
            r["dTP_prox"] = r["prox_tp"] - b["prox_tp"]
            lo, hi = P5._bootstrap_recall_diff(
                [r["tp_img"][i] for i in range(n_def)],
                [b["tp_img"][i] for i in range(n_def)],
                [npos_img[i] for i in range(n_def)], rng,
                n_boot=int(args.n_boot))
            r["boot_rec_lo"], r["boot_rec_hi"] = round(lo, 4), round(hi, 4)
            if vname == "C1_base" and not args.smoke:
                dets_c = [d for i in range(n_img)
                          for d in per_image[(sc_name, vname, i)]["dets"]]
                dets_b = [d for i in range(n_img)
                          for d in per_image[(sc_name, "C0_ref", i)]["dets"]]
                alo, ahi = P5._bootstrap_ap_diff(dets_c, dets_b, gts, rng,
                                                 n_boot=int(args.n_boot))
                r["boot_ap_lo"], r["boot_ap_hi"] = round(alo, 4), round(ahi, 4)
            else:
                r["boot_ap_lo"], r["boot_ap_hi"] = "", ""
            pc, _nc = P5._wilcoxon_p(r["crops_list"], b["crops_list"])
            pl, _nl = P5._wilcoxon_p(r["lat_list"], b["lat_list"])
            r["wx_crops_p"], r["wx_lat_p"] = pc, pl
            if not args.smoke:
                if not (r["dTP_frag"] > 0 and r["dTP_prox"] > 0):
                    verdict["pass"] = False
                    verdict["notes"].append(
                        f"{sc_name}: {vname} dTP_frag={r['dTP_frag']} "
                        f"dTP_prox={r['dTP_prox']} (can >0 ca 2)")
                if not (lo > 0 or hi < 0):
                    verdict["pass"] = False
                    verdict["notes"].append(
                        f"{sc_name}: {vname} bootCI recall [{lo},{hi}] chua 0")
                if not (pc < 0.01):
                    verdict["pass"] = False
                    verdict["notes"].append(
                        f"{sc_name}: {vname} wilcoxon crops p={pc} (can <0.01)")
                if r.get("lat_p95", 0) > 450:
                    verdict["pass"] = False
                    verdict["notes"].append(
                        f"{sc_name}: {vname} P95={r['lat_p95']}ms >450ms")
        full["scenarios"][sc_name] = sc_res
        for vname, cfg, rkw in variants:
            r = sc_res[vname]
            m = ev.PHASE5_CONFIGS[cfg]
            rows.append({"scenario": sc_name, "variant": vname,
                         "config": cfg,
                         "kind": m["kind"], "mode": m["mode"],
                         "gating": m["gating"],
                         "rescue_conf": rkw.get("rescue_conf"),
                         "rescue_keep_res": bool(rkw.get("rescue_keep_res", False)),
                         "rescue_r": rkw.get("rescue_r"),
                         **{k: r[k] for k in (
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
    slim = {"meta": full["meta"], "scenarios": {}}
    for sc, d in full["scenarios"].items():
        slim["scenarios"][sc] = {c: {k: v for k, v in r.items()
                                     if k not in ("tp_img", "lat_list",
                                                  "crops_list")}
                                 for c, r in d.items()}
    out_json = assert_writable_ok(Path(args.out_json))
    out_json.write_text(json.dumps(slim, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"[replay] wrote {out_csv} ({len(rows)} rows)")
    print(f"[replay] wrote {out_json}")

    print("\n===== PHASE5 F.3 REPLAY (so thuc do GPU) =====")
    for sc_name, _p, _s in scenarios:
        d = full["scenarios"][sc_name]
        b = d["C0_ref"]
        print(f"--- {sc_name} (def={n_def} good={len(good_items)}) --- "
              f"frag_n={b['frag_n']} prox_n={b['prox_n']}")
        for vname, _c, _k in variants:
            r = d[vname]
            print(f"  {vname:8s} AP={r['AP50']:.4f} R={r['recall']:.4f} "
                  f"P={r['precision']:.4f} TP={r['tp']} "
                  f"FP={r['fp']} FAR={r['far']:.3f} "
                  f"lat={r['lat_mean']:.0f}/{r['lat_p95']:.0f}ms "
                  f"crops={r['crops']:.2f} calls={r['calls']:.2f} "
                  f"fb={r['n_fb']} frag={r['frag_tp']}/{r['frag_n']} "
                  f"prox={r['prox_tp']}/{r['prox_n']} dTP={r.get('dTP')} "
                  f"dFrag={r.get('dTP_frag')} dProx={r.get('dTP_prox')} "
                  f"CI=[{r.get('boot_rec_lo')},{r.get('boot_rec_hi')}] "
                  f"wx=({r.get('wx_crops_p')},{r.get('wx_lat_p')})")
    print("VERDICT:", "PASS" if verdict["pass"] else "FAIL", verdict["notes"])


if __name__ == "__main__":
    main()
