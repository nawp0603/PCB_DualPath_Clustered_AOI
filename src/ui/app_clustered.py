"""Ngay 4 (Phase 4) — UI Streamlit demo Hướng 4: DBSCAN Adaptive Clustered ROI.

Toi thieu: 3 khung hinh cho 1 anh Board 05-12 (khong cham Board 01/04):
  (1) Anh dau vao + BBox cum (hop bao cum + det, he anh goc) + bucket/canh.
  (2) Diff heatmap Nhanh B (he anh goc).
  (3) Ket luan 4 muc: FAIL / REWORK / REVIEW / PASS + chi tiet cum/crop/NMS.

Thay Nhánh B "cents -> 8 crop 640 -> 1 batch -> gating don" bang pipeline cum:
  blobs (K_raw frozen) -> DBSCAN(m=1, eps frozen) -> Z_cluster top-8
  -> adaptive crops (mode A1/A2/B qua --crop-mode)
  -> bucket batches (predict_clusters) -> union_of_disks_gate (U, R=80)
  -> cross_crop_nms (IoU=0.50, class-aware) -> fuse_and_decide
  + fallback_single (1 blob) + fallback TOP-8 khi exception cluster.

CPU-only ngoai tru YOLO inject tu ngoai (evaluate_clustered truyen
YOLODetector that). Module nay khong doc Board 01/04 (Zero-defect-training
Boards) — chi Board 05-12, dac biet Board 07.

Chay portable:
  .venv/Scripts/python.exe -m streamlit run src/ui/app_clustered.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from configs.resource_paths import (  # noqa: E402
    LOCAL_TEMPLATES,
    find_pku_root as find_pku_root_portable,
)

import src.reference.board_id_and_align as ba  # noqa: E402
from src.models.ensemble import CONF_BOOST, FUSION_IOU, GATE_R  # noqa: E402
from src.models.yolo_detector import (  # noqa: E402
    DEFAULT_CONF,
    DEFAULT_IMGSZ,
    YOLODetector,
)
from scripts.evaluate_clustered import (  # noqa: E402
    CROP_MODE_A1,
    CROP_MODE_A2,
    CROP_MODE_B,
    CROP_MODES,
    DEFAULT_CROP_MODE,
    CLASS_ID,
    run_clustered_single,
)

# ---------------------------------------------------------------------------
# Hang so UI (khoa voi plan, khong tu y doi nguong quyet dinh o day)
# ---------------------------------------------------------------------------
LEVEL_STYLE: dict[int, tuple[str, str]] = {
    1: ("FAIL", "Mau do — loi nghiem trong (open_circuit / short / missing_hole, S>=0.55). Dung day chuyen, cach ly board."),
    2: ("REWORK", "Mau cam — loi co the sua (mouse_bite / spur / spurious_copper, S>=0.35). Chuyen tram sua chua."),
    3: ("REVIEW", "Mau vang — can ky su xem lai (3a cuu recall Z>=2.0 / 3b open-set Z>=3.5 & Area>=100)."),
    4: ("PASS", "Mau xanh — khong phat hien loi. Cho luu hanh."),
}

CLASS_COLORS_BGR: dict[int, tuple[int, int, int]] = {
    0: (0, 0, 255),        # open_circuit — do
    1: (0, 165, 255),      # short — cam
    2: (0, 255, 255),      # mouse_bite — vang
    3: (255, 0, 255),      # spur — tim
    4: (255, 100, 0),      # spurious_copper — lam
    5: (0, 255, 0),        # missing_hole — xanh la
}

ID2NAME = {v: k for k, v in CLASS_ID.items()}
_ = ID2NAME  # giu mapping lop cho debug console (khong dung trong render)

# 6 anh dai dien 6 lop Board 07 (tu LEGACY_PKU_ROOT, read-only) + 2 GOOD
# (templates 01/04 — chi hien thi, KHONG chay pipeline cum tren 01/04).
SAMPLE_FILES: list[tuple[str, str]] = [
    ("07 / open_circuit61", "open_circuit61.jpg"),
    ("07 / short69", "short69.jpg"),
    ("07 / mouse_bite61", "mouse_bite61.jpg"),
    ("07 / spur61", "spur61.jpg"),
    ("07 / spurious_copper61", "spurious_copper61.jpg"),
    ("07 / missing_hole61", "missing_hole61.jpg"),
    ("GOOD 01 (template)", "01.JPG"),
    ("GOOD 04 (template)", "04.JPG"),
]

# Canh ngan toi thieu de ORB+RANSAC on dinh (anh chuan 1586x3034).
INPUT_MIN_SIDE = 640


# ---------------------------------------------------------------------------
# IO helpers (Unicode-safe, chi doc)
# ---------------------------------------------------------------------------
def read_bgr(path: str | Path) -> np.ndarray:
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(f"Cannot decode image: {path}")
    return img


def read_gray(path: str | Path) -> np.ndarray:
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise FileNotFoundError(f"Cannot decode image: {path}")
    return img


def decode_upload(raw: bytes) -> np.ndarray:
    arr = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Khong doc duoc anh upload (chi nhan JPG/PNG).")
    return img


def validate_input_image(img_bgr: np.ndarray | None) -> str | None:
    """Kiem tra nhanh anh dau vao truoc khi align."""
    if img_bgr is None:
        return ("Chua co anh dau vao (giai ma that bai). "
                "Hay upload file JPG/PNG hoac chon anh mau Board 07.")
    if (not isinstance(img_bgr, np.ndarray) or img_bgr.ndim != 3
            or img_bgr.shape[2] != 3):
        return ("Anh dau vao khong dung dinh dang (can anh mau 3 kenh).")
    h, w = int(img_bgr.shape[0]), int(img_bgr.shape[1])
    if h <= 0 or w <= 0:
        return "Anh rong (kich thuoc = 0). Hay upload lai file JPG/PNG."
    if min(h, w) < INPUT_MIN_SIDE:
        return (f"Anh qua nho ({w}x{h}, canh ngan {min(h, w)}px < "
                f"{INPUT_MIN_SIDE}px). He thong can anh chup gan full-board.")
    return None


def render_rows_as_markdown(rows: list[dict], empty_text: str) -> None:
    """Ve bang chi tiet bang st.markdown (khong dung st.table)."""
    import streamlit as st
    if not rows:
        st.write(empty_text)
        return
    cols = list(rows[0].keys())

    def cell(v):
        if v is None:
            return "-"
        if isinstance(v, bool):
            return str(v)
        if isinstance(v, float):
            return f"{v:.3f}"
        if isinstance(v, list):
            parts = [f"{float(x):.1f}" if isinstance(x, (int, float)) else str(x)
                     for x in v]
            return "[" + ", ".join(parts) + "]"
        return str(v)

    lines = ["| " + " | ".join(cols) + " |",
             "| " + " | ".join(["---"] * len(cols)) + " |"]
    for r in rows:
        lines.append("| " + " | ".join(cell(r.get(c)) for c in cols) + " |")
    st.markdown("\n".join(lines))


def resolve_sample_board07(fname: str, pku_root: Path | None) -> Path:
    """Lay anh Board 07 READ-ONLY tu LEGACY_PKU_ROOT (images/<class>/<fname>)."""
    if fname in ("01.JPG", "04.JPG"):
        return LOCAL_TEMPLATES / fname
    from configs.resource_paths import LEGACY_PKU_ROOT
    roots: list[Path | None] = [pku_root, LEGACY_PKU_ROOT]
    class_dirs = ("Open_circuit", "Short", "Mouse_bite", "Spur",
                  "Spurious_copper", "Missing_hole")
    for root in roots:
        if root is None:
            continue
        for cls_dir in class_dirs:
            try:
                cand = root / "images" / cls_dir / fname
            except Exception:
                continue
            if cand.exists():
                return cand
    raise FileNotFoundError(f"Khong tim thay anh Board 07: {fname}")


def find_pku_root() -> Path | None:
    try:
        return find_pku_root_portable()
    except Exception:
        return None


def find_class_folder(fname: str, pku_root: Path | None) -> str | None:
    from configs.resource_paths import LEGACY_PKU_ROOT
    roots = [pku_root, LEGACY_PKU_ROOT]
    for root in roots:
        if root is None:
            continue
        for cls_dir in ("Open_circuit", "Short", "Mouse_bite", "Spur",
                        "Spurious_copper", "Missing_hole"):
            try:
                if (root / "images" / cls_dir / fname).exists():
                    return cls_dir
            except Exception:
                continue
    return None


# ---------------------------------------------------------------------------
# Pipeline don anh (Hướng 4 — evaluate_clustered::run_clustered_single)
# ---------------------------------------------------------------------------
def get_template_gray(board_id: str,
                      like_shape: tuple[int, int]) -> np.ndarray:
    tpl = read_gray(LOCAL_TEMPLATES / f"{board_id}.JPG")
    if tpl.shape != like_shape:
        tpl = cv2.resize(tpl, (like_shape[1], like_shape[0]),
                         interpolation=cv2.INTER_AREA)
    return tpl


def run_clustered_demo(img_bgr: np.ndarray, yolo: YOLODetector,
                       cache: dict, mode: str = DEFAULT_CROP_MODE,
                       conf: float | None = None,
                       image_id: str = "",
                       rescue_conf: float | None = None,
                       rescue_keep_res: bool = False,
                       rescue_r: float | None = None) -> dict:
    """Chay Hướng 4 tren 1 anh BGR. Tra ve dict day du cho UI ve."""
    return run_clustered_single(img_bgr, yolo, cache, {}, mode=mode,
                                conf=conf, image_id=image_id,
                                rescue_conf=rescue_conf,
                                rescue_keep_res=rescue_keep_res,
                                rescue_r=rescue_r)


# ---------------------------------------------------------------------------
# Ve hinh (BGR uint8 -> RGB uint8 cho st.image)
# ---------------------------------------------------------------------------
def draw_boxes_rgb(img_bgr: np.ndarray, items: list[dict]) -> np.ndarray:
    vis = img_bgr.copy()
    for it in items:
        if it.get("conf") is None or it.get("bbox") is None:
            continue
        x1, y1, x2, y2 = [int(round(v)) for v in it["bbox"]]
        cid = it.get("class_id")
        color = CLASS_COLORS_BGR.get(int(cid) if cid is not None else -1,
                                     (200, 200, 200))
        cv2.rectangle(vis, (x1, y1), (x2, y2), color, 3)
        label = f"{it.get('class_name', '?')} {float(it['conf']):.2f} [{it.get('source', '?')}]"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2)
        y1t = max(0, y1 - th - 10)
        cv2.rectangle(vis, (x1, y1t), (x1 + tw + 8, y1t + th + 10), color, -1)
        cv2.putText(vis, label, (x1 + 4, y1t + th + 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2, cv2.LINE_AA)
    return cv2.cvtColor(vis, cv2.COLOR_BGR2RGB)


def draw_cluster_boxes_rgb(img_bgr: np.ndarray,
                           crops_geo: list[dict]) -> np.ndarray:
    """Ve hop bao cum (box_input) + canh/bucket de demo Hướng 4."""
    vis = img_bgr.copy()
    for c in crops_geo:
        bx = [float(v) for v in c.get("box_input", c.get("bbox", [0, 0, 0, 0]))]
        x1, y1, x2, y2 = [int(round(v)) for v in bx]
        cid = int(c.get("cluster_id", -1))
        color = CLASS_COLORS_BGR.get(cid % 6 if cid >= 0 else 6, (200, 200, 200))
        h_img, w_img = vis.shape[:2]
        x1c, y1c = max(0, x1), max(0, y1)
        x2c, y2c = min(w_img, x2), min(h_img, y2)
        if x2c > x1c and y2c > y1c:
            cv2.rectangle(vis, (x1c, y1c), (x2c, y2c), color, 3)
        label = f"C{cid} side={int(round(float(c.get('side', 0))))} " \
                f"bucket={c.get('bucket', '?')} z={float(c.get('z_cluster', 0)):.1f}"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
        y1t = max(0, y1c - th - 10)
        cv2.rectangle(vis, (x1c, y1t), (x1c + tw + 8, y1t + th + 10), color, -1)
        cv2.putText(vis, label, (x1c + 4, y1t + th + 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 2, cv2.LINE_AA)
    return cv2.cvtColor(vis, cv2.COLOR_BGR2RGB)


def mark_centroids_rgb(img_rgb: np.ndarray, centroids: list[tuple[float, float]],
                       img_shape: tuple[int, int]) -> np.ndarray:
    vis = img_rgb.copy()
    h, w = img_shape[:2]
    for (px, py) in centroids:
        x, y = int(round(px)), int(round(py))
        if 0 <= x < w and 0 <= y < h:
            cv2.circle(vis, (x, y), 12, (255, 255, 255), 2, cv2.LINE_AA)
            cv2.circle(vis, (x, y), 3, (255, 255, 255), -1, cv2.LINE_AA)
    return vis


def diff_heatmap_input_space(diff_tpl: np.ndarray, h_inv: np.ndarray,
                             img_shape: tuple[int, int]) -> np.ndarray:
    """Chieu diff_map (he Template) ve he anh goc roi JET."""
    h_img, w_img = int(img_shape[0]), int(img_shape[1])
    try:
        diff_in = cv2.warpPerspective(
            np.asarray(diff_tpl, dtype=np.float32), np.asarray(h_inv),
            (w_img, h_img), flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    except cv2.error:
        diff_in = np.zeros((h_img, w_img), np.float32)
    mx = float(np.max(diff_in)) if diff_in.size else 0.0
    if not np.isfinite(mx) or mx <= 1e-9:
        norm = np.zeros_like(diff_in, dtype=np.uint8)
    else:
        norm = np.clip(diff_in / mx * 255.0, 0, 255).astype(np.uint8)
    heat_bgr = cv2.applyColorMap(norm, cv2.COLORMAP_JET)
    return cv2.cvtColor(heat_bgr, cv2.COLOR_BGR2RGB)


def overlay_heatmap(img_bgr: np.ndarray, heat_rgb: np.ndarray,
                    alpha: float = 0.45) -> np.ndarray:
    base = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB).astype(np.float32)
    heat = np.asarray(heat_rgb, dtype=np.float32)
    if heat.shape[:2] != base.shape[:2]:
        heat = cv2.resize(heat, (base.shape[1], base.shape[0]),
                          interpolation=cv2.INTER_LINEAR)
    out = base * (1.0 - alpha) + heat * alpha
    return np.clip(out, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------------------
# Streamlit app
# ---------------------------------------------------------------------------
def main() -> None:
    import streamlit as st

    st.set_page_config(page_title="Dual-Path Clustered AOI — Hướng 4 (Demo)",
                       page_icon="🔍", layout="wide")
    st.title("🔍 Dual-Path Clustered AOI — Hướng 4 (Demo)")
    st.caption(
        "Nhánh A (YOLO11n Full-Res, rect=True + half=True) + Nhánh B "
        "(Golden-Template Safety-Net: ORB Homography + Tolerance-Aware Diff, "
        "DBSCAN(m=1, eps frozen) gom cum blob he Template, top-8 theo "
        "Z_cluster, adaptive crop A1/A2/B, Union-of-Disks gating R=80 + "
        "Cross-Crop NMS IoU=0.50, Box Fusion, quyet dinh 4 muc). "
        "Board 01/04 la Zero-defect-training Boards — khong chay pipeline "
        "tren chung o demo (chi hien thi template)."
    )

    # -- Sidebar: cau hinh ---------------------------------------------------
    with st.sidebar:
        st.header("⚙️ Cau hinh")
        from configs.resource_paths import (LOCAL_BASE_PT, LOCAL_DAY2_PT)
        _opts: dict[str, Path] = {
            "yolo11n_day2.pt (fine-tune)": LOCAL_DAY2_PT,
            "yolo11n_base.pt (goc 50 epoch)": LOCAL_BASE_PT,
        }
        _opts = {k: v for k, v in _opts.items() if v.exists()}
        if not _opts:
            _opts = {"yolo11n_base.pt (goc 50 epoch)": LOCAL_BASE_PT}
        wlabel = st.selectbox("Trong so YOLO", list(_opts.keys()), index=0)
        wpath = _opts[wlabel]
        if not wpath.exists():
            st.warning(f"Khong thay {wpath.name}, dung ban con lai.")
            wpath = LOCAL_BASE_PT if wpath != LOCAL_BASE_PT else LOCAL_DAY2_PT
        conf = st.slider("conf YOLO", 0.05, 0.60, float(DEFAULT_CONF), 0.05)
        device_opt = st.selectbox("Device", ["auto", "cuda", "cpu"], index=0)
        imgsz_opt = st.selectbox("imgsz Nhanh A",
                                 ["Auto (3072, tu ha khi yeu)",
                                  "3072 (full-res chuan)",
                                  "2560 (VRAM 4-6GB)",
                                  "1920 (CPU / VRAM <4GB)"],
                                 index=0)
        imgsz_map = {"Auto (3072, tu ha khi yeu)": None,
                     "3072 (full-res chuan)": 3072,
                     "2560 (VRAM 4-6GB)": 2560,
                     "1920 (CPU / VRAM <4GB)": 1920}
        imgsz_req = imgsz_map[imgsz_opt]
        mode_opt = st.selectbox("Che do crop Nhánh B",
                                [CROP_MODE_A1, CROP_MODE_A2, CROP_MODE_B],
                                index=list(CROP_MODES).index(DEFAULT_CROP_MODE))
        fast = st.checkbox("Align fast (~45ms)", value=True)
        st.divider()
        st.caption(f"YOLO imgsz={DEFAULT_IMGSZ}, rect=True + half(cuda). "
                   f"Fusion IoU>={FUSION_IOU}, boost +{CONF_BOOST}, gate R={GATE_R:.0f}px.")
        st.caption(f"Frozen: eps=50.0, min_samples=1, K_raw=8 (calib_report).")

    # -- Nguon anh: upload hoac mau that Board 07 ---------------------------
    pku_root = find_pku_root()
    left, right = st.columns([1, 1])
    with left:
        up = st.file_uploader("📤 Upload anh mach (JPG/PNG)", type=["jpg", "jpeg", "png"])
    with right:
        sample_label = st.selectbox("📁 hoac chon anh mau Board 07",
                                    [s[0] for s in SAMPLE_FILES], index=0)

    img_bgr: np.ndarray | None = None
    src_name = ""
    is_good_template = False
    if up is not None:
        try:
            img_bgr = decode_upload(up.read())
            src_name = up.name
        except Exception as e:
            st.error(f"Khong doc duoc anh upload: {e}")
            return
    else:
        rel = dict(SAMPLE_FILES)[sample_label]
        try:
            if rel in ("01.JPG", "04.JPG"):
                img_bgr = read_bgr(resolve_sample_board07(rel, pku_root))
                is_good_template = True
                src_name = rel
            else:
                img_bgr = read_bgr(resolve_sample_board07(rel, pku_root))
                cls = find_class_folder(rel, pku_root)
                src_name = f"{rel} ({cls or '?'}, Board 07)"
        except Exception as e:
            st.error(f"Khong tai duoc anh mau: {e}")
            return

    run = st.button("▶️ Chay Hướng 4 (Clustered)", type="primary",
                    use_container_width=True)

    # -- Tai tai nguon nang 1 lan --------------------------------------------
    @st.cache_resource(show_spinner="Dang tai ORB cache + YOLO (1 lan)...")
    def _load_resources(wpath_s: str, device_s: str, conf_s: float,
                        imgsz_s: str):
        cache = ba.load_cache(LOCAL_TEMPLATES / "orb_cache.npz")
        import torch
        dev = device_s
        if dev == "auto":
            dev = "cuda" if torch.cuda.is_available() else "cpu"
        req = {"auto": None, "3072": 3072, "2560": 2560,
               "1920": 1920}.get(imgsz_s, None)
        yolo = YOLODetector(Path(wpath_s), conf_threshold=float(conf_s),
                            imgsz=req if req else int(DEFAULT_IMGSZ),
                            device=dev)
        if req is not None and yolo.imgsz != req:
            pass
        return cache, yolo, dev

    try:
        cache, yolo, device_used = _load_resources(
            str(wpath), str(device_opt), float(conf),
            {"auto": "auto", "3072": "3072", "2560": "2560",
             "1920": "1920"}[imgsz_opt])
    except Exception as e:
        st.error(f"Khong tai duoc model/cache: {type(e).__name__}: {e}")
        return
    st.caption(f"Nguon: **{src_name}** ({img_bgr.shape[1]}x{img_bgr.shape[0]}) · "
               f"weights=`{Path(wpath).name}` · device=`{device_used}` · "
               f"imgsz thuc te=`{yolo.imgsz}` · conf={float(conf):.2f} · "
               f"align={'fast' if fast else 'full'} · mode={mode_opt}")
    if getattr(yolo, "adapt_note", ""):
        st.caption(f"🖥️ {yolo.adapt_note}")

    if not run:
        st.info("Nhan **▶️ Chay Hướng 4 (Clustered)** de suy luan. "
                "Coder chi smoke-test 1-2 anh; eval full la viec cua Verifier.")
        return

    # -- Guard anh dau vao ---------------------------------------------------
    bad = validate_input_image(img_bgr)
    if bad is not None:
        st.error(bad)
        st.info("Goi y: chon 1 trong 6 anh mau Board 07 (du 6 lop).")
        return

    # GOOD 01/04 chi hien thi (Zero-defect-training Boards — KHONG chay pipeline).
    if is_good_template:
        st.info("GOOD template 01/04 chi hien thi (Zero-defect-training Boards — "
                "KHONG chay pipeline cum o demo). Hay chon 1 trong 6 anh Board 07.")
        st.image(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB),
                 use_container_width=True,
                 caption=f"{src_name} — GOOD template (display-only, khong chay pipeline).")
        return

    # -- Suy luan ------------------------------------------------------------
    with st.spinner("Dang chay Hướng 4 (align → diff → DBSCAN cum → adaptive crop → "
                    "gating → NMS → fusion → 4 muc)..."):
        try:
            out = run_clustered_demo(img_bgr, yolo, cache, mode=mode_opt,
                                     conf=float(conf),
                                     image_id=Path(src_name).stem)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            st.error(f"Pipeline loi: {msg}")
            return

    res = out["result"]
    items = res.get("items", [])
    level = int(res.get("level", 4))
    lname, ldesc = LEVEL_STYLE.get(level, ("?", ""))
    banner = {1: "error", 2: "warning", 3: "info", 4: "success"}.get(level, "info")

    # -- 3 khung hinh --------------------------------------------------------
    c1, c2, c3 = st.columns(3)
    with c1:
        st.subheader("(1) Anh + BBox cum + det (da fuse)")
        # Hop nhat 2 lop: cum o duoi, det o tren (ve cluster truoc roi det).
        cluster_rgb = draw_cluster_boxes_rgb(img_bgr, out.get("crops_geo", []))
        cluster_bgr = cv2.cvtColor(cluster_rgb, cv2.COLOR_RGB2BGR)
        combined_rgb = draw_boxes_rgb(cluster_bgr, items)
        boxed = mark_centroids_rgb(combined_rgb, out.get("centroids_input", []),
                                   img_bgr.shape)
        st.image(boxed, use_container_width=True,
                 caption=f"{len([i for i in items if i.get('conf') is not None])} det co conf · "
                         f"{len(out.get('crops_geo', []))} cum (AUDIT-03 ≤8) · "
                         f"tam blob = vong tron trang, R={GATE_R:.0f}px")
    with c2:
        st.subheader("(2) Diff heatmap Nhanh B")
        heat = diff_heatmap_input_space(out["diff_map"], out["H_inv"],
                                        img_bgr.shape)
        ov = overlay_heatmap(img_bgr, heat)
        ov = mark_centroids_rgb(ov, out.get("centroids_input", []), img_bgr.shape)
        st.image(ov, use_container_width=True,
                 caption=f"{len(out.get('blobs', []))} blob (Area>=60, K_raw frozen=8, hien co {len(out.get('blobs', []))})")
    with c3:
        st.subheader("(3) Ket luan 4 muc")
        msg = f"### {lname} (level {level})\n{ldesc}"
        getattr(st, banner)(msg)
        st.write(f"**Board**: `{out['board_id']}` (inliers={out['inliers']}, "
                 f"{out.get('mode', '?')})")
        st.write(f"**Latency**: align {out['t_align_ms']:.0f}ms · "
                 f"diff {out['t_diff_ms']:.0f}ms · cum/crop {out.get('t_cluster_ms', 0):.0f}ms · "
                 f"batch {out.get('t_roi_ms', 0):.0f}ms · "
                 f"**tong {out.get('t_total_ms', 0):.0f}ms**")
        st.write(f"**Cum**: {out.get('n_crops', 0)} crop · "
                 f"GPU-calls/anh={out.get('batch_stats', {}).get('n_calls', 0)} · "
                 f"fallback_single={out.get('fallback_single', False)} · "
                 f"fallback_reason={out.get('fallback_reason') or '—'}")
        for line in res.get("reasons", [])[:6]:
            st.write(f"- {line}")

    # -- Chi tiet cum/crop/NMS (cho hoi dong) -------------------------------
    with st.expander("🔬 Chi tiet Hướng 4 (cac cum: Z_cluster, side, bucket)"):
        rows = []
        for c in out.get("crops_geo", []):
            rows.append({
                "cluster_id": c.get("cluster_id", -1),
                "size": int(c.get("size", 0)),
                "z_cluster": round(float(c.get("z_cluster", 0.0)), 2),
                "side": round(float(c.get("side", 0.0)), 1),
                "origin": f"({int(c.get('origin', (0,0))[0])}, {int(c.get('origin', (0,0))[1])})",
                "bucket": c.get("bucket", "?"),
                "truncated": bool(c.get("truncated", False)),
                "bisect_depth": int(c.get("bisect_depth", 0)),
            })
        render_rows_as_markdown(rows, "Khong co cum (anh khong co blob hoac fallback TOP-8).")
        if out.get("dropped_log"):
            st.caption("Cum bi loai (AUDIT-03, ranked 9+ theo Z_cluster):")
            render_rows_as_markdown([{
                "cluster_id": d.get("cluster_id", -1),
                "z_cluster": round(float(d.get("z_cluster", 0.0)), 2),
                "reason": d.get("reason", "dropped_lowz"),
            } for d in out["dropped_log"]], "Khong cum nao bi loai.")
        st.caption(f"Det truoc gating={out.get('n_dets_before_gating', 0)} · "
                   f"sau gating={out.get('n_dets_after_gating', 0)} · "
                   f"sau NMS={out.get('n_dets_after_nms', 0)} (AUDIT-05: gating→NMS→fuse).")

    # -- Chi tiet det/fusion -------------------------------------------------
    with st.expander("🧩 Chi tiet evidence item (fusion + quyet dinh)"):
        rows = []
        for it in items:
            rows.append({
                "class_id": it.get("class_id"),
                "class_name": it.get("class_name"),
                "conf": round(float(it["conf"]), 3) if it.get("conf") is not None else None,
                "source": it.get("source", "?"),
                "cluster_id": it.get("cluster_id", -1),
                "z_diff": round(float(it["z_diff"]), 2) if it.get("z_diff") is not None else None,
                "area": round(float(it["area"]), 1) if it.get("area") is not None else None,
                "bbox": [round(float(v), 1) for v in it["bbox"]] if it.get("bbox") else None,
            })
        render_rows_as_markdown(rows, "Khong co evidence item.")

    st.divider()
    st.caption("Hướng 4 — Coder chi smoke-test 1-2 anh CPU-nhẹ; toàn bộ số liệu "
               "chốt bên Verifier (Phase 5). Không chạm Board 01/04 (Zero-defect-training Boards).")


if __name__ == "__main__":
    main()
