"""风险因子计算：每个因子独立、可解释。

所有因子的输入是“按区块全序排列的交易”，因此与摄入到达顺序无关，
同一输入必然得到同一组证据。每个因子无论触发与否都返回 FactorEvidence，
detail 中写明观测值与阈值，使判定依据可复核。
"""
from __future__ import annotations

from collections import defaultdict

from .models import FactorEvidence, FactorCode, Transaction


class FactorCalculator:
    # 因子权重（各因子独立贡献，评分层负责求和截断）
    WEIGHT_CONVERGENCE = 25.0
    WEIGHT_DISPERSION = 20.0
    WEIGHT_RING = 30.0
    WEIGHT_HIGH_FREQ = 15.0
    WEIGHT_HIGH_RISK_DIRECT = 30.0
    WEIGHT_HIGH_RISK_INDIRECT = 15.0

    # 判定阈值（全部显式，出现在依据文本中）
    CONVERGENCE_IN_DEGREE = 3      # 不同入账来源数
    DISPERSION_OUT_DEGREE = 3      # 不同出账目标数
    HIGH_FREQ_WINDOW = 5.0         # 时间窗口（秒）
    HIGH_FREQ_COUNT = 4            # 窗口内交互笔数达到值
    INDIRECT_MAX_HOPS = 2          # 间接关联最大跳数

    def __init__(self, transactions: list[Transaction], high_risk: set[str]) -> None:
        self._txs = transactions
        self._high_risk = set(high_risk)
        self._participation: dict[str, list[Transaction]] = defaultdict(list)
        self._out: dict[str, set[str]] = defaultdict(set)
        self._inn: dict[str, set[str]] = defaultdict(set)
        for tx in transactions:  # 调用方保证已按全序排列
            self._participation[tx.from_addr].append(tx)
            self._participation[tx.to_addr].append(tx)
            self._out[tx.from_addr].add(tx.to_addr)
            self._inn[tx.to_addr].add(tx.from_addr)

    def evaluate(self, address: str) -> tuple[FactorEvidence, ...]:
        return (
            self.convergence(address),
            self.dispersion(address),
            self.ring_flow(address),
            self.high_frequency(address),
            self.high_risk_direct(address),
            self.high_risk_indirect(address),
        )

    def convergence(self, address: str) -> FactorEvidence:
        """资金汇聚：多个不同来源向该地址转入。"""
        sources = self._inn.get(address, set())
        n = len(sources)
        hit = n >= self.CONVERGENCE_IN_DEGREE
        if hit:
            detail = (
                f"检测到 {n} 个不同来源地址向 {address} 转入"
                f"（阈值>={self.CONVERGENCE_IN_DEGREE}），呈资金汇聚形态"
            )
        else:
            detail = (
                f"入账来源数={n}，未达汇聚阈值"
                f"{self.CONVERGENCE_IN_DEGREE}"
            )
        return FactorEvidence(
            code=FactorCode.FUND_CONVERGENCE,
            triggered=hit,
            weight=self.WEIGHT_CONVERGENCE if hit else 0.0,
            detail=detail,
            related=tuple(sorted(sources)),
        )

    def dispersion(self, address: str) -> FactorEvidence:
        """资金分散：该地址向多个不同目标转出。"""
        targets = self._out.get(address, set())
        n = len(targets)
        hit = n >= self.DISPERSION_OUT_DEGREE
        if hit:
            detail = (
                f"{address} 向 {n} 个不同目标地址转出"
                f"（阈值>={self.DISPERSION_OUT_DEGREE}），呈资金分散形态"
            )
        else:
            detail = f"出账目标数={n}，未达分散阈值{self.DISPERSION_OUT_DEGREE}"
        return FactorEvidence(
            code=FactorCode.FUND_DISPERSION,
            triggered=hit,
            weight=self.WEIGHT_DISPERSION if hit else 0.0,
            detail=detail,
            related=tuple(sorted(targets)),
        )

    def ring_flow(self, address: str) -> FactorEvidence:
        """环形流转：有向资金图中存在经过该地址的环。"""
        cycle = self._find_cycle_through(address)
        hit = cycle is not None
        if hit:
            detail = (
                f"检测到经过 {address} 的资金环：{' -> '.join(cycle)}，"
                "资金最终回流，疑似环形流转"
            )
            related = tuple(cycle)
        else:
            detail = "未检测到经过该地址的有向资金环"
            related = ()
        return FactorEvidence(
            code=FactorCode.RING_FLOW,
            triggered=hit,
            weight=self.WEIGHT_RING if hit else 0.0,
            detail=detail,
            related=related,
        )

    def _find_cycle_through(self, address: str) -> list[str] | None:
        """返回经过 address 的最短环（节点序列首尾均为 address），无则 None。

        做法：从 address 的每个出边邻居 v 出发，BFS 找 v -> address 的最短路径。
        按邻居排序保证结果确定。
        """
        targets = self._out.get(address, set())
        best: list[str] | None = None
        for v in sorted(targets):
            if v == address:
                return [address, address]
            path = self._shortest_path(v, address)
            if path is not None:
                cycle = [address] + path
                if best is None or len(cycle) < len(best):
                    best = cycle
        return best

    def _shortest_path(self, src: str, dst: str) -> list[str] | None:
        """有向图 BFS 最短路径，返回含 src..dst 的节点列表。"""
        if src == dst:
            return [src]
        visited = {src}
        queue: list[tuple[str, list[str]]] = [(src, [src])]
        while queue:
            node, path = queue.pop(0)
            for nxt in sorted(self._out.get(node, set())):
                if nxt in visited:
                    continue
                if nxt == dst:
                    return path + [dst]
                visited.add(nxt)
                queue.append((nxt, path + [nxt]))
        return None

    def high_frequency(self, address: str) -> FactorEvidence:
        """短时高频交互：按交易时间戳排序的滑动窗口内交互笔数达标。"""
        times = sorted(t.timestamp for t in self._participation.get(address, ()))
        window: list[float] = []
        worst = 0
        worst_start = worst_end = 0.0
        for ts in times:
            window.append(ts)
            while window and ts - window[0] > self.HIGH_FREQ_WINDOW:
                window.pop(0)
            if len(window) > worst:
                worst = len(window)
                worst_start, worst_end = window[0], ts
        hit = worst >= self.HIGH_FREQ_COUNT
        if hit:
            detail = (
                f"{worst_start:.0f}~{worst_end:.0f} 秒的 {self.HIGH_FREQ_WINDOW:.0f}"
                f" 秒窗口内发生 {worst} 笔交互（阈值>={self.HIGH_FREQ_COUNT}），"
                "属短时高频交互"
            )
        else:
            detail = (
                f"最长窗口交互笔数={worst}，未达高频阈值{self.HIGH_FREQ_COUNT}"
            )
        return FactorEvidence(
            code=FactorCode.HIGH_FREQ_INTERACTION,
            triggered=hit,
            weight=self.WEIGHT_HIGH_FREQ if hit else 0.0,
            detail=detail,
        )

    def high_risk_direct(self, address: str) -> FactorEvidence:
        """与已知高风险地址直接（1 跳，双向）关联。"""
        neighbors = self._out.get(address, set()) | self._inn.get(address, set())
        direct = sorted(a for a in neighbors if a in self._high_risk)
        hit = bool(direct)
        if hit:
            detail = f"与已知高风险地址存在直接资金往来：{', '.join(direct)}"
        else:
            detail = "未发现与已知高风险地址的直接资金往来"
        return FactorEvidence(
            code=FactorCode.HIGH_RISK_DIRECT,
            triggered=hit,
            weight=self.WEIGHT_HIGH_RISK_DIRECT if hit else 0.0,
            detail=detail,
            related=tuple(direct),
        )

    def high_risk_indirect(self, address: str) -> FactorEvidence:
        """与已知高风险地址 2 跳间接关联（直接命中的不计入本因子）。"""
        direct = (self._out.get(address, set()) | self._inn.get(address, set())) & self._high_risk
        indirect: dict[str, int] = {}
        for hops in (2,):
            frontier_out = set()
            frontier_in = set()
            for a in (self._out.get(address, set()) | self._inn.get(address, set())):
                frontier_out |= self._out.get(a, set())
                frontier_in |= self._inn.get(a, set())
            reachable = frontier_out | frontier_in
            for h in reachable:
                if h != address and h in self._high_risk and h not in direct:
                    indirect[h] = hops
        names = sorted(indirect)
        hit = bool(names)
        if hit:
            detail = (
                f"经 1 个中间地址后与高风险地址关联（{self.INDIRECT_MAX_HOPS} 跳内）："
                f"{', '.join(names)}"
            )
        else:
            detail = f"{self.INDIRECT_MAX_HOPS} 跳内未发现间接高风险关联"
        return FactorEvidence(
            code=FactorCode.HIGH_RISK_INDIRECT,
            triggered=hit,
            weight=self.WEIGHT_HIGH_RISK_INDIRECT if hit else 0.0,
            detail=detail,
            related=tuple(names),
        )
