"""应用服务：串行化摄入、一致性查询与风险评估的统一入口。

并发模型：所有写操作在 ThreadSafeStore 的同一把 RLock 内执行，
ingest_many 还会把整批包进一个临界区。因此 N 个并发摄入请求的最终账本，
与把这些请求按某一顺序逐个串行执行的结果完全一致：
- 交易以 tx_id 去重，绝不重复计数；
- 派生态（余额/评分）由全量交易现算，不存在丢失的增量更新；
- 查询在锁内取快照，读不到未提交的中间状态。
"""
from __future__ import annotations

from .ledger import Ledger
from .models import (
    IngestResult,
    LedgerView,
    RiskScore,
    Transaction,
    Wallet,
    WalletType,
)
from .scoring import RiskScorer
from .store import ThreadSafeStore


class RiskService:
    def __init__(self) -> None:
        self._store = ThreadSafeStore()
        self._ledger = Ledger(self._store)

    def register_wallet(self, wallet: Wallet) -> None:
        self._store.put_wallet(wallet)

    def ingest_transaction(self, tx: Transaction) -> IngestResult:
        return self._ledger.ingest(tx)

    def ingest_many(self, txs: list[Transaction]) -> list[IngestResult]:
        """整批原子摄入：要么全部处理完，要么整体未完成，查询不会看到半批。"""
        results: list[IngestResult] = []
        with self._store.lock():
            for tx in txs:
                results.append(self._ledger.ingest(tx))
        return results

    def ledger_view(self) -> LedgerView:
        return self._ledger.view()

    def evaluate(self, address: str) -> RiskScore:
        # 同一锁内取交易与高风险名单，保证二者来自一致的时间点
        with self._store.lock():
            txs = self._store.all_transactions()
            high_risk = self._store.high_risk_snapshot()
        return RiskScorer(txs, high_risk).evaluate(address)

    @staticmethod
    def normal_wallet(address: str) -> Wallet:
        return Wallet(address=address, wallet_type=WalletType.NORMAL)
