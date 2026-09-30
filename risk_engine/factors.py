"""风险因子计算：每个因子独立、可解释，并自带维度级证据充分性判断。

所有因子的输入是“按区块全序排列的交易”，因此与摄入到达顺序无关，
同一输入必然得到同一组证据。每个因子无论触发与否都返回 FactorEvidence，
detail 中写明观测值与阈值，使判定依据可复核。

维度级证据三态（见 models.EvidenceStatus）：
- SUFFICIENT：观测样本足以支撑“触发/未触发”的明确结论；
- INSUFFICIENT：有相关数据，但样本方向单一 / 缺少应有对手方 / 样本量或
  时间跨度不够，此时 triggered 恒为 False 并在 insufficiency_* 中讲清缺什么；
- NO_DATA：该维度所需方向上完全没有交易，连“查过”都谈不上。
只有 SUFFICIENT 的维度才计入完整度汇总中的“已评估维度”。
"""
from __future__ import annotations

from collections import defaultdict

from .models import EvidenceStatus, FactorEvidence, FactorCode, Transaction


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

    # 维度级证据门槛：汇聚/分散需要至少 2 个不同对手方，才能区分
    # “确实没有汇聚/分散”与“只有单一方向样本”；1 个对手方记为证据不足。
    MIN_SOURCES_FOR_EVIDENCE = 2
    MIN_TARGETS_FOR_EVIDENCE = 2

    def __init__(self, transactions: list[Transaction], high_risk: set[str]) -> None:
        self._txs = transactions
        self._high_risk = set(high_risk)
        self._participation: dict[str, list[Transaction]] = defaultdict(list)
        self._incoming: dict[str, list[Transaction]] = defaultdict(list)
        self._outgoing: dict[str, list[Transaction]] = defaultdict(list)
        self._out: dict[str, set[str]] = defaultdict(set)
        self._inn: dict[str, set[str]] = defaultdict(set)
        for tx in transactions:  # 调用方保证已按全序排列
            self._participation[tx.from_addr].append(tx)
            self._participation[tx.to_addr].append(tx)
            self._outgoing[tx.from_addr].append(tx)
            self._incoming[tx.to_addr].append(tx)
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
        if n == 0:
            return FactorEvidence(
                code=FactorCode.FUND_CONVERGENCE,
                triggered=False,
                weight=0.0,
                detail="该地址没有任何入账交易，无法评估资金汇聚",
                related=(),
                status=EvidenceStatus.NO_DATA,
                insufficiency_code="NO_INCOMING_TX",
                insufficiency_detail="缺少入账方向的任何交易，无来源样本可判断是否汇聚",
            )
        if n < self.MIN_SOURCES_FOR_EVIDENCE:
            return FactorEvidence(
                code=FactorCode.FUND_CONVERGENCE,
                triggered=False,
                weight=0.0,
                detail=(
                    f"入账来源数={n}，样本方向单一，既不足以确认汇聚也不足以排除汇聚"
                ),
                related=tuple(sorted(sources)),
                status=EvidenceStatus.INSUFFICIENT,
                insufficiency_code="SINGLE_INCOMING_SOURCE",
                insufficiency_detail=(
                    f"仅有 {n} 个入账来源（至少需要 "
                    f"{self.MIN_SOURCES_FOR_EVIDENCE} 个不同来源才能判断汇聚形态），"
                    "缺少更多对手方样本"
                ),
            )
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
            status=EvidenceStatus.SUFFICIENT,
        )

    def dispersion(self, address: str) -> FactorEvidence:
        """资金分散：该地址向多个不同目标转出。"""
        targets = self._out.get(address, set())
        n = len(targets)
        if n == 0:
            return FactorEvidence(
                code=FactorCode.FUND_DISPERSION,
                triggered=False,
                weight=0.0,
                detail="该地址没有任何出账交易，无法评估资金分散",
                related=(),
                status=EvidenceStatus.NO_DATA,
                insufficiency_code="NO_OUTGOING_TX",
                insufficiency_detail="缺少出账方向的任何交易，无目标样本可判断是否分散",
            )
        if n < self.MIN_TARGETS_FOR_EVIDENCE:
            return FactorEvidence(
                code=FactorCode.FUND_DISPERSION,
                triggered=False,
                weight=0.0,
                detail=f"出账目标数={n}，样本方向单一，无法确认或排除分散形态",
                related=tuple(sorted(targets)),
                status=EvidenceStatus.INSUFFICIENT,
                insufficiency_code="SINGLE_OUTGOING_TARGET",
                insufficiency_detail=(
                    f"仅有 {n} 个出账目标（至少需要 "
                    f"{self.MIN_TARGETS_FOR_EVIDENCE} 个不同目标才能判断分散形态），"
                    "缺少更多对手方样本"
                ),
            )
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
            status=EvidenceStatus.SUFFICIENT,
        )

    def ring_flow(self, address: str) -> FactorEvidence:
        """环形流转：有向资金图中存在经过该地址的环。"""
        has_out = bool(self._out.get(address))
        has_in = bool(self._inn.get(address))
        if not has_out and not has_in:
            return FactorEvidence(
                code=FactorCode.RING_FLOW,
                triggered=False,
                weight=0.0,
                detail="该地址没有任何资金往来，无法评估环形流转",
                related=(),
                status=EvidenceStatus.NO_DATA,
                insufficiency_code="NO_INTERACTION",
                insufficiency_detail="该地址没有任何交易，无资金链路可判断是否回流成环",
            )
        if not has_out or not has_in:
            missing = "出账" if not has_out else "入账"
            return FactorEvidence(
                code=FactorCode.RING_FLOW,
                triggered=False,
                weight=0.0,
                detail=f"该地址只有{'入' if not has_out else '出'}账方向交易，缺少{missing}对手方，无法判定是否成环",
                related=(),
                status=EvidenceStatus.INSUFFICIENT,
                insufficiency_code="MISSING_COUNTERPARTY_DIRECTION",
                insufficiency_detail=(
                    f"环形流转要求资金有去有回，但该地址缺少{missing}方向的对手方，"
                    "无法形成可回流的链路样本"
                ),
            )
        cycle = self._find_cycle_through(address)
        hit = cycle is not None
        if hit:
            detail = (
                f"检测到经过 {address} 的资金环：{' -> '.join(cycle)}，"
                "资金最终回流，疑似环形流转"
            )
            related = tuple(cycle)
        else:
            detail = "入账与出账对手方均存在，未检测到经过该地址的有向资金环"
            related = ()
        return FactorEvidence(
            code=FactorCode.RING_FLOW,
            triggered=hit,
            weight=self.WEIGHT_RING if hit else 0.0,
            detail=detail,
            related=related,
            status=EvidenceStatus.SUFFICIENT,
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
        n = len(times)
        if n == 0:
            return FactorEvidence(
                code=FactorCode.HIGH_FREQ_INTERACTION,
                triggered=False,
                weight=0.0,
                detail="该地址没有任何交互记录，无法评估短时高频交互",
                status=EvidenceStatus.NO_DATA,
                insufficiency_code="NO_INTERACTION",
                insufficiency_detail="没有任何交互时间戳样本，无法判断交互是否短时高频",
            )
        if n < self.HIGH_FREQ_COUNT:
            span = (times[-1] - times[0]) if n >= 2 else 0.0
            return FactorEvidence(
                code=FactorCode.HIGH_FREQ_INTERACTION,
                triggered=False,
                weight=0.0,
                detail=(
                    f"仅有 {n} 笔交互（高频判定需要至少 {self.HIGH_FREQ_COUNT} 笔），"
                    f"观测时间跨度 {span:.0f} 秒，样本量不足"
                ),
                status=EvidenceStatus.INSUFFICIENT,
                insufficiency_code="TOO_FEW_INTERACTIONS",
                insufficiency_detail=(
                    f"交互样本仅 {n} 笔，少于高频窗口所需的 "
                    f"{self.HIGH_FREQ_COUNT} 笔；时间跨度 {span:.0f} 秒，"
                    "无法确认也无法排除短时高频"
                ),
            )
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
            status=EvidenceStatus.SUFFICIENT,
        )

    def high_risk_direct(self, address: str) -> FactorEvidence:
        """与已知高风险地址直接（1 跳，双向）关联。"""
        neighbors = self._out.get(address, set()) | self._inn.get(address, set())
        if not neighbors:
            return FactorEvidence(
                code=FactorCode.HIGH_RISK_DIRECT,
                triggered=False,
                weight=0.0,
                detail="该地址没有任何直接对手方，无法评估与高风险地址的直接关联",
                related=(),
                status=EvidenceStatus.NO_DATA,
                insufficiency_code="NO_COUNTERPARTY",
                insufficiency_detail="没有任何直接资金往来对手方，无法比对高风险名单",
            )
        direct = sorted(a for a in neighbors if a in self._high_risk)
        hit = bool(direct)
        if hit:
            detail = f"与已知高风险地址存在直接资金往来：{', '.join(direct)}"
        else:
            detail = (
                f"已核查全部 {len(neighbors)} 个直接对手方，"
                "未发现与已知高风险地址的直接资金往来"
            )
        return FactorEvidence(
            code=FactorCode.HIGH_RISK_DIRECT,
            triggered=hit,
            weight=self.WEIGHT_HIGH_RISK_DIRECT if hit else 0.0,
            detail=detail,
            related=tuple(direct),
            status=EvidenceStatus.SUFFICIENT,
        )

    def high_risk_indirect(self, address: str) -> FactorEvidence:
        """与已知高风险地址 2 跳间接关联（直接命中的不计入本因子）。"""
        neighbors = self._out.get(address, set()) | self._inn.get(address, set())
        if not neighbors:
            return FactorEvidence(
                code=FactorCode.HIGH_RISK_INDIRECT,
                triggered=False,
                weight=0.0,
                detail="该地址没有任何对手方，无法评估与高风险地址的间接关联",
                related=(),
                status=EvidenceStatus.NO_DATA,
                insufficiency_code="NO_COUNTERPARTY",
                insufficiency_detail="没有一跳对手方，二跳间接关联无从展开",
            )
        frontier_out = set()
        frontier_in = set()
        for a in neighbors:
            frontier_out |= self._out.get(a, set())
            frontier_in |= self._inn.get(a, set())
        reachable = frontier_out | frontier_in
        # 二跳“上下文”必须包含除被评估地址自身之外的节点，否则邻居都是
        # 孤立端点（如 A<->B 两条边），间接关联没有可核查的二跳样本。
        two_hop_context = reachable - {address}
        if not two_hop_context:
            return FactorEvidence(
                code=FactorCode.HIGH_RISK_INDIRECT,
                triggered=False,
                weight=0.0,
                detail=(
                    f"直接对手方 {len(neighbors)} 个，但其均无其他交易对手，"
                    "缺少可展开二跳核查的链路样本"
                ),
                related=tuple(sorted(neighbors)),
                status=EvidenceStatus.INSUFFICIENT,
                insufficiency_code="NO_TWO_HOP_CONTEXT",
                insufficiency_detail=(
                    "一跳对手方之外观察不到任何二跳地址，"
                    "无法核查 2 跳范围内是否存在高风险地址"
                ),
            )
        direct = neighbors & self._high_risk
        indirect: dict[str, int] = {}
        for h in two_hop_context:
            if h in self._high_risk and h not in direct:
                indirect[h] = self.INDIRECT_MAX_HOPS
        names = sorted(indirect)
        hit = bool(names)
        if hit:
            detail = (
                f"经 1 个中间地址后与高风险地址关联（{self.INDIRECT_MAX_HOPS} 跳内）："
                f"{', '.join(names)}"
            )
        else:
            detail = (
                f"已沿 {len(neighbors)} 个一跳对手方展开二跳核查"
                f"（{len(two_hop_context)} 个二跳地址），"
                f"{self.INDIRECT_MAX_HOPS} 跳内未发现间接高风险关联"
            )
        return FactorEvidence(
            code=FactorCode.HIGH_RISK_INDIRECT,
            triggered=hit,
            weight=self.WEIGHT_HIGH_RISK_INDIRECT if hit else 0.0,
            detail=detail,
            related=tuple(names),
            status=EvidenceStatus.SUFFICIENT,
        )
