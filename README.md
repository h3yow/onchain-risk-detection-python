# 本地链上交易风险评分服务

用**本地合成**的钱包与交易数据模拟链上活动，提供交易摄入、区块顺序回放、
幂等去重、冲突检测与基于可解释因子的风险评分。所有结论确定性可复现，
并发摄入的结果与逐个串行处理完全一致。

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
| GET  | `/risk/{address}` | 风险评分：级别、各因子依据、无法判定原因、确定性指纹 |

```bash
curl -s -X POST localhost:8000/wallets -H 'content-type: application/json' \
  -d '{"address":"BAD","wallet_type":"HIGH_RISK"}'
curl -s -X POST localhost:8000/transactions -H 'content-type: application/json' \
  -d '{"tx_id":"t1","from_addr":"A","to_addr":"BAD","amount":50,"block_height":1,"timestamp":1}'
curl -s localhost:8000/risk/A
```

### `/risk/{address}` 响应字段

原有字段保持不变，本轮只**新增**字段，旧消费方可直接忽略：

- 顶层原有：`address`、`score`、`level`、`factors`、`insufficient_reasons`、
  `evaluated_at_block`、`determinism_key` 全部保留，含义不变。
- `factors[]` 新增：
  - `status`：`SUFFICIENT` / `INSUFFICIENT` / `NO_DATA`；
  - `insufficiency_code`：证据不足时的机读原因码（充分时为 `null`）；
  - `insufficiency_detail`：证据不足时“缺什么”的说明（充分时为 `null`）。
- `insufficient_reasons[]` 新增 `factor`：原因为整体级时为 `null`，
  否则为该原因归属的维度代码。`code` / `detail` 不变。
- 顶层新增 `completeness`：完整度汇总对象（见上节）。

兼容范围：旧字段一个不删、取值口径不变（仅数据不足账户的 `level`
语义更严格——过去极少数参与 ≥2 笔但维度覆盖不足的账户可能给 `LOW`，
现在按需求一律 `UNKNOWN`，这属于修正“假低风险”而非删除能力）。

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

### 维度级证据充分性判定口径

每个因子（维度）在给出 `triggered` 结论前，先声明本次证据是否充分，三态互斥：

- **`SUFFICIENT`（证据够）**：样本足以支撑“触发/未触发”的明确判断，
  此时 `triggered=false` 才表示“查过了确实没问题”。
- **`INSUFFICIENT`（证据不够）**：有相关数据，但样本方向单一、缺少应有
  对手方、样本量/时间跨度不够，无法确认也无法排除风险。该态下
  `triggered` 恒为 `false`、权重为 0，并通过 `insufficiency_code` 与
  `insufficiency_detail` 讲清“缺什么”。
- **`NO_DATA`（完全没数据）**：该维度所需方向上零笔交易，连查都没查过。

各维度的具体门槛（集中在 `risk_engine/factors.py`，可调）：

| 维度 | NO_DATA | INSUFFICIENT（原因码） | SUFFICIENT 口径 |
| --- | --- | --- | --- |
| `FUND_CONVERGENCE` | 无任何入账（`NO_INCOMING_TX`） | 仅 1 个入账来源（`SINGLE_INCOMING_SOURCE`） | ≥ 2 个不同入账来源，即可判定汇聚/非汇聚 |
| `FUND_DISPERSION` | 无任何出账（`NO_OUTGOING_TX`） | 仅 1 个出账目标（`SINGLE_OUTGOING_TARGET`） | ≥ 2 个不同出账目标，即可判定分散/非分散 |
| `RING_FLOW` | 无任何交易（`NO_INTERACTION`） | 只有入账或只有出账（`MISSING_COUNTERPARTY_DIRECTION`） | 双向对手方都存在，BFS 查环结果可信 |
| `HIGH_FREQ_INTERACTION` | 无任何交互（`NO_INTERACTION`） | 交互 < 4 笔（`TOO_FEW_INTERACTIONS`，附时间跨度） | ≥ 4 笔交互，滑动窗口结论可信 |
| `HIGH_RISK_DIRECT` | 无直接对手方（`NO_COUNTERPARTY`） | —（有 1 个对手方即可核查名单） | 已核查全部直接对手方 |
| `HIGH_RISK_INDIRECT` | 无直接对手方（`NO_COUNTERPARTY`） | 一跳对手方之外观察不到二跳地址（`NO_TWO_HOP_CONTEXT`） | 存在可展开的二跳上下文，完成 2 跳核查 |

设计原则：证据状态只取决于“当前全量交易”，且随历史交易补录只会沿
`NO_DATA → INSUFFICIENT → SUFFICIENT` 单向演进（例如新增入账来源不会让
汇聚维度的证据变少），因此**完整度随数据补录单调不降**。

### 级别阈值

- `score >= 60` → `HIGH`
- `30 <= score < 60` → `MEDIUM`
- `score < 30` → `LOW`
- **下不了结论 → `UNKNOWN`**，且**绝不允许落到 `LOW`**。出现下列任一整体级
  门槛不满足时即为 UNKNOWN，并在 `insufficient_reasons` 中逐条列出原因：
  - `EMPTY_LEDGER`：账本为空；
  - `INSUFFICIENT_TRANSACTIONS`：地址参与交易少于 2 笔；
  - `DIMENSION_COVERAGE_INSUFFICIENT`：六个维度中证据充分的少于 3 个
    （`MIN_EVALUABLE_FOR_GRADE`），原因中列明未评上的全部维度。

  维度级原因（`factor` 字段非空）始终随报告返回，说明每个没评上的维度缺什么；
  但能否分级只由上面三个整体级门槛决定——覆盖达标后，个别维度仍证据不足
  不阻止给出明确分级，复核人可从完整度看到剩余缺口。因子照常计算并展示。

阈值、权重与覆盖门槛集中定义在 `risk_engine/factors.py` 与
`risk_engine/scoring.py`，可调。

### 完整度汇总（completeness）

每次评估返回 `completeness` 对象，让人一眼看清结论由多少维度支撑：

| 字段 | 含义 |
| --- | --- |
| `total_dimensions` | 维度总数（6） |
| `evaluated_dimensions` | 证据充分、实际支撑结论的维度数 |
| `pending_dimensions` | 有数据但不够（INSUFFICIENT）的维度数 |
| `missing_dimensions` | 完全没数据（NO_DATA）的维度数 |
| `evaluated` / `not_evaluated` | 两类维度的代码清单 |
| `coverage_ratio` | `evaluated / total`，0~1 |
| `confidence` | 整体可信度：≥4 个维度可评估为 `HIGH`，≥3 为 `MEDIUM`，否则 `LOW` |
| `sufficient_for_grade` | 覆盖是否达到分级门槛（≥3 个维度） |

读法：`level=LOW` 且 `confidence=HIGH / 6/6` 才是“查过了确实没问题”；
`level=UNKNOWN` 时完整度会指出这次结论只有几个维度支撑、哪些没评上。

### 确定性与单调补录

- 评分前交易一律按 `(block_height, block_index, tx_id)` 全序排序，
  与摄入到达顺序无关；因子内部对地址集合排序，证据顺序固定。
- 每次评估返回 `determinism_key`：对 `(被评估地址, 有序交易, 高风险集合)`
  的 SHA-256 指纹。同一输入（哪怕乱序、分批到达）多次评估，级别、分数、
  因子、维度状态、完整度与指纹完全一致
  （`test_determinism_same_input_same_output`、
  `test_repeated_evaluation_is_identical`）。
- 评分不缓存任何派生态：迟到/乱序历史交易补录后，下一次评估自动重算，
  之前评不了的维度随证据到位自动重评；覆盖抬过 3 个维度的判定线后，
  结论从 UNKNOWN 变为明确分级，维度原因同步消失/更新
  （`test_backfill_lifts_completeness_and_re_grades`），完整度逐笔补录
  单调不降（`test_completeness_is_monotonic_along_random_backfill`）。

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
- **补录期间的评估一致性**：评估在同一把锁内取“全量交易 + 高风险名单”
  快照后现算，因此补录进行中或补录前后发起的评估，看到的因子状态、
  原因与结论必然同源自洽（不会出现结论已分级但维度还显示缺证据）。
  同一批历史交易被多线程并发重复补录时，以 `tx_id` 幂等去重只算一次，
  终态与“按区块顺序一次性全部录入”逐字段（含 `determinism_key`）一致
  （`test_concurrent_backfill_query_is_consistent_and_dedup`）。

## 测试与日志

```bash
uv run pytest                      # 全部用例，stdout 打印流水/依据
uv run pytest tests/test_ledger.py # 只看幂等/冲突/乱序
uv run pytest tests/test_concurrency.py
uv run pytest tests/test_completeness.py  # 只看证据状态/完整度/补录重判
uv run python scripts/demo.py       # 端到端演示（含逐维度状态与完整度）
```

- `tests/test_ledger.py`：重复交易幂等、五个字段冲突可区分、乱序与
  迟到历史交易回放、非法输入归类。
- `tests/test_scoring.py`：六因子触发集合与依据文本、阈值分级、
  数据不足返回 UNKNOWN、空账本、确定性指纹。
- `tests/test_concurrency.py`：并发摄入等价串行、整批原子、并发评估稳定。
- `tests/test_completeness.py`（本轮新增）：
  - `test_low_information_account_is_never_graded`：低信息账户只能 UNKNOWN；
  - `test_dimension_insufficiency_reasons_are_distinguishable`：无数据 /
    样本单一 / 缺对手方 / 样本量不足等维度原因码可区分；
  - `test_backfill_lifts_completeness_and_re_grades`：补录历史交易后
    完整度 1→3→6，覆盖过线后 UNKNOWN→LOW，逐维度原因同步更新；
  - `test_repeated_evaluation_is_identical`：数据不变反复评估完全一致；
  - `test_completeness_is_monotonic_along_random_backfill`：逐笔乱序
    补录过程中多地址完整度单调不降，全量后 6/6；
  - `test_concurrent_backfill_query_is_consistent_and_dedup`：8 线程
    重复补录期间轮询评估，每份报告自洽、结论与依据对得上，终态等价串行；
  - `test_factor_statuses_exposed_over_http`：HTTP 新旧字段并存可用。
- `tests/test_api.py`：HTTP 状态码归类（409/422）与响应结构。

测试日志分两类（由 `tests/helpers.py` 统一打印）：

- `INGEST-STREAM`：每条提交的到达序号、`tx_id`、方向、金额、区块、时间戳；
- `DECISION / FACTOR / UNKNOWN-REASON`：被评估地址的级别、总分、
  **完整度（evaluated/pending/missing、coverage、confidence、gradeable）**、
  逐因子的证据状态与缺证据原因、整体级与维度级无法判定原因。

## 代码结构

```
risk_engine/
  models.py     # 钱包/交易/因子证据(含三态)/完整度/评分结论等不可变类型
  errors.py     # ConflictError / WalletConflictError / ValidationError ...
  store.py      # ThreadSafeStore：单锁并发存储与快照
  ledger.py     # 幂等摄入、冲突检测、区块全序回放、余额派生
  factors.py    # 六个可解释风险因子 + 每维度 SUFFICIENT/INSUFFICIENT/NO_DATA 口径
  scoring.py    # 因子聚合、完整度汇总、覆盖门槛保护、确定性指纹
  service.py    # 门面：并发摄入、一致查询、评估
  synthetic.py  # 确定性合成钱包/交易/到达顺序/冲突样本（含证据充分样本）
  api.py        # FastAPI 路由、错误归类与完整度响应
tests/          # pytest 用例（流水与判定依据写日志）
scripts/demo.py # 本地端到端演示
```
