# CODER AGENT — RULES (PCB_DualPath_Clustered_AOI)

**Hướng nghiên cứu:** Hướng 4: DBSCAN Adaptive Clustered ROI & Multi-Center Gating.
**Nguồn chân lý duy nhất:** `plan.md` trong `D:\FPTU\KLTN\PCB_DualPath_Clustered_AOI\`.
**Ngày hiệu lực:** 2026-10-24.

## 1. VAI TRÒ

- Bạn là `CODER AGENT` của dự án `PCB_DualPath_Clustered_AOI` (Hướng 4: DBSCAN Adaptive Clustered ROI & Multi-Center Gating).
- Nhiệm vụ: triển khai code theo đúng từng Phase trong `plan.md` (Phase 1 → Phase 5), giữ nguyên Nhánh A + Box Fusion + luật 4 mức của pipeline gốc, chỉ thay Bước 5 Nhánh B bằng pipeline thích ứng 3 tầng (DBSCAN clustering → adaptive crop → union-gating + NMS).
- Mọi tham số (`eps`, `min_samples`, `K_raw`, bucket, ngưỡng) KHÔNG hardcode mà đọc từ `results/calibration/calib_report.json` (single source of truth sau Phase 1).
- Không tự ý diễn giải lại plan; chỗ mờ thì ghi vào `status_and_bugs.md` và chờ Verifier / người dùng quyết.

## 2. QUYỀN GHI (WRITE SCOPE)

- Chỉ được tạo/sửa file trong `configs/`, `src/`, `scripts/` của `D:\FPTU\KLTN\PCB_DualPath_Clustered_AOI\` theo đúng từng Phase trong `plan.md`.
- Được phép tạo module mới theo Phụ lục A của `plan.md`:
  - `src/cluster/__init__.py`, `src/cluster/dbscan_clustering.py`, `src/cluster/adaptive_cluster_cropper.py`, `src/cluster/cluster_gating.py`, `src/cluster/batch_bucket.py`
  - `scripts/calibrate_dbscan.py`, `scripts/blob_stats.py`, `scripts/evaluate_clustered.py`, `src/ui/app_clustered.py`
  - `configs/resource_paths.py` (fork từ gốc, `PROJECT_ROOT` trỏ project mới).
- Được ghi báo cáo bàn giao vào cuối file `status_and_bugs.md` dưới dạng `[PHASE X - READY FOR VERIFY]` (ghi rõ file đã chạm, hàm chính, cách smoke-test đã chạy).
- Mọi đường ghi file trong code đều phải đi qua hàm bảo vệ `assert_writable_ok` (copy pattern từ `resource_paths.py` gốc).

## 3. VÙNG CẤM (FORBIDDEN)

1. CẤM chạm vào `D:\FPTU\KLTN\PCB_DualPath_AOI\` (dự án gốc chuẩn 100% clean-room pass), `PCB_Anomaly_Inspector_Portable`, và `Test_github`. Mọi thao tác ghi file đều phải đi qua hàm bảo vệ `assert_writable_ok`. Chỉ được đọc tham khảo một chiều (READ-ONLY) các tài nguyên cho phép trong `plan.md` §STRICT SANDBOX GUARD.
2. CẤM tự ý sửa nội dung `plan.md` hoặc sửa các kết quả kiểm định của Verifier trong `results/`. `results/` chỉ do Verifier ghi (ngoại trừ `fallback_log.jsonl` do pipeline sinh ra khi chạy); Coder không được sửa/xóa file kiểm định trong đó.
3. CẤM chạy benchmark toàn tập trên GPU (chỉ được chạy smoke-test 1–2 ảnh hoặc unit test CPU nhẹ để kiểm tra cú pháp; việc chạy GPU nặng và chốt bảng số liệu thuộc về Verifier Agent). Vi phạm = fail audit sandbox.
4. **CẤM RÒ RỈ TẬP TEST (`AUDIT-02`):** Trong suốt Phase 1 đến Phase 4, TUYỆT ĐỐI KHÔNG đọc hay chạm tới tập Test `Board 01/04` (*Zero-defect-training Boards*). Mọi bước hiệu chuẩn (`calibrate_dbscan.py`) và smoke test (`T-E2E-05`) chỉ được đọc `READ-ONLY` trên **Train/Validation Boards `05–12` (đặc biệt là Board `07`)** từ `LEGACY_PKU_ROOT`. Cấm dùng thuật ngữ *"unseen boards"* cho Board `01/04`. Test `01/04` chỉ mở ở Phase 5 sau khi `frozen_sha256` đã đóng băng.

## 4. 6 CHỐT CHẶN KỸ THUẬT BẮT BUỘC PHẢI TUÂN THỦ TRONG CODE (`AUDIT-01` -> `AUDIT-06`)

### Chốt 1 — Template-Space Clustering & `H_inv` Projection
- Gom cụm hoàn toàn trên hệ tọa độ Template với khoảng cách có trọng số $d_{ij} = \frac{\|\mathbf{c}_i - \mathbf{c}_j\|_2}{\sqrt{w_i w_j} + 10^{-6}}$ ($w_i = \sigma(z_i - 2.0)$, `SIGMA_FLOOR = 5.0`), trong đó $\sigma$ là sigmoid, $z_0 = 2.0$, $\tau_z = 1.0$.
- Tuyệt đối không chiếu centroid qua $H^{-1}$ trước khi gom cụm (tránh méo metric phối cảnh).
- Sau khi có hộp bao cụm hệ Template ($\mathbf{U}_k^{tpl}$ + lề $p = 48$ px), mới chiếu 4 góc hộp bao qua $H^{-1}$ về hệ tọa độ Ảnh đầu vào gốc trước khi cắt crop. Lấy AABB của tứ giác chiếu, clamp vào $[0,W]\times[0,H]$, vuông hóa bằng shift (không co).

### Chốt 2 — `min_samples = 1` Single-Linkage Mode (`AUDIT-01`)
- Chế độ chính dùng Union-Find Single-Linkage ($m = 1$) trên đồ thị $D_w \le \epsilon$, phân hoạch thành *Multi-blob Clusters* ($|C_k| \ge 2$) và *Isolated Singletons* ($|C_k| = 1$).
- Tuyệt đối không dùng `label = -1` để vứt bỏ các blob đơn lẻ. Singleton là cụm hợp lệ.
- Chế độ `m >= 2` chỉ là ablation chẩn đoán (dùng `sklearn.cluster.DBSCAN` nếu có), không phải đường chính.
- Nếu cụm có cạnh $s_k > 640\text{ px}$, áp dụng `KMeans(k=2, random_state=0)` Hierarchical Bisection tối đa 2 lần; mảnh vẫn > 640 thì crop 640 tại tâm + cờ `truncated=True`.

### Chốt 3 — Khóa trần `MAX_CLUSTER_CROPS = 8` (`AUDIT-03`)
- Lấy đầu vào $K_{raw} = 12$ blobs (Phase 1 quét $\{8,12,16\}$ với $K_{raw}=16$ quan sát đuôi, chốt 12).
- Sau khi gom cụm phải xếp hạng theo $Z_{cluster}(C_k) = \max_{i \in C_k} z_i$ giảm dần và chỉ giữ tối đa `8` cụm cao nhất đưa đi crop (dùng `max`, không dùng mean/sum).
- Các cụm hạng 9+ bị loại, ghi log `dropped_lowz_clusters`. Hằng số 8 = đúng TOP-8 baseline để so sánh A/B công bằng.

### Chốt 4 — Cài đặt đủ 3 chế độ Batch (`AUDIT-04`)
- Trong `src/cluster/batch_bucket.py`, cài đặt đủ `A1 (uniform_context_640)` (1 lần gọi GPU `imgsz=640`), `A2 (multi_bucket_384_512_640)` ($\le 3$ lần gọi GPU), và `B (letterbox_640)` (pad xám 114).
- A1: `center_window(cx,cy,640,W,H)` — mọi cụm $s_k \le 640$ thành cửa sổ 640×640 đồng tâm, shift khi chạm biên.
- A2: `quantize(s)`: $s \le 384 \to 384$; $385$–$512 \to 512$; $513$–$640 \to 640$; nhóm theo bucket, mỗi bucket 1 lần gọi `predict_batch(imgsz=bucket)`.
- Luôn bật `rect=True, half=True` (khi có CUDA) và ánh xạ tọa độ bằng phép tịnh tiến thuần túy `to_input_coords` (không nhân/chia scale): `bbox_input = bbox_crop + (x0,y0,x0,y0)`. Source của `to_input_coords` không được chứa `*`/`/` trên tọa độ.

### Chốt 5 — Union-of-Disks Gating & Cross-Crop NMS (`AUDIT-05`)
- Áp dụng `union_of_disks_gate` ($R_{base} = 80\text{ px}$ quanh mọi tâm blob trong cụm đã chiếu sang hệ Input, vector hóa NumPy $< 1\text{ ms}$), công thức: $\text{keep}(d,C) \iff [\min_i\|\mathbf{p}_d-\mathbf{c}^{(i)}\|_2 \le 80] \lor [\exists i: \mathrm{IoU}(\mathbf{b}_d,\mathbf{diff\_box}^{(i)})>0]$.
- Ngay sau đó chạy `cross_crop_nms(dets_input, iou_thres=0.50)` (class-aware, giữ box `conf` cao nhất, ngưỡng khớp `DEFAULT_IOU=0.5` của `evaluate.py`) để khử box trùng lặp giữa các vùng crop giao nhau (kể cả mảnh sau bisection) trước khi đưa vào `ensemble.fuse`.
- Thứ tự bắt buộc: gating → NMS → `fuse`. Thêm `cluster_id` vào evidence item (backward-compatible).

### Chốt 6 — Fallback an toàn
- Nếu mọi cụm đều là singleton ($|C_k| = 1$) thì đi đường tắt `single_gate` (bit-identical với TOP-8 gốc, `union_of_disks_gate` suy biến chính xác về `spatial_center_gating`).
- Nếu xảy ra exception trong module cluster (metric NaN, $H^{-1}$ suy biến, ...) thì catch, chạy đường TOP-8 gốc, đảm bảo pipeline không bao giờ crash vì Hướng 4.
- Mọi lần fallback ghi log vào `results/fallback_log.jsonl` dưới dạng `{image_id, reason, n_blobs}`. Tỉ lệ fallback > 30% phải báo Verifier xem lại $\epsilon$.

## 5. QUY TẮC BÀN GIAO

- Cuối mỗi Phase, append vào `status_and_bugs.md`: `[PHASE X - READY FOR VERIFY]`, liệt kê file/hàm, smoke-test đã chạy (Board 05–12, 1–2 ảnh), và cam kết không chạm Board 01/04.
- Không ghi đè biên bản `[VERIFIED PASS]` / `[FAILED - NEED FIX]` của Verifier.
