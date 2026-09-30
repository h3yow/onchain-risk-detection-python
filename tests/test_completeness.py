"""维度级证据充分度、完整度汇总与历史补录重新判定测试。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from risk_engine.models import EvidenceStatus, FactorCode, RiskLevel, Transaction
from risk_engine.service import RiskService
from risk_engine import synthetic
from tests.helpers import log_decision, log_stream


# W 的初始流水：只有 2 笔出账（方向单一、对手方不足、时间跨度短）
def _initial_txs() -> list[Transaction]:
    return [
        Transaction("w-init-1", "W", "P1", 10.0, 10, 0, 100.0),
        Transaction("w-init-2", "W", "P2", 11.0, 10, 1, 101.0),
    ]


# 迟到/乱序到达的历史流水（区块更早）：补齐入账方向与时间跨度，
# 但刻意不触发汇聚/分散（来源、目标都不足 3 个），用于验证“查过了确实没问题”的 LOW
def _backfill_txs() -> list[Transaction]:
    return [
        Transaction("w-late-1", "S1", "W", 5.0, 1, 0, 10.0),
        Transaction("w-late-2", "S2", "W", 6.0, 1, 1, 11.0),
    ]


def _status_map(score) -> dict:
    return {f.code: f.evidence_status for f in score.factors}


def _gap_map(score) -> dict:
    return {f.code: (f.gap_code, f.gap_detail) for f in score.factors}


def test_low_info_account_never_looks_safe(service, logger):
    """低信息账户：无论分数多低都不能给 LOW/MEDIUM/HIGH，只能 UNKNOWN。"""
    txs = _initial_txs()
    log_stream(logger, txs, "(低信息账户初始流水)")
    service.ingest_many(txs)

    score = service.evaluate("W")
    log_decision(logger, score)
    assert score.level is RiskLevel.UNKNOWN, "可评估维度太少时不得给任何分级结论"
    comp = score.completeness
    assert comp is not None
    assert comp.evaluated_dimensions < 3
    assert comp.confidence.value == "LOW"
    # 覆盖门禁原因 + 每个未评上维度的逐条原因都必须列出
    codes = {r.code for r in score.inconclusive_reasons}
    assert "INSUFFICIENT_DIMENSION_COVERAGE" in codes
    missing_codes = {g.code for g in comp.missing_dimensions}
    assert len(missing_codes) == comp.total_dimensions - comp.evaluated_dimensions
    for g in comp.missing_dimensions:
        assert f"{g.code.value}__{g.gap_code}" in codes

    # 完全没见过的地址同样不能“看似无风险”
    ghost = service.evaluate("NEVER_SEEN")
    log_decision(logger, ghost)
    assert ghost.level is RiskLevel.UNKNOWN
    assert ghost.completeness.evaluated_dimensions == 0
    assert all(
        g.evidence_status is EvidenceStatus.NO_DATA
        for g in ghost.completeness.missing_dimensions
    )


def test_dimension_gap_reasons_are_distinguishable(service, logger):
    """同一报告里要能区分证据充分 / 证据不足 / 完全无数据，且缺什么讲清楚。"""
    tx = Transaction("ab-1", "A", "B", 10.0, 1, 0, 1.0)
    log_stream(logger, [tx], "(单笔交易)")
    service.ingest_transaction(tx)

    # 全新地址：6 个维度全部 NO_DATA
    ghost = service.evaluate("GHOST")
    log_decision(logger, ghost)
    assert set(_status_map(ghost).values()) == {EvidenceStatus.NO_DATA}
    assert all(g.gap_code for g in ghost.completeness.missing_dimensions)

    # A 只有出账：入账维度无数据，其余维度“有观测但不足”
    score_a = service.evaluate("A")
    log_decision(logger, score_a)
    st = _status_map(score_a)
    gaps = _gap_map(score_a)
    assert st[FactorCode.FUND_CONVERGENCE] is EvidenceStatus.NO_DATA
    assert gaps[FactorCode.FUND_CONVERGENCE][0] == "NO_INCOMING_TX"
    assert st[FactorCode.FUND_DISPERSION] is EvidenceStatus.INSUFFICIENT
    assert gaps[FactorCode.FUND_DISPERSION][0] == "TOO_FEW_OUTGOING_TARGETS"
    assert st[FactorCode.RING_FLOW] is EvidenceStatus.INSUFFICIENT
    assert gaps[FactorCode.RING_FLOW][0] == "SINGLE_DIRECTION_ONLY"
    assert st[FactorCode.HIGH_FREQ_INTERACTION] is EvidenceStatus.INSUFFICIENT
    assert gaps[FactorCode.HIGH_FREQ_INTERACTION][0] == "TIME_SPAN_TOO_SHORT"
    assert st[FactorCode.HIGH_RISK_DIRECT] is EvidenceStatus.INSUFFICIENT
    assert gaps[FactorCode.HIGH_RISK_DIRECT][0] == "TOO_FEW_COUNTERPARTIES"
    assert st[FactorCode.HIGH_RISK_INDIRECT] is EvidenceStatus.INSUFFICIENT
    assert gaps[FactorCode.HIGH_RISK_INDIRECT][0] == "NO_TWO_HOP_PATH"
    # 每个不足原因都必须有人可读的“缺什么”说明
    for code, (gap_code, gap_detail) in gaps.items():
        assert gap_code and gap_detail and len(gap_detail) > 10

    # 补录后：未触发但证据充分（“查过了确实没问题”）与仍然不足的维度并存
    service.ingest_many(_initial_txs())
    service.ingest_many(_backfill_txs())
    score_w = service.evaluate("W")
    log_decision(logger, score_w)
    stw = _status_map(score_w)
    # 入账方向补齐后：环/高频/直接关联都“查过了确实没问题”
    assert stw[FactorCode.RING_FLOW] is EvidenceStatus.SUFFICIENT
    assert stw[FactorCode.HIGH_FREQ_INTERACTION] is EvidenceStatus.SUFFICIENT
    assert stw[FactorCode.HIGH_RISK_DIRECT] is EvidenceStatus.SUFFICIENT
    # 来源/目标仍不足 3，2 跳路径展不开：这三个维度依然是“有观测但不足”
    assert stw[FactorCode.FUND_CONVERGENCE] is EvidenceStatus.INSUFFICIENT
    assert stw[FactorCode.FUND_DISPERSION] is EvidenceStatus.INSUFFICIENT
    assert stw[FactorCode.HIGH_RISK_INDIRECT] is EvidenceStatus.INSUFFICIENT
    # 三种状态在这一轮评估集合中都出现过
    statuses = set()
    for s in (ghost, score_a, score_w):
        statuses.update(f.evidence_status for f in s.factors)
    assert statuses == {EvidenceStatus.SUFFICIENT, EvidenceStatus.INSUFFICIENT, EvidenceStatus.NO_DATA}


def test_backfill_history_raises_completeness_and_redecides(service, logger):
    """补录历史交易：完整度单调上升，UNKNOWN 自动翻成明确分级，逐维度原因同步更新。"""
    service.ingest_many(_initial_txs())
    before = service.evaluate("W")
    log_decision(logger, before)
    assert before.level is RiskLevel.UNKNOWN
    assert before.completeness.evaluated_dimensions == 1

    # 数据没变化时反复评估：结论与完整度必须完全一致（含确定性指纹）
    again = service.evaluate("W")
    assert again == before

    # 按“到达顺序”逐笔补录，覆盖度只能往上走
    arrival = list(reversed(_backfill_txs()))  # 故意乱序：block 2 的先到
    log_stream(logger, arrival, "(迟到历史补录)")
    coverages = [before.completeness.coverage_ratio]
    for tx in arrival:
        service.ingest_transaction(tx)
        cur = service.evaluate("W")
        log_decision(logger, cur)
        coverages.append(cur.completeness.coverage_ratio)
    assert all(b <= a for b, a in zip(coverages, coverages[1:])), (
        f"完整度必须单调不降: {coverages}"
    )

    after = service.evaluate("W")
    log_decision(logger, after)
    # 覆盖抬过判定线：从“下不了结论”变为明确分级
    assert after.completeness.evaluated_dimensions >= 3
    assert after.level is RiskLevel.LOW, "补录后无风险因子且证据充分，应为明确 LOW"
    assert not after.inconclusive_reasons
    # 输入指纹随补录变化，级别随覆盖门禁翻转为明确分级
    assert after.determinism_key != before.determinism_key
    # 逐维度原因同步更新：之前缺的维度现在大多充分，剩余缺口仍然挂原因
    for f in after.factors:
        if f.evidence_status is EvidenceStatus.SUFFICIENT:
            assert f.gap_code is None
        else:
            assert f.gap_code and f.gap_detail

    # 与“按区块顺序一次性全部录入”的结果完全一致（同样注册钱包/高风险名单）
    serial = RiskService()
    for w in synthetic.build_wallets():
        serial.register_wallet(w)
    serial.ingest_many(
        sorted(
            _initial_txs() + _backfill_txs(),
            key=lambda t: (t.block_height, t.block_index, t.tx_id),
        )
    )
    assert serial.evaluate("W") == after


def test_concurrent_backfill_queries_stay_consistent(service, canonical_txs, logger):
    """并发重复补录只算一次；补录期间的任何查询都是完整批次状态且结论自洽。"""
    service.ingest_many(_initial_txs())
    backfill = _backfill_txs()
    n_threads = 8
    barrier = threading.Barrier(n_threads)
    stop = threading.Event()
    observations: list = []
    obs_lock = threading.Lock()
    tx_counts: set[int] = set()

    def reader():
        while not stop.is_set():
            view = service.ledger_view()
            score = service.evaluate("W")
            with obs_lock:
                tx_counts.add(view.tx_count)
                observations.append((view.tx_count, score))

    def worker(_):
        barrier.wait()
        # 同一批历史交易被多个线程并发重复补录
        service.ingest_many(list(backfill))

    t = threading.Thread(target=reader)
    t.start()
    log_stream(logger, backfill, f"(并发补录 {n_threads} 线程重复同批)")
    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        list(pool.map(worker, range(n_threads)))
    stop.set()
    t.join()

    final = service.evaluate("W")
    log_decision(logger, final)
    # 同批并发重复补录只能算一次
    assert service.ledger_view().tx_count == len(_initial_txs()) + len(backfill)
    assert final.level is RiskLevel.LOW
    assert final.completeness.evaluated_dimensions >= 3

    # 补录全程的每次观察都必须自洽：
    # 计数只落在“初始 / 整批补录后”两个边界上；结论与依据不能互相矛盾
    assert tx_counts <= {len(_initial_txs()), len(_initial_txs()) + len(backfill)}, (
        f"看到只处理了一半的批次: {tx_counts}"
    )
    for count, score in observations:
        comp = score.completeness
        # 分数必须等于触发因子权重和
        assert score.score == round(
            min(100.0, max(0.0, sum(f.weight for f in score.factors))), 2
        )
        # 级别与覆盖门禁一致：覆盖不足必 UNKNOWN；UNKNOWN 必有原因
        if comp.evaluated_dimensions < 3 or count < 2:
            assert score.level is RiskLevel.UNKNOWN
            assert score.inconclusive_reasons
        else:
            assert score.level in (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH)
            assert not score.inconclusive_reasons
        # 每个因子状态与其缺口字段对得上
        for f in score.factors:
            if f.evidence_status is EvidenceStatus.SUFFICIENT:
                assert f.gap_code is None and f.gap_detail is None
            else:
                assert f.gap_code and f.gap_detail
        # 缺失维度清单与因子状态一一对应
        assert len(comp.missing_dimensions) == comp.total_dimensions - comp.evaluated_dimensions

    # 最终结果等于按区块顺序一次性录入（同样注册钱包/高风险名单）
    serial = RiskService()
    for w in synthetic.build_wallets():
        serial.register_wallet(w)
    serial.ingest_many(
        sorted(
            _initial_txs() + backfill,
            key=lambda t: (t.block_height, t.block_index, t.tx_id),
        )
    )
    assert serial.evaluate("W") == final
    logger.info("并发补录观察到的 tx_count 集合=%s（只落在批次边界）", sorted(tx_counts))
