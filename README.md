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
- `score < 30` → `LOW`
- **数据不足 → `UNKNOWN`**：地址参与交易少于 2 笔，或账本为空。
  此时返回明确的 `INSUFFICIENT_TRANSACTIONS` / `EMPTY_LEDGER` 原因，
  **绝不默认判为安全**。因子仍照常计算并展示，便于人工研判。

阈值与权重集中定义在 `risk_engine/factors.py` 与 `risk_engine/scoring.py`，可调。

### 确定性

- 评分前交易一律按 `(block_height, block_index, tx_id)` 全序排序，
  与摄入到达顺序无关；因子内部对地址集合排序，证据顺序固定。
- 每次评估返回 `determinism_key`：对 `(被评估地址, 有序交易, 高风险集合)`
  的 SHA-256 指纹。同一输入（哪怕乱序、分批到达）多次评估，
  级别、分数、因子与指纹完全一致（见 `test_determinism_same_input_same_output`）。

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
- `tests/test_concurrency.py`：并发摄入等价串行、整批原子、并发评估稳定。
- `tests/test_api.py`：HTTP 状态码归类（409/422）与响应结构。

测试日志分两类（由 `tests/helpers.py` 统一打印）：

- `INGEST-STREAM`：每条提交的到达序号、`tx_id`、方向、金额、区块、时间戳；
- `DECISION / FACTOR / UNKNOWN-REASON`：每个被评估地址的级别、总分、
  逐因子判定依据与数据不足原因。

## 代码结构

```
risk_engine/
  models.py     # 钱包/交易/因子证据/评分结论等不可变类型
  errors.py     # ConflictError / WalletConflictError / ValidationError ...
  store.py      # ThreadSafeStore：单锁并发存储与快照
  ledger.py     # 幂等摄入、冲突检测、区块全序回放、余额派生
  factors.py    # 六个可解释风险因子
  scoring.py    # 因子聚合、阈值分级、UNKNOWN 保护、确定性指纹
  service.py    # 门面：并发摄入、一致查询、评估
  synthetic.py  # 确定性合成钱包/交易/到达顺序/冲突样本
  api.py        # FastAPI 路由与错误归类
tests/          # pytest 用例（流水与判定依据写日志）
scripts/demo.py # 本地端到端演示
```
