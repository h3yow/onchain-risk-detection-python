"""并发一致性测试：并发摄入结果必须与逐个串行一致，查询不得暴露中间态。"""
from __future__ import annotations

import logging
import random
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from app import synthdata
from app.models import IngestStatus, Transaction
from app.service import RiskService

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("concurrency")


def _load_serial(txs: list[Transaction]) -> RiskService:
    svc = RiskService()
    for w in synthdata.build_wallets():
        svc.ingest_wallet(w)
    for t in txs:
        svc.ingest_transaction(t)
    return svc


def _load_concurrent(txs: list[Transaction], workers: int, seed: int) -> RiskService:
    svc = RiskService()
    for w in synthdata.build_wallets():
        svc.ingest_wallet(w)
    rng = random.Random(seed)
    rng.shuffle(txs)

    def push(t: Transaction):
        # 模拟少量调度抖动，制造真实交错
        if rng.random() < 0.3:
            for _ in range(50):
                pass
        return svc.ingest_transaction(t)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(push, txs))
    svc.state_results = results  # type: ignore[attr-defined]
    return svc


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_ingest_equals_serial(self) -> None:
        base_txs = synthdata.build_transactions()
        serial = _load_serial(list(base_txs))
        s_serial = serial.snapshot()

        for workers in (2, 8, 32):
            concurrent = _load_concurrent(list(base_txs), workers, seed=1000 + workers)
            snap = concurrent.snapshot()
            self.assertEqual(len(snap.transactions), len(s_serial.transactions),
                             "并发摄入不得丢更新")
            self.assertEqual(snap.balances, s_serial.balances, "并发余额必须与串行一致")
            self.assertEqual(snap.tip, s_serial.tip)
            self.assertEqual(
                [(t.tx_id, t.block, t.tx_index) for t in snap.transactions],
                [(t.tx_id, t.block, t.tx_index) for t in s_serial.transactions],
                "规范回放顺序必须与串行一致",
            )
            for addr in s_serial.wallets:
                r1, r2 = serial.evaluate(addr), concurrent.evaluate(addr)
                self.assertEqual((r1.verdict, r1.score), (r2.verdict, r2.score),
                                 f"{addr} 并发评分与串行不一致")
        log.info("并发判定依据: 32 线程并发摄入 %d 笔后，交易数/余额/tip/顺序/全部评分=串行基线",
                 len(base_txs))

    def test_concurrent_duplicates_counted_once(self) -> None:
        txs = synthdata.build_transactions()
        # 每笔交易被 4 个线程同时重复提交
        repeated = txs * 4
        concurrent = _load_concurrent(repeated, workers=16, seed=77)
        accepted = [r for r in concurrent.state_results if r.status == IngestStatus.ACCEPTED]
        duplicates = [r for r in concurrent.state_results if r.status == IngestStatus.DUPLICATE]
        snap = concurrent.snapshot()
        log.info("输入流水: %d 笔交易 x4 份并发提交 -> ACCEPTED=%d DUPLICATE=%d 账本实际=%d",
                 len(txs), len(accepted), len(duplicates), len(snap.transactions))
        self.assertEqual(len(accepted), len(txs))
        self.assertEqual(len(duplicates), len(txs) * 3)
        self.assertEqual(len(snap.transactions), len(txs))
        serial = _load_serial(list(txs))
        self.assertEqual(snap.balances, serial.snapshot().balances)

    def test_reads_never_observe_partial_writes(self) -> None:
        """并发查询期间，任何时刻看到的余额都必须等于规范重放结果，交易数单调。"""
        svc = RiskService()
        for w in synthdata.build_wallets():
            svc.ingest_wallet(w)

        per_thread_obs: list[list[int]] = []
        obs_lock = threading.Lock()
        stop = threading.Event()

        def reader() -> None:
            mine: list[int] = []
            while not stop.is_set():
                snap = svc.snapshot()
                # 不变式 1：快照内余额恒等于按快照交易规范重放的结果
                replay = {a: 0 for a in snap.wallets}
                for t in snap.transactions:
                    replay[t.sender] -= t.amount
                    replay[t.receiver] += t.amount
                assert replay == snap.balances, "快照余额与重放不一致（暴露了写入中间态）"
                # 不变式 2：快照键集完整
                assert set(snap.balances) == set(snap.wallets)
                mine.append(len(snap.transactions))
            with obs_lock:
                per_thread_obs.append(mine)

        readers = [threading.Thread(target=reader) for _ in range(4)]
        for t in readers:
            t.start()

        rng = random.Random(99)
        shuffled = list(synthdata.build_transactions())
        rng.shuffle(shuffled)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(svc.ingest_transaction, shuffled))

        stop.set()
        for t in readers:
            t.join()

        observations = [n for seq in per_thread_obs for n in seq]
        self.assertTrue(observations)
        total = len(synthdata.build_transactions())
        # 每个读线程自身看到的交易数必须单调（写入不回滚、不重复）
        for i, seq in enumerate(per_thread_obs):
            self.assertTrue(all(x <= y for x, y in zip(seq, seq[1:])),
                            f"读线程 {i} 观测到交易数回退，说明读到了中间状态")
        # 停止后再取一次快照：所有写入已可见，最终值=全集且余额=串行基线
        final = svc.snapshot()
        self.assertEqual(len(final.transactions), total)
        self.assertEqual(final.balances, _load_serial(shuffled).snapshot().balances)
        log.info("一致性判定依据: 4 个读线程并发抽样 %d 个快照，全部满足余额重放不变式，"
                 "各线程观测交易数单调增长，写入完成后最终交易数=%d、余额=串行基线",
                 len(observations), total)


if __name__ == "__main__":
    unittest.main(verbosity=2)
