"""线程安全的内存账本：钱包/交易的幂等摄入与快照查询。

一致性约定：
- 所有读写都在同一把 ``RLock`` 下完成，并发摄入不会丢更新或重复计数。
- 交易按 (block, tx_index, timestamp, tx_id) 规范排序；迟到的历史交易
  自然插入到正确的区块位置，回放顺序与逐个串行到达完全一致。
- ``snapshot()`` 在一次加锁内复制全部状态，查询方永远看不到写入中间态。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from app.errors import ContentConflictError, InvalidTransactionError, LedgerNotFoundError
from app.models import IngestResult, IngestStatus, Transaction, Wallet

# 参与幂等/冲突比较的交易字段（按比较顺序）
_TX_FIELDS = ("block", "tx_index", "sender", "receiver", "amount", "timestamp")
_WALLET_FIELDS = ("label", "high_risk", "risk_tags")


def canonical_key(tx: Transaction) -> tuple:
    """交易在区块流中的规范位置。"""
    return (tx.block, tx.tx_index, tx.timestamp, tx.tx_id)


@dataclass(frozen=True)
class LedgerSnapshot:
    """某一时刻账本的不可变一致性快照。"""

    wallets: dict[str, Wallet]
    transactions: tuple[Transaction, ...]
    tip: tuple[int, int]
    balances: dict[str, int]

    def transactions_for(self, address: str) -> tuple[Transaction, ...]:
        return tuple(
            tx for tx in self.transactions if tx.sender == address or tx.receiver == address
        )

    def balance(self, address: str) -> int:
        return self.balances.get(address, 0)


class Ledger:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._wallets: dict[str, Wallet] = {}
        self._transactions: dict[str, Transaction] = {}
        self._addr_index: dict[str, set[str]] = {}

    # ------------------------------------------------------------------ #
    # 钱包摄入
    # ------------------------------------------------------------------ #
    def ingest_wallet(self, wallet: Wallet) -> IngestResult:
        if not wallet.address:
            raise InvalidTransactionError("钱包地址不能为空")
        with self._lock:
            existing = self._wallets.get(wallet.address)
            if existing is None:
                self._wallets[wallet.address] = wallet
                self._addr_index.setdefault(wallet.address, set())
                return IngestResult(IngestStatus.ACCEPTED, wallet.address, "新钱包已入账")
            if existing == wallet:
                return IngestResult(
                    IngestStatus.DUPLICATE, wallet.address, "钱包已存在且内容一致（幂等命中）"
                )
            for name in _WALLET_FIELDS:
                old = getattr(existing, name)
                new = getattr(wallet, name)
                if old != new:
                    raise ContentConflictError(wallet.address, name, old, new)
            # 理论不可达：address 相同且字段全相等时 frozen dataclass 必然相等
            raise ContentConflictError(wallet.address, "<unknown>", existing, wallet)

    # ------------------------------------------------------------------ #
    # 交易摄入
    # ------------------------------------------------------------------ #
    def ingest_transaction(self, tx: Transaction) -> IngestResult:
        self._validate(tx)
        with self._lock:
            existing = self._transactions.get(tx.tx_id)
            if existing is not None:
                if existing == tx:
                    return IngestResult(
                        IngestStatus.DUPLICATE,
                        tx.tx_id,
                        "交易已存在且内容一致（幂等命中，不重复计入）",
                    )
                for name in _TX_FIELDS:
                    old = getattr(existing, name)
                    new = getattr(tx, name)
                    if old != new:
                        raise ContentConflictError(tx.tx_id, name, old, new)
                raise ContentConflictError(tx.tx_id, "<unknown>", existing, tx)

            self._transactions[tx.tx_id] = tx
            self._addr_index.setdefault(tx.sender, set()).add(tx.tx_id)
            self._addr_index.setdefault(tx.receiver, set()).add(tx.tx_id)
            return IngestResult(
                IngestStatus.ACCEPTED,
                tx.tx_id,
                "新交易已入账",
                detail=f"位置=({tx.block},{tx.tx_index}) {tx.sender}->{tx.receiver} {tx.amount}",
            )

    def _validate(self, tx: Transaction) -> None:
        if not tx.tx_id:
            raise InvalidTransactionError("交易 ID 不能为空")
        if tx.block < 0:
            raise InvalidTransactionError(f"区块高度非法: {tx.block}")
        if tx.tx_index < 0:
            raise InvalidTransactionError(f"区块内序号非法: {tx.tx_index}")
        if tx.amount <= 0:
            raise InvalidTransactionError(f"交易金额必须为正: {tx.amount}")
        if tx.sender == tx.receiver:
            raise InvalidTransactionError(f"交易付款方与收款方不能相同: {tx.sender}")
        with self._lock:
            if tx.sender not in self._wallets:
                raise InvalidTransactionError(f"未知付款方钱包: {tx.sender}")
            if tx.receiver not in self._wallets:
                raise InvalidTransactionError(f"未知收款方钱包: {tx.receiver}")

    # ------------------------------------------------------------------ #
    # 查询（均在锁内复制，杜绝中间态泄漏）
    # ------------------------------------------------------------------ #
    def get_wallet(self, address: str) -> Wallet | None:
        with self._lock:
            return self._wallets.get(address)

    def get_transaction(self, tx_id: str) -> Transaction | None:
        with self._lock:
            return self._transactions.get(tx_id)

    def transactions_for(self, address: str) -> tuple[Transaction, ...]:
        with self._lock:
            ids = self._addr_index.get(address, set())
            return tuple(sorted((self._transactions[i] for i in ids), key=canonical_key))

    def all_transactions(self) -> tuple[Transaction, ...]:
        with self._lock:
            return tuple(sorted(self._transactions.values(), key=canonical_key))

    def all_wallets(self) -> dict[str, Wallet]:
        with self._lock:
            return dict(self._wallets)

    def chain_tip(self) -> tuple[int, int]:
        with self._lock:
            if not self._transactions:
                return (0, 0)
            top = max(self._transactions.values(), key=lambda t: (t.block, t.tx_index))
            return (top.block, top.tx_index)

    def wallet_balance(self, address: str) -> int:
        with self._lock:
            if address not in self._wallets:
                raise LedgerNotFoundError(f"钱包不存在: {address}")
            total = 0
            for tx_id in self._addr_index.get(address, ()):  # type: ignore[arg-type]
                tx = self._transactions[tx_id]
                if tx.receiver == address:
                    total += tx.amount
                if tx.sender == address:
                    total -= tx.amount
            return total

    def snapshot(self) -> LedgerSnapshot:
        """在单次加锁内取全部状态的不可变快照（查询一致性的核心）。"""
        with self._lock:
            ordered = tuple(sorted(self._transactions.values(), key=canonical_key))
            balances: dict[str, int] = {addr: 0 for addr in self._wallets}
            for tx in ordered:
                balances[tx.sender] = balances.get(tx.sender, 0) - tx.amount
                balances[tx.receiver] = balances.get(tx.receiver, 0) + tx.amount
            tip = (ordered[-1].block, ordered[-1].tx_index) if ordered else (0, 0)
            return LedgerSnapshot(dict(self._wallets), ordered, tip, balances)
