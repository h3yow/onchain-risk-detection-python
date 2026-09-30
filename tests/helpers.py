"""测试日志辅助：打印输入流水与判定依据。"""
from __future__ import annotations

import logging

from risk_engine.models import RiskScore, Transaction


def log_stream(logger: logging.Logger, txs: list[Transaction], note: str = "") -> None:
    """打印输入流水：到达序号、tx_id、方向、金额、区块。"""
    logger.info("==== 输入流水 %s 共 %d 笔 ====", note, len(txs))
    for i, t in enumerate(txs):
        logger.info(
            "INGEST-STREAM #%02d tx_id=%s %s->%s amount=%s block=%s.%s ts=%s",
            i,
            t.tx_id,
            t.from_addr,
            t.to_addr,
            t.amount,
            t.block_height,
            t.block_index,
            t.timestamp,
        )


def log_decision(logger: logging.Logger, score: RiskScore) -> None:
    """打印判定依据：总分、级别、每个因子、数据不足原因、完整度汇总。"""
    comp = score.completeness
    comp_summary = (
        f"coverage={comp.evaluated_dimensions}/{comp.total_dimensions}"
        f"({comp.coverage_ratio}) confidence={comp.confidence.value}"
        if comp is not None
        else "coverage=n/a"
    )
    logger.info(
        "DECISION addr=%s level=%s score=%s @block=%s key=%s %s",
        score.address,
        score.level.value,
        score.score,
        score.evaluated_at_block,
        score.determinism_key,
        comp_summary,
    )
    for f in score.factors:
        logger.info(
            "  FACTOR %-20s triggered=%-5s weight=%-5s evidence=%-12s gap=%s | %s",
            f.code.value,
            f.triggered,
            f.weight,
            f.evidence_status.value,
            f.gap_code or "-",
            f.detail,
        )
    for r in score.insufficient_reasons:
        logger.info("  UNKNOWN-REASON %s | %s", r.code, r.detail)
    for r in score.inconclusive_reasons:
        logger.info("  INCONCLUSIVE %s | %s", r.code, r.detail)
    if comp is not None:
        for g in comp.missing_dimensions:
            logger.info(
                "  MISSING-DIM %s status=%s gap=%s | %s",
                g.code.value,
                g.evidence_status.value,
                g.gap_code,
                g.gap_detail,
            )
