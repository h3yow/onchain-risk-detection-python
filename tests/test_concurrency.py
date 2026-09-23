"""并发摄入一致性：结果等价于某种串行排列；查询只见完整状态。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from risk_engine import synthetic
from tests.helpers import log_stream


def test_concurrent_ingest_equals_serial(service, canonical_txs, logger):
    # 每个线程拿全量集合，制造大量重复提交；最终账本必须等于串行去重结果
    n_threads = 8
    barrier = threading.Barrier(n_threads)

    def worker(_):
        barrier.wait()  # 尽量让所有线程同时开始
        for tx in synthetic.shuffled_view(canonical_txs, seed=hash(threading.get_ident()) & 0xFFFF):
            service.ingest_transaction(tx)

    log_stream(logger, canonical_txs, f"(并发 {n_threads} 线程 x 全量乱序)")
    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        list(pool.map(worker, range(n_threads)))

    view = service.ledger_view()

    serial = RiskServiceSerial(canonical_txs)
    assert view.tx_count == serial.tx_count == len(canonical_txs)
    assert view.balances == serial.balances
    logger.info("并发结果 tx_count=%s 与串行一致，余额=%s", view.tx_count, view.balances)


def test_batched_atomicity_no_partial_state(service, canonical_txs, logger):
    """ingest_many 整批原子：并发查询看到的 tx_count 必须等于某次完整批次后的计数。"""
    batches = [canonical_txs[i::3] for i in range(3)]
    seen_counts: set[int] = set()
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            seen_counts.add(service.ledger_view().tx_count)

    t = threading.Thread(target=reader)
    t.start()
    for b in batches:
        log_stream(logger, b, "(原子批次)")
        service.ingest_many(b)
    stop.set()
    t.join()

    total = len(canonical_txs)
    sizes = [len({x.tx_id for x in b}) for b in batches]
    cumulative = {0}
    acc = 0
    for z in sizes:
        acc += z
        cumulative.add(acc)
    assert seen_counts <= cumulative, f"观察到非批次边界的中间计数: {seen_counts - cumulative}"
    assert service.ledger_view().tx_count == total


def test_concurrent_evaluations_are_stable_and_consistent(service, canonical_txs, logger):
    service.ingest_many(canonical_txs)
    results: list = []
    lock = threading.Lock()

    def evaluate(_):
        r = service.evaluate("H")
        with lock:
            results.append((r.level.value, r.score, r.determinism_key))

    log_stream(logger, canonical_txs, "(并发评估)")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(evaluate, range(16)))
    assert len(set(results)) == 1, f"并发评估结论不一致: {set(results)}"
    logger.info("16 次并发评估结论唯一: %s", results[0])


class RiskServiceSerial:
    """串行参考实现：去重后按区块全序回放。"""

    def __init__(self, txs):
        unique = {t.tx_id: t for t in txs}
        ordered = sorted(unique.values(), key=lambda t: (t.block_height, t.block_index, t.tx_id))
        balances: dict[str, float] = {}
        for tx in ordered:
            balances[tx.from_addr] = balances.get(tx.from_addr, 0.0) - tx.amount
            balances[tx.to_addr] = balances.get(tx.to_addr, 0.0) + tx.amount
        self.balances = balances
        self.tx_count = len(ordered)
