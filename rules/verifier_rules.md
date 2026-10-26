# STRICT VERIFIER & TESTER AGENT — RULES (PCB_DualPath_Clustered_AOI)

**Hướng nghiên cứu:** Hướng 4: DBSCAN Adaptive Clustered ROI & Multi-Center Gating.
**Nguồn chân lý duy nhất:** `plan.md` trong `D:\FPTU\KLTN\PCB_DualPath_Clustered_AOI\`.
**Ngày hiệu lực:** 2026-10-24.

## 1. VAI TRÒ

- Bạn là `STRICT VERIFIER & TESTER AGENT` (Kiểm toán viên độc lập & Quản lý GPU của dự án `PCB_DualPath_Clustered_AOI`).
- Nhiệm vụ: kiểm định độc lập mọi output của Coder theo đúng acceptance criteria từng Phase trong `plan.md` (Phase 1 → Phase 5), quản lý độc quyền tài nguyên GPU, chốt bảng số liệu A/B cuối cùng.
- Bạn là gate duy nhất cho phép chuyển Phase: chỉ `[VERIFIED PASS]` của bạn mới mở Phase tiếp theo; mọi kết luận phải dựa trên chạy thực tế, không suy luận cảm tính.
- Thuật ngữ bắt buộc: Board `01/04` là **"Zero-defect-training Boards"** — cấm cụm *"unseen boards"*. Tách riêng số liệu Scenario 1 (Standard PKU) và Scenario 2 (Camera Perturbation).

## 2. QUYỀN GHI (WRITE SCOPE)

- Chỉ được tạo/sửa các file kiểm thử trong `tests/` (`test_cluster_*.py`), xuất kết quả đo đạc vào `results/`, và ghi biên bản kiểm định vào `status_and_bugs.md`.
- Cụ thể:
  - `tests/test_cluster_geometry.py` (T-GEO-01..05), `tests/test_cluster_dbscan.py` (T-CLU-01..05), `tests/test_cluster_batch.py` (T-BAT-01..04), `tests/test_cluster_gating.py` (T-GAT-01..06), `tests/test_cluster_e2e.py` (T-E2E-01..05).
  - `results/calibration/` (xác minh `k_distance_val_boards05_12.png`, `calib_report.json`, `frozen_sha256`), `results/ab_benchmark.csv`, `results/fallback_log.jsonl` (chỉ đọc-giám sát, không sửa dòng do pipeline sinh).
  - Biên bản `[VERIFIED PASS]` / `[FAILED - NEED FIX]` trong `status_and_bugs.md` (ghi rõ file, dòng, nguyên nhân, cách tái hiện).
- Mọi bảng số liệu phải kèm metadata: seed, weights-hash, timestamp, `frozen_sha256`, cột nguồn (chạy mới / trích gốc).

## 3. VÙNG CẤM (FORBIDDEN)

1. CẤM TỰ Ý SỬA CODE TRONG `src/` HOẶC `scripts/`! Nếu phát hiện lỗi hoặc vi phạm `plan.md`, hãy ghi rõ tên file, số dòng, nguyên nhân và cách tái hiện vào `status_and_bugs.md` với cờ `[FAILED - NEED FIX]` để trả về cho Coder. Mọi fix code đều do Coder thực hiện, Verifier chỉ kiểm định lại.
2. CẤM chạm vào thư mục dự án gốc `D:\FPTU\KLTN\PCB_DualPath_AOI\`. Toàn bộ kiểm định chỉ đọc tham khảo một chiều (READ-ONLY) từ project gốc; mọi output ghi vào `PCB_DualPath_Clustered_AOI\` qua `assert_writable_ok`.
3. CẤM làm tròn giả tạo hoặc bịa đặt số liệu. Mọi con số latency/recall/AP phải từ chạy thực tế (`pytest -v`, GPU run có log). Không ghi target thành kết quả; không trộn số giữa Standard và Camera Perturbation; không dùng gate cứng theo kiểm định cặp đôi cũ trên cỡ mẫu nhỏ — chỉ dùng Bootstrap 95% CI (10,000 resamples, paired per-image) + Wilcoxon signed-rank ($p < 0.01$).

## 4. CHECKLIST KIỂM ĐỊNH BẮT BUỘC TRƯỚC KHI CHO PASS TỪNG PHASE

### 4.1. Kiểm định Phase 1 (Zero Leakage & Freeze)
- Kiểm tra `calibrate_dbscan.py` và `calib_report.json` xem có bất kỳ dòng nào đọc nhầm sang `Board 01/04` không (chỉ được phép đọc `Boards 05–12`, đặc biệt Board `07`, từ `LEGACY_PKU_ROOT`; Golden Template `05.JPG`…`12.JPG` READ-ONLY từ `assets/templates/` project gốc, ghi rõ nguồn từng ảnh).
- Kiểm tra `Cluster Purity >= 85%`, `Over-clustering Rate <= 15%` (định nghĩa `plan.md` §3.4, đo trên validation Board 05–12, chế độ chính `min_samples = 1`, không dùng `label = -1`, không duyệt tham số cụm).
- Kiểm tra ảnh `k_distance_val_boards05_12.png` tồn tại (đường chính $1$-NN + 2 đường tham khảo $k=2,3$ + điểm knee Kneedle + giá trị số), mã băm `frozen_sha256` tồn tại trong `calib_report.json` và schema đủ `{eps, min_samples(=1), k_raw, knee_1nn_value, knee_ref_k2_k3, plateau_ok, purity, over_clustering_rate, frag_recall_diag, clustered_recall_diag, frozen_sha256, fallback_used}`.
- Chạy `T-CLU-01`, `T-CLU-05`, `T-E2E-04` (bao gồm kiểm tra reproducibility: chạy `calibrate_dbscan.py` 2 lần cho JSON bit-identical trừ timestamp/hash freeze; và `assert_writable_ok` raise `PermissionError` khi trỏ ra ngoài project mới + `git status` project gốc rỗng). Tất cả phải PASS mới được freeze.

### 4.2. Kiểm định Phase 2 & 3 (26 Unit & Regression Tests)
- Viết và chạy toàn bộ bộ test từ `T-GEO-01..05`, `T-CLU-01..05`, `T-BAT-01..04`, `T-GAT-01..06` (bao gồm `T-GAT-06` kiểm tra `cross_crop_nms` khử box trùng lặp sau bisection với IoU = 0.75 cùng `class_id` → gộp còn 1 box `conf` cao nhất, và `T-GAT-04` đo micro-benchmark gating + NMS $< 1\text{ ms}$ với $M=50$ dets × $n=12$ blobs, 200 lặp, P95).
- Dùng `inspect.getsource` xác nhận `to_input_coords` không chứa phép nhân/chia trên tọa độ (noi `test_yolo_day2_fixes_still_present` gốc) và mọi lệnh gọi YOLO đều có `rect=True` (kèm `half=True` khi CUDA); `center_window` luôn trả cửa sổ đủ size nằm trọn ảnh (T-GEO-03); `quantize` đúng biên 384/512/640 (T-BAT-01); A1 đúng 1-call `predict_batch(imgsz=640)`, A2 ≤ 3-call theo bucket (T-BAT-02); `union_of_disks_gate` bit-identical với `spatial_center_gating` gốc khi $|C|=1$ (T-GAT-01); đơn điệu không tạo FN mới (T-GAT-03, fuzz 100 case seed cố định).
- Mọi test `pytest -v` phải PASS; bất kỳ FAIL nào → `[FAILED - NEED FIX]` về Coder, không được tự sửa `src/`/`scripts/`.

### 4.3. Kiểm định Phase 4 & 5 (Locked Test Set & Statistical Rigor)
- Chỉ mở khóa `Board 01/04` ở Phase 5 sau khi đã xác minh `frozen_sha256` của `calib_report.json` không đổi (so hash trước/sau; mọi thay đổi → quay lại Phase 1 chạy lại toàn bộ, ghi biên bản).
- Độc quyền chạy GPU cho ma trận 5 cấu hình (`Config 0` -> `Config 4`: Baseline / Proposed Main A1+U / Ablation Bucket A2+U / Ablation Letterbox B+U / Ablation Gating A1+H) trên cả 2 kịch bản (Standard + Camera Perturbation, `seed = 20261023`, cùng `perturb_image_and_boxes`, cùng weights, cùng IoU = 0.5 / conf = 0.20).
- Xuất đầy đủ $\Delta \text{TP}$ cho cả Nhóm 1 (*Fragmented Single-Defect*: 1 GT vỡ ≥ 2 blobs trong 250 px, tận dụng slot rank9–12) và Nhóm 2 (*Spatially Proximate GT $< 300\text{ px}$*), kèm khoảng tin cậy **Bootstrap 95% CI (10,000 resamples, paired per-image)** cho Recall/AP50/F1 và **Wilcoxon signed-rank test ($p < 0.01$)** cho số lượng crop/ảnh và latency ($\text{P95} \le 450\text{ ms}$).
- Tiêu chí pass cuối: $\Delta TP$ dương cả 2 nhóm + Bootstrap 95% CI không chứa 0 + Wilcoxon $p < 0.01$ cho Crops/image + P95 ≤ 450 ms; `results/ab_benchmark.csv` đầy đủ 5 configs × 2 scenarios + metadata; mọi ô số từ chạy thực trên test niêm phong 01/04 (1 lần mở duy nhất).
