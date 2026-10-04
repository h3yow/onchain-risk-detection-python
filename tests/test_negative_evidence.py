"""资金汇聚/资金分散两个维度的「未触发但证据充分」判定测试。

口径：未触发时，只有该方向观测面足够宽（来源/目标不单一、样本笔数够、
观测时间跨度覆盖至少一个判定窗口）才给 SUFFICIENT（查过了确实没问题）；
样本太窄/方向单一仍是 INSUFFICIENT（没查出来），完全无数据是 NO_DATA。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from risk_engine.models import EvidenceStatus, FactorCode, RiskLevel, Transaction
from risk_engine.service import RiskService
from risk_engine import synthetic
from tests.helpers import log_decision, log_stream


def _factor(score, code: FactorCode):
    return next(f for f in score.factors if f.code is code)


# OBS 的完整流水：入账 3 笔 2 来源、出账 3 笔 2 目标，跨度均覆盖判定窗口，
# 刻意不触发汇聚/分散（来源、目标都不足 3 个）
def _obs_full_txs() -> list[Transaction]:
    return [
        Transaction("obs-in-1", "S1", "OBS", 5.0, 1, 0, 100.0),
        Transaction("obs-in-2", "S2", "OBS", 6.0, 2, 0, 110.0),
        Transaction("obs-in-3", "S1", "OBS", 7.0, 3, 0, 120.0),
        Transaction("obs-out-1", "OBS", "D1", 4.0, 4, 0, 130.0),
        Transaction("obs-out-2", "OBS", "D2", 3.0, 5, 0, 140.0),
        Transaction("obs-out-3", "OBS", "D1", 2.0, 6, 0, 150.0),
    ]


def test_untriggered_but_sufficient_is_reachable(service, logger):
    """未触发但观测面足够：汇聚/分散都应是 SUFFICIENT，且分值仍为 0。"""
    txs = _obs_full_txs()
    log_stream(logger, txs, "(观测面充分但不触发)")
    service.ingest_many(txs)

    score = service.evaluate("OBS")
    log_decision(logger, score)
    for code in (FactorCode.FUND_CONVERGENCE, FactorCode.FUND_DISPERSION):
        f = _factor(score, code)
        assert not f.triggered, "观测充分但未达触发阈值，不得触发"
        assert f.weight == 0.0, "未触发时分值必须为 0"
        assert f.evidence_status is EvidenceStatus.SUFFICIENT, (
            "观测面足以排除时应如实给出「查过了确实没问题」"
        )
        assert f.gap_code is None and f.gap_detail is None
    # 同一报告里仍然存在「有观测但不足」的维度，两档分得开
    indirect = _factor(score, FactorCode.HIGH_RISK_INDIRECT)
    assert indirect.evidence_status is EvidenceStatus.INSUFFICIENT
    assert indirect.gap_code == "NO_TWO_HOP_PATH"
    # 改充分口径不得带偏评分：无触发因子时总分 0、明确 LOW
    assert score.score == 0.0
    assert score.level is RiskLevel.LOW
    assert not score.inconclusive_reasons


def test_three_statuses_distinguishable_in_one_report(service, logger):
    """同一份报告里：证据充分 / 证据不足 / 完全没数据 三档同时可区分。"""
    # MIX 只有入账：3 笔、2 个来源、跨度 20 秒 -> 汇聚「查过了确实没问题」；
    # 完全没有出账 -> 分散「完全没数据」；环形只有单方向 -> 「有观测但不足」
    txs = [
        Transaction("mix-1", "S1", "MIX", 5.0, 1, 0, 0.0),
        Transaction("mix-2", "S2", "MIX", 6.0, 2, 0, 10.0),
        Transaction("mix-3", "S1", "MIX", 7.0, 3, 0, 20.0),
    ]
    log_stream(logger, txs, "(单方向账户：三档同报告)")
    service.ingest_many(txs)

    score = service.evaluate("MIX")
    log_decision(logger, score)
    conv = _factor(score, FactorCode.FUND_CONVERGENCE)
    disp = _factor(score, FactorCode.FUND_DISPERSION)
    ring = _factor(score, FactorCode.RING_FLOW)
    assert conv.evidence_status is EvidenceStatus.SUFFICIENT
    assert conv.gap_code is None
    assert disp.evidence_status is EvidenceStatus.NO_DATA
    assert disp.gap_code == "NO_OUTGOING_TX"
    assert ring.evidence_status is EvidenceStatus.INSUFFICIENT
    assert ring.gap_code == "SINGLE_DIRECTION_ONLY"
    # 「没查出来」(NO_DATA) 与「查过了没问题」(SUFFICIENT) 必须分开
    statuses = {f.evidence_status for f in score.factors}
    assert statuses == {
        EvidenceStatus.SUFFICIENT,
        EvidenceStatus.INSUFFICIENT,
        EvidenceStatus.NO_DATA,
    }


def test_narrow_sample_boundary_stays_insufficient(service, logger):
    """边界卡准：来源单一 / 笔数太窄 / 跨度不足 / 无数据，都不能放进充分档。"""
    txs = [
        # BND-SRC：3 笔入账但来源单一
        Transaction("bs-1", "S1", "BND-SRC", 5.0, 1, 0, 0.0),
        Transaction("bs-2", "S1", "BND-SRC", 6.0, 2, 0, 10.0),
        Transaction("bs-3", "S1", "BND-SRC", 7.0, 3, 0, 20.0),
        # BND-CNT：2 个来源但只有 2 笔入账
        Transaction("bc-1", "S1", "BND-CNT", 5.0, 1, 1, 0.0),
        Transaction("bc-2", "S2", "BND-CNT", 6.0, 2, 1, 10.0),
        # BND-SPAN：3 笔 2 来源但挤在 2 秒内
        Transaction("bp-1", "S1", "BND-SPAN", 5.0, 1, 2, 0.0),
        Transaction("bp-2", "S2", "BND-SPAN", 6.0, 2, 2, 1.0),
        Transaction("bp-3", "S1", "BND-SPAN", 7.0, 3, 2, 2.0),
        # BND-OUT：只有出账（3 笔 2 目标跨度够），入账方向完全无数据
        Transaction("bo-1", "BND-OUT", "D1", 5.0, 1, 3, 0.0),
        Transaction("bo-2", "BND-OUT", "D2", 6.0, 3, 3, 10.0),
        Transaction("bo-3", "BND-OUT", "D1", 7.0, 4, 0, 20.0),
        # BND-TGT：3 笔出账但目标单一
        Transaction("bt-1", "BND-TGT", "D1", 5.0, 1, 4, 0.0),
        Transaction("bt-2", "BND-TGT", "D1", 6.0, 4, 1, 10.0),
        Transaction("bt-3", "BND-TGT", "D1", 7.0, 5, 0, 20.0),
    ]
    log_stream(logger, txs, "(边界样本：各缺一项)")
    service.ingest_many(txs)

    cases = {
        "BND-SRC": (FactorCode.FUND_CONVERGENCE, "TOO_FEW_INCOMING_SOURCES"),
        "BND-CNT": (FactorCode.FUND_CONVERGENCE, "TOO_FEW_INCOMING_TX"),
        "BND-SPAN": (FactorCode.FUND_CONVERGENCE, "INCOMING_SPAN_TOO_SHORT"),
        "BND-OUT": (FactorCode.FUND_CONVERGENCE, "NO_INCOMING_TX"),
        "BND-TGT": (FactorCode.FUND_DISPERSION, "TOO_FEW_OUTGOING_TARGETS"),
    }
    for addr, (code, gap) in cases.items():
        score = service.evaluate(addr)
        log_decision(logger, score)
        f = _factor(score, code)
        assert not f.triggered and f.weight == 0.0
        expected_status = (
            EvidenceStatus.NO_DATA if gap == "NO_INCOMING_TX"
            else EvidenceStatus.INSUFFICIENT
        )
        assert f.evidence_status is expected_status, (
            f"{addr} {code.value} 应为 {expected_status.value}，实际 {f.evidence_status.value}"
        )
        assert f.gap_code == gap, f"{addr} 缺口代码应为 {gap}，实际 {f.gap_code}"
        assert f.gap_detail and len(f.gap_detail) > 10, "缺口必须说明缺什么"

    # 对照：BND-OUT 的出账方向观测面足够，分散维度应是「查过了确实没问题」
    out_score = service.evaluate("BND-OUT")
    disp = _factor(out_score, FactorCode.FUND_DISPERSION)
    assert disp.evidence_status is EvidenceStatus.SUFFICIENT
    assert disp.gap_code is None


# OBS 的初始流水（新区块）：入账 2 笔 2 来源但跨度仅 1 秒，出账仅 1 笔，
# 汇聚/分散都只能是「有观测但不足」
def _obs_initial_txs() -> list[Transaction]:
    return [
        Transaction("obs-new-1", "S1", "OBS", 5.0, 10, 0, 200.0),
        Transaction("obs-new-2", "S2", "OBS", 6.0, 10, 1, 201.0),
        Transaction("obs-new-3", "OBS", "D1", 4.0, 10, 2, 202.0),
    ]


# 迟到/乱序到达的历史流水（区块更早）：把入账笔数与跨度、出账目标与跨度补齐，
# 仍不触发汇聚/分散（来源、目标都保持 2 个）
def _obs_backfill_txs() -> list[Transaction]:
    return [
        Transaction("obs-late-1", "S1", "OBS", 7.0, 1, 0, 100.0),
        Transaction("obs-late-2", "OBS", "D2", 3.0, 2, 0, 150.0),
        Transaction("obs-late-3", "OBS", "D1", 2.0, 3, 0, 160.0),
    ]


def test_backfill_raises_flow_dimensions_and_redecides(service, logger):
    """补录历史交易：汇聚/分散由「不足」升「充分」，完整度单调不降并触发重判。"""
    initial = _obs_initial_txs()
    log_stream(logger, initial, "(初始流水：观测面不足)")
    service.ingest_many(initial)

    before = service.evaluate("OBS")
    log_decision(logger, before)
    assert before.level is RiskLevel.UNKNOWN
    assert before.completeness.evaluated_dimensions == 2
    for code in (FactorCode.FUND_CONVERGENCE, FactorCode.FUND_DISPERSION):
        f = _factor(before, code)
        assert f.evidence_status is EvidenceStatus.INSUFFICIENT
        assert f.gap_code and f.gap_detail
    # 下不了结论的逐条原因里必须包含这两个维度的缺口
    codes = {r.code for r in before.inconclusive_reasons}
    assert "FUND_CONVERGENCE__TOO_FEW_INCOMING_TX" in codes
    assert "FUND_DISPERSION__TOO_FEW_OUTGOING_TARGETS" in codes

    # 数据没变化时反复评估：结论与完整度完全一致
    assert service.evaluate("OBS") == before

    # 按到达顺序逐笔补录（故意乱序：区块 3 先到、区块 2 最后到）
    arrival = [
        _obs_backfill_txs()[2],
        _obs_backfill_txs()[0],
        _obs_backfill_txs()[1],
    ]
    log_stream(logger, arrival, "(迟到历史补录)")
    coverages = [before.completeness.coverage_ratio]
    for tx in arrival:
        service.ingest_transaction(tx)
        cur = service.evaluate("OBS")
        log_decision(logger, cur)
        coverages.append(cur.completeness.coverage_ratio)
    assert all(b <= a for b, a in zip(coverages, coverages[1:])), (
        f"完整度必须单调不降: {coverages}"
    )

    after = service.evaluate("OBS")
    log_decision(logger, after)
    # 两个维度同步升为「查过了确实没问题」，逐条原因同步消失
    for code in (FactorCode.FUND_CONVERGENCE, FactorCode.FUND_DISPERSION):
        f = _factor(after, code)
        assert f.evidence_status is EvidenceStatus.SUFFICIENT
        assert f.gap_code is None and f.gap_detail is None
        assert not f.triggered and f.weight == 0.0
    assert after.completeness.evaluated_dimensions > before.completeness.evaluated_dimensions
    assert after.level is RiskLevel.LOW, "覆盖过线且无触发因子，应翻成明确 LOW"
    assert after.score == 0.0
    assert not after.inconclusive_reasons
    assert after.determinism_key != before.determinism_key

    # 与「按区块顺序一次性全部录入」的结果逐字段相等
    serial = RiskService()
    for w in synthetic.build_wallets():
        serial.register_wallet(w)
    serial.ingest_many(
        sorted(
            initial + _obs_backfill_txs(),
            key=lambda t: (t.block_height, t.block_index, t.tx_id),
        )
    )
    assert serial.evaluate("OBS") == after


def test_concurrent_backfill_flow_dimensions_consistent(service, logger):
    """并发重复补录只算一次；补录全程查询只见批次边界、结论与依据自洽。"""
    initial = _obs_initial_txs()
    service.ingest_many(initial)
    backfill = _obs_backfill_txs()
    n_threads = 8
    barrier = threading.Barrier(n_threads)
    stop = threading.Event()
    observations: list = []
    obs_lock = threading.Lock()
    tx_counts: set[int] = set()

    def reader():
        while not stop.is_set():
            view = service.ledger_view()
            score = service.evaluate("OBS")
            with obs_lock:
                tx_counts.add(view.tx_count)
                observations.append((view.tx_count, score))

    def worker(_):
        barrier.wait()
        service.ingest_many(list(backfill))  # 多线程并发重复补录同一批

    t = threading.Thread(target=reader)
    t.start()
    log_stream(logger, backfill, f"(并发补录 {n_threads} 线程重复同批)")
    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        list(pool.map(worker, range(n_threads)))
    stop.set()
    t.join()

    final = service.evaluate("OBS")
    log_decision(logger, final)
    # 同批并发重复补录只能算一次
    assert service.ledger_view().tx_count == len(initial) + len(backfill)
    assert final.level is RiskLevel.LOW
    for code in (FactorCode.FUND_CONVERGENCE, FactorCode.FUND_DISPERSION):
        assert _factor(final, code).evidence_status is EvidenceStatus.SUFFICIENT

    # 补录全程的每次观察都必须自洽
    assert tx_counts <= {len(initial), len(initial) + len(backfill)}, (
        f"看到只处理了一半的批次: {tx_counts}"
    )
    for count, score in observations:
        comp = score.completeness
        # 分数必须等于触发因子权重和（未触发的充分档贡献 0）
        assert score.score == round(
            min(100.0, max(0.0, sum(f.weight for f in score.factors))), 2
        )
        # 级别与覆盖门禁一致：覆盖不足必 UNKNOWN 且有原因；过线必为明确 LOW
        if comp.evaluated_dimensions < 3:
            assert score.level is RiskLevel.UNKNOWN
            assert score.inconclusive_reasons
        else:
            assert score.level is RiskLevel.LOW
            assert not score.inconclusive_reasons
        # 因子状态与缺口字段对得上；缺失维度清单一一对应
        for f in score.factors:
            if f.evidence_status is EvidenceStatus.SUFFICIENT:
                assert f.gap_code is None and f.gap_detail is None
            else:
                assert f.gap_code and f.gap_detail
        assert (
            len(comp.missing_dimensions)
            == comp.total_dimensions - comp.evaluated_dimensions
        )

    # 最终结果等于按区块顺序一次性录入
    serial = RiskService()
    for w in synthetic.build_wallets():
        serial.register_wallet(w)
    serial.ingest_many(
        sorted(
            initial + backfill,
            key=lambda t: (t.block_height, t.block_index, t.tx_id),
        )
    )
    assert serial.evaluate("OBS") == final
    logger.info("并发补录观察到的 tx_count 集合=%s（只落在批次边界）", sorted(tx_counts))
