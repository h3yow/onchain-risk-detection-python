"""应用服务层：串行化摄入、错误分类、统一评分入口。

- 同一把 ``RLock`` 串行化所有摄入与查询：并发到达的请求按进入顺序逐个提交，
  最终账本/评分与逐个串行处理严格一致；查询永远基于完整快照。
- 错误分类（原因可区分）：
  * 内容一致的重复标识  -> DUPLICATE（幂等成功，不重复计入）
  * 标识相同但字段冲突  -> CONFLICT，reason 标明冲突字段
  * 数据非法            -> REJECTED，reason 标明非法原因
"""
from __future__ import annotations

import threading

from app.errors import ContentConflictError, InvalidTransactionError
from app.ledger import Ledger, LedgerSnapshot
from app.models import IngestResult, IngestStatus, RiskReport, Transaction, Wallet
from app.scoring import RiskScorer


class RiskService:
    def __init__(self, ledger: Ledger | None = None) -> None:
        self._gate = threading.RLock()
        self._ledger = ledger if ledger is not None else Ledger()
        self._scorer = RiskScorer(self._ledger)

    @classmethod
    def bootstrap_demo(cls) -> "RiskService":
        """加载本地合成数据（钱包先于交易）。"""
        from app import synthdata

        svc = cls()
        for w in synthdata.build_wallets():
            svc.ingest_wallet(w)
        for t in synthdata.build_transactions():
            svc.ingest_transaction(t)
        return svc

    @property
    def ledger(self) -> Ledger:
        return self._ledger

    def ingest_wallet(self, wallet: Wallet) -> IngestResult:
        with self._gate:
            try:
                return self._ledger.ingest_wallet(wallet)
            except ContentConflictError as exc:
                return IngestResult(
                    IngestStatus.CONFLICT, wallet.address,
                    f"钱包字段冲突[{exc.field_name}]", str(exc),
                )
            except InvalidTransactionError as exc:
                return IngestResult(IngestStatus.REJECTED, wallet.address, "钱包数据非法", str(exc))

    def ingest_transaction(self, tx: Transaction) -> IngestResult:
        with self._gate:
            try:
                return self._ledger.ingest_transaction(tx)
            except ContentConflictError as exc:
                return IngestResult(
                    IngestStatus.CONFLICT, tx.tx_id,
                    f"交易字段冲突[{exc.field_name}]", str(exc),
                )
            except InvalidTransactionError as exc:
                return IngestResult(IngestStatus.REJECTED, tx.tx_id, "交易数据非法", str(exc))

    def ingest_many(self, wallets=(), transactions=()) -> list[IngestResult]:
        results: list[IngestResult] = []
        for w in wallets:
            results.append(self.ingest_wallet(w))
        for t in transactions:
            results.append(self.ingest_transaction(t))
        return results

    def evaluate(self, address: str) -> RiskReport:
        # 评分基于 ledger 内部的一致性快照；持 gate 保证与并发摄入的可串行化
        with self._gate:
            return self._scorer.evaluate(address)

    def snapshot(self) -> LedgerSnapshot:
        with self._gate:
            return self._ledger.snapshot()

    @staticmethod
    def is_ingest_failure(result: IngestResult) -> bool:
        return result.status in (IngestStatus.CONFLICT, IngestStatus.REJECTED)
