"""本地合成数据：确定性的钱包与交易场景生成器。

场景图（地址说明）：
- S1,S2,S3,S4  普通来源钱包
- X             汇聚后分散的“混币”嫌疑钱包
- D1,D2,D3     普通目标钱包
- R,Q           构成环形流转 R->Q->R
- F             短时高频交互钱包，对手方 K
- BAD           已知高风险地址
- VIC           与 BAD 直接往来的地址
- MID,U         仅经间接路径 *->VIC->BAD 关联高风险
- H             汇聚+分散+直接关联 BAD，预期 HIGH 级
- CLEAN         交易充分但无任何风险因子
另含一笔迟到历史交易 LATE_SRC->X（block 0，最晚到达），用于乱序回放。
"""
from __future__ import annotations

import random

from .models import Transaction, Wallet, WalletType

# (tx_id, from, to, amount, block, index, ts)
_RAW_TXS: tuple[tuple[str, str, str, float, int, int, float], ...] = (
    # block 3: 多源汇聚到 X
    ("tx-30", "S1", "X", 100.0, 3, 0, 300.0),
    ("tx-31", "S2", "X", 120.0, 3, 1, 301.0),
    ("tx-32", "S3", "X", 90.0, 3, 2, 302.0),
    # block 5: X 分散转出
    ("tx-50", "X", "D1", 60.0, 5, 0, 500.0),
    ("tx-51", "X", "D2", 70.0, 5, 1, 501.0),
    ("tx-52", "X", "D3", 50.0, 5, 2, 502.0),
    # block 7: 环形流转 R <-> Q
    ("tx-70", "R", "Q", 40.0, 7, 0, 700.0),
    ("tx-71", "Q", "R", 38.0, 7, 1, 701.0),
    # block 8: F 与 K 在 3 秒内 4 笔交互
    ("tx-80", "F", "K", 10.0, 8, 0, 800.0),
    ("tx-81", "K", "F", 10.0, 8, 1, 801.0),
    ("tx-82", "F", "K", 9.0, 8, 2, 802.0),
    ("tx-83", "K", "F", 9.0, 8, 3, 803.0),
    # block 9: 直接高风险 VIC -> BAD；CLEAN 正常往来
    ("tx-90", "VIC", "BAD", 200.0, 9, 0, 900.0),
    ("tx-91", "CLEAN", "S1", 5.0, 9, 1, 901.0),
    ("tx-92", "S4", "CLEAN", 7.0, 9, 2, 902.0),
    # block 11: 间接高风险 MID -> VIC -> BAD（MID 有足够交互可出分）
    ("tx-110", "MID", "VIC", 30.0, 11, 0, 1100.0),
    ("tx-111", "S2", "MID", 12.0, 11, 1, 1101.0),
    # block 12: U 两跳间接关联 U -> VIC -> BAD，且 U 有足够交互
    ("tx-120", "U", "VIC", 18.0, 12, 0, 1200.0),
    ("tx-121", "S3", "U", 8.0, 12, 1, 1201.0),
    # block 13: H 汇聚(3源)+分散(3目标)+直接关联 BAD，预期 HIGH
    ("tx-130", "S1", "H", 20.0, 13, 0, 1300.0),
    ("tx-131", "S2", "H", 21.0, 13, 1, 1301.0),
    ("tx-132", "S3", "H", 22.0, 13, 2, 1302.0),
    ("tx-133", "H", "D1", 10.0, 13, 3, 1303.0),
    ("tx-134", "H", "D2", 11.0, 13, 4, 1304.0),
    ("tx-135", "H", "BAD", 12.0, 13, 5, 1305.0),
    # block 14~20: 为 VIC/MID/U/CLEAN 补足“非触发方向”的对手方与时间跨度样本，
    # 使其六个维度都证据充分（补进来后仍不触发汇聚/分散/高频），
    # 用于验证“查过了确实没问题”的 LOW 与仅凭少量样本的 UNKNOWN 的区别。
    ("tx-140", "S1", "MID", 11.0, 14, 0, 1400.0),   # MID 第二个入账来源
    ("tx-150", "VIC", "DV1", 13.0, 15, 0, 1500.0),  # VIC 第二个出账目标
    ("tx-160", "MID", "DM1", 9.0, 16, 0, 1600.0),   # MID 第二个出账目标
    ("tx-170", "S2", "U", 7.0, 17, 0, 1700.0),      # U 第二个入账来源
    ("tx-180", "U", "DU1", 6.0, 18, 0, 1800.0),     # U 第二个出账目标
    ("tx-190", "SC1", "CLEAN", 4.0, 19, 0, 1900.0), # CLEAN 第二个入账来源
    ("tx-200", "CLEAN", "DC1", 3.0, 20, 0, 2000.0), # CLEAN 第二个出账目标
    # block 0: 迟到的历史交易（构造时故意排在最后）
    ("tx-late", "LATE_SRC", "X", 25.0, 0, 0, 10.0),
)

_HIGH_RISK_ADDRESSES = frozenset({"BAD"})


def build_wallets() -> list[Wallet]:
    addresses = set()
    for _, src, dst, *_ in _RAW_TXS:
        addresses.add(src)
        addresses.add(dst)
    wallets: list[Wallet] = []
    for addr in sorted(addresses):
        wtype = (
            WalletType.HIGH_RISK
            if addr in _HIGH_RISK_ADDRESSES
            else WalletType.NORMAL
        )
        wallets.append(Wallet(address=addr, wallet_type=wtype))
    return wallets


def build_transactions() -> list[Transaction]:
    """规范的区块顺序交易列表（late 交易仍按区块排在最前）。"""
    txs = [
        Transaction(
            tx_id=tid,
            from_addr=src,
            to_addr=dst,
            amount=amount,
            block_height=height,
            block_index=index,
            timestamp=ts,
        )
        for tid, src, dst, amount, height, index, ts in _RAW_TXS
    ]
    return sorted(txs, key=lambda t: (t.block_height, t.block_index, t.tx_id))


def arrival_order() -> list[Transaction]:
    """模拟实际到达顺序：late 交易最后到达，并夹带一个重复提交。"""
    by_id = {t.tx_id: t for t in build_transactions()}
    arrival_ids = [r[0] for r in _RAW_TXS]  # 定义顺序即“到达顺序”
    ordered = [by_id[tid] for tid in arrival_ids]
    # 到达中途重复提交 tx-50（幂等场景）
    ordered.insert(9, by_id["tx-50"])
    return ordered


def shuffled_view(txs: list[Transaction], seed: int = 20260923) -> list[Transaction]:
    """固定随机种子的乱序视图，保证测试可复现。"""
    view = list(txs)
    random.Random(seed).shuffle(view)
    return view


def conflicting_submission() -> Transaction:
    """与已存在 tx_id 相同但金额被篡改的提交，用于冲突检测。"""
    original = next(t for t in build_transactions() if t.tx_id == "tx-90")
    return Transaction(
        tx_id=original.tx_id,
        from_addr=original.from_addr,
        to_addr=original.to_addr,
        amount=original.amount + 1.0,  # 内容不一致
        block_height=original.block_height,
        block_index=original.block_index,
        timestamp=original.timestamp,
    )
