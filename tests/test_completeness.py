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
# 但刻意不触发汇聚/分散（来源、目标都只有 2 个，严格低于触发阈值 3），
# 用于验证汇聚/分散“未触发但证据充分（查过了确实没问题）”这一档可达
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
    gapw = _gap_map(score_w)
    # 入账方向补齐后：环/高频/直接关联都“查过了确实没问题”
    assert stw[FactorCode.RING_FLOW] is EvidenceStatus.SUFFICIENT
    assert stw[FactorCode.HIGH_FREQ_INTERACTION] is EvidenceStatus.SUFFICIENT
    assert stw[FactorCode.HIGH_RISK_DIRECT] is EvidenceStatus.SUFFICIENT
    # 资金汇聚/分散：2 个来源/目标（< 触发阈值 3，未触发、分值为 0），
    # 但双向都观测过且时间跨度 91 秒（≥ 一个判定窗口），观测面足以排除形态，
    # 必须如实给“查过了确实没问题”，不能再误标成证据不足
    conv = next(f for f in score_w.factors if f.code is FactorCode.FUND_CONVERGENCE)
    disp = next(f for f in score_w.factors if f.code is FactorCode.FUND_DISPERSION)
    assert conv.triggered is False and conv.weight == 0.0
    assert disp.triggered is False and disp.weight == 0.0
    assert stw[FactorCode.FUND_CONVERGENCE] is EvidenceStatus.SUFFICIENT
    assert stw[FactorCode.FUND_DISPERSION] is EvidenceStatus.SUFFICIENT
    assert conv.gap_code is None and disp.gap_code is None
    # 2 跳路径仍展不开：该维度依然是“有观测但不足”，与充分档明确区分
    assert stw[FactorCode.HIGH_RISK_INDIRECT] is EvidenceStatus.INSUFFICIENT
    assert gapw[FactorCode.HIGH_RISK_INDIRECT][0] == "NO_TWO_HOP_PATH"
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
    # 汇聚/分散在并发补录结束后同样达到“查过了没问题”，且始终 0 分
    for code in (FactorCode.FUND_CONVERGENCE, FactorCode.FUND_DISPERSION):
        f = next(x for x in final.factors if x.code is code)
        assert f.evidence_status is EvidenceStatus.SUFFICIENT
        assert f.triggered is False and f.weight == 0.0

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


def test_flow_dimensions_negative_sufficient_tier_is_reachable(service, logger):
    """资金汇聚/分散：未触发但观测面足以排除时必须给 SUFFICIENT，且分值保持 0。

    观测口径（严格低于触发阈值，因此该档真实可达）：该方向 >=2 个不同对手方、
    两个方向都有交易、参与时间跨度 >=5 秒，仍未触发（2 < 3）即“查过了没问题”。
    """
    txs = [
        Transaction("z-in-1", "ZS1", "Z", 5.0, 1, 0, 100.0),
        Transaction("z-in-2", "ZS2", "Z", 6.0, 1, 1, 101.0),
        Transaction("z-out-1", "Z", "ZD1", 4.0, 5, 0, 600.0),
        Transaction("z-out-2", "Z", "ZD2", 3.0, 5, 1, 601.0),
    ]
    log_stream(logger, txs, "(双向/多对手方/长跨度但不触发汇聚分散)")
    service.ingest_many(txs)

    score = service.evaluate("Z")
    log_decision(logger, score)
    by_code = {f.code: f for f in score.factors}
    conv = by_code[FactorCode.FUND_CONVERGENCE]
    disp = by_code[FactorCode.FUND_DISPERSION]

    # 未触发：分值严格为 0，不能因为放宽证据口径而带上任何风险分
    assert conv.triggered is False and conv.weight == 0.0
    assert disp.triggered is False and disp.weight == 0.0
    # 观测面足以排除：证据充分，缺口字段为空
    assert conv.evidence_status is EvidenceStatus.SUFFICIENT
    assert disp.evidence_status is EvidenceStatus.SUFFICIENT
    assert conv.gap_code is None and conv.gap_detail is None
    assert disp.gap_code is None and disp.gap_detail is None
    # 总分仍为 0（其余因子在该场景下也无触发）
    assert score.score == 0.0


def test_flow_dimension_gaps_are_split_by_boundary(service, logger):
    """汇聚/分散的“没查出来”必须按边界如实分开，且不能为档位好看放宽。

    - 来源/目标只有 1 个 -> TOO_FEW_*（样本太窄）
    - 只存在单一方向     -> SINGLE_DIRECTION_ONLY
    - 双向、2 个对手方但时间跨度 <5 秒 -> OBSERVATION_SPAN_TOO_SHORT
    - 该方向完全没交易   -> NO_*（NO_DATA）
    """
    txs = [
        # NARROW：入账只来自 1 个来源（出账、跨度都充分，仍只因来源面太窄而不足）
        Transaction("n-in-1", "NS", "NARROW", 5.0, 1, 0, 10.0),
        Transaction("n-out-1", "NARROW", "ND1", 4.0, 9, 0, 900.0),
        Transaction("n-out-2", "NARROW", "ND2", 4.0, 9, 1, 901.0),
        # ONEWAY：只有出账（2 个目标、跨度长，但没有入账）
        Transaction("o-out-1", "ONEWAY", "OD1", 4.0, 1, 0, 10.0),
        Transaction("o-out-2", "ONEWAY", "OD2", 4.0, 9, 0, 900.0),
        # SHORT：双向都有、各 2 个对手方，但全部发生在 4 秒内
        Transaction("h-in-1", "HS1", "SHORT", 5.0, 1, 0, 100.0),
        Transaction("h-in-2", "HS2", "SHORT", 5.0, 1, 1, 101.0),
        Transaction("h-out-1", "SHORT", "HD1", 4.0, 1, 2, 102.0),
        Transaction("h-out-2", "SHORT", "HD2", 4.0, 1, 3, 103.0),
        # EMPTYIN：只有入账（用于分散维度的 NO_OUTGOING_TX / NO_DATA）
        Transaction("e-in-1", "ES1", "EMPTYIN", 5.0, 1, 0, 10.0),
    ]
    log_stream(logger, txs, "(汇聚/分散边界缺口样本)")
    service.ingest_many(txs)

    narrow = service.evaluate("NARROW")
    oneway = service.evaluate("ONEWAY")
    short = service.evaluate("SHORT")
    empty_in = service.evaluate("EMPTYIN")
    ghost = service.evaluate("GHOST")
    for s in (narrow, oneway, short, empty_in, ghost):
        log_decision(logger, s)

    def factor(score, code):
        return next(f for f in score.factors if f.code is code)

    # 样本太窄：汇聚来源仅 1 个 -> 不足；分散 2 目标、双向、长跨度 -> 充分
    nc = factor(narrow, FactorCode.FUND_CONVERGENCE)
    nd = factor(narrow, FactorCode.FUND_DISPERSION)
    assert nc.evidence_status is EvidenceStatus.INSUFFICIENT
    assert nc.gap_code == "TOO_FEW_INCOMING_SOURCES"
    assert nd.evidence_status is EvidenceStatus.SUFFICIENT and nd.weight == 0.0

    # 方向单一：汇聚完全无入账 -> NO_DATA；分散有 2 目标但无入账方向 -> 不足
    oc = factor(oneway, FactorCode.FUND_CONVERGENCE)
    od = factor(oneway, FactorCode.FUND_DISPERSION)
    assert oc.evidence_status is EvidenceStatus.NO_DATA
    assert oc.gap_code == "NO_INCOMING_TX"
    assert od.evidence_status is EvidenceStatus.INSUFFICIENT
    assert od.gap_code == "SINGLE_DIRECTION_ONLY"

    # 跨度太短：两个维度双向、对手方都够，但只有 3 秒跨度 -> 不足
    sc = factor(short, FactorCode.FUND_CONVERGENCE)
    sd = factor(short, FactorCode.FUND_DISPERSION)
    assert sc.evidence_status is EvidenceStatus.INSUFFICIENT
    assert sc.gap_code == "OBSERVATION_SPAN_TOO_SHORT"
    assert sd.evidence_status is EvidenceStatus.INSUFFICIENT
    assert sd.gap_code == "OBSERVATION_SPAN_TOO_SHORT"

    # 该方向完全无数据：只有入账的地址，分散维度必须是 NO_DATA
    ed = factor(empty_in, FactorCode.FUND_DISPERSION)
    assert ed.evidence_status is EvidenceStatus.NO_DATA
    assert ed.gap_code == "NO_OUTGOING_TX"

    # 没参与任何交易的地址：两个资金流维度都是 NO_DATA
    gc = factor(ghost, FactorCode.FUND_CONVERGENCE)
    gd = factor(ghost, FactorCode.FUND_DISPERSION)
    assert gc.evidence_status is EvidenceStatus.NO_DATA
    assert gd.evidence_status is EvidenceStatus.NO_DATA

    # 所有缺口说明都必须讲清“缺什么”，且未触发一律 0 分
    for s in (narrow, oneway, short, empty_in, ghost):
        for f in s.factors:
            assert f.weight == 0.0 or f.triggered
            if f.evidence_status is not EvidenceStatus.SUFFICIENT:
                assert f.gap_code and f.gap_detail and len(f.gap_detail) > 10


def test_backfill_lifts_flow_dimensions_to_sufficient_without_score(service, logger):
    """补录更早历史后，汇聚/分散从“证据不足”翻成“查过了没问题”，但分值始终为 0。

    初始：2 笔出账（目标 2 个）-> 汇聚 NO_DATA、分散因方向单一 INSUFFICIENT；
    补录 2 笔更早区块的入账后：双向齐全、跨度拉长到 91 秒，两维都 SUFFICIENT，
    整份报告从 UNKNOWN 翻成 LOW；整个过程两维从未触发、分值严格为 0。
    """
    service.ingest_many(_initial_txs())
    before = service.evaluate("W")
    log_decision(logger, before)

    def fac(score, code):
        return next(f for f in score.factors if f.code is code)

    bc, bd = fac(before, FactorCode.FUND_CONVERGENCE), fac(before, FactorCode.FUND_DISPERSION)
    assert bc.evidence_status is EvidenceStatus.NO_DATA
    assert bc.gap_code == "NO_INCOMING_TX"
    assert bd.evidence_status is EvidenceStatus.INSUFFICIENT
    assert bd.gap_code == "SINGLE_DIRECTION_ONLY"
    assert bc.weight == 0.0 and bd.weight == 0.0
    assert before.level is RiskLevel.UNKNOWN

    backfill = _backfill_txs()
    log_stream(logger, backfill, "(补录历史：翻资金流证据状态)")
    # 逐笔乱序补录，每一步完整度都不能下降
    coverage_path = [before.completeness.coverage_ratio]
    for tx in reversed(backfill):
        service.ingest_transaction(tx)
        cur = service.evaluate("W")
        log_decision(logger, cur)
        coverage_path.append(cur.completeness.coverage_ratio)
        # 任何中间状态下，未触发的资金流维度都不允许带分
        for code in (FactorCode.FUND_CONVERGENCE, FactorCode.FUND_DISPERSION):
            f = fac(cur, code)
            assert f.triggered is False and f.weight == 0.0
    assert all(x <= y for x, y in zip(coverage_path, coverage_path[1:])), coverage_path

    after = service.evaluate("W")
    ac, ad = fac(after, FactorCode.FUND_CONVERGENCE), fac(after, FactorCode.FUND_DISPERSION)
    assert ac.evidence_status is EvidenceStatus.SUFFICIENT and ac.gap_code is None
    assert ad.evidence_status is EvidenceStatus.SUFFICIENT and ad.gap_code is None
    assert ac.weight == 0.0 and ad.weight == 0.0
    assert after.score == 0.0
    assert after.level is RiskLevel.LOW

    # 与“按区块顺序一次性录入”逐字段相等
    serial = RiskService()
    for w in synthetic.build_wallets():
        serial.register_wallet(w)
    serial.ingest_many(
        sorted(
            _initial_txs() + backfill,
            key=lambda t: (t.block_height, t.block_index, t.tx_id),
        )
    )
    assert serial.evaluate("W") == after
