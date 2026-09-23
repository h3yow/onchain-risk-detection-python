"""本地端到端演示：合成数据 -> 乱序/迟到摄入 -> 账本 -> 风险结论。

运行：uv run python scripts/demo.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from risk_engine.errors import ConflictError
from risk_engine.service import RiskService
from risk_engine import synthetic
from tests.helpers import log_decision, log_stream  # noqa: E402

import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger("demo")


def main() -> None:
    svc = RiskService()
    for w in synthetic.build_wallets():
        svc.register_wallet(w)

    arrival = synthetic.arrival_order()  # 乱序、迟到交易最后到、夹带重复提交
    log_stream(logger, arrival, "(到达顺序)")
    results = svc.ingest_many(arrival)
    accepted = sum(r.accepted for r in results)
    duplicated = sum(r.duplicate for r in results)
    logger.info("摄入结果：接受 %d 笔，幂等忽略 %d 笔", accepted, duplicated)

    # 内容不一致的冲突提交
    try:
        svc.ingest_transaction(synthetic.conflicting_submission())
    except ConflictError as exc:
        logger.info("冲突被拒绝：%s（归类=%s，字段=%s）", exc, type(exc).__name__, exc.field_name)

    view = svc.ledger_view()
    logger.info("账本：tx_count=%d last_block=%d", view.tx_count, view.last_block)
    for addr in sorted(view.balances):
        logger.info("  余额 %-9s = %.2f (交互 %d 笔)", addr, view.balances[addr], view.nonces.get(addr, 0))

    for addr in ["H", "X", "R", "F", "VIC", "MID", "U", "CLEAN", "GHOST"]:
        log_decision(logger, svc.evaluate(addr))


if __name__ == "__main__":
    main()
