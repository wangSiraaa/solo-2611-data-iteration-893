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
| `cart_audit/runner.py` | `operations.jsonl` 操作流（audit / revise_profiles / read_report / ink_voucher / read_voucher / allocate_paper / confirm_paper） |
| `cart_audit/ink.py` | **格面受墨量**：按格/印模/动作段精确积分出理数凭单，冻结与独立复核 |
| `cart_audit/paper.py` | **纸张配给**：闭区间门槛、字典序最优分配、原子确认、并发与旧印样 |
| `demo.py` | materials/728 主场景 + 六类边界场景端到端演示 |
| `demo_ink.py` | materials/ink 受墨量凭单与纸张配给端到端演示 |
| `tests/test_contract.py` | 27 个原核查契约级测试（含 20 车 × 100 段规模用例） |
| `tests/test_ink_paper.py` | 22 个受墨量与纸张配给契约级测试 |

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
python3 -m unittest tests.test_contract -v       # 27 个原契约测试
python3 -m unittest tests.test_ink_paper -v      # 22 个受墨量/配给测试
python3 demo.py                                   # 原端到端演示，生成 cart_audit_demo.sqlite3
python3 demo_ink.py                               # 受墨量与纸张配给演示（临时库，自动清理）
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
audit_attempt`（报告与留痕），`ink_voucher / ink_cell / ink_stamp_cell /
ink_segment_contrib / ink_attempt`（受墨量凭单），`paper_type /
paper_confirmation / sample_record`（纸种库存、确认与旧印样）。有理数以
规范分数字符串 `p/q` 落库，读出还原为 `Fraction`，全程无浮点。

## 格面受墨量与纸样配给（materials/ink）

虚构印模仅作离线数据复核。一张试版由互不相交的轴对齐矩形格组成，每格
可挂若干只印模及各自恒定的非负流墨率。沿用旧档的分段线性路径与对应
轮廓半宽/半高，按

    ink(cell) = ∫_horizon Σ_stamp flow_rate · area(stamp(t) ∩ cell) dt

计算。只碰边、只碰角没有面积——与旧核查合同的严格正面积一致。动作段内
某一方向的交叠长度是分段线性的，折点出现在印模边缘越过格边界处；两个
方向同时考虑后交叠面积在折点之间是二次多项式，用有理数三点 Simpson
精确积分，**不采样**。因此斜向平移导致面积在同一动作段内改变也能给出
精确凭单。

凭单冻结当时的轮廓版本、轨迹快照、格网与流墨率；按格 / 印模 / 有效动作段
逐层保存精确有理数贡献，`verify_voucher` 可仅凭凭单内容独立重算比对。
更换格网或更正轮廓不会改变旧凭单；后续修订只影响新凭单。相同长短的两格
可以因印模摆放不同而有不同墨量。

备忘录手算（`materials/ink`）：单格 `[0,1]×[0,1]`、静止、半尺寸 1/2、
流墨率 1、时限 4，中心 `(5/4,1/2)` 得 `1`，中心 `(3/4,1/2)` 得 `3`，两次都
只有一只印模、无重叠；斜向平移 `(1/2,1/2)→(3/2,3/2)` 的凭单为 `1/3`。

纸张管理员独立登记纸种与余张数。每张试版有加急权重；每个指定格在某纸种
上容许的墨量是**闭区间**，全部指定格合格才能用该纸种出一张。为同时到达的
一批试版分配纸张时按字典序：获配权重总和最大 → 获配张数最多 → 按试版号
排序的纸种序列字典序最小（未配排在所有纸种之后）。实现上先做一次带容量
的二分匹配最小费用流求最优权重/张数，再逐版固定字典序最小的可行纸种。
确认整批在 `BEGIN IMMEDIATE` 事务里一次扣减，要么全扣要么全不扣；同一
`confirmation_id` 同计划幂等、异文拒绝。并发争抢最后一张时只有一方成功，
失败方捕获 `PaperShortage.shortages`（逐纸种请求/可用/缺口），可用新库存
重算后再确认。旧印样记录固定原用纸与墨量凭单。

示例：薄纸仅收 `[0,2]`、厚纸仅收 `[2,4]`，则墨量 3 的后一版只能用厚纸；
厚纸被更早的确认领完则该版无法交付。


SQLite 表：`profile_revision / profile / horizon / cart / trajectory_knot`
（目录与轨迹），`report / report_pair / conflict / conflict_evidence /
audit_attempt`（报告与留痕）。有理数以规范分数字符串 `p/q` 落库，读出
还原为 `Fraction`，全程无浮点。
