# 本地链上交易风险评分服务

使用本地**合成**的钱包与交易数据模拟链上活动，提供：

- 钱包 / 交易摄入的**幂等处理**与**内容冲突识别**；
- 迟到历史交易按区块顺序**确定性回放**；
- 基于 5 类**可解释因子**的风险评分与阈值判定，数据不足时明确返回「无法判定」而非默认安全；
- 锁串行化 + 一致性快照，保证**并发摄入与逐个串行处理结果完全一致**，查询不暴露写入中间态；
- FastAPI HTTP 接口 + 25 个单元/并发测试（测试日志打印输入流水与判定依据）。

## 目录结构

```
app/
  models.py    领域模型（Wallet / Transaction / IngestResult / RiskReport / FactorEvidence）
  errors.py    可区分错误（DUPLICATE_RECORD / CONTENT_CONFLICT / INVALID_TRANSACTION / NOT_FOUND）
  ledger.py    线程安全内存账本：幂等摄入、规范排序、余额、不可变快照 LedgerSnapshot
  scoring.py   五因子风险评分引擎（快照的纯函数）
  service.py   服务层：摄入串行化门禁、错误分类、统一评分/快照入口、demo 引导
  synthdata.py 确定性合成数据集（覆盖全部风险形态与数据不足场景）
  api.py       FastAPI 端点
tests/         幂等/乱序、因子/稳定性/数据不足、并发一致性、HTTP 四类测试
main.py        uvicorn 入口与终端 demo
```

## 快速开始

```bash
uv sync                       # 安装依赖
uv run python main.py demo    # 终端查看合成数据的评分与逐因子解释
uv run uvicorn main:app       # 启动 HTTP 服务（默认 http://127.0.0.1:8000）
uv run python -m pytest       # 运行全部测试（含实时 INFO 日志）
```

## 风险评分因子

评分是**账本快照的纯函数**，每个因子独立给出：触发/未触发、贡献分、中文解释、
结构化数据点（对手方、环路径、交易 ID 等）；数据不够时给出 `insufficient_reason`。

| 因子 | 代码 | 触发条件 | 权重 |
|---|---|---|---|
| 资金汇聚 | `CONCENTRATION` | ≥3 个不同上游、≥3 笔汇入，且汇入笔数 ≥ 汇出笔数 | 0.20 |
| 资金分散 | `DISPERSION` | ≥3 个不同下游、≥3 笔转出，且转出笔数 ≥ 转入笔数 | 0.20 |
| 短时高频 | `HIGH_FREQUENCY` | 与同一对手方在区块跨度 ≤2 内发生 ≥3 笔交互（滑动窗口） | 0.20 |
| 环形流转 | `RING_FLOW` | 目标处于长度 ≥3 条边的有向资金环中（确定性 DFS，环长上界 6） | 0.30 |
| 高风险关联 | `HIGH_RISK_LINK` | 与标记钱包直接交易 0.30；或经一跳中介的两跳资金路径 0.15 | 0.30 / 0.15 |

总分 = 各触发因子权重之和（封顶 1.0）。判定阈值：

- `score ≥ 0.60` → **HIGH**
- `0.30 ≤ score < 0.60` → **MEDIUM**
- 其余且数据充分 → **LOW**
- **数据不足 → UNDETERMINED**：相关交易 <2 笔，或 5 个因子中可评估因子 <3 个。
  此时若已有 ≥0.30 的强证据仍会判 MEDIUM/HIGH 并保留不足说明；
  证据不足绝不判 LOW——不会因为「没看到风险」而默认安全。

合成数据中的预期结论：`SUS`=HIGH（汇聚+高频+环），`R1`/`DIR`=MEDIUM，
`INDIR`/`HUB`/`SPREAD`/`FREQ_A`=LOW（单一弱信号），`QUIET`=UNDETERMINED。

## 幂等、冲突与乱序回放

- **幂等键**：钱包用 `address`，交易用 `tx_id`。
- 标识已存在且内容**完全一致** → 返回 `DUPLICATE`（幂等成功），
  余额与指标**绝不重复计入**。
- 标识相同但任一关键字段
  （交易：`block/tx_index/sender/receiver/amount/timestamp`；
  钱包：`label/high_risk/risk_tags`）不一致 → 返回 `CONFLICT`，
  `reason` 精确到冲突字段，例如 `交易字段冲突[amount]`，`detail` 给出新旧值；
  冲突记录被拒绝且不污染账本。
- 记录本身非法（金额 ≤0、收付款方相同、引用未注册钱包等）→ `REJECTED`；
  若一笔重提记录既冲突又非法，先按非法拒绝（分类边界在测试中有断言）。
- **乱序回放**：交易按规范键 `(block, tx_index, timestamp, tx_id)` 排序存储，
  迟到的历史交易自然插入正确区块位置；无论以任何顺序提交，最终账本顺序、
  余额、链顶与评分都与按区块顺序逐个提交一致。评分只反映「当前已提交集合」，
  迟到交易回放后基于新区快重算，历史报告不会被原地改写。

## 并发摄入与一致性查询

- `Ledger` 内部用一把 `RLock` 保护全部读写；`RiskService` 再加一道摄入门禁，
  HTTP 层所有摄入请求经单线程执行器排队。并发摄入因此与**某种串行顺序等价**，
  而最终状态（集合 + 规范排序 + 余额 + 评分）对提交顺序不敏感，
  所以必然与逐个串行处理的结果一致——不会丢更新或重复计数。
- 查询通过 `Ledger.snapshot()` 在**一次加锁内复制**全部状态，
  返回 frozen `LedgerSnapshot`：快照内余额恒等于按快照交易规范重放的结果，
  查询期间即使有并发写入，也绝不会看到只写了一半的中间状态。
- 并发测试（`tests/test_concurrency.py`）覆盖：2/8/32 线程并发摄入全量数据后
  交易数/余额/链顶/顺序/所有评分与串行基线一致；每笔交易 ×4 份并发重复提交时
  恰好 ACCEPTED 一次、其余 DUPLICATE；4 个读线程并发抽样快照，
  每个线程观测到的交易数单调增长、余额重放不变式恒成立。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/wallets` | 摄入钱包 |
| POST | `/transactions` | 摄入交易（允许乱序） |
| GET | `/wallets/{address}/risk` | 风险报告（逐因子解释、阈值结论、无法判定原因） |
| GET | `/wallets/{address}` | 钱包信息与当前余额 |
| GET | `/ledger/snapshot` | 全账本一致性快照 |
| GET | `/` | 服务概览 |

状态码：`200` ACCEPTED/DUPLICATE；`409` 内容冲突（body 内含字段级原因）；
`422` 数据非法；`404` 钱包不存在。

```bash
curl -s http://127.0.0.1:8000/wallets/SUS/risk | python3 -m json.tool
```

## 测试与日志

```bash
uv run python -m pytest                 # 全部 25 个用例，实时打印 INFO 流水
uv run python -m pytest tests/test_concurrency.py
```

测试日志会打印「输入流水」与「判定依据」，例如：

```
INFO [idempotency] 输入流水: 同一笔交易 c1 提交 3 次 -> ACCEPTED, DUPLICATE, DUPLICATE
INFO [idempotency] 判定依据: 账本仅 1 笔交易，HUB 余额=100（重复提交未重复计数）
INFO [scoring]     输入流水: A->B->C->A -> RING_FLOW triggered=True 依据=发现环形流转：A -> B -> C -> A ...
```
