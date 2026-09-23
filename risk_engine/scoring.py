"""确定性风险评分：因子聚合、阈值判定、数据不足保护。

确定性保证：
1) 输入交易在评分前按区块全序排序，与摄入顺序无关；
2) 因子内部对地址集合排序，证据顺序固定；
3) determinism_key 是 (地址, 有序交易, 高风险集合) 的哈希指纹，
   同一输入两次评估的 key 与结论必然一致。

数据不足保护：地址参与交互笔数 < MIN_TX_FOR_DECISION 时 level=UNKNOWN，
明确列出原因，绝不默认判为安全（LOW）。
"""
from __future__ import annotations

import hashlib
import json

from .factors import FactorCalculator
from .models import (
    Insufficiency,
    RiskLevel,
    RiskScore,
    Transaction,
)


class RiskScorer:
    MEDIUM_THRESHOLD = 30.0
    HIGH_THRESHOLD = 60.0
    MIN_TX_FOR_DECISION = 2  # 参与交易少于 2 笔视为数据不足

    def __init__(self, transactions: list[Transaction], high_risk: set[str]) -> None:
        self._txs = sorted(
            transactions,
            key=lambda t: (t.block_height, t.block_index, t.tx_id),
        )
        self._high_risk = set(high_risk)
        self._calc = FactorCalculator(self._txs, self._high_risk)

    def evaluate(self, address: str) -> RiskScore:
        factors = self._calc.evaluate(address)
        participation = sum(
            1 for t in self._txs if t.from_addr == address or t.to_addr == address
        )

        reasons: list[Insufficiency] = []
        if participation < self.MIN_TX_FOR_DECISION:
            reasons.append(
                Insufficiency(
                    code="INSUFFICIENT_TRANSACTIONS",
                    detail=(
                        f"地址 {address} 仅有 {participation} 笔参与交易，"
                        f"少于判定所需的最少 {self.MIN_TX_FOR_DECISION} 笔，"
                        "证据不足，无法判定风险"
                    ),
                )
            )
        if not self._txs:
            reasons.append(
                Insufficiency(
                    code="EMPTY_LEDGER",
                    detail="账本中尚无任何交易，无证据可评估",
                )
            )

        raw_score = sum(f.weight for f in factors)
        score = round(min(100.0, max(0.0, raw_score)), 2)

        if reasons:
            level = RiskLevel.UNKNOWN
        elif score >= self.HIGH_THRESHOLD:
            level = RiskLevel.HIGH
        elif score >= self.MEDIUM_THRESHOLD:
            level = RiskLevel.MEDIUM
        else:
            level = RiskLevel.LOW

        evaluated_at_block = (
            max(
                (
                    t.block_height
                    for t in self._txs
                    if t.from_addr == address or t.to_addr == address
                ),
                default=0,
            )
        )

        return RiskScore(
            address=address,
            score=score,
            level=level,
            factors=tuple(factors),
            insufficient_reasons=tuple(reasons),
            evaluated_at_block=evaluated_at_block,
            determinism_key=self.determinism_key(address, self._txs, self._high_risk),
        )

    @staticmethod
    def determinism_key(address: str, txs: list[Transaction], high_risk: set[str]) -> str:
        payload = {
            "address": address,
            "high_risk": sorted(high_risk),
            "txs": [
                [
                    t.tx_id,
                    t.from_addr,
                    t.to_addr,
                    t.amount,
                    t.block_height,
                    t.block_index,
                    t.timestamp,
                ]
                for t in txs
            ],
        }
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
