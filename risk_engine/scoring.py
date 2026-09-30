"""确定性风险评分：因子聚合、维度级证据状态、完整度汇总与结论保护。

确定性保证：
1) 输入交易在评分前按区块全序排序，与摄入顺序无关；
2) 因子内部对地址集合排序，证据顺序固定；
3) determinism_key 是 (地址, 有序交易, 高风险集合) 的哈希指纹，
   同一输入两次评估的 key、结论与完整度必然一致。

结论保护（“没查出来问题”不等于“查过了确实没问题”）：
- 每个维度自带 SUFFICIENT / INSUFFICIENT / NO_DATA 三态，只有 SUFFICIENT
  才计入“可评估维度”；
- 地址参与交互笔数 < MIN_TX_FOR_DECISION，或可评估维度数
  < MIN_EVALUABLE_FOR_GRADE 时，level 只能是 UNKNOWN（下不了结论），
  并逐条列出整体与维度级原因，绝不落到 LOW（看似无风险）一档。
- 维度状态只取决于全量交易（随交易补录单调从 NO_DATA -> INSUFFICIENT
  -> SUFFICIENT 演进），因此完整度只会随历史数据补录上升，不会回落。
"""
from __future__ import annotations

import hashlib
import json

from .factors import FactorCalculator
from .models import (
    CompletenessSummary,
    ConfidenceLevel,
    EvidenceStatus,
    Insufficiency,
    RiskLevel,
    RiskScore,
    Transaction,
)


class RiskScorer:
    MEDIUM_THRESHOLD = 30.0
    HIGH_THRESHOLD = 60.0
    MIN_TX_FOR_DECISION = 2  # 参与交易少于 2 笔视为数据不足
    # 六个维度中至少要有 3 个证据充分，整体结论才允许分级（LOW/MEDIUM/HIGH）；
    # 否则只能给 UNKNOWN，避免“只有一两个维度没查出问题”被误读为低风险。
    MIN_EVALUABLE_FOR_GRADE = 3
    # 可信度门槛：>=2/3 维度可评估为 HIGH；达到可分级门槛为 MEDIUM；更低为 LOW。
    HIGH_COVERAGE_CONFIDENCE = 4

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

        # 维度级缺证据原因：顺序与 factors 一致，保证确定性
        dimension_reasons: list[Insufficiency] = []
        evaluated: list = []
        not_evaluated: list = []
        pending = 0
        missing = 0
        for f in factors:
            if f.status is EvidenceStatus.SUFFICIENT:
                evaluated.append(f.code)
            else:
                not_evaluated.append(f.code)
                if f.status is EvidenceStatus.INSUFFICIENT:
                    pending += 1
                else:
                    missing += 1
                dimension_reasons.append(
                    Insufficiency(
                        code=f.insufficiency_code or "DIMENSION_NOT_EVALUABLE",
                        detail=f.insufficiency_detail or f.detail,
                        factor=f.code,
                    )
                )

        total = len(factors)
        n_evaluated = len(evaluated)
        coverage = round(n_evaluated / total, 4) if total else 0.0
        sufficient_for_grade = n_evaluated >= self.MIN_EVALUABLE_FOR_GRADE
        if n_evaluated >= self.HIGH_COVERAGE_CONFIDENCE:
            confidence = ConfidenceLevel.HIGH
        elif sufficient_for_grade:
            confidence = ConfidenceLevel.MEDIUM
        else:
            confidence = ConfidenceLevel.LOW
        completeness = CompletenessSummary(
            total_dimensions=total,
            evaluated_dimensions=n_evaluated,
            pending_dimensions=pending,
            missing_dimensions=missing,
            evaluated=tuple(evaluated),
            not_evaluated=tuple(not_evaluated),
            coverage_ratio=coverage,
            confidence=confidence,
            sufficient_for_grade=sufficient_for_grade,
        )

        # 整体级原因（先列账本/参与度这类硬性不足，再列覆盖率原因）
        global_reasons: list[Insufficiency] = []
        if not self._txs:
            global_reasons.append(
                Insufficiency(
                    code="EMPTY_LEDGER",
                    detail="账本中尚无任何交易，无证据可评估",
                )
            )
        if participation < self.MIN_TX_FOR_DECISION:
            global_reasons.append(
                Insufficiency(
                    code="INSUFFICIENT_TRANSACTIONS",
                    detail=(
                        f"地址 {address} 仅有 {participation} 笔参与交易，"
                        f"少于判定所需的最少 {self.MIN_TX_FOR_DECISION} 笔，"
                        "证据不足，无法判定风险"
                    ),
                )
            )
        if not sufficient_for_grade:
            pending_names = "、".join(c.value for c in not_evaluated) or "无"
            global_reasons.append(
                Insufficiency(
                    code="DIMENSION_COVERAGE_INSUFFICIENT",
                    detail=(
                        f"六个风险维度中仅 {n_evaluated} 个证据充分"
                        f"（至少需要 {self.MIN_EVALUABLE_FOR_GRADE} 个才能分级），"
                        f"未评上的维度：{pending_names}；目前下不了结论，"
                        "不得按低风险处理"
                    ),
                )
            )

        reasons = tuple(global_reasons + dimension_reasons)

        raw_score = sum(f.weight for f in factors)
        score = round(min(100.0, max(0.0, raw_score)), 2)

        # 能否分级只由整体级门槛决定；维度级原因始终列出供复核，
        # 但只要覆盖率达标，没评上的维度不阻止给出明确分级。
        if global_reasons:
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
            insufficient_reasons=reasons,
            evaluated_at_block=evaluated_at_block,
            determinism_key=self.determinism_key(address, self._txs, self._high_risk),
            completeness=completeness,
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
