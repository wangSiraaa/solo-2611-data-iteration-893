# 独立推车轮廓目录与连续动作核查库

仅处理**合成平面轨迹**：轴对齐矩形轮廓（正有理数半宽/半高），姿态固定，
推车中心随时间分段线性平移。不做旋转、加速度、避障、自动改轨，不控制
任何实体设备。所有数值按精确有理数（`fractions.Fraction`）处理，拒绝
二进制浮点与非法输入。

## 严格合同

两辆车在时刻 t 有**正面积重叠**，当且仅当两个方向同时严格满足：

- `|x_a(t) - x_b(t)| < half_width_a  + half_width_b`
- `|y_a(t) - y_b(t)| < half_height_a + half_height_b`

只碰边（一个方向取等号）或只碰角（两个方向取等号）**不算重叠**，根点
一律按开端点排除。核查域是半开区间 `[h0, h1)`，每辆轨迹必须恰好覆盖
该域（首结点 = h0、末结点 = h1），无外推、缺段、瞬移；时间结点严格递增。

## 模块

| 文件 | 职责 |
| --- | --- |
| `cart_audit/rational.py` | 精确有理数解析；JSON 浮点字面量直接拒绝 |
| `cart_audit/models.py` | `Profile / Knot / Trajectory / MotionSegment` 与合法性校验 |
| `cart_audit/intervals.py` | 带开闭端点的时间区间、求交、同对区间并集合并 |
| `cart_audit/catalog.py` | SQLite 版本化轮廓目录 + 当前轨迹存储（修订冻结、异文拒绝） |
| `cart_audit/service.py` | **独立轮廓服务**：单对线性动作段精确求交，无采样、无浮点 |
| `cart_audit/library.py` | 共同细分核查、证据保存、批次报告、幂等与失败不发布 |
| `cart_audit/verifier.py` | 用报告冻结快照独立重算并比对（篡改可检出） |
| `cart_audit/runner.py` | `operations.jsonl` 操作流（audit / revise_profiles / read_report） |
| `demo.py` | materials/728 主场景 + 六类边界场景端到端演示 |
| `tests/test_contract.py` | 27 个契约级测试（含 20 车 × 100 段规模用例） |

## 核查方法

按车对逐段归并（题目允许的两种方案之一；扫描线候选筛选不改变严格接触
合同，本实现选择在稀疏轨迹下边界组织更直接的共同细分方案）：

1. 取两车落在请求时段内的全部时间结点并集，形成共同细分网格；
2. 对每个细分 cell 各定位一辆车的一个线性动作段，调用轮廓服务精确求交，
   结果再与半开 cell `[u, v)` 裁剪，防止越出请求时段；
3. 合并同对车重复/相邻区间（两端皆开的共享点不合并），累计重叠时长；
4. 每个合并区间保存贡献它的 cell、两车动作段下标与原始区间作为可独立
   复核的证据。

完全静止、零相对速度（同速恒距）走 `slope = 0` 精确分支；只在一个方向
接触时该方向解集为空，整体即不冲突。不使用固定采样步长，也不凭端点
分离判无冲突——A/B 两端分离但途中 `(4, 6)` 重叠即材料中的反例。

## 报告与版本

- 报告保存：轮廓版本及完整修订载荷、轨迹快照、冲突区间（含开闭端点）、
  每对累计重叠时长、可通过结论、逐 cell 证据、输入指纹与 UTC 时间。
- 同 `report_id` 同内容复读返回原报告；异文（不同车对、不同修订或不同
  快照）抛 `AuditRejected`，绝不覆盖。
- 轮廓修订只影响新报告；旧报告的快照保留原尺寸，`read_report` 与
  `verify_report` 均按冻结内容工作。
- 核查失败抛业务异常、整笔回滚并在 `audit_attempt` 留痕，`report` 表
  不产生任何“可通过”外观记录。

## 运行

```bash
python3 -m unittest tests.test_contract -v   # 27 个测试
python3 demo.py                               # 端到端演示，生成 cart_audit_demo.sqlite3
```

主场景（materials/728）手算对应关系：

- r1：A/B 横向中心差 `|10 - 2t| < 2` ⇒ `(4, 6)` 两端开，时长 2；
  A/C 的 y 向恒距 3 > 半高和 2，不冲突；
- r2（half_height 1→2）：A/C 半高和 4 > 3，整个 `[0, 10)` 冲突，时长 10；
  R1 仍保留 half_height=1 的原结论。

## 独立查询接口

- `library.query_profile(revision, profile_id)`：查冻结轮廓尺寸；
- `library.query_pair_segment(revision, a, b, seg_a, seg_b)`：一对单段
  在共同线性时段上的精确重叠；
- `library.audit_pair(revision, a, b, lo?, hi?)`：整车对（或子时段）核查；
- `library.read_report(report_id)` / `verify_report(library, report_id)`：
  读取整批车对结论、来源证据并独立复核。

## 存储

SQLite 表：`profile_revision / profile / horizon / cart / trajectory_knot`
（目录与轨迹），`report / report_pair / conflict / conflict_evidence /
audit_attempt`（报告与留痕）。有理数以规范分数字符串 `p/q` 落库，读出
还原为 `Fraction`，全程无浮点。
