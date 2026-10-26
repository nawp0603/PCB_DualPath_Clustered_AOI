# KẾ HOẠCH NGHIÊN CỨU & TRIỂN KHAI: DENSITY-BASED ADAPTIVE CLUSTERED ROI & MULTI-CENTER GATING (DBSCAN-AOI)

**Dự án mới (cô lập tuyệt đối):** `D:\FPTU\KLTN\PCB_DualPath_Clustered_AOI\`
**Dự án gốc (CHUẨN ĐÃ NGHIỆM THU — TUYỆT ĐỐI CHỈ ĐỌC, KHÔNG SỬA):** `D:\FPTU\KLTN\PCB_DualPath_AOI\`
**Hướng nghiên cứu:** Hướng 4 — thay thế cơ chế cố định TOP-8 + Crop $640 \times 640$ của Nhánh B bằng cụm thích ứng DBSCAN.
**Ngày biên soạn:** 2026-10-24
**Ngày cập nhật kiểm toán (Audit Fix v2):** 2026-10-24 — áp dụng 6 chốt chặn của Verifier Agent (AUDIT-01→06, xem từng khối `[AUDIT-FIX]` trong tài liệu)
**Tác giả:** Computer Vision R&D Architect
**Trạng thái:** Bản kế hoạch (chưa viết mã nguồn — mọi triển khai tuân thủ sandbox-first: chỉ ghi trong project mới).

> **Quy tắc bảo vệ cô lập (STRICT SANDBOX GUARD).** Thư mục `PCB_DualPath_AOI` là bản phát hành chuẩn đã nghiệm thu 100% clean-room pass. Mọi lệnh ghi/xóa/sửa trong thư mục đó đều bị cấm tuyệt đối. Mọi file mới (code, script, kết quả, cache `.npz`, weights fine-tune) chỉ được tạo trong `PCB_DualPath_Clustered_AOI\`. Tài nguyên được phép **đọc tham khảo một chiều**: `configs/resource_paths.py`, `src/reference/board_id_and_align.py`, `src/reference/tolerance_diff.py`, `src/models/yolo_detector.py`, `src/models/ensemble.py`, `src/decision/logic.py`, `scripts/evaluate.py::run_dualpath`, `assets/weights/*.pt`, `assets/templates/01.JPG + 04.JPG`, `data/demo_and_test_samples/` (Board 01/04).

---

## PHẦN 1 — TỔNG QUAN KIẾN TRÚC & ĐỘNG LỰC CẢI TIẾN (EXECUTIVE SUMMARY)

### 1.1. Kiến trúc gốc Dual-Path Industrial AOI (tóm tắt để đối chứng)

Pipeline gốc gồm hai nhánh song song trên cùng ảnh đầu vào full-board (Input space, hệ tọa độ ảnh gốc):

- **Nhánh A (Semantic Defect Detector):** YOLO11n suy luận full-resolution (`imgsz=3072`, `conf=0.20`, bắt buộc `rect=True, half=True` khi CUDA) cho tập dự đoán $B_A = \{(bbox, class, S_A)\}$.
- **Nhánh B (Golden-Template Safety-Net):** (1) Board ID + Coarse Align bằng ORB (8000 features) + RANSAC Homography $H$ (Input→Template) và $H^{-1}$ (Template→Input); (2) chuẩn hóa gain cục bộ (`SMOOTH_WIN=151`); (3) Tolerance-Aware Diff với $\tau=2$ (runtime) trên structure mask (dilate-7 của copper ∪ edge); (4) Connected Components (`Area ≥ 60 px²`, seed `k=1.25`, safe Z-score với `SIGMA_FLOOR=5.0`); (5) ROI Verification: **lấy đúng TOP-8 blob có `Z_diff` cao nhất, crop cố định $640 \times 640$ quanh tâm blob đã chiếu qua $H^{-1}$, gọi `predict_batch()` đúng 1 lần GPU**, rồi lọc bằng `spatial_center_gating(radius=80)` (giữ box nếu tâm box trong bán kính 80 px quanh tâm diff HOẶC `IoU(box, diff_box) > 0`).
- **Hợp nhất:** Box Fusion (`IoU(B_A,B_B) ≥ 0.3 → S_final = min(1.0, S_A + 0.15)`) + quy tắc quyết định 4 mức FAIL / REWORK / REVIEW / PASS.

### 1.2. Ba điểm nghẽn cốt tử của cơ chế cố định TOP-8 + Crop $640 \times 640$

| # | Điểm nghẽn | Hệ quả đo được / lập luận |
|---|---|---|
| G1 | **Crop cố định lãng phí và mù ngữ cảnh cụm.** Mỗi blob — dù là hạt bụi lẻ hay chùm 5–6 blob lỗi short/spurious_copper nằm rải rác 300–500 px — đều bị ép vào đúng một ô $640 \times 640$. Với lỗi cô lập nhỏ, 95%+ diện tích crop là nền vô nghĩa (tốn GPU, tăng nguy cơ FP ở rìa crop); với lỗi chùm, TOP-8 cắt rời ngữ cảnh không gian chung của cụm (mất tương quan liên-blob mà YOLO có thể khai thác nếu thấy cả cụm trong một crop). | Latency Nhánh B ≈ const × 8 crops bất kể mật độ lỗi; recall trên ca lỗi chùm (clustered defect) bị giới hạn bởi việc mỗi blob được xác minh độc lập. |
| G2 | **Single-centroid gating gây False Negative ở rìa cụm.** `spatial_center_gating` + `ensemble.fuse` hiện tính khoảng cách từ tâm detection đến **một tâm blob duy nhất** (hoặc `diff_box` $160 \times 160$ quanh tâm đó). Với cụm trải dài (ví dụ 3 blob cách nhau 120 px), detection khớp blob rìa có thể cách tâm "đại diện" > 80 px và không giao `diff_box` của blob đó → bị drop oan dù là true positive. | Gating drop oan tỉ lệ thuận với đường kính cụm; càng gom cụm (mục tiêu của Hướng 4) thì gating cũ càng sai. |
| G3 | **TOP-K cố định không thích ứng mật độ.** Ảnh có 2 blob thật + 6 blob nhiễu → 6 crop rác; ảnh có 12 blob thật → mất 4 blob (recall trần). Ngưỡng TOP-8 là hằng số cảm tính, không suy ra từ phân bố dữ liệu. | Không có cơ sở thực nghiệm cho K=8 trên lỗi chùm; cần tham số học từ dữ liệu (DBSCAN $\epsilon$, `min_samples`). |

### 1.3. Giải pháp đề xuất: DBSCAN-AOI (tổng quan một đoạn)

Thay **Bước 5 của Nhánh B** (TOP-8 + crop 640 cố định + single-centroid gating) bằng pipeline thích ứng 3 tầng, giữ nguyên toàn bộ các bước 1–4 (align, tolerance diff, blob extraction) và toàn bộ Nhánh A + Box Fusion + luật 4 mức:

```
Blobs (≤ K_raw thô, hệ Template) [AUDIT-01/03] ──► [T1] DBSCAN gom cụm trên ma trận
Single-Linkage gom cụm (m=1 chính: đồ thị kề ngưỡng d_ij ≤ ε, Union-Find) [AUDIT-01]
  ──► Multi-blob Clusters + Isolated Singletons ──► [T1b] Xếp hạng Z_cluster, giữ tối đa MAX_CLUSTER_CROPS=8 [AUDIT-03]
  ──► [T2] Mỗi cụm → 1 adaptive crop (A1 uniform_context_640 HOẶC A2 multi-bucket; Phase 5 chọn) [AUDIT-04]
  ──► [T3] Union-of-Disks gating + Cross-Crop Class-aware NMS (IoU=0.50) [AUDIT-05]
  ──► Box Fusion + luật 4 mức (GIỮ NGUYÊN từ project gốc)
```

Điểm khác biệt cốt lõi so với cơ chế cũ:

| Khía cạnh | Cơ chế cũ (TOP-8 cố định) | Cơ chế mới (DBSCAN-AOI) |
|---|---|---|
| Đơn vị verify | 1 blob = 1 crop $640 \times 640$ | 1 **cụm** = 1 adaptive crop (kích thước theo cụm) |
| Số lượng crop | Luôn ≤ 8, không theo mật độ | ≤ `MAX_CLUSTER_CROPS = 8` cụm (xếp hạng $Z_{cluster} = \max z_i$); thường 1–4 khi lỗi chùm (AUDIT-03) |
| Kích thước crop | Hằng số 640 | A1: luôn $640 \times 640$ (1-call); A2: bucket 384 / 512 / 640 (≤ 3-call) — cả hai cài đặt song song, Phase 5 chọn bằng số liệu (AUDIT-04) |
| Gating | 1 tâm + $R=80$ | Union-of-Disks đa tâm + Cross-Crop Class-aware NMS (IoU = 0.50) trước fusion (AUDIT-05) |
| Tham số | TOP_K = 8 (cảm tính) | $\epsilon$ + `min_samples = 1` chính (Single-Linkage CC; $m \ge 2$ chỉ ablation) — hiệu chuẩn trên Board validation, KHÔNG trên Board test 01/04 (AUDIT-01, AUDIT-02) |
| Fallback | Không có | Tự động về single-centroid khi board chỉ có lỗi cô lập |

**Cam kết tương thích:** tọa độ mọi detection trả về vẫn ở **hệ ảnh gốc** (qua $H^{-1}$ trước khi crop, đúng Chốt 1 của project gốc); định dạng blob dict (`centroid_tpl, area, bbox_tpl, z_diff, …`) và evidence item (`class_id, conf, source, bbox, z_diff, area, diff_box, centroid_input`) được giữ nguyên để `ensemble.fuse` / `logic.decide` tái sử dụng không đổi (hoặc với adapter tối thiểu được ghi rõ trong Phase 4).

---

## PHẦN 2 — MÔ HÌNH TOÁN HỌC & GIẢI THUẬT CHI TIẾT

### 2.1. Ký hiệu chung

- Ảnh đầu vào (Input space): $I \in \mathbb{R}^{H \times W \times 3}$, hệ tọa độ ảnh gốc (pixel, gốc trên-trái).
- Template space: hệ Golden Template sau warp bởi $H$ (Input→Template); $H^{-1}$ là Template→Input (`board_id_and_align.project_points`, dùng `cv2.perspectiveTransform`).
- Tập blob thô từ `run_branch_b` (đã sắp xếp theo `Z_diff` giảm dần, tối đa $K_{raw}$ blob, đề xuất $K_{raw} = 12$ — xem §2.2.4):
  $$B = \{b_1, \dots, b_N\}, \quad b_i = (\mathbf{c}_i, a_i, z_i, \mathbf{box}_i),$$
  trong đó $\mathbf{c}_i = (x_i, y_i)$ là centroid **hệ Template**, $a_i$ là area (px²), $z_i$ là $Z_{diff}$, $\mathbf{box}_i = (x_1, y_1, x_2, y_2)$ là bbox hệ Template.
- Tham số DBSCAN: bán kính lân cận $\epsilon > 0$ (px, hệ Template), số điểm tối thiểu `min_samples` $= m \ge 1$.
  > **[AUDIT-FIX-01 — Nghịch lý `min_samples` & `noise_rate`.]** Chế độ chính là **`min_samples = 1`**: mọi blob cô lập (1 điểm) tạo thành **Isolated Singleton** ($|C_k| = 1$, nhãn cụm riêng, KHÔNG phải noise `label = -1`); chỉ các blob liên thông qua đồ thị $d_{ij} \le \epsilon$ mới tạo **Multi-blob Cluster** ($|C_k| \ge 2$). Về mặt toán học, `min_samples = 1` tương đương **Single-Linkage Connected Components** trên đồ thị kề $A_{ij} = [d_{ij} \le \epsilon]$ (mọi điểm đều là core point, `DBSCAN(m=1) ≡ CC trên đồ thị ngưỡng`). Nhờ đó không tồn tại khái niệm "blob thật bị đánh dấu noise" ở chế độ chính → tiêu chí `noise_rate < 5%` cũ là bất khả thi và được **thay thế** bằng `Cluster Purity ≥ 85%` + `Over-clustering Rate ≤ 15%` (định nghĩa tại §3.4). Chế độ `m ≥ 2` chỉ giữ làm **ablation chẩn đoán** trong script hiệu chuẩn (để vẽ $k$-distance và chứng minh vì sao $m \ge 2$ gây phân mảnh/noise trên lỗi đơn lẻ), KHÔNG phải đường chính.

### 2.2. Thuật toán gom cụm blob: ma trận khoảng cách $Z_{diff}$-weighted Euclidean

#### 2.2.1. Vì sao gom trong hệ Template (không phải hệ ảnh gốc)?

Mọi centroid blob sinh ra trong Template space. Chiếu từng centroid qua $H^{-1}$ (phép phối cảnh phi tuyến) **trước** khi đo khoảng cách sẽ làm méo metric: hai blob cách nhau 100 px trong Template có thể thành 95 px hoặc 108 px trong Input tùy vị trí trên mặt phẳng phối cảnh. DBSCAN nhạy với metric ($\epsilon$ là ngưỡng cứng), nên **toàn bộ bước gom cụm thực hiện trong hệ Template với metric Euclidean thuần túy**, và chỉ chiếu **hộp bao cụm** (4 góc) qua $H^{-1}$ khi đã cần crop trên ảnh gốc. Điều này bảo toàn tính đẳng hướng của $\epsilon$ và khớp Chốt 1 (chỉ chiếu khi đã có thực thể hình học cuối cùng).

#### 2.2.2. Định nghĩa khoảng cách $Z_{diff}$-weighted

Khoảng cách Euclidean thuần túy gom cụm theo gần-kề hình học, nhưng hai blob gần nhau trong đó một blob có $Z$ rất thấp (nhiễu) không nên kéo blob $Z$ cao thành cụm lớn. Định nghĩa trọng số tin cậy:

$$w_i = \sigma\left(\frac{z_i - z_0}{\tau_z}\right), \quad \sigma(u) = \frac{1}{1 + e^{-u}},$$

với $z_0 = 2.0$ (đúng ngưỡng REVIEW 3a của luật 4 mức — blob dưới ngưỡng này chưa đủ "cứu recall" nên ảnh hưởng yếu) và $\tau_z = 1.0$ (độ dốc sigmoid). Khi đó $w_i \in (0,1)$; blob $z_i = 2.0 \Rightarrow w_i = 0.5$; blob $z_i = 5.0 \Rightarrow w_i \approx 0.95$.

Khoảng cách có trọng số giữa blob $i, j$:

$$\boxed{ d_{ij} = \frac{\|\mathbf{c}_i - \mathbf{c}_j\|_2}{\sqrt{w_i \cdot w_j} + \delta} }, \qquad \delta = 10^{-6}.$$

Tính chất: (i) khi cả hai blob tin cậy cao ($w \approx 1$), $d_{ij} \approx$ Euclidean — gom bình thường; (ii) khi một trong hai là nhiễu ($w \to 0$), mẫu số nhỏ → $d_{ij}$ bị phóng đại → nhiễu khó "dính" vào cụm thật (ở ablation m ≥ 2: dễ thành noise; ở chế độ chính m = 1: nhiễu thành singleton Z thấp và bị loại ở tầng xếp hạng top-8 nếu không đủ hạng) [AUDIT-01]; (iii) $d_{ij}$ đối xứng, không âm, $d_{ii} = 0$ — đủ điều kiện làm precomputed metric cho DBSCAN.

> **Ghi chú thiết kế (trung thực học thuật):** $d_{ij}$ nói chung **không thỏa mãn bất đẳng thức tam giác** chặt chẽ (do mẫu số phụ thuộc cặp điểm). DBSCAN chỉ cần quan hệ lân cận $\epsilon$ (không cần không gian metric đầy đủ), nên điều này chấp nhận được trong thực hành và sẽ được kiểm chứng thực nghiệm (Phase 1 so sánh cụm weighted vs Euclidean thuần trên tập validation — nếu weighted không cải thiện Silhouette/cluster-purity thì quay về Euclidean thuần, xem §5.3).

#### 2.2.3. Pseudocode gom cụm (chế độ chính `min_samples = 1`)

> **[AUDIT-FIX-01 — Sửa nghịch lý `min_samples` & `noise_rate`.]** Với `min_samples ≥ 2`, mọi lỗi đơn lẻ cô lập (đúng 1 blob) đều bị gán `label = -1`, khiến tiêu chí `noise_rate < 5%` ở bản cũ là bất khả thi về mặt toán học (lỗi đơn lẻ thật luôn thành "noise"). Chế độ chính của plan này là **`min_samples = 1`**: mọi blob đều là core point, DBSCAN suy biến thành **Single-Linkage Connected Components** trên đồ thị kề ngưỡng $A_{ij} = [d_{ij} \le \epsilon]$ — phân hoạch tự nhiên thành **Multi-blob Clusters** ($|C_k| \ge 2$) và **Isolated Singletons** ($|C_k| = 1$, mỗi singleton là một cụm hợp lệ, KHÔNG phải noise). Các giá trị $m \ge 2$ chỉ giữ làm ablation chẩn đoán trong script hiệu chuẩn (vẽ $k$-distance với $k = m$ để minh họa phân mảnh), không phải đường chính.

```
INPUT:  blobs B = [(c_i, a_i, z_i, box_i)] (hệ Template, đã sort Z giảm),
        eps (px), min_samples m = 1 (chính), z0=2.0, tau_z=1.0
OUTPUT: clusters C_k (k = 0..C-1), MỖI blob thuộc đúng 1 cụm (|C_k| ≥ 1);
        KHÔNG tồn tại nhãn noise ở chế độ chính (noise chỉ xuất hiện
        ở ablation m ≥ 2, dùng để chẩn đoán, không dùng để crop).

1. w_i ← sigmoid((z_i − z0)/tau_z)                       # (N,)
2. D ← pairwise Euclidean(c)                             # (N,N), float32
3. W ← sqrt(w ⊗ w) + δ                                    # outer product
4. D_w ← D / W;  D_w[i,i] ← 0
5. NẾU m == 1:
     L ← ConnectedComponents(A = [D_w ≤ eps])            # Single-Linkage,
        # tương đương DBSCAN(eps, min_samples=1); implement bằng Union-Find
        # O(N² α(N)) — deterministic, không cần sklearn (xem test T-CLU-03).
   NGƯỢC LẠI (ablation m ≥ 2):
     L ← DBSCAN(eps=eps, min_samples=m, metric='precomputed').fit(D_w)
6. RETURN L (chế độ chính: mọi L[i] ≥ 0)
```

Độ phức tạp: $O(N^2)$ với $N \le K_{raw} = 12$ → ma trận $12 \times 12$, thời gian < 0.05 ms (không đáng kể so với ngân sách 450 ms toàn pipeline). Chế độ chính dùng Union-Find thuần NumPy (~30 dòng, không phụ thuộc sklearn); nhánh ablation $m \ge 2$ dùng `sklearn.cluster.DBSCAN` nếu có (xem test §6.2).

#### 2.2.4. Vì sao $K_{raw} = 12$ (thay vì TOP-8) ở đầu vào cụm? + Trần `MAX_CLUSTER_CROPS = 8`

`extract_blobs(..., top_k=8)` cắt cứng ở 8 blob **trước khi biết cấu trúc cụm**. Một cụm 5 blob thật có thể bị mất 1–2 thành viên nếu 8 suất bị nhiễu $Z$ trung bình chiếm chỗ. Nới đầu vào lên $K_{raw} = 12$ (chi phí diff/CC không đổi — `extract_blobs` tính toàn bộ rồi mới cắt), để DBSCAN nhìn thấy đủ thành viên cụm; sau gom cụm, số crop thực tế thường **giảm** (1 cụm = 1 crop) nên tổng latency không tăng (chứng minh bằng benchmark Phase 5). Tham số này được hiệu chuẩn cùng $\epsilon$ (Phase 1 đo phân bố số blob/ảnh trên Board hiệu chuẩn để chốt $K_{raw} \in \{8, 12, 16\}$ — xem §3.1 về tách tập hiệu chuẩn/đánh giá).

> **[AUDIT-FIX-03 — Khóa trần số lượng crop.]** $K_{raw} = 12$ blob rời rạc hoàn toàn (worst case, mọi blob cách nhau $> \epsilon$) sẽ sinh ra 12 cụm singleton → 12 crops, **nhiều hơn baseline 8 crops** và vi phạm Chốt 3 (latency guardrail). Do đó bổ sung quy tắc cứng sau gom cụm (áp dụng cho cả multi-blob clusters lẫn isolated singletons):
>
> $$\boxed{ Z_{cluster}(C_k) = \max_{i \in C_k} z_i, \quad \text{giữ tối đa } MAX\_CLUSTER\_CROPS = 8 \text{ cụm có } Z_{cluster} \text{ cao nhất} }$$
>
> - Sắp xếp mọi cụm (kể cả singleton) theo $Z_{cluster}$ giảm dần, chỉ đưa tối đa 8 cụm đầu đi crop → verify → gating → fusion. Các cụm hạng 9+ bị loại ở tầng này (ghi log `dropped_lowz_clusters` để giám sát).
> - Dùng $\max$ chứ không dùng mean/sum: một cụm chứa 1 blob $Z$ rất cao + vài blob $Z$ thấp (nhiễu dính vào) vẫn phải được ưu tiên — đúng tinh thần "cứu recall" của Nhánh B; mean/sum sẽ pha loãng hoặc thiên vị cụm đông.
> - Hằng số 8 = đúng TOP-8 baseline → so sánh A/B công bằng về ngân sách crop tối đa (không bao giờ tốn hơn baseline về số crop), lợi thế của Hướng 4 thể hiện ở **crop ít hơn nhưng trúng hơn** (xem metric `Crops-per-image reduction` §5.1).
> - Trường hợp suy biến: nếu > 8 cụm thật (board lỗi nặng toàn cục), 4 cụm thấp nhất bị drop — đây là trade-off có ý thức để giữ latency; Phase 5 đo `recall@rank9-12` (xem §5.1 Fragmented Defect Merging) để lượng hóa tổn thất này.

#### 2.2.5. Từ cụm → adaptive crop (hình học thuận, không biến dạng)

Với mỗi cụm $C_k$ (multi-blob cluster $|C_k| \ge 2$ hoặc isolated singleton $|C_k| = 1$ — chế độ chính m=1 không có noise [AUDIT-01]; các trường hợp ngoại lệ xem Fallback §5.4):

1. Hộp bao hệ Template: $\mathbf{U}_k^{tpl} = \bigcup_{i \in C_k} \mathbf{box}_i$ (min/max tọa độ), cộng lề ngữ cảnh $p = 48$ px mỗi phía (đủ chứa biên defect + texture lân cận cho YOLO; 48 px ≈ 1.5× receptive field P3 ở imgsz 640).
2. Chiếu 4 góc $\mathbf{U}_k^{tpl}$ qua $H^{-1}$ → tứ giác hệ Input; lấy hộp bao trục chuẩn (axis-aligned bbox) của tứ giác → $\mathbf{U}_k^{input} = (X_1, Y_1, X_2, Y_2)$, clamp vào $[0, W] \times [0, H]$.
3. Cạnh crop vuông: $s_k = \max(X_2 - X_1, Y_2 - Y_1)$, tâm $= $ tâm $\mathbf{U}_k^{input}$; mở rộng đối xứng thành hình vuông $s_k \times s_k$ (clamp biên; nếu chạm biên thì trượt — shift — thay vì co, để giữ kích thước).
4. **Chế độ crop ba chế độ (AUDIT-04, chi tiết §2.3):** mọi hộp bao cụm $s_k \le 640$ được xử lý theo 1 trong 3 chế độ cài đặt song song trong `batch_bucket.py` — **A1 `uniform_context_640`** (mở rộng hộp bao thành cửa sổ $640 \times 640$ trên ảnh gốc, đúng 1 lần gọi GPU) + **A2 `multi_bucket_384_512_640`** (lượng tử hóa 384/512/640, tối đa 3 lần gọi GPU) + **B `letterbox_640`** (pad xám 114 về $640 \times 640$, baseline đối chứng); Phase 5 đo đạc khách quan trên ma trận Config 0–4, không chọn trước bằng lập luận.
5. **Cửa sổ / lượng tử hóa crop:** A1: luôn $S_k = 640$; A2: $S_k \in \{384, 512, 640\}$ theo hộp bao cụm. Cả ba chế độ đều lưu bộ ba $(x_0, y_0, S_k)$ (gốc crop hệ Input + cạnh; B lưu thêm mask pad) cho bước ánh xạ ngược tọa độ tịnh tiến 1:1.

Giới hạn trần: nếu $s_k > 640$ (cụm khổng lồ do $\epsilon$ quá lớn) → kích hoạt **Max Cluster Size Cap + Hierarchical Bisection** (xem §3.3): đệ quy tách đôi cụm theo trục dài bằng KMeans(k=2) trên centroid (tối đa 2 lần), mỗi mảnh xử lý như cụm độc lập; mảnh nào vẫn > 640 thì crop 640 tại tâm mảnh (ghi cờ `truncated=True` để đánh giá recall riêng).

### 2.3. Giải pháp xử lý Batch Variable-Size (vấn đề §1 của yêu cầu)

> **[AUDIT-FIX-04 — Đối chứng 1-Call Native 640 Context Crop vs Multi-Call Size-Bucket Batching.]** Bản cũ chọn trước Giải pháp A (multi-bucket, tối đa 3 lần gọi GPU) bằng lập luận. Verifier chỉ ra: (i) 3 lần gọi `predict_batch` gây thêm Python/CUDA launch overhead (~15–30 ms thực tế, lớn hơn ước tính 10 ms cũ); (ii) `imgsz ∈ {384, 512}` làm lệch kích thước feature map P5 so với điều kiện train `yolo11n_day2.pt` ở 640×640 (rủi ro distribution shift ngầm); (iii) trong khi đó cắt trực tiếp cửa sổ ngữ cảnh $640 \times 640$ quanh hộp bao cụm ($s_k \le 640$) vẫn giữ tỉ lệ 1:1 tuyệt đối, không tăng FP (nhờ Union-of-Disks gating $R = 80$ khóa tâm), và chỉ tốn **1 lần gọi GPU duy nhất** như baseline. Vì vậy §2.3 được viết lại thành **so sánh thực nghiệm công bằng hai chế độ**, cả hai đều cài đặt đầy đủ trong `batch_bucket.py`, Phase 5 đo đạc khách quan rồi mới kết luận — **cấm chọn trước bằng lập luận**.

#### 2.3.1. Phát biểu vấn đề (cập nhật)

Sau T2 + trần `MAX_CLUSTER_CROPS = 8` (AUDIT-03), ta có danh sách cụm $\le 8$ với cạnh hộp bao $s_k$ (hệ Input, đã gồm lề $p = 48$). Hai cách biến chúng thành input YOLO:

- **Cách A1 (`uniform_context_640`, 1-call):** mở rộng mỗi hộp bao $s_k \le 640$ thành cửa sổ vuông $640 \times 640$ đồng tâm (shift khi chạm biên — đúng kỹ thuật crop của `evaluate.py::run_dualpath` gốc), cắt trực tiếp từ ảnh gốc → mọi crop đồng nhất $640 \times 640$ → **đúng 1 lần gọi** `predict_batch(crops, imgsz=640, rect=True)`.
- **Cách A2 (`multi_bucket_384_512_640`, ≤ 3-call):** lượng tử hóa $s_k \mapsto S_k \in \{384, 512, 640\}$, crop $S_k \times S_k$ → nhóm theo bucket → tối đa 3 lần gọi `predict_batch` với `imgsz` bằng cạnh bucket.

Nếu truyền crop hỗn hợp kích thước vào một lần gọi duy nhất, Ultralytics sẽ resize/pad ngầm về kích thước lớn nhất (crop 384 bị upscale ×1.67, nhòe biên defect vi mô 10–40 px) — nên A2 **bắt buộc** tách sub-batch; A1 tránh vấn đề này từ gốc bằng cách đồng nhất 640.

#### 2.3.2. Chế độ A1 — Uniform Context 640 (1 lần gọi GPU — đồng ứng viên chính)

**Ý tưởng:** Mọi cụm $s_k \le 640$ đều thành crop $640 \times 640$ (tâm = tâm hộp bao cụm, shift — không co — khi chạm biên ảnh). Cụm $s_k > 640$ đi qua bisection trước (§3.3) nên không bao giờ vào nhánh này với $s_k > 640$.

```
INPUT: clusters [(U_k^input, cx_k, cy_k)], ảnh gốc I (H×W)
1. Với mỗi cụm: (x0, y0) ← center_window(cx, cy, 640, W, H)  # vuông 640, shift biên
   J_k ← I[y0:y0+640, x0:x0+640] (BGR→RGB cho YOLO)
2. dets_all ← yolo.predict_batch([J_1..J_M], imgsz=640)      # ĐÚNG 1 lần gọi
3. Mỗi det: bbox^input = bbox^crop + (x0, y0, x0, y0)        # tịnh tiến 1:1
```

**Ưu điểm:** (i) đúng 1 CUDA launch — overhead launch = 0 so với baseline; (ii) `imgsz=640` khớp đúng điều kiện train/fine-tune (`yolo11n_day2.pt` train ở tile 640) → feature map P5 ($20 \times 20$ tại stride 32) cùng phân phối với lúc train, không distribution shift; (iii) pixel defect giữ nguyên 1:1 tuyệt đối (crop trực tiếp từ ảnh gốc, không resize — đúng Chốt 1 mở rộng); (iv) FP ở vùng nền thừa trong cửa sổ 640 được Union-of-Disks gating ($R = 80$ quanh mọi tâm blob) khóa chặt — det xa mọi tâm blob đều bị loại trước fusion. **Nhược điểm:** tốn pixel GPU hơn A2 (cửa sổ 640 cho cả cụm nhỏ 100 px → ~97% pixel là ngữ cảnh); ngữ cảnh rộng có thể chứa texture gây FP (nhưng đã có gating khóa).

#### 2.3.3. Chế độ A2 — Multi-Bucket 384/512/640 (tối đa 3 lần gọi GPU — đồng ứng viên chính)

**Ý tưởng:** Gom các crop theo đúng bucket kích thước của chúng; mỗi bucket là một sub-batch đồng nhất gọi `predict_batch` riêng với `imgsz` bằng cạnh bucket; giữ `rect=True` trong mọi lần gọi.

```
INPUT: crops [(J_k, x0_k, y0_k, S_k)], S_k ∈ {384, 512, 640}
1. Nhóm chỉ số theo S_k → tối đa 3 nhóm G_384, G_512, G_640 (nhóm rỗng bỏ qua).
2. Với mỗi nhóm G_s ≠ ∅:
     dets_s ← yolo.predict_batch([J_k], imgsz=s)   # 1 lần gọi GPU
     # det bbox ở hệ crop s×s; cộng gốc (x0_k, y0_k) → hệ ảnh gốc
     # (KHÔNG có phép co giãn nào vì imgsz == cạnh crop → scale 1:1)
3. Ghép tất cả dets về một danh sách hệ ảnh gốc → sang T3 (gating).
```

**Vì sao scale 1:1 được bảo toàn:** Ultralytics với `imgsz=s` và ảnh đầu vào đúng $s \times s$ (+ `rect=True`) không resize mặt khả kiến (với YOLO11n stride 32, 384/512/640 đều chia hết cho 32 nên **zero-pad, zero-resize**). Ánh xạ ngược là phép tịnh tiến thuần túy (công thức gốc `evaluate.py::run_dualpath` mở rộng):

$$\boxed{ \mathbf{bbox}^{input} = \mathbf{bbox}^{crop} + (x_0, y_0, x_0, y_0) }$$

**Ưu điểm:** tổng pixel GPU nhỏ hơn (crop 384 chỉ bằng 36% diện tích crop 640). **Nhược điểm / rủi ro (ghi rõ để Phase 5 kiểm chứng):** (i) overhead launch phụ $(n_{calls}-1) \times T_{launch}$, đo thực tế kỳ vọng **15–30 ms** ở trường hợp 3 buckets (hiệu chỉnh từ ước tính 10 ms cũ — số chính xác do Phase 5 đo, không dùng số ước tính để kết luận); (ii) `imgsz=384/512` cho feature map P5 $12 \times 12$ / $16 \times 16$, khác điều kiện train 640 ($20 \times 20$) — nguy cơ nhẹ distribution shift trên defect vi mô (Phase 5 so AP trên cùng tập để lượng hóa).

**Phân tích chi phí overhead (khung đo — số liệu thật ở Phase 5):**
- Baseline gốc: 1 lần gọi batch-8×640 ≈ $T_{640}$ (batch crop nhỏ; số chính xác do Phase 5 đo trên cùng GPU).
- A1: đúng 1 lần gọi batch-$M \times 640$ ($M \le 8$) → overhead launch = 0; tổng pixel = $M \times 640^2$.
- A2: $n_{calls} \in \{1,2,3\}$ lần gọi; overhead launch thêm = $(n_{calls}-1) \times T_{launch}$; tổng pixel = $\sum S_k^2 \le M \times 640^2$.
- Bộ nhớ: các sub-batch chạy tuần tự, peak VRAM = max batch đơn — không tăng ở cả hai chế độ.

#### 2.3.4. Giải pháp B — Letterbox Padding (phương án tham khảo, KHÔNG nằm trong ma trận A/B chính)

Pad mọi crop về $640 \times 640$ bằng letterbox (giữ tỉ lệ 1:1, pad xám 114), gọi đúng 1 lần `predict_batch(imgsz=640)`, rồi un-pad + loại det có tâm trong vùng pad. Giữ làm code path dự phòng trong `batch_bucket.py` (`force_single_batch=True`) và được đưa vào ma trận benchmark chính làm `Config 3 (Ablation Letterbox)` ở Phase 5 (DBSCAN + B + U); ngoài Phase 5, chỉ kích hoạt B nếu cả A1 và A2 đều vi phạm latency/accuracy (quyết định bằng số liệu, ghi log lý do).

#### 2.3.5. Hàm xử lý tọa độ tương ứng (đặc tả chính xác cho Coder)

```python
# Module: src/cluster/batch_bucket.py  (tên dự kiến, chốt ở Phase 3)
BUCKETS = (384, 512, 640)
CROP_MODE_A1 = "uniform_context_640"   # AUDIT-04: 1-call, mọi crop 640×640
CROP_MODE_A2 = "multi_bucket_384_512_640"  # AUDIT-04: ≤ 3-call theo bucket

def center_window(cx, cy, size, W, H) -> tuple[int, int]:
    """Cửa sổ vuông `size`×`size` đồng tâm (cx,cy), SHIFT (không co) khi chạm biên."""

def quantize(s: float) -> int:
    """(Chỉ dùng cho A2) s = cạnh vuông hệ Input → bucket nhỏ nhất chứa được s."""

def to_input_coords(bbox_crop, origin) -> list[float]:
    """Tịnh tiến thuần túy (scale 1:1, đúng cho cả A1 và A2)."""
    x0, y0 = origin
    x1, y1, x2, y2 = bbox_crop
    return [x1 + x0, y1 + y0, x2 + x0, y2 + y0]

def predict_clusters(clusters_crops, yolo, mode: str):
    """A1: 1 lần predict_batch(imgsz=640). A2: nhóm theo bucket, mỗi bucket
    1 lần predict_batch(imgsz=bucket). Cả hai đều rect=True, half khi CUDA."""
```

Bất biến kiểm thử: `to_input_coords` không chứa phép nhân/chia nào (assert bằng đọc source trong test — noi theo `test_yolo_day2_fixes_still_present` của project gốc dùng `inspect.getsource`); `center_window` luôn trả về cửa sổ đủ `size × size` nằm trọn trong ảnh (test T-GEO-03).

### 2.4. Tái định nghĩa không gian Gating cho cụm lỗi (vấn đề §2 của yêu cầu)

#### 2.4.1. Phát biểu vấn đề

Gating gốc giữ detection $d$ (tâm $\mathbf{p}_d$ hệ ảnh gốc) nếu $\|\mathbf{p}_d - \mathbf{c}\|_2 \le R$ với $R = 80$ px quanh **một** tâm blob $\mathbf{c}$ (hoặc IoU với `diff_box` $160 \times 160$ quanh $\mathbf{c}$). Với cụm $C = \{\mathbf{c}^{(1)}, \dots, \mathbf{c}^{(n)}\}$ trải dài $D = \max_{i,j}\|\mathbf{c}^{(i)} - \mathbf{c}^{(j)}\|_2$ (hệ Input, sau chiếu $H^{-1}$), nếu lấy centroid cụm $\bar{\mathbf{c}} = \frac{1}{n}\sum \mathbf{c}^{(i)}$ làm tâm duy nhất, detection khớp blob rìa có khoảng cách đến $\bar{\mathbf{c}}$ lên tới $\approx D/2 + r_{det}$ — vượt 80 px khi $D > \sim 120$ px → **False Negative do gating** (drop oan đúng ca lỗi chùm mà Hướng 4 muốn cứu).

#### 2.4.2. Cơ chế U — Union-of-Disks Gating (KHUYẾN NGHỊ — lựa chọn chính thức)

Giữ detection $d$ nếu tâm nó nằm trong **hợp của các đĩa** bán kính $R_{base} = 80$ px quanh **mọi** tâm blob thuộc cụm (tâm đã chiếu sang hệ Input), HOẶC giao cắt (`IoU > 0`) với `diff_box` $160 \times 160$ của **bất kỳ** blob nào trong cụm:

$$\boxed{ \text{keep}(d, C) \iff \Bigl[\min_{i \in C}\,\|\mathbf{p}_d - \mathbf{c}^{(i)}_{input}\|_2 \le R_{base}\Bigr] \;\lor\; \Bigl[\exists\, i \in C : \mathrm{IoU}(\mathbf{b}_d, \mathbf{diff\_box}^{(i)}) > 0\Bigr] }, \quad R_{base} = 80.$$

Tính chất: (i) khi $|C| = 1$, công thức suy biến **chính xác** về gating gốc → tương thích ngược hoàn hảo, fallback tự nhiên (§5.4); (ii) đơn điệu theo cụm: thêm blob vào cụm chỉ có thể giữ thêm detection, không bao giờ drop detection đã giữ → không tạo FN mới so với single-centroid; (iii) FP không bùng nổ vì mỗi đĩa vẫn bán kính 80 px như cũ (tổng diện tích gate tăng theo số blob nhưng đó đúng là vùng uncertainty của cụm).

Vector hóa NumPy (chứng minh $< 1$ ms): với $M$ detections và $n$ tâm cụm ($M \le \sim 50$, $n \le 12$), ma trận khoảng cách $(M \times n)$ bằng broadcasting:

```python
P = det_centers[:, None, :]      # (M,1,2)
C = blob_centers[None, :, :]     # (1,n,2)
D = np.linalg.norm(P - C, axis=-1)   # (M,n)
keep_disk = D.min(axis=1) <= R_BASE  # (M,)
```

Chi phí: $M \cdot n \cdot 2$ phép trừ/bình phương ≈ $50 \times 12 \times 2 = 1200$ FLOP + 1 lần `min` — **≈ 5–20 µs** trên CPU hiện đại, nhỏ hơn 2 bậc so với ngân sách 1 ms. Phần IoU: $M \times n$ IoU hộp ($50 \times 12 = 600$ IoU, mỗi IoU ~10 FLOP) ≈ **≈ 10–30 µs**. Tổng $< 0.1$ ms — đạt yêu cầu với biên an toàn 10×. (Số liệu-claim này phải được **đo thực tế** bằng micro-benchmark Phase 4, xem test §6.4.)

#### 2.4.3. Cơ chế H — Cluster Bounding Margin Gating (phương án so sánh, KHÔNG chọn làm chính)

Tạo hộp bao tối thiểu (minimum enclosing AABB hệ Input) của toàn bộ `diff_box` các blob trong cụm, cộng lề $\Delta = 40$ px mỗi phía:

$$\mathbf{H}_C = \bigl[\min X_1 - \Delta,\; \min Y_1 - \Delta,\; \max X_2 + \Delta,\; \max Y_2 + \Delta\bigr],$$
$$\text{keep}(d, C) \iff \mathrm{IoU}(\mathbf{b}_d, \mathbf{H}_C) > 0 \;\lor\; \mathbf{p}_d \in \mathbf{H}_C.$$

**Vì sao không chọn làm chính:** (i) với cụm thưa (2 blob cách nhau 400 px), $\mathbf{H}_C$ bao cả vùng nền lớn ở giữa → giữ FP ở vùng không có bằng chứng diff (đi ngược nguyên tắc "gating là safety filter" của Chốt Nhánh B); (ii) Union-of-Disks bao sát uncertainty từng blob hơn về mặt hình học; (iii) công thức H suy biến kém về single-blob (hộp $160 \times 160$ + lề 40 ≠ gating gốc). Cơ chế H được implement làm **đối chứng ablation** trong Phase 5 (đo FP/FAR chênh lệch U vs H) rồi loại.

**Kết luận §2.4: Chọn Cơ chế U (Union-of-Disks), $R_{base} = 80$ px giữ nguyên từ project gốc** (không phát minh ngưỡng mới — mọi ngưỡng mới đều phải hiệu chuẩn, và 80 px đã được nghiệm thu). Cơ chế H implement tối thiểu để ablation.

#### 2.4.4. Tích hợp với `ensemble.fuse` gốc + Cross-Crop NMS (đặc tả adapter)

> **[AUDIT-FIX-05 — Bổ sung Cross-Crop Class-aware NMS nội bộ Nhánh B.]** Khi 2 cụm gần nhau (vùng crop A1 $640 \times 640$ chồng lấn lớn) hoặc 2 mảnh sau Hierarchical Bisection (§3.3) có vùng crop giao nhau, YOLO có thể phát hiện trùng cùng một lỗi vật lý trên cả hai crop → cùng một GT sinh 2 TP (tăng FP theo chuẩn `pr_counts`/VOC). Do đó bổ sung tầng NMS nội bộ Nhánh B **sau** Union-of-Disks gating, **trước** `ensemble.fuse`:

`ensemble.fuse(yolo_dets, blobs, h_inv, ...)` gốc chiếu centroid từng blob qua $H^{-1}$ rồi match từng blob với det tốt nhất. Adapter cụm (`src/cluster/cluster_gating.py`) có 3 tầng:

1. **Tầng gating (thay thế `spatial_center_gating` cho dets từ crop cụm):** áp dụng công thức U (§2.4.2) với tập tâm = mọi blob trong cụm đã chiếu qua $H^{-1}$. Dets từ crop singleton dùng gating gốc không đổi (suy biến chính xác — T-GAT-01).
2. **Tầng Cross-Crop NMS — MỚI (AUDIT-05):** gộp toàn bộ dets Nhánh B đã qua gating (mọi crop, đã ở hệ ảnh gốc) rồi chạy **class-aware NMS** (`IoU = 0.50`, giữ box có `conf` cao nhất trong mỗi nhóm cùng lớp):
   - *Class-aware (không phải class-agnostic):* hai lỗi khác lớp chồng lấn (ví dụ short đè lên spurious_copper — thường gặp trên PCB) là hai lỗi thật khác nhau, KHÔNG được triệt tiêu lẫn nhau; chỉ triệt tiêu trùng lặp cùng lớp.
   - *Ngưỡng IoU = 0.50:* khớp đúng ngưỡng đánh giá `DEFAULT_IOU = 0.5` của `evaluate.py` — trùng lặp bị tính FP ở đánh giá thì cũng bị triệt tiêu ở NMS với cùng ngưỡng (nhất quán tiêu chí).
   - *Thứ tự:* NMS chạy **sau gating** (chỉ NMS trên dets đã sạch FP rìa crop) và **trước `fuse`** (fuse nhận dets đã dedup → không double-count trong Box Fusion `S_final = min(1.0, S_A + 0.15)`).
   - Độ phức tạp $O(M^2)$ với $M \le \sim 50$ dets → < 0.1 ms; implement thuần NumPy (~25 dòng), không cần `torchvision.ops.nms` (tránh dependency CUDA ở tầng CPU).
3. **Tầng fusion (tái sử dụng `ensemble.fuse` nguyên vẹn):** sau gating + NMS, dets đã ở hệ ảnh gốc + blobs gốc (kèm `cluster_id`) được đưa vào `fuse()` không đổi; `fuse` match từng blob–det như cũ nên không mất recall (tính đơn điệu §2.4.2 đảm bảo det rìa đã qua gating sẽ được match với đúng blob rìa của nó; NMS chỉ loại trùng lặp cùng lớp nên không loại det của blob khác). Thêm trường `cluster_id` vào evidence item (backward-compatible: `decide()` bỏ qua trường thừa).

### 2.5. Độ phức tạp tổng thể và ngân sách latency

| Thành phần mới | Độ phức tạp | Ước tính thời gian |
|---|---|---|
| Ma trận $D_w$ + Single-Linkage CC ($N \le 12$, Union-Find) | $O(N^2)$ | < 0.1 ms |
| Xếp hạng cụm $Z_{cluster}$ + cắt top-8 | $O(C \log C)$ | < 0.01 ms |
| Hộp bao cụm + chiếu $H^{-1}$ (4 góc/cụm) | $O(C)$ | < 0.05 ms |
| Chế độ A1: 0 lần gọi GPU phụ (đúng 1-call như baseline) | hằng số | 0 ms overhead launch |
| Chế độ A2: tối đa 2 lần gọi GPU phụ (bucket batching) | hằng số | 15–30 ms worst-case (đo thực Phase 5) |
| Union-of-Disks gating vectorized | $O(M \cdot n)$ | < 0.1 ms |
| Cross-Crop Class-aware NMS ($M \le \sim 50$) | $O(M^2)$ | < 0.1 ms |
| **Tổng overhead thêm vs baseline (A1)** | — | **≤ ~1 ms** |
| **Tổng overhead thêm vs baseline (A2, xấu nhất 3 buckets)** | — | **≤ ~30 ms** |
| Ngưỡng latency toàn pipeline | — | **≤ 450 ms** (giữ nguyên từ yêu cầu) |

Vì số crop thường giảm (1 cụm = 1 crop thay vì $n$ crop), tổng latency Nhánh B kỳ vọng **giảm ròng (A2, crop nhỏ) hoặc tương đương (A1, cùng 1-call)** so với baseline. Mọi con số phải được thay bằng số đo thực tế ở Phase 5 (quy tắc trung thực số liệu của project gốc được kế thừa nguyên văn).

---

## PHẦN 3 — QUY TRÌNH HIỆU CHUẨN THAM SỐ KHÔNG RÒ RỈ DỮ LIỆU TRÊN TẬP VALIDATION/TRAIN (BOARD 05–12)

> [AUDIT-02/Chốt 1-2] Toàn bộ Phần 3 chỉ dùng Board 05–12; test 01/04 khóa đến Phase 5.

### 3.1. Dữ liệu đầu vào hiệu chuẩn (tập Validation/Train — Board 05–12, KHÔNG dùng 01/04) [AUDIT-02]

> **Chốt chặn Zero Data Leakage (bất khả xâm phạm).** Toàn bộ Phase 1 hiệu chuẩn CHỈ được đọc **READ-ONLY** từ `LEGACY_PKU_ROOT` trên các **Train/Validation Boards (`05–12`, đặc biệt là Validation Board `07`)** — cùng layout `images/<class>/*.jpg` + `Annotations/<class>/*.xml`, Golden Template tương ứng (`05.JPG` … `12.JPG`, đặc biệt `07.JPG`) lấy READ-ONLY từ `assets/templates/` project gốc; ghi rõ nguồn từng ảnh trong `calib_report.json`. Tuyệt đối **KHÔNG** dùng Board `01/04` cho bất kỳ bước hiệu chuẩn nào (kể cả knee-plot unsupervised hay "nhìn bằng mắt rồi chỉnh tay").

- **Tập Test 01/04 — KHÓA HOÀN TOÀN trong suốt Phase 1–4.** Board 01/04 là **Zero-defect-training Boards** (có Golden Sample, kế thừa thuật ngữ chuẩn project gốc, không gọi là "unseen"), đồng thời là **held-out test set** của Hướng 4: bị khóa hoàn toàn trong Phase 1–4, **chỉ được mở ra chạy đánh giá đúng 1 lần ở Phase 5** trên cả 2 biến thể Standard + Camera Perturbation. Mọi quyết định siêu tham số ($\epsilon$, $K_{raw}$, ngưỡng $R$, bucket) phải được đóng băng trong `calib_report.json` (kèm `frozen_sha256`) **trước khi** mở test.
- **Hai biến thể nhiễu bắt buộc trên Board 05–12:** (a) ảnh PKU gốc (standard); (b) ảnh nhiễu camera mô phỏng (dịch ±25 px, xoay ±2°, phối cảnh, gain/bias, vignette, noise — tái sử dụng đúng hàm `perturb_image_and_boxes` của `evaluate.py`, seed cố định `20261023`) để $\epsilon$ robust với camera thực tế.
- **Đầu vào của script hiệu chuẩn:** chạy lại đúng `identify_and_align(fast=True)` + `run_branch_b` gốc (import READ-ONLY từ project gốc hoặc copy nguyên văn vào project mới — quyết định ở Phase 1, xem §4.1) trên toàn bộ ảnh **Board 05–12**, thu thập với **$K_{raw} = 16$ (nới lỏng để quan sát đuôi phân bố)** mỗi ảnh: centroid hệ Template, $Z_{diff}$, area.
- **Khóa cứng (Freeze):** sau Phase 1, `results/calibration/calib_report.json` (chứa $\epsilon$, $K_{raw}$, hash SHA256 bản copy pipeline, `frozen_sha256`) bị **đóng băng**; Phase 2–5 không được sửa, trừ khi quay lại Phase 1 với lý do ghi biên bản và chạy lại toàn bộ từ đầu.

### 3.2. Vẽ biểu đồ khoảng cách lân cận & tìm Knee ($\epsilon_{optimal}$, đồng bộ `min_samples = 1`) [AUDIT-01/02]

Đồng bộ với `min_samples = 1` ở §2.2.3 (Single-Linkage CC, mọi blob đều là core point — **tuyệt đối không dùng `label = -1` để loại bỏ bất kỳ blob đơn lẻ (Singleton) nào**; singleton $|C_k| = 1$ là cụm hợp lệ):

1. Với mỗi blob $i$, tính **khoảng cách lân cận gần nhất bậc 1 ($1$-NN distance)** trên đồ thị trọng số $D_w$ (§2.2.2): $d_i^{(1)} = \min_{j \neq i} d_{ij}$.
2. Sắp xếp $d_i^{(1)}$ giảm dần (cố định một chiều, ghi rõ trong code), vẽ **đường cong $1$-NN distance** cho từng biến thể (standard/camera) trên Board 05–12 (vẽ theo từng board + gộp). Giữ thêm **2 đường cong tham khảo phụ $k \in \{2,3\}$** ($k$-distance bậc 2, 3) chỉ để đối chiếu độ dốc — KHÔNG dùng để chọn $\epsilon$, KHÔNG dùng `label = -1`.
3. Tìm **điểm uốn (knee)** trên đường cong $1$-NN bằng thuật toán **Kneedle** (Satopaa et al. 2011 — implement thuần NumPy ~40 dòng, không thêm dependency, hoặc `kneed` nếu môi trường có): điểm cong cực đại của đường cong chuẩn hóa. Giá trị $1$-NN tại knee = $\epsilon_{optimal}$ ứng viên chính.
4. Xuất figure `results/calibration/k_distance_val_boards05_12.png` (đường chính $1$-NN + 2 đường tham khảo $k=2,3$ + điểm knee đánh dấu + giá trị số) vào `results/calibration/` của project mới — figure này là **bằng chứng số liệu bắt buộc** trong báo cáo (thay cho "chọn bừa cảm tính").

**Nhánh B — chọn $\epsilon$ chính (chế độ $m = 1$, trên tập VALIDATION Board 05–12 có GT):**
5. Quét lưới $\epsilon$ (đề xuất $80$–$300$ px hệ Template, bước $10$–$20$ px — chốt bước quét ở Phase 1 sau khi xem phân bố $1$-NN) với `min_samples = 1`, đo trên tập validation Board 05–12 có GT: **Cluster Purity** và **Over-clustering Rate** (định nghĩa §3.4 — chỉ 2 chỉ số nghiệm thu, không chỉ số tỷ lệ nhiễu, không duyệt tham số cụm).
6. Chọn $\epsilon_{optimal}$ = giá trị lớn nhất trong đoạn plateau mà Purity ≥ 85% và Over-clustering ≤ 15% (ưu tiên $\epsilon$ lớn hơn trong plateau để gom tốt hơn).

### 3.3. Ngưỡng chặn an toàn Max Cluster Size Cap + Hierarchical Bisection

Ràng buộc cứng: **mọi crop cuối cùng ≤ $640 \times 640$** (giới hạn `imgsz` và bảo toàn độ phân giải vi mô — crop > 640 buộc YOLO downscale, đúng điều G3 muốn tránh).

```
INPUT: cụm C (centroid hệ Template), cap L_max = 640 (px hệ Input sau chiếu)
1. Tính s_k (cạnh vuông hệ Input, theo §2.2.5 bước 1–3).
2. Nếu s_k ≤ 640: RETURN [C] (không tách).
3. Nếu s_k > 640 (tối đa 2 lần đệ quy):
     tách C thành 2 mảnh bằng KMeans(k=2) trên centroid hệ Template
     (khởi tạo k-means++ , seed cố định 0 — deterministic, xem test §6.2);
     đệ quy mỗi mảnh.
4. Mảnh nào sau 2 lần tách vẫn > 640: crop 640 tại tâm mảnh + cờ truncated=True.
```

Vì sao KMeans(k=2) mà không DBSCAN-lại với $\epsilon$ nhỏ hơn: bisection đảm bảo **kết thúc** (mỗi lần tách giảm đường kính) và deterministic; DBSCAN-lại có thể vẫn cho 1 cụm (không tiến triển). Ghi nhận số lần bisection/ảnh làm metric giám sát (nếu > 10% ảnh phải bisect → $\epsilon$ quá lớn, quay lại §3.2). Các mảnh sau bisection có crop chồng lấn được xử lý bởi Cross-Crop NMS §2.4.4 (AUDIT-05) — không lo double-count.

### 3.4. Validate $\epsilon$ bằng nhãn GT trên tập VALIDATION Board 05–12 (không unsupervised mù) [AUDIT-01/02]

> Chế độ chính $m = 1$ không sinh điểm ngoại lai (mọi blob đều thuộc 1 cụm — singleton $|C_k|=1$ là cụm hợp lệ, không bị loại). Phase 1 **không sử dụng bất kỳ chỉ số tỷ lệ nhiễu nào và không duyệt bất kỳ giá trị `min_samples` nào khác ngoài 1**; nghiệm thu bằng đúng 2 chỉ số dưới đây trên tập Validation (Board 05–12). Tuyệt đối không dùng GT 01/04 ở Phase 1 (Zero Data Leakage §3.1). [Chốt 1-2]

Sau khi có $\epsilon$ ứng viên (từ knee $1$-NN §3.2): đo trên tập validation Board 05–12 có GT (dùng đúng `voc_ap`/`pr_counts` IoU=0.5 của `evaluate.py` làm chuẩn đối chiếu GT-only):
- **Cluster Purity ≥ 85%:** % cụm đa phần tử $|C_k| \ge 2$ mà các blob thành viên thuộc cùng một lỗi GT hoặc nằm trong bán kính hợp lệ của cùng 1 lỗi (centroid blob rơi vào GT box mở rộng 10 px). Công thức: $Purity = N_{pure\_clusters} / N_{total\_multi\_clusters}$ (chỉ tính trên ảnh validation có ít nhất 1 GT; singleton trúng GT không tính vào tử số Purity đa phần tử nhưng được báo cáo riêng).
- **Over-clustering Rate ≤ 15%:** % cụm bị phình kích thước vượt `640 px` (cạnh vuông $s_k > 640$) **trước khi bisection** §3.3. Công thức: $OverClustering = N_{oversize\_clusters} / N_{total\_clusters}$. Ràng buộc này khóa chặt với trần an toàn §3.3 (tỷ lệ cụm vượt 640 px < 5% trước bisection ở mức §3.3 là điều kiện cần; ngưỡng nghiệm thu Phase 1 là ≤ 15%).
- **Fragmented-defect recall (chẩn đoán, tiền đề cho Nhóm 1 §5.1):** recall trên tập con validation Board 05–12 có GT bị vỡ mảnh (1 GT box sinh ≥ 2 blobs trong bán kính 250 px) — báo cáo tham khảo, không gate pass/fail ở Phase 1.
- **Clustered-defect recall (chẩn đoán, tiền đề cho Nhóm 2 §5.1):** recall trên tập con ảnh validation Board 05–12 có ≥ 2 GT boxes cách nhau < 300 px — báo cáo tham khảo; so sánh chính thức với baseline TOP-8 để dành cho Phase 5 trên tập test niêm phong 01/04.

---

## PHẦN 4 — LỘ TRÌNH TRIỂN KHAI THEO TỪNG NGÀY (PHASED IMPLEMENTATION ROADMAP)

> Nguyên tắc: **sandbox-first, đo-trước-viết-sau, không số liệu bịa.** Mọi con số latency/recall trong báo cáo cuối cùng đều từ chạy thực tế. Mỗi Phase kết thúc bằng acceptance criteria đo được; không đạt thì dừng và xin chỉ thị (noi theo §6.2–6.3 plan gốc).

### Phase 1 — Script hiệu chuẩn tham số & phân tích phân bố blob (2–3 ngày)

**Mục tiêu:** có $\epsilon_{optimal}$ (chế độ chính `min_samples = 1`), $K_{raw}$ bằng số liệu trên tập VALIDATION, chưa đụng đến inference. [AUDIT-01/02]

| Hạng mục | Chi tiết |
|---|---|
| File tạo mới (trong project mới) | `scripts/calibrate_dbscan.py`, `scripts/blob_stats.py`, `results/calibration/k_distance_val_boards05_12.png`, `results/calibration/calib_report.json` [AUDIT-02/Chốt 1-2] |
| Đầu vào | Blob $K_{raw}=16$ từ pipeline gốc chạy READ-ONLY từ `LEGACY_PKU_ROOT` trên Train/Validation Boards 05–12 (đặc biệt Validation Board 07) — cả 2 biến thể Standard + Camera Perturbation, seed 20261023. Test 01/04 (Zero-defect-training Boards) KHÓA HOÀN TOÀN trong Phase 1–4, chỉ mở đánh giá 1 lần ở Phase 5 (Zero Data Leakage) [AUDIT-02/Chốt 1-2] |
| Công việc | (1) Thu thập centroid/$Z$/area toàn tập Board 05–12; (2) tính $1$-NN $d_i^{(1)}=\min_{j\neq i} d_{ij}$ trên $D_w$ + Kneedle → $\epsilon_{optimal}$ (giữ $k\in\\{2,3\\}$ làm 2 đường tham khảo phụ, CẤM `label=-1` loại Singleton); (3) quét lưới $\epsilon$ với `min_samples=1`, đo Purity/Over-clustering bằng GT validation Board 05–12; (4) plateau check ±20%; (5) chốt $(\epsilon, K_{raw})$ + fallback $\epsilon=150$ + đóng băng calib_report.json [AUDIT-01/02/Chốt 1-2] |
| Đầu ra | `calib_report.json`: `{eps, min_samples(=1), k_raw, knee_1nn_value, knee_ref_k2_k3, plateau_ok, purity, over_clustering_rate, frag_recall_diag, clustered_recall_diag, frozen_sha256, fallback_used}` — file này là **single source of truth** mọi Phase sau đọc (không hardcode tham số trong code) |
| Acceptance | Knee-plot $1$-NN (`k_distance_val_boards05_12.png`) có điểm uốn xác định HOẶC fallback được ghi rõ; Cluster Purity ≥ 85% + Over-clustering Rate ≤ 15% (đo trên Board 05–12, định nghĩa §3.4); figure PNG tồn tại và reproducible (seed cố định, chạy 2 lần cho cùng JSON trừ timestamp/hash freeze) [AUDIT-01/02/Chốt 1-2] |

**Quyết định kiến trúc phải chốt trong Phase 1:** import pipeline gốc bằng cách nào — (A) `sys.path` trỏ READ-ONLY sang project gốc (rủi ro: vô tình ghi), hay (B) copy nguyên văn 3 file (`board_id_and_align.py`, `tolerance_diff.py`, `yolo_detector.py`) vào project mới rồi đóng băng (khuyến nghị B — cô lập hoàn toàn, đúng tinh thần sandbox; ghi rõ hash SHA256 bản copy trong `calib_report.json` để truy xuất nguồn).

### Phase 2 — Module `adaptive_cluster_cropper.py` + `cluster_gating.py` (2–3 ngày)

**Mục tiêu:** hai module thuần hình học (chưa cần GPU), test được bằng CPU.

| Hạng mục | Chi tiết |
|---|---|
| File tạo mới | `src/cluster/__init__.py`, `src/cluster/dbscan_clustering.py` (§2.2: trọng số Z, ma trận $D_w$, DBSCAN, bisection), `src/cluster/adaptive_cluster_cropper.py` (§2.2.5: hộp bao + chiếu $H^{-1}$ + vuông hóa + clamp/shift biên + quantize bucket), `src/cluster/cluster_gating.py` (§2.4: Union-of-Disks chính + Bounding-Margin ablation + adapter `fuse`) |
| Contract IO (bắt buộc) | `cluster_blobs(blobs_tpl, eps, min_samples=1) -> clusters` (chính: Union-Find Single-Linkage, mọi blob thuộc 1 cụm; ablation m≥2 trả kèm noise mask) [AUDIT-01]; `clusters_to_crops(clusters, blobs_tpl, h_inv, img_shape, max_cluster_crops=8) -> [(crop_bgr, origin, bucket, cluster_id, z_cluster, truncated)]` (sắp xếp mọi cụm — gồm Multi-blob Clusters và Singletons — theo $Z_{cluster}(C_k)=\max_{i\in C_k} z_i$ giảm dần và cắt cứng tối đa `MAX_CLUSTER_CROPS=8` cụm trước khi sinh ảnh crop) [Chốt 3]; `union_of_disks_gate(dets_crop, cluster_blob_centers_input, r_base=80, diff_boxes) -> kept`; `cross_crop_nms(dets_input, iou_thres=0.50) -> deduped_dets` (class-aware; chạy ngay sau `union_of_disks_gate` và trước khi đưa vào `ensemble.fuse`) [Chốt 5/AUDIT-05]; mọi tọa độ ra ở hệ ảnh gốc |
| Acceptance | Toàn bộ unit test §6.2–§6.4 (gồm T-GAT-06 NMS) pass; micro-benchmark gating < 1 ms + NMS < 1 ms (đo thực, ghi vào report); roundtrip hình học sai số < 1e-3 px trên H tổng hợp (noi theo `test_project_points_roundtrip_zero_error` gốc) |

### Phase 3 — Module suy luận Batch Bucket / Letterbox (1–2 ngày)

**Mục tiêu:** `batch_bucket.py` cài đặt đầy đủ 3 chế độ A1 (`uniform_context_640`, 1-call, đường chính khuyến nghị) + A2 (`multi_bucket_384_512_640`, ≤3-call) + B (`letterbox_640`, baseline đối chứng), đúng đặc tả §2.3. [Chốt 4/AUDIT-04]

| Hạng mục | Chi tiết |
|---|---|
| File tạo mới | `src/cluster/batch_bucket.py` cài đặt đầy đủ 3 chế độ [Chốt 4/AUDIT-04]: (A1 `uniform_context_640` — đường chính khuyến nghị: mọi cụm $s_k\le640$ px lấy cửa sổ ngữ cảnh $640\times640$ px trực tiếp trên ảnh gốc, giữ nguyên scale 1:1, gom vào đúng 1 lần gọi GPU `predict_batch(imgsz=640, rect=True, half=True)` duy nhất); (A2 `multi_bucket_384_512_640`: chia sub-batch theo 3 bucket `{384, 512, 640}`, tối đa 3 lần gọi GPU); (B `letterbox_640`: pad xám 114 về $640\times640$, baseline đối chứng); `to_input_coords` tịnh tiến thuần túy |
| Công việc | (1) implement A1 (`uniform_context_640`, 1-call); (2) implement A2 (multi-bucket `{384,512,640}`, ≤3-call); (3) implement B (`letterbox_640` pad xám 114, baseline đối chứng); (4) đo số lần gọi GPU/ảnh + thời gian từng chế độ trên Board 05–12 (timing unsupervised — không dùng GT nên không leak; chọn chế độ để dành Phase 5 trên test 01/04); (5) assert `rect=True, half=True` trong mọi lần gọi (noi Chốt 3 gốc) [AUDIT-02/04/Chốt 4] |
| Acceptance | Test §6.3, §6.5 pass (A1-center_window + A2-bucket/quantize + B-letterbox T-GEO-05 unpad); overhead đo thực: A1 = 0 ms launch-phụ, A2 ≤ 30 ms worst-case, B = 1-call; zero-resize/scale-1:1 chứng minh bằng log shape (`imgsz` == cạnh crop, `rect=True, half=True`) [AUDIT-04/Chốt 4] |

### Phase 4 — Tích hợp pipeline Dual-Path cục bộ của project mới (2 ngày)

**Mục tiêu:** `run_dualpath_clustered()` chạy end-to-end trong project mới, không chạm project gốc.

| Hạng mục | Chi tiết |
|---|---|
| File tạo mới | `scripts/evaluate_clustered.py` (fork `run_dualpath` gốc: thay đoạn "cents → 8 crop 640 → 1 batch → gating đơn" bằng "blobs → DBSCAN(m=1, eps frozen) → Z_cluster top-8 → adaptive crops (mode A1/A2/B qua flag `--crop-mode`) → bucket batches → union-gating → cross_crop_nms(IoU=0.50) → `fuse_and_decide` + fallback_single [AUDIT-01/03/04/05]"), `src/ui/app_clustered.py` (tối thiểu: hiển thị hộp bao cụm + bucket để demo; không fork toàn bộ UI gốc trừ khi cần) |
| Thứ tự tích hợp (an toàn) | (1) chạy Nhánh B gốc trong project mới → khớp số blob với project gốc ±0 (sanity); (2) bật DBSCAN(m=1, eps frozen) → so số crop (<=8 sau Z_cluster) vs 8; (3) bật batch lần lượt A1 → A2 → B (flag `--crop-mode`) → so dets trước/sau gating; (4) bật union-gating → cross_crop_nms → fusion + luật 4 mức → kết luận cuối [AUDIT-05] |
| Acceptance | Chạy được trên ≥ 6 ảnh mẫu (đủ 6 lớp lỗi) không exception; tọa độ det cuối ở hệ ảnh gốc (spot-check trực quan 2 ảnh); NMS chèn sau gating trước fuse (log số det trước/sau NMS); fallback_single khi ảnh chỉ 1 blob/singleton (log `fallback_single=True`, bit-identical TOP-8); mọi exception cluster → fallback TOP-8 + dòng fallback_log.jsonl [AUDIT-01/05] |

### Phase 5 — A/B Benchmark đối chứng trực tiếp với `PCB_DualPath_AOI` (2–3 ngày)

**Mục tiêu:** bảng số liệu đối chứng công bằng, cùng tập test, cùng metric, cùng seed.

| Hạng mục | Chi tiết |
|---|---|
| Thiết kế thí nghiệm | Cùng tập: Board 01/04 standard + camera (seed 20261023, cùng `perturb_image_and_boxes`); cùng YOLO weights (`yolo11n_day2.pt` hoặc base — ghi rõ); cùng IoU=0.5, conf=0.20. Ma trận 5 cấu hình chuẩn trên Board 01/04 (Standard + Camera Perturbation) [Chốt 4/AUDIT-04/05]: `Config 0 (Baseline)`: TOP-8 cố định + Crop 640 + Single-centroid gating; `Config 1 (Proposed Main)`: DBSCAN ($m=1$, $K_{raw}=12$, Cap=8) + **A1 (`uniform_context_640`, 1 GPU call)** + **U (`Union-of-Disks` + Cross-Crop NMS)**; `Config 2 (Ablation Bucket)`: DBSCAN + **A2 (`multi_bucket_384_512_640`)** + **U (`Union-of-Disks` + Cross-Crop NMS)**; `Config 3 (Ablation Letterbox)`: DBSCAN + **B (`letterbox_640`)** + **U (`Union-of-Disks` + Cross-Crop NMS)**; `Config 4 (Ablation Gating)`: DBSCAN + A1 + **H (`Cluster Bounding-Margin Gating`)** |
| Metric (xem §5.1–§5.2) | AP50, recall toàn tập, **clustered-defect recall** (chính), **fragmented-defect merging recall** (gom mảnh vỡ, tận dụng slot rank9-12) [AUDIT-06], precision, FAR GOOD, latency P50/P95 toàn pipeline + breakdown, **crops/ảnh + lần gọi GPU/ảnh (giảm vs 8/1)** [AUDIT-06], tỉ lệ bisection/truncated, tỉ lệ fallback_single. Thống kê: ΔTP + Bootstrap 95% CI (paired per-image, 10000 resample) + Wilcoxon signed-rank (p<0.01) cho crops/latency; không dùng gate cứng theo kiểm định tham số cặp đôi cũ trên cỡ mẫu nhỏ [Chốt 6/AUDIT-06] |
| Acceptance | Bảng `results/ab_benchmark.csv` đầy đủ 5 cấu hình × 2 scenario + metadata (seed, weights-hash, timestamp, calib frozen_sha256); mọi ô số từ chạy thực trên test niêm phong 01/04; latency P95 ≤ 450 ms HOẶC báo cáo vi phạm trung thực + đề xuất tối ưu; kết luận A1 vs A2, U vs H bằng số liệu (ΔTP + CI + Wilcoxon), không bằng cảm tính [AUDIT-04/06] |

---

## PHẦN 5 — TIÊU CHÍ ĐÁNH GIÁ & RỦI RO THẤT BẠI (SUCCESS METRICS & RISK MITIGATION)

### 5.1. Metric chính (primary) và phụ (secondary)

**Primary — 2 nhóm mục tiêu thực tế trên mạch PCB (metric thành công của Hướng 4) [Chốt 6/AUDIT-06]:**
- *Nhóm 1 — Fragmented Single-Defect & Top-K Slot Recovery:* các vết lỗi bị `tolerance_diff` phân mảnh thành ≥ 2 blobs trong bán kính 250 px. Kiểm chứng việc gom nhiều mảnh vỡ thành 1 cụm giúp giải phóng suất crop để cứu các lỗi thật nằm ở hạng 9–12 (vốn bị TOP-8 cố định vứt bỏ). Metric: $Recall_{frag} = TP_{frag} / N_{frag}$ (TP theo chuẩn `pr_counts` gốc, IoU ≥ 0.5, đúng lớp).
- *Nhóm 2 — Spatially Proximate GT Defects:* các ảnh có ≥ 2 hộp GT cách nhau < 300 px. Metric: $Recall_{clustered} = TP_{clustered} / N_{clustered}$ (cùng chuẩn `pr_counts`, IoU ≥ 0.5, đúng lớp).
- *Thống kê (thay thế hoàn toàn điều kiện cứng cũ theo cỡ mẫu nhỏ 36 ảnh Board 01/04):* báo cáo **$\Delta TP$ (số lỗi cứu thêm tuyệt đối)** cho từng nhóm + **Recall / AP50 kèm khoảng tin cậy Bootstrap 95% CI (10,000 lần lấy mẫu lặp, paired per-image)**; kiểm định **Wilcoxon signed-rank test (p < 0.01)** chứng minh giảm có ý nghĩa thống kê **số lượng crop trung bình/ảnh (Crops/image)** và giữ **Latency P95 ≤ 450 ms** trên toàn bộ 72 mẫu đánh giá (36 ảnh × 2 scenarios: Standard + Camera Perturbation).
- *Tiêu chí pass:* $\Delta TP$ dương ở cả Nhóm 1 và Nhóm 2 + Bootstrap 95% CI không chứa 0 + Wilcoxon p < 0.01 cho Crops/image + P95 ≤ 450 ms (xem §5.2).

**Secondary (không được hy sinh):**
| Metric | Định nghĩa | Ngưỡng |
|---|---|---|
| Recall toàn tập | `pr_counts` IoU=0.5, board 01/04 | Không thấp hơn baseline quá 2 điểm % (non-inferiority) |
| Precision / FAR | FP trên defect; FAR = % GOOD bị kết luận ≠ PASS | FAR không tăng quá 1 điểm % vs baseline |
| Latency P95 toàn pipeline | Từ ảnh vào đến kết luận 4 mức | **≤ 450 ms** (ràng buộc cứng từ yêu cầu) |
| Fragmented-defect merging recall [AUDIT-06] | Recall trên GT bị vỡ mảnh (1 GT sinh ≥ 2 blobs): DBSCAN gom lại đúng 1 box (tận dụng slot rank9–12 mà TOP-8 vứt bỏ) | Cao hơn baseline (báo cáo ΔTP + Bootstrap 95% CI) |
| Số crop TB/ảnh + lần gọi GPU TB/ảnh (giảm tải) [AUDIT-06] | Hiệu quả tính toán (Z_cluster top-8, MAX_CLUSTER_CROPS=8) | Thấp hơn hoặc bằng baseline (8 crop / 1 gọi) trong ≥ 80% ảnh; báo Wilcoxon signed-rank cho crops/ảnh + latency |
| Tỉ lệ truncated/bisect | % cụm vượt cap | < 5% (nếu không, xem lại $\epsilon$) |

### 5.2. Phương pháp so sánh công bằng (tránh "thắng do may")

1. **Paired design + thống kê hiện đại:** mọi ảnh chạy cả hai pipeline với cùng seed perturb, cùng weights, cùng conf/IoU → so sánh từng ảnh: **Bootstrap 95% CI (10,000 resample, paired per-image)** cho Recall/AP50/F1; **Wilcoxon signed-rank test (p < 0.01)** cho số lượng crop/ảnh và latency. Không sử dụng gate cứng theo kiểm định cặp đôi cũ trên cỡ mẫu nhỏ [Chốt 6/AUDIT-06].
2. **Tách scenario:** standard và camera báo cáo riêng (kế thừa quy tắc "không trộn số" của plan gốc §4.2).
3. **Ablation ma trận:** 5 cấu hình Phase 5 (Config 0–4) cho phép quy công lao đúng chỗ (DBSCAN vs bucket vs gating) — cấm kết luận "DBSCAN tốt hơn" nếu thắng lợi thực ra đến từ bucket-crop nhỏ hơn (phân tích bằng cách giữ gating cũ + chỉ đổi crop, và ngược lại).
4. **Quy tắc trung thực số liệu** (kế thừa nguyên văn tinh thần plan gốc): không ghi target thành kết quả; mọi bảng có cột "nguồn" (chạy mới / trích gốc); seed + weights-hash + timestamp trong metadata.

### 5.3. Rủi ro & giảm thiểu

| # | Rủi ro | Dấu hiệu sớm | Giảm thiểu |
|---|---|---|---|
| R1 | $\epsilon$ quá lớn → crop khổng lồ, mất độ phân giải vi mô | Tỉ lệ bisect > 10% (Phase 1/5) | Cap 640 + bisection (§3.3); plateau check ±20%; fallback $\epsilon=150$ |
| R2 | $\epsilon$ quá nhỏ → phân mảnh như blob đơn lẻ (không khác baseline) | Số cụm ≈ số blob trong > 80% ảnh | Knee-plot + purity check; nếu knee mờ, ưu tiên $\epsilon$ lớn hơn trong plateau |
| R3 | Metric weighted phá vỡ cấu trúc cụm (không thỏa tam giác) | Purity weighted < purity Euclidean | So sánh trực tiếp Phase 1; **quay về Euclidean thuần** nếu weighted không thắng (đã dự liệu §2.2.2) |
| R4 | Batch nhiều lần gọi GPU vượt ngân sách latency | P95 > 450 ms | Ưu tiên A1 (1-call, overhead launch-phụ = 0) nếu A1 non-inferior về recall; nếu dùng A2: gộp bucket hiếm (upscale crop nhỏ vào bucket lớn kề — quyết bằng đo), worst-case ≤ 30 ms đã đo ở Phase 3; giảm $K_{raw}$ nếu cần [AUDIT-04] |
| R5 | Union-gating tăng FP (gate rộng hơn) | FAR tăng > 1 điểm % | Giữ $R_{base}=80$ (không nới); Cross-Crop NMS (IoU=0.50, class-aware) sau gating hấp thụ FP trùng lặp do bisection/crop chồng lấn [AUDIT-05]; ablation vs Bounding-Margin xác nhận trade-off |
| R6 | Lệch tọa độ hệ Template/Input (bug hình học) | Test hồi quy §6.1 fail | Roundtrip test < 1e-3 px bắt buộc trước mọi Phase tích hợp; spot-check trực quan |
| R7 | Ô nhiễm project gốc (vi phạm sandbox) | Mọi write ngoài project mới | Mọi script dùng `assert_writable_ok` (copy pattern từ `resource_paths.py` gốc); pre-commit check `git status` project gốc rỗng; Phase 1 quyết định copy-đóng-băng thay vì sys.path trỏ sang |
| R8 | Rò rỉ test-set (dùng 01/04 hiệu chuẩn) [AUDIT-02/Chốt 1] | calib_report chứa board 01/04 | Mọi bước hiệu chuẩn chỉ trên Board 05–12 (kể cả knee-plot); test 01/04 khóa đến Phase 5; đóng băng calib_report.json (frozen_sha256) trước khi mở test |

### 5.4. Cơ chế Fallback về Single-Centroid (bắt buộc)

Kích hoạt **tự động, từng ảnh** (không phải cờ toàn cục) khi bất kỳ điều kiện nào đúng:
1. Chế độ chính m=1 KHÔNG sinh noise [AUDIT-01] — trường hợp "toàn noise" chỉ xảy ra ở ablation m≥2; khi đó mỗi blob noise thành cụm đơn → đúng pipeline cũ (crop theo blob, gating đơn $R=80$).
2. Mọi cụm đều là singleton $|C_k| = 1$ (board chỉ có lỗi cô lập) — đây là đường chính hợp lệ, KHÔNG phải fallback: Union-of-Disks suy biến về gating gốc (§2.4.2) — code đi đường tắt `if n == 1: single_gate()` để bit-identical với baseline.
3. Bất kỳ exception nào trong module cluster (metric NaN, $H^{-1}$ suy biến) → catch, log `fallback_reason`, chạy đường TOP-8 gốc (đảm bảo pipeline không bao giờ crash vì Hướng 4 — tính sẵn sàng công nghiệp).
4. Mọi lần fallback ghi log `{image_id, reason, n_blobs}` vào `results/fallback_log.jsonl` — metric giám sát: tỉ lệ fallback > 30% nghĩa là DBSCAN không phù hợp tập này (xem lại $\epsilon$).

---

## PHẦN 6 — KẾ HOẠCH KIỂM THỬ ĐƠN VỊ & KIỂM ĐỊNH HỒI QUY (UNIT & REGRESSION TESTS)

> Quy ước: `tests/test_cluster_*.py` trong project mới, chạy bằng `pytest -v`. Noi theo phong cách Verifier của project gốc (assert hằng số plan, `inspect.getsource` chống regression kiến trúc, roundtrip sai số, zero-touch guard). Không mock quá đà: test hình học dùng H tổng hợp + ảnh tổng hợp deterministic (seed cố định).

### 6.1. Nhóm bảo vệ tọa độ (khối hình học — ưu tiên cao nhất)

| ID | Tên test | Nội dung & tiêu chí pass |
|---|---|---|
| T-GEO-01 | `test_cluster_bbox_in_template_before_project` | Tạo 3 centroid Template + H phối cảnh tổng hợp (shift/rotate/perspective như `test_project_points_roundtrip_zero_error` gốc); assert DBSCAN nhận đầu vào là centroid Template (chưa chiếu) — kiểm bằng thứ tự gọi (centroid chiếu chỉ xuất hiện sau khi có `cluster_id`) |
| T-GEO-02 | `test_hinv_roundtrip_subpixel` | Chiếu 4 góc hộp bao cụm qua $H^{-1}$ rồi ngược lại qua $H$; sai số max < 1e-3 px (kế thừa chuẩn gốc) |
| T-GEO-03 | `test_crop_clamp_shift_not_shrink` | Cụm sát biên ảnh (tâm cách biên < $S_k/2$): assert crop vẫn đủ $S_k \times S_k$ (trượt gốc, không co) và nằm hoàn toàn trong $[0,W]\times[0,H]$ |
| T-GEO-04 | `test_bucket_translate_only_no_scale` | Với mỗi bucket $s$: tạo det giả ở 4 góc crop, `to_input_coords` phải bằng cộng gốc thuần túy; assert source của `to_input_coords` **không chứa `*`/`/`** trên tọa độ (noi `inspect.getsource` của Verifier gốc) |
| T-GEO-05 | `test_letterbox_unpad_inverse` | (Cho fallback B) `pad → unpad` là đồng nhất: `unpad(pad(bbox)) == bbox` ± 1e-6; det có tâm trong vùng pad bị loại 100% |

### 6.2. Nhóm gom cụm & hiệu chuẩn

| ID | Tên test | Nội dung & tiêu chí pass |
|---|---|---|
| T-CLU-01 | `test_weighted_distance_properties` | Đối xứng $d_{ij}=d_{ji}$, $d_{ii}=0$, không âm; blob nhiễu ($z \ll z_0$) có $d$ đến blob thật **lớn hơn** Euclidean thuần (kiểm đúng hướng tác dụng của trọng số) |
| T-CLU-02 | `test_dbscan_two_clusters_plus_noise` | Chế độ chính m=1 [AUDIT-01]: 2 cụm multi-blob (3–4 điểm, cách nhau 400 px) + 1 singleton cô lập xa 800 px → assert đúng 2 multi-blob clusters + 1 isolated singleton (KHÔNG noise) với $\epsilon$ frozen; ablation m=2 trên cùng dữ liệu → singleton thành noise (minh họa nghịch lý cũ); assert phụ (Chốt 3): truyền $K_{raw}=12$ blobs rời rạc đôi một cách nhau > $\epsilon$ vào `clusters_to_crops(..., max_cluster_crops=8)` → chỉ trả về đúng 8 crops có $Z_{diff}$ cao nhất (xếp hạng $Z_{cluster}$ giảm dần + cắt cứng `MAX_CLUSTER_CROPS=8`) |
| T-CLU-03 | `test_dbscan_deterministic_and_sklearn_free_fallback` | Chạy 2 lần cùng seed cho cùng labels (KMeans bisection seed cố định); nếu thiếu sklearn, fallback Union-Find cho cùng kết quả trên case T-CLU-02 |
| T-CLU-04 | `test_bisection_terminates_and_caps` | Cụm tổng hợp đường kính 1500 px → sau bisection mọi mảnh ≤ 640 hoặc có cờ `truncated`; số lần đệ quy ≤ 2; log bisect được ghi |
| T-CLU-05 | `test_calib_report_schema_and_reproducibility` | `calib_report.json` chứa đủ `{eps, min_samples(=1), k_raw, knee_1nn_value, knee_ref_k2_k3, plateau_ok, purity, over_clustering_rate, frag_recall_diag, clustered_recall_diag, frozen_sha256, fallback_used}` [AUDIT-01/02/Chốt 1-2]; chạy `calibrate_dbscan.py` 2 lần cho JSON bit-identical (trừ timestamp/hash freeze) |

### 6.3. Nhóm batch inference

| ID | Tên test | Nội dung & tiêu chí pass |
|---|---|---|
| T-BAT-01 | `test_quantize_buckets` | (A2) $s \le 384 \to 384$; $385$–$512 \to 512$; $513$–$640 \to 640$; $> 640 \to 640$ + cờ cap (assert biên từng pixel); (A1) `center_window` mọi cụm dù 200 hay 600 px đều ra cửa sổ 640×640 nằm trọn ảnh (clamp/shift, không resize) [AUDIT-04] |
| T-BAT-02 | `test_group_by_bucket_max_three_calls` | (A2) 7 crop hỗn hợp 3 bucket → đúng 3 nhóm, nhóm rỗng bỏ; mock `predict_batch` đếm ≤ 3 lần gọi, `imgsz` == cạnh bucket; (A1) 7 crop bất kỳ size → đúng 1 lần gọi `predict_batch(imgsz=640)` [AUDIT-04] |
| T-BAT-03 | `test_rect_true_every_call` | Assert source `batch_bucket.py` chứa `rect=True` trong mọi đường gọi `predict` (noi Chốt 3 gốc) |
| T-BAT-04 | `test_batch_empty_and_single` | 0 crop → `[]` không gọi GPU; 1 crop → 1 gọi đúng bucket (không crash, không pad thừa) |

### 6.4. Nhóm gating đa tâm

| ID | Tên test | Nội dung & tiêu chí pass |
|---|---|---|
| T-GAT-01 | `test_union_single_blob_equals_legacy` | Cụm 1 blob: `union_of_disks_gate` cho kết quả **bit-identical** với `YOLODetector.spatial_center_gating` gốc trên cùng input (tương thích ngược — chạy với cả `require_overlap=True/False`) |
| T-GAT-02 | `test_union_rescues_edge_detection` | Cụm 3 blob thẳng hàng cách nhau 120 px; det khớp blob rìa (cách centroid cụm ~120 px > 80): gating đơn (centroid) **loại**, union-gating **giữ** (tái hiện đúng bug §2.4.1 và chứng minh fix) |
| T-GAT-03 | `test_union_monotonic_no_new_fn` | Thêm blob vào cụm không bao giờ loại det đã giữ (tính đơn điệu §2.4.2) — fuzz 100 case ngẫu nhiên (seed cố định) |
| T-GAT-04 | `test_gating_latency_under_1ms` | Micro-benchmark $M=50$ dets × $n=12$ blobs, 200 lặp: P95 < 1 ms (ghi số thực vào report; fail nếu vượt — buộc tối ưu vector hóa) |
| T-GAT-05 | `test_margin_gate_ablation_exists` | Bounding-Margin implement đúng công thức $\mathbf{H}_C$ + $\Delta=40$ (dùng cho ablation Phase 5, không phải đường chính) |
| T-GAT-06 | `test_cross_crop_nms_deduplicates_bisected_overlap` | Tạo 2 mảnh crop chồng lấn từ một cụm bị bisection (hoặc 2 cụm gần nhau) cùng phát hiện 1 vết lỗi tại vùng giao với IoU = 0.75 cùng `class_id`; assert `cross_crop_nms(iou_thres=0.50)` gộp còn đúng 1 box có `conf` cao nhất, không gây nhân đôi False Positive |

### 6.5. Nhóm tích hợp & hồi quy pipeline

| ID | Tên test | Nội dung & tiêu chí pass |
|---|---|---|
| T-E2E-01 | `test_evidence_item_schema_compatible` | Item từ pipeline cụm chứa đủ trường `ensemble.fuse`/`logic.decide` cần (`class_id, conf, source, bbox, z_diff, area`) + `cluster_id` thừa bị `decide` bỏ qua (không crash) |
| T-E2E-02 | `test_fallback_single_centroid_path` | Ảnh tổng hợp 1 blob (singleton m=1 — đường chính, KHÔNG phải noise) → log `fallback_single=True` + đường tắt single_gate, kết quả khớp pipeline gốc bit-identical (TOP-8 cùng input) [AUDIT-01] |
| T-E2E-03 | `test_fallback_on_cluster_exception` | Ép DBSCAN raise (metric NaN) → pipeline vẫn ra kết luận 4 mức qua đường fallback + `fallback_log.jsonl` có dòng reason |
| T-E2E-04 | `test_no_write_outside_new_project` | Mọi output path trong code mới qua `assert_writable_ok` (copy guard từ project gốc); test raise `PermissionError` khi trỏ ra ngoài project mới; `git status` project gốc rỗng sau full test-suite (kiểm thủ công trước Phase 5) |
| T-E2E-05 | `test_six_class_smoke` | Chạy `evaluate_clustered` trên 6 ảnh mẫu validation (đủ 6 lớp, Board 07 — KHÔNG dùng test 01/04 ở smoke test) [AUDIT-02] không exception, kết luận thuộc {FAIL, REWORK, REVIEW, PASS} |

### Ma trận truy xuất yêu cầu → test (Requirement Traceability)

| Yêu cầu trong đề bài | Thiết kế trong plan | Test bao phủ |
|---|---|---|
| Batch variable-size: So sánh A vs B, chọn 1, hàm ánh xạ bbox | §2.3 (chọn A, B fallback) + §2.3.4 | T-GEO-04, T-GEO-05, T-BAT-01→04 |
| Multi-centroid gating: 1 trong 2 cơ chế, vectorized < 1 ms | §2.4 (chọn U, H ablation) + NMS §2.4.4 | T-GAT-01→06 |
| Calibration $\epsilon$/`min_samples`: pre-calib script, knee-plot, cap 640 + bisection | §3 + §3.3 | T-CLU-02, 04, 05 (schema mới Purity/Over-clustering/frozen_sha256) [AUDIT-01/02] |
| 6 chốt kiểm toán (AUDIT-01–06) | §2.1–§2.5, §3.1–§3.4, Phase 1–5, §5.1–§5.4, §6 | T-CLU-02 (singleton), T-CLU-05 (schema), T-BAT-01/02 (A1/A2), T-GAT-06 (NMS), T-E2E-02/05 (fallback/validation-smoke) |
| 6 phần tài liệu | Toàn bộ plan này | — (review thủ công) |

---

## PHỤ LỤC A — CẤU TRÚC THƯ MỤC DỰ ÁN MỚI (DỰ KIẾN)

```
D:\FPTU\KLTN\PCB_DualPath_Clustered_AOI\
+-- plan.md                        <- Tài liệu này
+-- configs/
|   +-- resource_paths.py          <- Fork từ gốc, PROJECT_ROOT trỏ project mới
|                                    + assert_writable_ok (sandbox guard)
+-- src/
|   +-- cluster/
|   |   +-- __init__.py
|   |   +-- dbscan_clustering.py   <- Phase 2 (§2.2 + bisection §3.3)
|   |   +-- adaptive_cluster_cropper.py  <- Phase 2 (§2.2.5)
|   |   +-- cluster_gating.py      <- Phase 2 (§2.4: U chính + cross_crop_nms [AUDIT-05] + H ablation)
|   |   +-- batch_bucket.py        <- Phase 3 (§2.3: A1 1-call + A2 ≤3-call + B letterbox=Config3 [AUDIT-04/Chốt 4])
|   +-- reference/                 <- Copy ĐÓNG BĂNG từ gốc (ghi SHA256) HOẶC
|   +-- models/                       import READ-ONLY (quyết định Phase 1)
|   +-- decision/
+-- scripts/
|   +-- calibrate_dbscan.py        <- Phase 1 (§3.2)
|   +-- blob_stats.py              <- Phase 1 (§3.1)
|   +-- evaluate_clustered.py      <- Phase 4 (fork run_dualpath)
+-- tests/
|   +-- test_cluster_geometry.py   <- T-GEO-01→05
|   +-- test_cluster_dbscan.py     <- T-CLU-01→05
|   +-- test_cluster_batch.py      <- T-BAT-01→04
|   +-- test_cluster_gating.py     <- T-GAT-01→06 (gồm T-GAT-06 NMS) [AUDIT-05]
|   +-- test_cluster_e2e.py        <- T-E2E-01→05
+-- results/
|   +-- calibration/               <- knee-plot PNG + calib_report.json
|   +-- ab_benchmark.csv           <- Phase 5
|   +-- fallback_log.jsonl         <- §5.4
+-- assets/                        <- Symlink/copy READ-ONLY weights + templates
                                       01/04 (KHÔNG copy toàn bộ nếu không cần)
```

## PHỤ LỤC B — QUY TẮC KẾ THỪA TỪ PROJECT GỐC (KHÔNG THƯƠNG LƯỢNG)

1. **Chốt 1 (Coordinate Sync):** mọi centroid/hộp bao Template phải qua $H^{-1}$ trước khi crop/IoU — áp dụng cho cả hộp bao cụm (§2.2.5 bước 2).
2. **Chốt 2 (Sigma Floor):** $Z_{diff}$ giữ `SIGMA_FLOOR = 5.0`; trọng số sigmoid dùng $z_0 = 2.0$ (ngưỡng REVIEW 3a) — không phát minh ngưỡng mới khi chưa hiệu chuẩn.
3. **Chốt 3 (Latency Guardrail):** mọi `model.predict` đều `rect=True, half=True` (CUDA); không vòng lặp `for`-predict; số lần gọi GPU/ảnh ≤ 3; A1 = 1 gọi `predict_batch(imgsz=640)` (overhead 0), A2 ≤ 3 gọi (worst-case ≤ 30 ms đo thực) [AUDIT-04].
4. **Thuật ngữ:** 01/04 là "Zero-defect-training Boards" — cấm cụm "unseen boards".
5. **Trung thực số liệu:** không target thành kết quả; tách scenario standard/camera; mọi bảng có cột nguồn + seed + weights-hash.
6. **Sandbox:** không ghi/xóa/sửa bất kỳ file nào trong `PCB_DualPath_AOI`; mọi output trong project mới; `assert_writable_ok` cho mọi đường ghi.

---

*Hết kế hoạch. Bước tiếp theo (ngoài phạm vi lượt này): thực hiện Phase 1 sau khi người dùng phê duyệt plan.md này.*
