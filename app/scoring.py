"""基于可解释因子的风险评分。

五类因子（均可单独解释、单独给出触发/未触发/数据不足结论）：
- CONCENTRATION 资金汇聚：>=3 个不同上游、>=3 笔汇入，且汇入笔数不少于汇出
- DISPERSION   资金分散：>=3 个不同下游、>=3 笔转出，且转出笔数不少于汇入
- HIGH_FREQUENCY 短时高频：与同一对手方在跨度 <=2 个区块内发生 >=3 笔交互
- RING_FLOW    环形流转：目标处于长度 >=3 条边的有向环中（A->B->C->A）
- HIGH_RISK_LINK 高风险关联：沿资金流向与标记钱包直连（0.30），
               或经一跳中介的两跳资金路径相连（0.15）

总分 = 各触发因子权重之和（封顶 1.0）：
- >= 0.60 HIGH；>= 0.30 MEDIUM；其余 LOW
- 数据不足（观察交易 <2 笔或可评估因子 <3 个）时：
  * 若已有 >=0.30 的强因子证据，仍给出 MEDIUM/HIGH，同时保留不足说明；
  * 否则一律 UNDETERMINED——证据不足绝不判 LOW（不默认安全）。

评分是账本快照的纯函数：同一快照多次评估结论必然一致；迟到交易回放后
基于新区快重算，历史结论不会被“原地污染”。
"""
from __future__ import annotations

from app.errors import LedgerNotFoundError
from app.ledger import Ledger, LedgerSnapshot
from app.models import FactorCode, FactorEvidence, RiskReport, Transaction, Verdict

# 因子权重
W_CONCENTRATION = 0.20
W_DISPERSION = 0.20
W_HIGH_FREQUENCY = 0.20
W_RING_FLOW = 0.30
W_LINK_DIRECT = 0.30
W_LINK_INDIRECT = 0.15

# 判定阈值
SCORE_HIGH = 0.60
SCORE_MEDIUM = 0.30
MIN_OBSERVED_TXS = 2      # 整体可判定所需的最少相关交易数
MIN_EVALUABLE_FACTORS = 3  # 整体可判定所需的最少可评估因子数
FAN_DEGREE = 3            # 汇聚/分散所需的不同对手方数
HF_WINDOW_BLOCKS = 2      # 高频窗口（区块跨度）
HF_WINDOW_TXS = 3         # 高频窗口内最少交互笔数
MAX_RING_LEN = 6          # 环搜索最大边长，保证有界确定


def _ev_insufficient(code: FactorCode, reason: str) -> FactorEvidence:
    return FactorEvidence(code=code, triggered=False, score=0.0,
                          explanation="数据不足，未参与评分", data_points={},
                          insufficient=True, insufficient_reason=reason)


class RiskScorer:
    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger

    def evaluate(self, address: str) -> RiskReport:
        snap = self._ledger.snapshot()
        if address not in snap.wallets:
            raise LedgerNotFoundError(f"钱包不存在: {address}")
        rel = snap.transactions_for(address)

        factors = (
            self._factor_concentration(address, rel),
            self._factor_dispersion(address, rel),
            self._factor_high_frequency(address, rel),
            self._factor_ring_flow(address, snap),
            self._factor_high_risk_link(address, snap),
        )

        undetermined: list[str] = []
        if len(rel) < MIN_OBSERVED_TXS:
            undetermined.append(
                f"整体观察样本不足：仅 {len(rel)} 笔相关交易，"
                f"至少需要 {MIN_OBSERVED_TXS} 笔才能给出风险结论"
            )
        evaluable = [f for f in factors if not f.insufficient]
        if len(evaluable) < MIN_EVALUABLE_FACTORS:
            undetermined.append(
                f"可评估因子不足：仅 {len(evaluable)}/5 个因子具备足够数据 "
                f"（{', '.join(f.code.value + ':' + f.insufficient_reason for f in factors if f.insufficient) or '无'})"
            )

        score = round(min(1.0, sum(f.score for f in factors)), 4)
        if undetermined:
            # 数据不足时，只有达到 MEDIUM 的强证据才能单独支撑风险结论，
            # 否则返回 UNDETERMINED（绝不因证据缺失而默认 LOW）。
            if score >= SCORE_HIGH:
                verdict = Verdict.HIGH
            elif score >= SCORE_MEDIUM:
                verdict = Verdict.MEDIUM
            else:
                verdict = Verdict.UNDETERMINED
        elif score >= SCORE_HIGH:
            verdict = Verdict.HIGH
        elif score >= SCORE_MEDIUM:
            verdict = Verdict.MEDIUM
        else:
            verdict = Verdict.LOW

        return RiskReport(
            address=address,
            score=score,
            verdict=verdict,
            factors=factors,
            undetermined_reasons=tuple(undetermined),
            evaluated_tip=snap.tip,
        )

    # ------------------------------------------------------------------ #
    # 因子 1：资金汇聚
    # ------------------------------------------------------------------ #
    def _factor_concentration(self, addr: str, rel: tuple[Transaction, ...]) -> FactorEvidence:
        inflows = tuple(sorted((t for t in rel if t.receiver == addr),
                               key=lambda t: (t.block, t.tx_index)))
        out_n = len(rel) - len(inflows)
        if len(inflows) < 2:
            return _ev_insufficient(
                FactorCode.CONCENTRATION,
                f"汇入样本不足：仅观察到 {len(inflows)} 笔入账，至少需要 2 笔",
            )
        sources = sorted({t.sender for t in inflows})
        amount_in = sum(t.amount for t in inflows)
        triggered = (len(sources) >= FAN_DEGREE and len(inflows) >= FAN_DEGREE
                     and len(inflows) >= out_n)
        why = (
            f"检测到 {len(sources)} 个不同上游地址向 {addr} 汇聚资金，"
            f"汇入 {len(inflows)} 笔 / 汇出 {out_n} 笔，合计流入 {amount_in}"
            if triggered else
            f"汇入 {len(inflows)} 笔来自 {len(sources)} 个上游，未达到汇聚形态"
            f"（需>={FAN_DEGREE} 个不同上游、>={FAN_DEGREE} 笔汇入且汇入笔数>=汇出笔数）"
        )
        return FactorEvidence(
            FactorCode.CONCENTRATION, triggered,
            W_CONCENTRATION if triggered else 0.0, why,
            {"distinct_sources": sources, "source_count": len(sources),
             "inflow_count": len(inflows), "outflow_count": out_n,
             "total_inflow": amount_in},
        )

    # ------------------------------------------------------------------ #
    # 因子 2：资金分散
    # ------------------------------------------------------------------ #
    def _factor_dispersion(self, addr: str, rel: tuple[Transaction, ...]) -> FactorEvidence:
        outflows = tuple(sorted((t for t in rel if t.sender == addr),
                                key=lambda t: (t.block, t.tx_index)))
        in_n = len(rel) - len(outflows)
        if len(outflows) < 2:
            return _ev_insufficient(
                FactorCode.DISPERSION,
                f"汇出样本不足：仅观察到 {len(outflows)} 笔出账，至少需要 2 笔",
            )
        targets = sorted({t.receiver for t in outflows})
        amount_out = sum(t.amount for t in outflows)
        triggered = (len(targets) >= FAN_DEGREE and len(outflows) >= FAN_DEGREE
                     and len(outflows) >= in_n)
        why = (
            f"检测到 {addr} 向 {len(targets)} 个不同下游分散转出，"
            f"转出 {len(outflows)} 笔 / 转入 {in_n} 笔，合计流出 {amount_out}"
            if triggered else
            f"转出 {len(outflows)} 笔至 {len(targets)} 个下游，未达到分散形态"
            f"（需>={FAN_DEGREE} 个不同下游、>={FAN_DEGREE} 笔转出且转出笔数>=转入笔数）"
        )
        return FactorEvidence(
            FactorCode.DISPERSION, triggered,
            W_DISPERSION if triggered else 0.0, why,
            {"distinct_targets": targets, "target_count": len(targets),
             "outflow_count": len(outflows), "inflow_count": in_n,
             "total_outflow": amount_out},
        )

    # ------------------------------------------------------------------ #
    # 因子 3：短时高频交互
    # ------------------------------------------------------------------ #
    def _factor_high_frequency(self, addr: str, rel: tuple[Transaction, ...]) -> FactorEvidence:
        if len(rel) < HF_WINDOW_TXS:
            return _ev_insufficient(
                FactorCode.HIGH_FREQUENCY,
                f"交互样本不足：仅 {len(rel)} 笔相关交易，窗口至少需要 {HF_WINDOW_TXS} 笔",
            )
        groups: dict[str, list[Transaction]] = {}
        for t in rel:
            cpty = t.receiver if t.sender == addr else t.sender
            groups.setdefault(cpty, []).append(t)

        hit: tuple[str, list[str], int] | None = None
        for cpty in sorted(groups):
            txs = sorted(groups[cpty], key=lambda t: (t.block, t.tx_index))
            for i in range(len(txs) - HF_WINDOW_TXS + 1):
                window = txs[i:i + HF_WINDOW_TXS]
                span = window[-1].block - window[0].block
                if span <= HF_WINDOW_BLOCKS:
                    hit = (cpty, [t.tx_id for t in window], span)
                    break
            if hit:
                break

        if hit:
            cpty, tx_ids, span = hit
            why = (f"与对手方 {cpty} 在 {span} 个区块跨度内发生 {HF_WINDOW_TXS} 笔交互"
                   f"（{', '.join(tx_ids)}），符合短时高频模式")
            return FactorEvidence(FactorCode.HIGH_FREQUENCY, True, W_HIGH_FREQUENCY, why,
                                  {"counterparty": cpty, "window_tx_ids": tx_ids,
                                   "block_span": span, "window_blocks": HF_WINDOW_BLOCKS})
        detail = {c: len(v) for c, v in sorted(groups.items())}
        return FactorEvidence(
            FactorCode.HIGH_FREQUENCY, False, 0.0,
            f"未发现同一对手方在 {HF_WINDOW_BLOCKS} 个区块内 >= {HF_WINDOW_TXS} 笔的交互",
            {"per_counterparty_counts": detail, "window_blocks": HF_WINDOW_BLOCKS},
        )

    # ------------------------------------------------------------------ #
    # 因子 4：环形流转
    # ------------------------------------------------------------------ #
    def _factor_ring_flow(self, addr: str, snap: LedgerSnapshot) -> FactorEvidence:
        edges: dict[str, list[tuple[str, str]]] = {}
        for t in snap.transactions:
            edges.setdefault(t.sender, []).append((t.receiver, t.tx_id))
        for k in edges:
            edges[k].sort()

        rel = snap.transactions_for(addr)
        if len(rel) < 2:
            return _ev_insufficient(
                FactorCode.RING_FLOW,
                f"流转样本不足：仅 {len(rel)} 笔相关交易，无法构成环形回路",
            )

        found = self._search_ring(addr, edges)
        if found is None:
            return FactorEvidence(
                FactorCode.RING_FLOW, False, 0.0,
                f"从 {addr} 出发未发现长度 >=3 条边且回到自身的有向环（搜索上界 {MAX_RING_LEN} 边）",
                {"max_ring_len": MAX_RING_LEN},
            )

        nodes, edge_ids = found
        why = f"发现环形流转：{' -> '.join(nodes)}（{len(edge_ids)} 条边：{', '.join(edge_ids)}）"
        return FactorEvidence(FactorCode.RING_FLOW, True, W_RING_FLOW, why,
                              {"ring_nodes": nodes, "ring_tx_ids": edge_ids})

    def _search_ring(self, start: str, edges: dict[str, list[tuple[str, str]]]
                     ) -> tuple[list[str], list[str]] | None:
        """确定性 DFS：返回包含 start 且长度 >=3 的有向环（节点路径, 交易路径）。"""
        visited = {start}

        def dfs(node: str, node_path: list[str], edge_path: list[str]
                ) -> tuple[list[str], list[str]] | None:
            if len(edge_path) >= MAX_RING_LEN:
                return None
            for nxt, tx_id in edges.get(node, ()):
                if nxt == start:
                    if len(edge_path) + 1 >= 3:
                        return node_path + [start], edge_path + [tx_id]
                    continue
                if nxt in visited:
                    continue
                visited.add(nxt)
                found = dfs(nxt, node_path + [nxt], edge_path + [tx_id])
                visited.discard(nxt)
                if found:
                    return found
            return None

        return dfs(start, [start], [])

    # ------------------------------------------------------------------ #
    # 因子 5：高风险地址关联（直接 / 间接一跳）
    # ------------------------------------------------------------------ #
    def _factor_high_risk_link(self, addr: str, snap: LedgerSnapshot) -> FactorEvidence:
        if not snap.transactions_for(addr):
            return _ev_insufficient(
                FactorCode.HIGH_RISK_LINK,
                "关联样本不足：目标钱包没有任何交易，无法判断与高风险地址的关联",
            )
        # 有向资金流：out[a] 中保存 a 直接转出资金到达的地址与交易
        out: dict[str, list[tuple[str, str]]] = {}
        inn: dict[str, list[tuple[str, str]]] = {}
        for t in snap.transactions:
            out.setdefault(t.sender, []).append((t.receiver, t.tx_id))
            inn.setdefault(t.receiver, []).append((t.sender, t.tx_id))
        for table in (out, inn):
            for k in table:
                table[k] = sorted(set(table[k]))

        def is_bad(a: str) -> bool:
            w = snap.wallets.get(a)
            return bool(w and w.high_risk)

        # 直连：任一方向发生过直接交易
        direct = sorted(
            [(c, tx, "OUT") for c, tx in out.get(addr, ()) if is_bad(c)]
            + [(c, tx, "IN") for c, tx in inn.get(addr, ()) if is_bad(c)]
        )
        # 间接：两跳资金路径 addr -> M -> BAD 或 BAD -> M -> addr（污资流向）
        indirect: list[tuple[str, str, str, str]] = []
        if not direct:
            for mid, tx1 in out.get(addr, ()):
                if not is_bad(mid):
                    for far, tx2 in out.get(mid, ()):
                        if is_bad(far):
                            indirect.append((mid, far, tx1, tx2))
            for mid, tx1 in inn.get(addr, ()):
                if not is_bad(mid):
                    for far, tx2 in inn.get(mid, ()):
                        if is_bad(far):
                            indirect.append((mid, far, tx2, tx1))
        indirect = sorted(set(indirect))

        data: dict = {
            "direct_links": [{"address": c, "tx_id": tx, "direction": direction,
                              "risk_tags": list(snap.wallets[c].risk_tags)}
                             for c, tx, direction in direct],
            "indirect_links": [{"intermediary": m, "high_risk_address": f,
                                "tx_ids": [t1, t2]} for m, f, t1, t2 in indirect],
        }
        if direct:
            parts = []
            for c, tx, direction in direct:
                arrow = f"{addr}->{c}" if direction == "OUT" else f"{c}->{addr}"
                parts.append(f"{c}({snap.wallets[c].label})[{arrow} tx={tx}]")
            why = f"与 {len(direct)} 个已知高风险地址直接交互：" + ", ".join(parts)
            return FactorEvidence(FactorCode.HIGH_RISK_LINK, True, W_LINK_DIRECT, why, data)
        if indirect:
            sample = indirect[0]
            why = (f"经一跳中介 {sample[0]} 与高风险地址 {sample[1]} 存在两跳资金路径"
                   f"（{addr}->{sample[0]}->{sample[1]}，交易 {sample[2]}, {sample[3]}）")
            return FactorEvidence(FactorCode.HIGH_RISK_LINK, True, W_LINK_INDIRECT, why, data)
        return FactorEvidence(
            FactorCode.HIGH_RISK_LINK, False, 0.0,
            "直接交易对手与一跳资金路径中均未发现已知高风险地址", data,
        )
