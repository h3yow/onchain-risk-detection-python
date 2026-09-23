"""本地合成的钱包与交易数据（确定性，无随机源）。

场景覆盖：
- HUB    资金汇聚：U1/U2/U3 三个上游汇入
- SPREAD 资金分散：向下游 D1/D2/D3 分散转出
- FREQ_A/FREQ_B 短时高频：区块 5~6 内 3 笔交互
- R1/R2/R3 环形流转：R1->R2->R3->R1
- INDIR  间接高风险关联：INDIR->MED->SAN1（一跳中介）
- DIR    直接高风险关联：DIR 直接与 SAN2 交易
- QUIET  数据不足：只有 1 笔交易
- NORMAL 普通用户：少量正常往来
交易返回顺序为规范区块顺序；调用方可故意打乱后提交以测试乱序回放。
"""
from __future__ import annotations

from app.models import Transaction, Wallet


def build_wallets() -> list[Wallet]:
    specs = [
        # address, label, high_risk, tags
        ("U1", "user-1", False, ()),
        ("U2", "user-2", False, ()),
        ("U3", "user-3", False, ()),
        ("D1", "downstream-1", False, ()),
        ("D2", "downstream-2", False, ()),
        ("D3", "downstream-3", False, ()),
        ("HUB", "concentration-hub", False, ()),
        ("SPREAD", "dispersion-wallet", False, ()),
        ("FREQ_A", "frequent-party-a", False, ()),
        ("FREQ_B", "frequent-party-b", False, ()),
        ("R1", "ring-node-1", False, ()),
        ("R2", "ring-node-2", False, ()),
        ("R3", "ring-node-3", False, ()),
        ("MED", "mediator", False, ()),
        ("INDIR", "indirect-link-wallet", False, ()),
        ("DIR", "direct-link-wallet", False, ()),
        ("SAN1", "sanctioned-entity-1", True, ("sanctioned", "mixer")),
        ("SAN2", "sanctioned-entity-2", True, ("sanctioned",)),
        ("QUIET", "quiet-wallet", False, ()),
        ("NORMAL", "normal-wallet", False, ()),
        ("S1", "suspect-source-1", False, ()),
        ("S2", "suspect-source-2", False, ()),
        ("S3", "suspect-source-3", False, ()),
        ("SUS", "combined-suspect", False, ()),
    ]
    return [Wallet(a, label, risky, tuple(tags)) for a, label, risky, tags in specs]


def build_transactions() -> list[Transaction]:
    # (tx_id, block, index, sender, receiver, amount, ts)
    rows = [
        # 区块 1：汇聚场景
        ("tx-c1", 1, 0, "U1", "HUB", 100, 1000),
        ("tx-c2", 1, 1, "U2", "HUB", 200, 1001),
        ("tx-c3", 1, 2, "U3", "HUB", 300, 1002),
        # 区块 2：分散场景
        ("tx-d1", 2, 0, "SPREAD", "D1", 50, 2000),
        ("tx-d2", 2, 1, "SPREAD", "D2", 60, 2001),
        ("tx-d3", 2, 2, "SPREAD", "D3", 70, 2002),
        # 区块 3：环形流转 R1->R2->R3->R1
        ("tx-r1", 3, 0, "R1", "R2", 80, 3000),
        ("tx-r2", 3, 1, "R2", "R3", 80, 3001),
        ("tx-r3", 3, 2, "R3", "R1", 80, 3002),
        # 区块 4：普通往来 + 间接高风险关联 INDIR->MED->SAN1
        # （为 INDIR/DIR 补一笔普通往来，保证可评估因子数 >=3）
        ("tx-n1", 4, 0, "NORMAL", "U1", 20, 4000),
        ("tx-i1", 4, 1, "INDIR", "MED", 40, 4001),
        ("tx-i2", 4, 2, "MED", "SAN1", 40, 4002),
        ("tx-i3", 4, 3, "INDIR", "NORMAL", 5, 4003),
        ("tx-q1", 4, 4, "QUIET", "U1", 10, 4004),
        ("tx-b2", 4, 5, "DIR", "NORMAL", 7, 4005),
        # 区块 5~6：短时高频 FREQ_A <-> FREQ_B（3 笔，跨度 1 个区块）
        ("tx-f1", 5, 0, "FREQ_A", "FREQ_B", 5, 5000),
        ("tx-f2", 5, 1, "FREQ_B", "FREQ_A", 5, 5001),
        ("tx-f3", 6, 0, "FREQ_A", "FREQ_B", 5, 6000),
        # 区块 6：直接高风险关联
        ("tx-b1", 6, 1, "DIR", "SAN2", 99, 6001),
        # 区块 7：再补一笔普通往来，保证 NORMAL 也有多笔观察（不触发任何因子）
        ("tx-n2", 7, 0, "NORMAL", "U1", 15, 7000),
        # 区块 8~9：SUS 综合高风险（汇聚 + 高频 + 环形），预期 HIGH
        ("tx-s1", 8, 0, "S1", "SUS", 100, 8000),
        ("tx-s2", 8, 1, "S2", "SUS", 100, 8001),
        ("tx-s3", 8, 2, "S3", "SUS", 100, 8002),
        ("tx-s4", 8, 3, "SUS", "S1", 10, 8003),
        ("tx-s5", 9, 0, "S1", "SUS", 10, 9000),
        ("tx-s6", 9, 1, "SUS", "S2", 10, 9001),
        ("tx-s7", 9, 2, "S2", "S3", 10, 9002),
        ("tx-s8", 9, 3, "S3", "SUS", 10, 9003),
    ]
    return [Transaction(tid, blk, idx, s, r, amt, ts)
            for tid, blk, idx, s, r, amt, ts in rows]
