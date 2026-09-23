"""重复交易、内容冲突、乱序与迟到历史交易回放测试。"""
from __future__ import annotations

import pytest

from risk_engine.errors import ConflictError, ValidationError
from risk_engine.models import Transaction
from tests.helpers import log_stream
from risk_engine import synthetic


def test_duplicate_transaction_is_idempotent(service, canonical_txs, logger):
    stream = [canonical_txs[0], canonical_txs[0], canonical_txs[0]]
    log_stream(logger, stream, "(重复提交)")

    r1 = service.ingest_transaction(stream[0])
    r2 = service.ingest_transaction(stream[1])
    r3 = service.ingest_transaction(stream[2])

    assert r1.accepted and not r1.duplicate
    assert not r2.accepted and r2.duplicate
    assert not r3.accepted and r3.duplicate

    view = service.ledger_view()
    assert view.tx_count == 1, "重复交易不得重复计数"
    tid = canonical_txs[0]
    expected_bal = {tid.from_addr: -tid.amount, tid.to_addr: tid.amount}
    assert view.balances == expected_bal
    logger.info("幂等后余额=%s 交易数=%s", view.balances, view.tx_count)


def test_conflict_fields_are_distinguishable(service, canonical_txs, logger):
    tx = next(t for t in canonical_txs if t.tx_id == "tx-90")
    service.ingest_transaction(tx)

    cases = {
        "amount": Transaction(tx.tx_id, tx.from_addr, tx.to_addr, tx.amount + 1, tx.block_height, tx.block_index, tx.timestamp),
        "from_addr": Transaction(tx.tx_id, "ZZZ", tx.to_addr, tx.amount, tx.block_height, tx.block_index, tx.timestamp),
        "to_addr": Transaction(tx.tx_id, tx.from_addr, "ZZZ", tx.amount, tx.block_height, tx.block_index, tx.timestamp),
        "block_height": Transaction(tx.tx_id, tx.from_addr, tx.to_addr, tx.amount, tx.block_height + 9, tx.block_index, tx.timestamp),
        "timestamp": Transaction(tx.tx_id, tx.from_addr, tx.to_addr, tx.amount, tx.block_height, tx.block_index, tx.timestamp + 9),
    }
    for field_name, bad in cases.items():
        log_stream(logger, [tx, bad], f"(冲突字段={field_name})")
        with pytest.raises(ConflictError) as ei:
            service.ingest_transaction(bad)
        assert ei.value.field_name == field_name, f"必须指出差异字段 {field_name}"
        logger.info("冲突归类=ConflictError 字段=%s 原因=%s", ei.value.field_name, ei.value)

    assert service.ledger_view().tx_count == 1, "冲突提交必须被拒绝，不得写入"


def test_out_of_order_arrival_replays_by_block(service, canonical_txs, logger):
    shuffled = synthetic.shuffled_view(canonical_txs)
    log_stream(logger, shuffled, "(乱序到达)")
    service.ingest_many(shuffled)

    ordered_ids = [t.tx_id for t in service._ledger.replay_in_order()]
    expected_ids = [t.tx_id for t in canonical_txs]  # canonical 已按区块全序
    assert ordered_ids == expected_ids
    assert ordered_ids[0] == "tx-late", "迟到的 block 0 历史交易必须回放到最前"

    view = service.ledger_view()
    # 迟到交易 (LATE_SRC->X, 25) 必须体现在余额中，且后续评估不会被“后来插入”污染
    assert view.balances["LATE_SRC"] == -25.0
    assert view.tx_count == len(canonical_txs)
    logger.info("乱序摄入后回放首笔=%s 末笔=%s tx_count=%s",
                ordered_ids[0], ordered_ids[-1], view.tx_count)


def test_late_history_tx_does_not_change_then_redo(service, canonical_txs, logger):
    # 先摄入 block>=5 的交易并评估 X，再补到 block 0 的历史交易，
    # 结论只需在包含完整输入时保持确定；不允许出现重复计数。
    later = [t for t in canonical_txs if t.block_height >= 5]
    late = next(t for t in canonical_txs if t.tx_id == "tx-late")
    service.ingest_many(later)
    before = service.ledger_view().balances.get("X")
    log_stream(logger, [late], "(迟到历史交易)")
    service.ingest_transaction(late)
    after = service.ledger_view()

    assert after.balances["LATE_SRC"] == -25.0
    assert after.balances["X"] == before + 25.0
    assert after.tx_count == len(later) + 1
    # 再次提交迟到交易 -> 幂等
    again = service.ingest_transaction(late)
    assert again.duplicate
    assert service.ledger_view().balances["X"] == after.balances["X"]


@pytest.mark.parametrize(
    "bad",
    [
        Transaction("", "A", "B", 1.0, 1, 0),
        Transaction("t", "", "B", 1.0, 1, 0),
        Transaction("t", "A", "B", 0.0, 1, 0),
        Transaction("t", "A", "B", -3.0, 1, 0),
        Transaction("t", "A", "B", 1.0, -1, 0),
        Transaction("t", "A", "A", 1.0, 1, 0),
    ],
)
def test_invalid_transactions_raise_validation_error(service, bad, logger):
    logger.info("非法输入: %s", bad)
    with pytest.raises(ValidationError):
        service.ingest_transaction(bad)
