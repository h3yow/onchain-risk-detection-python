# 本地链上交易风险评分服务

用**本地合成**的钱包与交易数据模拟链上活动，提供交易摄入、区块顺序回放、
幂等去重、冲突检测与基于可解释因子的风险评分。所有结论确定性可复现，
并发摄入的结果与逐个串行处理完全一致。自本轮起每个风险维度还会单独标注
**证据充分程度**，并给出本次评估的**完整度汇总**：可评估维度太少时只给
“下不了结论”（`UNKNOWN`），绝不把“没查出问题”误报成低风险。

- 运行时：Python 3.14 / FastAPI，纯内存存储，无需外部依赖
- 包：`risk_engine/`，测试：`tests/`，演示：`scripts/demo.py`

## 快速开始

```bash
uv sync                                  # 安装依赖（含 pytest）
uv run python scripts/demo.py            # 端到端演示：乱序/重复/冲突 + 风险结论
uv run pytest                            # 全量测试（日志打印输入流水与判定依据）
uv run uvicorn main:app --reload         # 启动 HTTP 服务（http://127.0.0.1:8000/docs）
```

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/wallets` | 注册钱包（`wallet_type` 为 `NORMAL` / `HIGH_RISK`） |
| POST | `/transactions` | 摄入一笔交易；重复提交幂等，内容冲突返回 409 |
| GET  | `/ledger` | 当前账本一致快照：余额、交互计数、最高区块、交易数 |
| GET  | `/risk/{address}` | 风险评分：级别、逐维度证据状态与依据、完整度汇总、无法判定原因、确定性指纹 |

```bash
curl -s -X POST localhost:8000/wallets -H 'content-type: application/json' \
  -d '{"address":"BAD","wallet_type":"HIGH_RISK"}'
curl -s -X POST localhost:8000/transactions -H 'content-type: application/json' \
  -d '{"tx_id":"t1","from_addr":"A","to_addr":"BAD","amount":50,"block_height":1,"timestamp":1}'
curl -s localhost:8000/risk/A
```

## 风险评分模型

评分由六个**相互独立、各自可解释**的因子组成。每个因子无论触发与否都会返回
观测值、阈值与关联地址，触发时给出正权重，最终求和并截断到 `[0, 100]`。

| 因子代码 | 含义 | 触发阈值（合成数据口径） | 权重 |
| --- | --- | --- | --- |
| `FUND_CONVERGENCE` | 资金汇聚：不同来源向该地址转入 | 入账来源数 ≥ 3 | 25 |
| `FUND_DISPERSION` | 资金分散：该地址向不同目标转出 | 出账目标数 ≥ 3 | 20 |
| `RING_FLOW` | 环形流转：有向资金图中经过该地址的环 | 存在环（给出最短环路径） | 30 |
| `HIGH_FREQ_INTERACTION` | 短时高频交互 | 5 秒滑动窗口内 ≥ 4 笔 | 15 |
| `HIGH_RISK_DIRECT` | 与已知高风险地址直接资金往来（双向 1 跳） | 存在即触发 | 30 |
| `HIGH_RISK_INDIRECT` | 间接关联高风险地址（2 跳内，且非直接） | 存在即触发 | 15 |

**级别阈值**

- `score >= 60` → `HIGH`
- `30 <= score < 60` → `MEDIUM`
- `score < 30` → `LOW`（**前提是覆盖门禁通过**，见下）
- **数据不足/覆盖不足 → `UNKNOWN`**：地址参与交易少于 2 笔、账本为空，
  或 6 个维度中证据充分的少于 3 个。此时返回明确的
  `INSUFFICIENT_TRANSACTIONS` / `EMPTY_LEDGER` /
  `INSUFFICIENT_DIMENSION_COVERAGE` 与逐维度原因，
  **绝不默认判为安全**。因子仍照常计算并展示，便于人工研判。

阈值与权重集中定义在 `risk_engine/factors.py` 与 `risk_engine/scoring.py`，可调。

## 维度级证据判定口径（人工复核导向）

每个因子无论是否触发，都会在原有 `triggered/weight/detail/related` 之外
新增两个字段 `evidence_status` 与 `gap_code/gap_detail`，把“没查出来问题”
和“查过了确实没问题”彻底分开：

| evidence_status | 含义 | 对结论的作用 |
| --- | --- | --- |
| `SUFFICIENT` | 证据充分：要么已触发，要么观测面已覆盖到足以排除 | 支撑结论（正向命中或可信放行） |
| `INSUFFICIENT` | 有观测数据，但覆盖面不足以下结论 | **不**计入支撑维度，附 `gap_code` 说明缺什么 |
| `NO_DATA` | 该维度完全没有相关数据 | **不**计入支撑维度，附 `gap_code` 说明缺什么 |

未触发时各维度“证据充分”的具体口径（触发一律算充分）：

| 因子 | 未触发即充分（可安全放行）的口径 | 常见 INSUFFICIENT 的 `gap_code` |
| --- | --- | --- |
| `FUND_CONVERGENCE` | 不同入账来源 ≥ 3 | `TOO_FEW_INCOMING_SOURCES`（来源面太窄）；无入账为 `NO_INCOMING_TX`(NO_DATA) |
| `FUND_DISPERSION` | 不同出账目标 ≥ 3 | `TOO_FEW_OUTGOING_TARGETS`；无出账为 `NO_OUTGOING_TX`(NO_DATA) |
| `RING_FLOW` | 入账、出账两个方向都有交易，图中仍无经过该地址的环 | `SINGLE_DIRECTION_ONLY`（样本方向单一，资金不可能回流）；无交易为 `NO_PARTICIPATION`(NO_DATA) |
| `HIGH_FREQ_INTERACTION` | 交互时间跨度已覆盖 ≥ 1 个 5 秒判定窗口 | `TIME_SPAN_TOO_SHORT`（笔数少且跨度不足一个窗口）；无交互为 `NO_INTERACTION`(NO_DATA) |
| `HIGH_RISK_DIRECT` | 不同直接对手方 ≥ 2 且均非高风险 | `TOO_FEW_COUNTERPARTIES`（缺少应有的对手方覆盖）；无对手方为 `NO_COUNTERPARTY`(NO_DATA) |
| `HIGH_RISK_INDIRECT` | 至少 1 个一跳对手方还存在绕过本地址的后续交易（2 跳路径展得开），2 跳内仍无高风险 | `NO_TWO_HOP_PATH`（对手方都没有后续往来）；无对手方为 `NO_COUNTERPARTY`(NO_DATA) |

### 完整度汇总怎么看

`GET /risk/{address}` 返回新增的 `completeness` 对象：

- `total_dimensions`：风险维度总数（固定 6）。
- `evaluated_dimensions`：证据充分、真正支撑本次结论的维度数。
- `coverage_ratio = evaluated / total`。
- `confidence`：整体可信度，`coverage >= 0.8` 为 `HIGH`，达到最低覆盖线
  （≥ 3/6）为 `MEDIUM`，否则为 `LOW`。
- `missing_dimensions`：没评上的维度逐条列出 `code / evidence_status /
  gap_code / gap_detail`，复核人可直接看到每个缺口缺什么。

**覆盖门禁**：可评估维度少于 `MIN_EVALUATED_DIMENSIONS = 3` 时，哪怕触发
分数为 0，也**不允许落到 `LOW`/`MEDIUM`/`HIGH`**，只能给 `UNKNOWN`
（“目前下不了结论”）。此时除了原有的 `insufficient_reasons`，还会返回
`inconclusive_reasons`：第一条是 `INSUFFICIENT_DIMENSION_COVERAGE`
汇总原因，其后按 `维度代码__gap_code` 逐条列出每个没评上维度的具体原因。
原有 `INSUFFICIENT_TRANSACTIONS` / `EMPTY_LEDGER` 保护保持不变。

### 历史补录后的自动重新评估

评分不缓存任何派生态，每次评估都由当前全量交易按区块全序现算，因此：

- 迟到/乱序的历史交易落入索引后，下一次评估时之前“样本不足”的维度会按
  新证据自动重评；覆盖一旦抬过 3/6 判定线，级别会从 `UNKNOWN` 翻成明确的
  `LOW/MEDIUM/HIGH`，逐维度 `gap_*` 原因同步消失或更新。
- **同输入同输出**：数据没变时反复评估，级别、分数、因子、`completeness`
  与 `determinism_key` 完全一致（dataclass 相等）。
- **完整度单调不降**：证据只会随历史补录增多，`evaluated_dimensions` 与
  `coverage_ratio` 不可能因新数据到达而下降（补录不会删除既有观测）。

### 确定性

- 评分前交易一律按 `(block_height, block_index, tx_id)` 全序排序，
  与摄入到达顺序无关；因子内部对地址集合排序，证据顺序固定。
- 每次评估返回 `determinism_key`：对 `(被评估地址, 有序交易, 高风险集合)`
  的 SHA-256 指纹。同一输入（哪怕乱序、分批到达）多次评估，
  级别、分数、因子、逐维度证据状态、完整度汇总与指纹完全一致
  （见 `test_determinism_same_input_same_output` 及
  `test_backfill_history_raises_completeness_and_redecides`）。

## 幂等、乱序回放与冲突处理

- **幂等**：交易以 `tx_id` 唯一去重。内容完全一致的重复提交返回
  `duplicate=true`，既不重复写入，也不重复计入余额与指标。
- **乱序 / 迟到回放**：账本不缓存任何派生态，余额与评分都由全量交易
  按区块全序现算。迟到的历史交易（合成数据中的 `tx-late`，`block=0`）
  落入索引后自然回放到正确位置，不需要撤销/重做，也不会污染既有结论。
- **内容冲突**：`tx_id` 相同但 `from_addr / to_addr / amount /
  block_height / block_index / timestamp` 任一不一致，拒绝写入并抛出
  `ConflictError`（HTTP 409），错误体含首个差异字段名（`field`），
  不同冲突原因可区分。钱包类型注册不一致对应 `WalletConflictError`。
- **输入校验**：空地址、自环、非正金额、负区块号抛 `ValidationError`（HTTP 422）。

## 并发与一致性保证

- `ThreadSafeStore` 用**单一可重入锁**保护全部可变状态；交易、钱包均为
  不可变对象。锁内只做内存计算，不做任何等待。
- 并发摄入在锁内串行化；`ingest_many` 把整批包进一个临界区。
  派生态（余额/评分）由全量交易现算，因此不存在丢失的增量更新，
  最终账本等于“把并发请求按某一顺序串行处理”的结果
  （`test_concurrent_ingest_equals_serial` 用 8 线程 × 全量乱序重复提交，
  与串行参考实现逐字段比对）。
- 查询在同一把锁内取快照（交易、余额、计数同源），读不到半批写入等
  中间状态（`test_batched_atomicity_no_partial_state` 轮询观察计数，
  只能落在批次边界上）。并发评估结论唯一
  （`test_concurrent_evaluations_are_stable_and_consistent`）。
- 补录场景同理：多个线程并发重复补录同一批历史交易，锁内去重使其只算
  一次；补录进行中发起的评估与账本查询要么看到补录前、要么看到整批补录后，
  不会看到半批，且每次观察到的级别、因子状态与完整度彼此自洽，最终结果与
  “按区块顺序一次性全部录入”逐字段相等
  （`test_concurrent_backfill_queries_stay_consistent`）。

## 接口字段兼容范围

本轮只做**向后兼容的增量扩展**，原有字段一个不删、语义不变：

- `/risk` 顶层保留 `address/score/level/factors/insufficient_reasons/
  evaluated_at_block/determinism_key`；新增 `completeness` 与
  `inconclusive_reasons`。
- `factors[]` 每项保留 `code/triggered/weight/detail/related`；新增
  `evidence_status`（`SUFFICIENT/INSUFFICIENT/NO_DATA`）、`gap_code`、
  `gap_detail`（充分时后两者为 `null`）。
- 旧客户端可继续只读老字段；需要注意的**唯一语义变化**：在新口径下，
  证据覆盖面不足的地址（如合成数据里的 `R`、`F`）级别会从原先的分级
  变成 `UNKNOWN`，这正是“低信息不得看似无风险”的预期行为；证据充分的
  地址（`H/X/VIC/MID/U/CLEAN`）分级保持不变。

## 测试与日志

```bash
uv run pytest                      # 全部用例，stdout 打印流水/依据
uv run pytest tests/test_ledger.py # 只看幂等/冲突/乱序
uv run pytest tests/test_concurrency.py
```

- `tests/test_ledger.py`：重复交易幂等、五个字段冲突可区分、乱序与
  迟到历史交易回放、非法输入归类。
- `tests/test_scoring.py`：六因子触发集合与依据文本、阈值分级、
  数据不足返回 UNKNOWN、空账本、确定性指纹。
- `tests/test_completeness.py`（本轮新增）：
  - 低信息账户（只有 2 笔同向交易、以及从未出现的地址）只能得 `UNKNOWN`，
    且 `inconclusive_reasons` 逐条列出覆盖门禁原因与每个维度缺口；
  - 同一报告中 `SUFFICIENT / INSUFFICIENT / NO_DATA` 三种状态与六种
    `gap_code` 可区分，缺口说明都讲清“缺什么”；
  - 乱序补录历史交易后完整度**单调不降**、覆盖过线后 `UNKNOWN` 自动翻成
    明确 `LOW`、逐维度原因同步更新，并与一次性按序录入结果逐字段相等；
  - 8 线程并发重复补录同一批只算一次，补录全程的查询只见批次边界状态、
    结论与依据始终自洽。
- `tests/test_concurrency.py`：并发摄入等价串行、整批原子、并发评估稳定。
- `tests/test_api.py`：HTTP 状态码归类（409/422）、响应结构，以及本轮
  新增的字段向后兼容检查（老字段不丢、新字段齐全、UNKNOWN 门禁）。

测试日志分两类（由 `tests/helpers.py` 统一打印）：

- `INGEST-STREAM`：每条提交的到达序号、`tx_id`、方向、金额、区块、时间戳；
- `DECISION / FACTOR / MISSING-DIM / UNKNOWN-REASON / INCONCLUSIVE`：
  每个被评估地址的级别、总分、完整度（`coverage=x/6`、`confidence`）、
  逐因子证据状态与判定依据、没评上的维度缺口及下不了结论的逐条原因。

## 代码结构

```
risk_engine/
  models.py     # 钱包/交易/因子证据/证据状态/完整度/评分结论等不可变类型
  errors.py     # ConflictError / WalletConflictError / ValidationError ...
  store.py      # ThreadSafeStore：单锁并发存储与快照
  ledger.py     # 幂等摄入、冲突检测、区块全序回放、余额派生
  factors.py    # 六个可解释风险因子 + 维度级证据充分/不足/无数据判定
  scoring.py    # 因子聚合、阈值分级、覆盖门禁、完整度汇总、确定性指纹
  service.py    # 门面：并发摄入、一致查询、评估
  synthetic.py  # 确定性合成钱包/交易/到达顺序/冲突样本
  api.py        # FastAPI 路由与错误归类（含 completeness 等新字段）
tests/          # pytest 用例（流水与判定依据写日志）
  test_completeness.py # 低信息保护、维度缺口可区分、补录重判、并发补录一致
scripts/demo.py # 本地端到端演示
```

## 本地怎么验证新能力

```bash
uv run pytest tests/test_completeness.py -q   # 只看本轮新增的证据/补录用例
uv run pytest -q                              # 全量回归（28 个用例，老用例不回归）
uv run python scripts/demo.py                 # 观察决策行的 coverage=x/6 与 confidence
```

也可起服务后手工验证低信息账户与补录过程：

```bash
uv run uvicorn main:app --reload
# 1) 低信息账户：只有 1 笔交易 -> level=UNKNOWN，completeness.evaluated_dimensions 很低
curl -s -X POST localhost:8000/transactions -H 'content-type: application/json' \
  -d '{"tx_id":"w1","from_addr":"W","to_addr":"P1","amount":10,"block_height":10,"block_index":0,"timestamp":100}'
curl -s localhost:8000/risk/W | python -m json.tool
# 2) 补录更早区块的历史交易后再次评估：coverage 上升，过线后 level 翻成明确分级
curl -s -X POST localhost:8000/transactions -H 'content-type: application/json' \
  -d '{"tx_id":"s1","from_addr":"S1","to_addr":"W","amount":5,"block_height":1,"block_index":0,"timestamp":10}'
curl -s -X POST localhost:8000/transactions -H 'content-type: application/json' \
  -d '{"tx_id":"s2","from_addr":"S2","to_addr":"W","amount":6,"block_height":1,"block_index":1,"timestamp":11}'
curl -s localhost:8000/risk/W | python -m json.tool
```
