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
    """打印判定依据：总分、级别、完整度、每个因子的证据状态与数据不足原因。"""
    c = score.completeness
    completeness_text = (
        f" completeness={c.evaluated_dimensions}/{c.total_dimensions}"
        f"(pending={c.pending_dimensions},missing={c.missing_dimensions})"
        f" coverage={c.coverage_ratio} confidence={c.confidence.value}"
        f" gradeable={c.sufficient_for_grade}"
        if c is not None
        else ""
    )
    logger.info(
        "DECISION addr=%s level=%s score=%s @block=%s key=%s%s",
        score.address,
        score.level.value,
        score.score,
        score.evaluated_at_block,
        score.determinism_key,
        completeness_text,
    )
    for f in score.factors:
        logger.info(
            "  FACTOR %-20s status=%-12s triggered=%-5s weight=%-5s | %s%s",
            f.code.value,
            f.status.value,
            f.triggered,
            f.weight,
            f.detail,
            f" | 缺证据原因={f.insufficiency_code}: {f.insufficiency_detail}"
            if f.insufficiency_code
            else "",
        )
    for r in score.insufficient_reasons:
        scope = r.factor.value if r.factor else "OVERALL"
        logger.info("  UNKNOWN-REASON [%-22s] %s | %s", scope, r.code, r.detail)
