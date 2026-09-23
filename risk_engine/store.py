"""并发安全的内存存储：钱包、交易索引与高风险名单。"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .errors import WalletConflictError
from .models import Transaction, Wallet, WalletType


@dataclass
class _State:
    wallets: dict[str, Wallet] = field(default_factory=dict)
    transactions: dict[str, Transaction] = field(default_factory=dict)
    high_risk: set[str] = field(default_factory=set)


class ThreadSafeStore:
    """以单一可重入锁保护全部可变状态。

    所有读写都经过同一把锁；交易与钱包均为不可变对象，快照复制容器后
    在锁外使用也安全。锁内不执行任何等待操作，因此：
    - 并发写入串行化，不会丢更新或重复计数；
    - 查询拿到的是某一时刻的完整一致状态，不含半成品。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state = _State()

    def lock(self) -> threading.RLock:
        """暴露锁，供 Ledger/Service 把多步操作包成一个原子段。"""
        return self._lock

    def put_wallet(self, wallet: Wallet) -> None:
        with self._lock:
            existing = self._state.wallets.get(wallet.address)
            if existing is not None and existing.wallet_type != wallet.wallet_type:
                raise WalletConflictError(
                    wallet.address,
                    "wallet_type",
                    existing.wallet_type.value,
                    wallet.wallet_type.value,
                )
            self._state.wallets[wallet.address] = wallet
            if wallet.wallet_type is WalletType.HIGH_RISK:
                self._state.high_risk.add(wallet.address)

    def get_wallet(self, address: str) -> Wallet | None:
        with self._lock:
            return self._state.wallets.get(address)

    def put_transaction(self, tx: Transaction) -> None:
        with self._lock:
            self._state.transactions[tx.tx_id] = tx

    def get_transaction(self, tx_id: str) -> Transaction | None:
        with self._lock:
            return self._state.transactions.get(tx_id)

    def all_transactions(self) -> list[Transaction]:
        with self._lock:
            return list(self._state.transactions.values())

    def mark_high_risk(self, address: str) -> None:
        with self._lock:
            self._state.high_risk.add(address)

    def is_high_risk(self, address: str) -> bool:
        with self._lock:
            return address in self._state.high_risk

    def high_risk_snapshot(self) -> set[str]:
        with self._lock:
            return set(self._state.high_risk)

    def snapshot(self) -> _State:
        """返回容器的防御性拷贝；元素不可变，可在锁外安全读取。"""
        with self._lock:
            return _State(
                wallets=dict(self._state.wallets),
                transactions=dict(self._state.transactions),
                high_risk=set(self._state.high_risk),
            )
