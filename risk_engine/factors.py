"""风险因子计算：每个因子独立、可解释。

所有因子的输入是“按区块全序排列的交易”，因此与摄入到达顺序无关，
同一输入必然得到同一组证据。每个因子无论触发与否都返回 FactorEvidence，
detail 中写明观测值与阈值，使判定依据可复核。
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

    # 维度级证据充分口径（详见 README“维度级证据判定口径”）：
    # 未触发时，只有观测面覆盖到“足以排除”的程度才算证据充分（可安全放行）；
    # 有观测但覆盖面不足 -> INSUFFICIENT，并给出具体缺什么；
    # 完全没有相关数据 -> NO_DATA。
    # 资金汇聚/分散的“未触发也充分”必须同时满足三个边界条件：
    #   a) 该方向已见过足够多的不同对手方（严格小于触发阈值，使该档可达）；
    #   b) 两个方向都有观测（方向不单一，资金进出全貌都看过）；
    #   c) 参与交易的时间跨度足以覆盖一个判定窗口。
    # 任一条不满足都只能算证据不足，并指出具体缺什么。
    CONVERGENCE_NEGATIVE_MIN_SOURCES = 2   # 入账方向上至少看过 2 个不同来源（< 触发阈值 3）
    DISPERSION_NEGATIVE_MIN_TARGETS = 2    # 出账方向上至少看过 2 个不同目标（< 触发阈值 3）
    FLOW_NEGATIVE_MIN_SPAN = 5.0           # 资金流维度观测时间跨度下限（秒，≥一个判定窗口）
    HIGH_RISK_DIRECT_MIN_COUNTERPARTIES = 2  # 至少见过 2 个不同直接对手方
    HIGH_FREQ_MIN_SPAN = 5.0               # 观测时间跨度至少覆盖一个判定窗口
    INDIRECT_MIN_INTERMEDIARIES = 1        # 至少存在 1 个可展开 2 跳的中间地址

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

    def _participation_span(self, address: str) -> tuple[int, float]:
        """该地址全部参与交易的（笔数, 最早~最晚时间戳跨度）。

        汇聚/分散的“观测是否充分”除了看该方向对手方面，还要确认账户在两个
        方向上都被观测过、且观测窗口足够长，因此跨度按全部参与交易计算。
        """
        times = sorted(t.timestamp for t in self._participation.get(address, ()))
        if not times:
            return 0, 0.0
        return len(times), times[-1] - times[0]

    @staticmethod
    def _evidence(
        code: FactorCode,
        triggered: bool,
        weight: float,
        detail: str,
        related: tuple[str, ...] = (),
        status: EvidenceStatus = EvidenceStatus.SUFFICIENT,
        gap_code: str | None = None,
        gap_detail: str | None = None,
    ) -> FactorEvidence:
        """统一构造因子证据；未触发且证据不充分时挂上缺证据原因。"""
        if triggered:
            # 触发即证据充分：观测本身已支撑正向结论
            status = EvidenceStatus.SUFFICIENT
            gap_code = gap_detail = None
        return FactorEvidence(
            code=code,
            triggered=triggered,
            weight=weight if triggered else 0.0,
            detail=detail,
            related=related,
            evidence_status=status,
            gap_code=gap_code,
            gap_detail=gap_detail,
        )

    def convergence(self, address: str) -> FactorEvidence:
        """资金汇聚：多个不同来源向该地址转入。"""
        incoming = [
            t for t in self._participation.get(address, ()) if t.to_addr == address
        ]
        sources = {t.from_addr for t in incoming}
        n = len(sources)
        hit = n >= self.CONVERGENCE_IN_DEGREE
        if hit:
            detail = (
                f"检测到 {n} 个不同来源地址向 {address} 转入"
                f"（阈值>={self.CONVERGENCE_IN_DEGREE}），呈资金汇聚形态"
            )
            return self._evidence(
                FactorCode.FUND_CONVERGENCE, True, self.WEIGHT_CONVERGENCE, detail,
                related=tuple(sorted(sources)),
            )
        detail = (
            f"入账来源数={n}，未达汇聚阈值{self.CONVERGENCE_IN_DEGREE}"
        )
        if not incoming:
            return self._evidence(
                FactorCode.FUND_CONVERGENCE, False, 0.0,
                f"{detail}；该地址没有任何入账记录，本维度完全无数据",
                status=EvidenceStatus.NO_DATA,
                gap_code="NO_INCOMING_TX",
                gap_detail=f"地址 {address} 无任何入账交易，无法评估资金汇聚",
            )
        # 未触发时，三类覆盖面缺口要如实分开（均为证据不足，不能当作安全放行）
        if n < self.CONVERGENCE_NEGATIVE_MIN_SOURCES:
            return self._evidence(
                FactorCode.FUND_CONVERGENCE, False, 0.0,
                f"{detail}；入账仅覆盖 {n} 个来源，来源面不足以排除汇聚",
                status=EvidenceStatus.INSUFFICIENT,
                gap_code="TOO_FEW_INCOMING_SOURCES",
                gap_detail=(
                    f"仅观察到 {n} 个不同入账来源（充分口径需 "
                    f"{self.CONVERGENCE_NEGATIVE_MIN_SOURCES} 个，且仍低于触发阈值 "
                    f"{self.CONVERGENCE_IN_DEGREE}），入账来源面太窄，"
                    "不能排除资金汇聚"
                ),
                related=tuple(sorted(sources)),
            )
        if not self._out.get(address):
            return self._evidence(
                FactorCode.FUND_CONVERGENCE, False, 0.0,
                f"{detail}；仅有入账、没有任何出账，观测方向单一",
                status=EvidenceStatus.INSUFFICIENT,
                gap_code="SINGLE_DIRECTION_ONLY",
                gap_detail=(
                    f"地址 {address} 只观察到入账方向交易，没有任何出账，"
                    "资金进出全貌未覆盖（汇聚后的去向未被观测），"
                    "样本方向单一，不能排除资金汇聚"
                ),
                related=tuple(sorted(sources)),
            )
        _, span = self._participation_span(address)
        if span < self.FLOW_NEGATIVE_MIN_SPAN:
            return self._evidence(
                FactorCode.FUND_CONVERGENCE, False, 0.0,
                f"{detail}；虽有 {n} 个入账来源且双向都有交易，但观测时间跨度仅 "
                f"{span:.0f} 秒，不足以排除汇聚",
                status=EvidenceStatus.INSUFFICIENT,
                gap_code="OBSERVATION_SPAN_TOO_SHORT",
                gap_detail=(
                    f"双向交易已覆盖、入账来源 {n} 个，但整体观测时间跨度仅 "
                    f"{span:.0f} 秒（充分口径需 ≥ "
                    f"{self.FLOW_NEGATIVE_MIN_SPAN:.0f} 秒），"
                    "观测窗口太短，历史上仍可能存在未被看到的汇聚，不能排除资金汇聚"
                ),
                related=tuple(sorted(sources)),
            )
        # 来源面足够、双向都看过、跨度足够且仍未触发：查过了确实没问题
        return self._evidence(
            FactorCode.FUND_CONVERGENCE, False, 0.0,
            (
                f"{detail}；已观测到 {n} 个不同入账来源、出账方向也有交易、"
                f"时间跨度 {span:.0f} 秒，观测面足以排除资金汇聚形态"
            ),
            related=tuple(sorted(sources)),
        )

    def dispersion(self, address: str) -> FactorEvidence:
        """资金分散：该地址向多个不同目标转出。"""
        outgoing = [
            t for t in self._participation.get(address, ()) if t.from_addr == address
        ]
        targets = {t.to_addr for t in outgoing}
        n = len(targets)
        hit = n >= self.DISPERSION_OUT_DEGREE
        if hit:
            detail = (
                f"{address} 向 {n} 个不同目标地址转出"
                f"（阈值>={self.DISPERSION_OUT_DEGREE}），呈资金分散形态"
            )
            return self._evidence(
                FactorCode.FUND_DISPERSION, True, self.WEIGHT_DISPERSION, detail,
                related=tuple(sorted(targets)),
            )
        detail = f"出账目标数={n}，未达分散阈值{self.DISPERSION_OUT_DEGREE}"
        if not outgoing:
            return self._evidence(
                FactorCode.FUND_DISPERSION, False, 0.0,
                f"{detail}；该地址没有任何出账记录，本维度完全无数据",
                status=EvidenceStatus.NO_DATA,
                gap_code="NO_OUTGOING_TX",
                gap_detail=f"地址 {address} 无任何出账交易，无法评估资金分散",
            )
        if n < self.DISPERSION_NEGATIVE_MIN_TARGETS:
            return self._evidence(
                FactorCode.FUND_DISPERSION, False, 0.0,
                f"{detail}；出账仅覆盖 {n} 个目标，目标面不足以排除分散",
                status=EvidenceStatus.INSUFFICIENT,
                gap_code="TOO_FEW_OUTGOING_TARGETS",
                gap_detail=(
                    f"仅观察到 {n} 个不同出账目标（充分口径需 "
                    f"{self.DISPERSION_NEGATIVE_MIN_TARGETS} 个，且仍低于触发阈值 "
                    f"{self.DISPERSION_OUT_DEGREE}），出账目标面太窄，"
                    "不能排除资金分散"
                ),
                related=tuple(sorted(targets)),
            )
        if not self._inn.get(address):
            return self._evidence(
                FactorCode.FUND_DISPERSION, False, 0.0,
                f"{detail}；仅有出账、没有任何入账，观测方向单一",
                status=EvidenceStatus.INSUFFICIENT,
                gap_code="SINGLE_DIRECTION_ONLY",
                gap_detail=(
                    f"地址 {address} 只观察到出账方向交易，没有任何入账，"
                    "资金进出全貌未覆盖（分散资金的来源未被观测），"
                    "样本方向单一，不能排除资金分散"
                ),
                related=tuple(sorted(targets)),
            )
        _, span = self._participation_span(address)
        if span < self.FLOW_NEGATIVE_MIN_SPAN:
            return self._evidence(
                FactorCode.FUND_DISPERSION, False, 0.0,
                f"{detail}；虽有 {n} 个出账目标且双向都有交易，但观测时间跨度仅 "
                f"{span:.0f} 秒，不足以排除分散",
                status=EvidenceStatus.INSUFFICIENT,
                gap_code="OBSERVATION_SPAN_TOO_SHORT",
                gap_detail=(
                    f"双向交易已覆盖、出账目标 {n} 个，但整体观测时间跨度仅 "
                    f"{span:.0f} 秒（充分口径需 ≥ "
                    f"{self.FLOW_NEGATIVE_MIN_SPAN:.0f} 秒），"
                    "观测窗口太短，历史上仍可能存在未被看到的分散，不能排除资金分散"
                ),
                related=tuple(sorted(targets)),
            )
        # 目标面足够、双向都看过、跨度足够且仍未触发：查过了确实没问题
        return self._evidence(
            FactorCode.FUND_DISPERSION, False, 0.0,
            (
                f"{detail}；已观测到 {n} 个不同出账目标、入账方向也有交易、"
                f"时间跨度 {span:.0f} 秒，观测面足以排除资金分散形态"
            ),
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
            return self._evidence(
                FactorCode.RING_FLOW, True, self.WEIGHT_RING, detail,
                related=tuple(cycle),
            )
        # 无环时，依据样本结构判断证据是否足以排除
        has_out = bool(self._out.get(address))
        has_in = bool(self._inn.get(address))
        if not has_out and not has_in:
            return self._evidence(
                FactorCode.RING_FLOW, False, 0.0,
                "该地址无任何交易，无法评估环形流转",
                status=EvidenceStatus.NO_DATA,
                gap_code="NO_PARTICIPATION",
                gap_detail=f"地址 {address} 未参与任何交易，环形流转维度完全无数据",
            )
        if not has_out or not has_in:
            return self._evidence(
                FactorCode.RING_FLOW, False, 0.0,
                "未检测到经过该地址的有向资金环；交互方向单一，资金不可能回流",
                status=EvidenceStatus.INSUFFICIENT,
                gap_code="SINGLE_DIRECTION_ONLY",
                gap_detail=(
                    f"地址 {address} 只有{'出账' if has_out else '入账'}方向交易，"
                    "样本方向单一，缺少回流路径，无法据此排除环形流转"
                ),
            )
        return self._evidence(
            FactorCode.RING_FLOW, False, 0.0,
            "未检测到经过该地址的有向资金环",
            related=tuple(sorted(self._out.get(address, set()) | self._inn.get(address, set()))),
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
            return self._evidence(
                FactorCode.HIGH_FREQ_INTERACTION, True, self.WEIGHT_HIGH_FREQ, detail,
            )
        detail = (
            f"最长窗口交互笔数={worst}，未达高频阈值{self.HIGH_FREQ_COUNT}"
        )
        n = len(times)
        if n == 0:
            return self._evidence(
                FactorCode.HIGH_FREQ_INTERACTION, False, 0.0,
                f"{detail}；该地址无任何带时间戳的交互，本维度完全无数据",
                status=EvidenceStatus.NO_DATA,
                gap_code="NO_INTERACTION",
                gap_detail=f"地址 {address} 无任何交互记录，无法评估短时高频",
            )
        span = times[-1] - times[0]
        if n < self.HIGH_FREQ_COUNT and span < self.HIGH_FREQ_MIN_SPAN:
            return self._evidence(
                FactorCode.HIGH_FREQ_INTERACTION, False, 0.0,
                f"{detail}；仅 {n} 笔交互、时间跨度 {span:.0f} 秒，不足一个判定窗口",
                status=EvidenceStatus.INSUFFICIENT,
                gap_code="TIME_SPAN_TOO_SHORT",
                gap_detail=(
                    f"仅有 {n} 笔交互（阈值需 {self.HIGH_FREQ_COUNT} 笔），"
                    f"时间跨度 {span:.0f} 秒，未覆盖一个 "
                    f"{self.HIGH_FREQ_MIN_SPAN:.0f} 秒判定窗口，"
                    "观测时长不足，不能排除短时高频"
                ),
            )
        return self._evidence(
            FactorCode.HIGH_FREQ_INTERACTION, False, 0.0, detail,
        )

    def high_risk_direct(self, address: str) -> FactorEvidence:
        """与已知高风险地址直接（1 跳，双向）关联。"""
        neighbors = self._out.get(address, set()) | self._inn.get(address, set())
        direct = sorted(a for a in neighbors if a in self._high_risk)
        hit = bool(direct)
        if hit:
            detail = f"与已知高风险地址存在直接资金往来：{', '.join(direct)}"
            return self._evidence(
                FactorCode.HIGH_RISK_DIRECT, True, self.WEIGHT_HIGH_RISK_DIRECT,
                detail, related=tuple(direct),
            )
        n = len(neighbors)
        detail = "未发现与已知高风险地址的直接资金往来"
        if n == 0:
            return self._evidence(
                FactorCode.HIGH_RISK_DIRECT, False, 0.0,
                f"{detail}；该地址没有任何对手方，本维度完全无数据",
                status=EvidenceStatus.NO_DATA,
                gap_code="NO_COUNTERPARTY",
                gap_detail=f"地址 {address} 无任何交易对手方，无法评估直接高风险关联",
            )
        if n < self.HIGH_RISK_DIRECT_MIN_COUNTERPARTIES:
            return self._evidence(
                FactorCode.HIGH_RISK_DIRECT, False, 0.0,
                f"{detail}；仅观察到 {n} 个对手方，缺少应有的对手方覆盖",
                status=EvidenceStatus.INSUFFICIENT,
                gap_code="TOO_FEW_COUNTERPARTIES",
                gap_detail=(
                    f"仅观察到 {n} 个不同直接对手方（充分口径需 "
                    f"{self.HIGH_RISK_DIRECT_MIN_COUNTERPARTIES} 个），"
                    "对手方覆盖不足，不能排除与高风险地址直接往来"
                ),
                related=tuple(sorted(neighbors)),
            )
        return self._evidence(
            FactorCode.HIGH_RISK_DIRECT, False, 0.0, detail,
            related=tuple(sorted(neighbors)),
        )

    def high_risk_indirect(self, address: str) -> FactorEvidence:
        """与已知高风险地址 2 跳间接关联（直接命中的不计入本因子）。"""
        one_hop = self._out.get(address, set()) | self._inn.get(address, set())
        direct = one_hop & self._high_risk
        frontier_out = set()
        frontier_in = set()
        intermediaries: set[str] = set()
        for a in one_hop:
            beyond_out = self._out.get(a, set()) - {address}
            beyond_in = self._inn.get(a, set()) - {address}
            if beyond_out or beyond_in:
                intermediaries.add(a)
            frontier_out |= self._out.get(a, set())
            frontier_in |= self._inn.get(a, set())
        reachable = frontier_out | frontier_in
        indirect = {
            h for h in reachable
            if h != address and h in self._high_risk and h not in direct
        }
        names = sorted(indirect)
        hit = bool(names)
        if hit:
            detail = (
                f"经 1 个中间地址后与高风险地址关联（{self.INDIRECT_MAX_HOPS} 跳内）："
                f"{', '.join(names)}"
            )
            return self._evidence(
                FactorCode.HIGH_RISK_INDIRECT, True, self.WEIGHT_HIGH_RISK_INDIRECT,
                detail, related=tuple(names),
            )
        detail = f"{self.INDIRECT_MAX_HOPS} 跳内未发现间接高风险关联"
        if not one_hop:
            return self._evidence(
                FactorCode.HIGH_RISK_INDIRECT, False, 0.0,
                f"{detail}；该地址没有任何对手方，本维度完全无数据",
                status=EvidenceStatus.NO_DATA,
                gap_code="NO_COUNTERPARTY",
                gap_detail=f"地址 {address} 无任何交易对手方，无法评估间接高风险关联",
            )
        if len(intermediaries) < self.INDIRECT_MIN_INTERMEDIARIES:
            return self._evidence(
                FactorCode.HIGH_RISK_INDIRECT, False, 0.0,
                f"{detail}；{len(one_hop)} 个一跳对手方均无后续往来，2 跳路径展不开",
                status=EvidenceStatus.INSUFFICIENT,
                gap_code="NO_TWO_HOP_PATH",
                gap_detail=(
                    f"观察到 {len(one_hop)} 个一跳对手方，但没有任何对手方存在"
                    f"绕过 {address} 的后续交易，缺少 2 跳路径，"
                    "无法据此排除间接高风险关联"
                ),
                related=tuple(sorted(one_hop)),
            )
        return self._evidence(
            FactorCode.HIGH_RISK_INDIRECT, False, 0.0, detail,
            related=tuple(sorted(intermediaries)),
        )
