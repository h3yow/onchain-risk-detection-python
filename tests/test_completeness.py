"""维度级证据状态、完整度汇总、补录重判与并发一致性测试。

覆盖需求：
- 低信息账户不会得到看似无风险的分级结论（只能 UNKNOWN）；
- 维度级缺证据原因可区分（无数据 / 样本方向单一 / 缺对手方 / 样本量不足）；
- 补录历史交易后完整度单调提升，越过覆盖线后从 UNKNOWN 变为明确分级；
- 同一数据反复评估结论与完整度完全一致；
- 并发重复补录只算一次，补录期间任何查询看到的结论与依据都对得上。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from risk_engine.models import EvidenceStatus, FactorCode, RiskLevel, Transaction
from tests.helpers import log_decision, log_stream


# ---- 补录场景：围绕 W 的三批历史数据 -------------------------------------
def _w_seed() -> list[Transaction]:
    """第一批：W 只有一笔出账，典型低信息账户。"""
    return [Transaction("w1", "W", "E1", 10.0, 100, 0, 1000.0)]


def _w_stage2() -> list[Transaction]:
    """补录第二批：给 W 一个入账来源，并给 E1 补一笔对外交易（提供二跳上下文）。"""
    return [
        Transaction("w2", "S1", "W", 8.0, 90, 0, 900.0),
        Transaction("w3", "E1", "M1", 4.0, 101, 0, 1100.0),
    ]


def _w_stage3() -> list[Transaction]:
    """补录第三批：第二个入账来源、第二个出账目标，时间戳继续拉开。"""
    return [
        Transaction("w4", "S2", "W", 7.0, 80, 0, 800.0),
        Transaction("w5", "W", "E2", 6.0, 102, 0, 1200.0),
    ]


def _status_map(score) -> dict[FactorCode, EvidenceStatus]:
    return {f.code: f.status for f in score.factors}


def _code_map(score) -> dict[FactorCode, str | None]:
    return {f.code: f.insufficiency_code for f in score.factors}


def test_low_information_account_is_never_graded(service, logger):
    txs = _w_seed()
    log_stream(logger, txs, "(低信息账户-单笔)")
    service.ingest_many(txs)

    score = service.evaluate("W")
    log_decision(logger, score)
    assert score.level is RiskLevel.UNKNOWN, "只有 1 笔交互时下不了结论，严禁给分级"
    assert score.score == 0.0, "证据不足的维度权重必须为 0"
    c = score.completeness
    assert c.evaluated_dimensions < 3
    assert c.sufficient_for_grade is False
    assert c.confidence.value == "LOW"
    overall = {r.code for r in score.insufficient_reasons if r.factor is None}
    assert "INSUFFICIENT_TRANSACTIONS" in overall
    assert "DIMENSION_COVERAGE_INSUFFICIENT" in overall
    # UNKNOWN 的具体原因逐条列出，且能归属到维度
    dim_reasons = {r.factor: r.code for r in score.insufficient_reasons if r.factor}
    assert dim_reasons[FactorCode.FUND_CONVERGENCE] == "NO_INCOMING_TX"
    # HIGH_RISK_DIRECT 有一个直接对手方，属于可评估维度，不出现在缺证据原因里
    assert FactorCode.HIGH_RISK_DIRECT not in dim_reasons


def test_dimension_insufficiency_reasons_are_distinguishable(service, logger):
    log_stream(logger, _w_seed(), "(维度原因区分-仅出账)")
    service.ingest_many(_w_seed())
    score = service.evaluate("W")
    log_decision(logger, score)
    statuses, codes = _status_map(score), _code_map(score)
    # 完全没数据 vs 有但不够，必须区分开
    assert statuses[FactorCode.FUND_CONVERGENCE] is EvidenceStatus.NO_DATA
    assert codes[FactorCode.FUND_CONVERGENCE] == "NO_INCOMING_TX"
    assert statuses[FactorCode.FUND_DISPERSION] is EvidenceStatus.INSUFFICIENT
    assert codes[FactorCode.FUND_DISPERSION] == "SINGLE_OUTGOING_TARGET"
    assert statuses[FactorCode.RING_FLOW] is EvidenceStatus.INSUFFICIENT
    assert codes[FactorCode.RING_FLOW] == "MISSING_COUNTERPARTY_DIRECTION"
    assert statuses[FactorCode.HIGH_FREQ_INTERACTION] is EvidenceStatus.INSUFFICIENT
    assert codes[FactorCode.HIGH_FREQ_INTERACTION] == "TOO_FEW_INTERACTIONS"
    assert statuses[FactorCode.HIGH_RISK_INDIRECT] is EvidenceStatus.INSUFFICIENT
    assert codes[FactorCode.HIGH_RISK_INDIRECT] == "NO_TWO_HOP_CONTEXT"
    # 有一个直接对手方 -> 直接关联维度证据充分且明确未触发，这是“查过没问题”
    assert statuses[FactorCode.HIGH_RISK_DIRECT] is EvidenceStatus.SUFFICIENT
    direct = next(f for f in score.factors if f.code is FactorCode.HIGH_RISK_DIRECT)
    assert direct.triggered is False and direct.weight == 0.0
    # 证据不足的维度不得带正权重
    for f in score.factors:
        if f.status is not EvidenceStatus.SUFFICIENT:
            assert f.triggered is False and f.weight == 0.0
            assert f.insufficiency_detail  # 必须讲清缺什么

    # 从未出现的地址：六个维度全部 NO_DATA
    ghost = service.evaluate("GHOST_ADDR")
    log_decision(logger, ghost)
    assert ghost.level is RiskLevel.UNKNOWN
    assert all(f.status is EvidenceStatus.NO_DATA for f in ghost.factors)
    assert ghost.completeness.missing_dimensions == 6


def test_backfill_lifts_completeness_and_re_grades(service, logger):
    service.ingest_many(_w_seed())
    s1 = service.evaluate("W")
    log_decision(logger, s1)
    assert s1.level is RiskLevel.UNKNOWN
    assert s1.completeness.evaluated_dimensions == 1

    # 补录第二批（含更早区块 90 的历史交易，模拟迟到/乱序）
    log_stream(logger, _w_seed() + _w_stage2(), "(补录第二批)")
    service.ingest_many(_w_stage2())
    s2 = service.evaluate("W")
    log_decision(logger, s2)
    # 覆盖抬过判定线：下不了结论 -> 明确的 LOW（各维度都查过且未触发）
    assert s2.completeness.evaluated_dimensions == 3
    assert s2.completeness.sufficient_for_grade is True
    assert s2.level is RiskLevel.LOW
    assert s2.score == 0.0
    assert s2.completeness.confidence.value == "MEDIUM"

    # 补录第三批：完整度继续上升，结论仍 LOW 但可信度升到 HIGH
    log_stream(logger, _w_stage3(), "(补录第三批)")
    service.ingest_many(_w_stage3())
    s3 = service.evaluate("W")
    log_decision(logger, s3)
    assert s3.completeness.evaluated_dimensions == 6
    assert s3.completeness.pending_dimensions == 0
    assert s3.completeness.missing_dimensions == 0
    assert s3.level is RiskLevel.LOW
    assert s3.completeness.confidence.value == "HIGH"
    assert not s3.insufficient_reasons

    # 完整度单调不降：1 -> 3 -> 6
    evals = [s1.completeness.evaluated_dimensions,
             s2.completeness.evaluated_dimensions,
             s3.completeness.evaluated_dimensions]
    assert evals == sorted(evals) == [1, 3, 6]


def test_repeated_evaluation_is_identical(service, logger):
    service.ingest_many(_w_seed() + _w_stage2())
    a = service.evaluate("W")
    b = service.evaluate("W")
    log_decision(logger, a)
    assert a == b, "数据不变时反复评估，结论、原因与完整度必须完全一致"
    assert a.determinism_key == b.determinism_key


def test_completeness_is_monotonic_along_random_backfill(canonical_txs, logger):
    """把合成数据按固定随机顺序逐笔补录，多个地址的已评估维度数只能上升。"""
    from risk_engine import synthetic
    from risk_engine.scoring import RiskScorer

    stream = synthetic.shuffled_view(canonical_txs, seed=4242)
    watch = ["H", "X", "VIC", "MID", "U", "CLEAN"]
    prev: dict[str, int] = {a: 0 for a in watch}
    ingested: list[Transaction] = []
    for tx in stream:
        ingested.append(tx)
        scorer = RiskScorer(list(ingested), {"BAD"})
        for addr in watch:
            cur = scorer.evaluate(addr).completeness.evaluated_dimensions
            assert cur >= prev[addr], f"{addr} 完整度在补录过程中回落: {prev[addr]} -> {cur}"
            prev[addr] = cur
    logger.info("逐笔补录单调完整度终值: %s", prev)
    # 全量补录后这些充分样本都应 6/6
    assert all(v == 6 for v in prev.values())


def test_concurrent_backfill_query_is_consistent_and_dedup(service, logger):
    batches = [_w_seed(), _w_stage2(), _w_stage3()]
    full = [t for b in batches for t in b]

    observed: list[tuple] = []
    stop = threading.Event()
    obs_lock = threading.Lock()

    def reader():
        while not stop.is_set():
            s = service.evaluate("W")
            c = s.completeness
            with obs_lock:
                observed.append((
                    s.level.value,
                    c.evaluated_dimensions,
                    c.pending_dimensions,
                    c.missing_dimensions,
                    s.determinism_key,
                ))
            # 不变式：报告内部任何时刻都自洽，依据与结论对得上
            n_suff = sum(1 for f in s.factors if f.status is EvidenceStatus.SUFFICIENT)
            assert n_suff == c.evaluated_dimensions
            assert c.evaluated_dimensions + c.pending_dimensions + c.missing_dimensions == 6
            if not c.sufficient_for_grade:
                assert s.level is RiskLevel.UNKNOWN, "覆盖不足时任何查询都不得看到分级结论"
            else:
                assert s.level in (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH)
                assert not any(r.code == "DIMENSION_COVERAGE_INSUFFICIENT"
                               for r in s.insufficient_reasons)

    t = threading.Thread(target=reader)
    t.start()

    def worker(_):
        for b in batches:
            log_stream(logger, b, f"(并发补录线程)")
            service.ingest_many(list(b))

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(worker, range(8)))  # 8 线程重复补录同一批
    finally:
        stop.set()
        t.join()

    # 同一批并发重复补录只算一次：共 5 个不同 tx_id
    assert service.ledger_view().tx_count == len({t.tx_id for t in full})

    final = service.evaluate("W")
    log_decision(logger, final)
    assert final.level is RiskLevel.LOW
    assert final.completeness.evaluated_dimensions == 6

    # 与按区块顺序一次性全部录入的参考结果逐字段一致
    from risk_engine.scoring import RiskScorer
    ordered = sorted(full, key=lambda x: (x.block_height, x.block_index, x.tx_id))
    high_risk = service._store.high_risk_snapshot()
    reference = RiskScorer(ordered, high_risk).evaluate("W")
    assert final == reference, "并发重复补录结果必须等于按区块顺序一次性录入"
    logger.info("并发期间观察到 %d 份自洽报告，终态与串行参考一致", len(observed))


def test_factor_statuses_exposed_over_http(client):
    """HTTP 层向后兼容：原字段保留，新字段可用。"""
    r = client.post("/transactions", json={
        "tx_id": "lonely-x", "from_addr": "LX", "to_addr": "LY",
        "amount": 10.0, "block_height": 1, "block_index": 0, "timestamp": 1.0,
    })
    assert r.status_code == 200
    body = client.get("/risk/LX").json()
    # 原有字段
    for key in ("address", "score", "level", "factors",
                "insufficient_reasons", "evaluated_at_block", "determinism_key"):
        assert key in body
    assert body["level"] == "UNKNOWN"
    # 新增：因子状态
    factor0 = body["factors"][0]
    assert "status" in factor0 and "insufficiency_code" in factor0
    # 新增：完整度汇总
    c = body["completeness"]
    assert set(c) == {
        "total_dimensions", "evaluated_dimensions", "pending_dimensions",
        "missing_dimensions", "evaluated", "not_evaluated",
        "coverage_ratio", "confidence", "sufficient_for_grade",
    }
    assert c["sufficient_for_grade"] is False
    # 新增：原因可归属维度
    assert "factor" in body["insufficient_reasons"][0]
