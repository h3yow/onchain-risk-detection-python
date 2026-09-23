"""区块顺序账本：幂等摄入、冲突检测、乱序回放与余额计算。

设计要点：账本不缓存任何派生态（余额/评分均由全量交易按区块全序现算），
因此迟到的历史交易只需落入交易索引，下一次派生时自然插入正确位置，
不会污染既有结论，也不需要“撤销/重做”。
"""
from __future__ import annotations

from .errors import ConflictError, ValidationError
from .models import IngestResult, LedgerView, Transaction
from .store import ThreadSafeStore

# 冲突检测时的字段比较顺序（保证错误原因稳定可预期）
_COMPARED_FIELDS: tuple[str, ...] = (
    "from_addr",
    "to_addr",
    "amount",
    "block_height",
    "block_index",
    "timestamp",
)


class Ledger:
    def __init__(self, store: ThreadSafeStore) -> None:
        self._store = store

    @staticmethod
    def _validate(tx: Transaction) -> None:
        if not tx.tx_id or not isinstance(tx.tx_id, str):
            raise ValidationError("tx_id 不能为空")
        if not tx.from_addr or not tx.to_addr:
            raise ValidationError("from_addr/to_addr 不能为空")
        if tx.from_addr == tx.to_addr:
            raise ValidationError(f"交易 {tx.tx_id} 不允许自环转账")
        if tx.amount <= 0:
            raise ValidationError(f"交易 {tx.tx_id} 金额必须为正数，收到 {tx.amount}")
        if tx.block_height < 0:
            raise ValidationError(f"交易 {tx.tx_id} block_height 不能为负")
        if tx.block_index < 0:
            raise ValidationError(f"交易 {tx.tx_id} block_index 不能为负")

    def ingest(self, tx: Transaction) -> IngestResult:
        """原子摄入一笔交易。

        - 全新交易：写入索引（余额在查询时按全序派生，无重复计数可能）；
        - 内容完全一致的重复提交：幂等忽略，duplicate=True；
        - tx_id 相同但任何字段不一致：抛 ConflictError，指明首个差异字段。
        """
        self._validate(tx)
        with self._store.lock():
            existing = self._store.get_transaction(tx.tx_id)
            if existing is not None:
                for name in _COMPARED_FIELDS:
                    old = getattr(existing, name)
                    new = getattr(tx, name)
                    if old != new:
                        raise ConflictError(tx.tx_id, name, old, new)
                return IngestResult(
                    tx_id=tx.tx_id,
                    accepted=False,
                    duplicate=True,
                    reason="内容一致的重复提交，已幂等忽略",
                )
            self._store.put_transaction(tx)
            return IngestResult(tx_id=tx.tx_id, accepted=True, duplicate=False)

    def replay_in_order(self) -> list[Transaction]:
        """按 (block_height, block_index, tx_id) 全序回放。

        tx_id 作为末位 tiebreaker，保证即使区块内序号缺失也有确定顺序。
        """
        return sorted(
            self._store.all_transactions(),
            key=lambda t: (t.block_height, t.block_index, t.tx_id),
        )

    def balances(self) -> dict[str, float]:
        balances: dict[str, float] = {}
        for tx in self.replay_in_order():
            balances[tx.from_addr] = balances.get(tx.from_addr, 0.0) - tx.amount
            balances[tx.to_addr] = balances.get(tx.to_addr, 0.0) + tx.amount
        return balances

    def tx_count(self) -> int:
        return len(self._store.all_transactions())

    def view(self) -> LedgerView:
        """在同一把锁内取一致快照：交易快照、余额、计数同源。"""
        with self._store.lock():
            ordered = self.replay_in_order()
            balances: dict[str, float] = {}
            nonces: dict[str, int] = {}
            for tx in ordered:
                balances[tx.from_addr] = balances.get(tx.from_addr, 0.0) - tx.amount
                balances[tx.to_addr] = balances.get(tx.to_addr, 0.0) + tx.amount
                nonces[tx.from_addr] = nonces.get(tx.from_addr, 0) + 1
                nonces[tx.to_addr] = nonces.get(tx.to_addr, 0) + 1
            last_block = ordered[-1].block_height if ordered else 0
            return LedgerView(
                balances=balances,
                nonces=nonces,
                last_block=last_block,
                tx_count=len(ordered),
            )
