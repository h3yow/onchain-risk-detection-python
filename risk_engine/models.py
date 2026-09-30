"""领域类型定义：钱包、交易、摄入结果、风险因子与评分结论。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class WalletType(str, Enum):
    NORMAL = "NORMAL"
    HIGH_RISK = "HIGH_RISK"  # 已知高风险地址


class TxDirection(str, Enum):
    OUTGOING = "OUTGOING"
    INCOMING = "INCOMING"


class FactorCode(str, Enum):
    """可解释风险因子代码。"""
    FUND_CONVERGENCE = "FUND_CONVERGENCE"        # 资金汇聚
    FUND_DISPERSION = "FUND_DISPERSION"          # 资金分散
    RING_FLOW = "RING_FLOW"                      # 环形流转
    HIGH_FREQ_INTERACTION = "HIGH_FREQ_INTERACTION"  # 短时高频交互
    HIGH_RISK_DIRECT = "HIGH_RISK_DIRECT"        # 与高风险地址直接关联
    HIGH_RISK_INDIRECT = "HIGH_RISK_INDIRECT"    # 与高风险地址间接关联


@dataclass(frozen=True)
class Wallet:
    address: str
    wallet_type: WalletType = WalletType.NORMAL


@dataclass(frozen=True)
class Transaction:
    """一笔链上转账。

    block_height/block_index 决定全序；同一 tx_id 必须始终携带相同内容。
    """
    tx_id: str
    from_addr: str
    to_addr: str
    amount: float
    block_height: int
    block_index: int = 0
    timestamp: float = 0.0


@dataclass(frozen=True)
class IngestResult:
    tx_id: str
    accepted: bool
    duplicate: bool          # 幂等命中：内容一致的重复提交
    reason: str | None = None


class EvidenceStatus(str, Enum):
    """单个风险维度的证据充分程度。

    - SUFFICIENT   证据足够，本维度给出了明确的触发/未触发判断；
    - INSUFFICIENT 有相关数据，但样本不足以支撑结论（样本方向单一、
                   对手方缺失、时间跨度/窗口样本不够等），detail 说明缺什么；
    - NO_DATA      完全没有可用于该维度的数据（如该维度方向上零笔交互）。
    """
    SUFFICIENT = "SUFFICIENT"
    INSUFFICIENT = "INSUFFICIENT"
    NO_DATA = "NO_DATA"


@dataclass(frozen=True)
class FactorEvidence:
    code: FactorCode
    triggered: bool
    weight: float            # 该因子贡献分值（0 表示未触发）
    detail: str              # 人可读的判定依据
    related: tuple[str, ...] = ()
    # 新增：维度级证据状态。只有 SUFFICIENT 时 triggered 才有判定意义；
    # INSUFFICIENT/NO_DATA 时 triggered 恒为 False（证据不足不得当作“查过没问题”）。
    status: EvidenceStatus = EvidenceStatus.SUFFICIENT
    insufficiency_code: str | None = None  # 非 SUFFICIENT 时的机读原因码
    insufficiency_detail: str | None = None  # 非 SUFFICIENT 时“缺什么”的说明


@dataclass(frozen=True)
class Insufficiency:
    """数据不足/无法判定的原因。

    factor 非空时表示某一维度级别的缺证据原因；为空时表示整体级别的原因
    （如参与交易过少、可评估维度覆盖率不足、账本为空）。
    """
    code: str
    detail: str
    factor: FactorCode | None = None


class ConfidenceLevel(str, Enum):
    """整体结论可信度，由可评估维度的覆盖率决定。"""
    HIGH = "HIGH"      # 覆盖率 >= 高门槛：结论由多数维度支撑
    MEDIUM = "MEDIUM"  # 覆盖率达到可判定门槛但未到高门槛
    LOW = "LOW"        # 覆盖率低于可判定门槛：结论只能是 UNKNOWN


@dataclass(frozen=True)
class CompletenessSummary:
    """本次评估的完整度汇总，供人工复核一眼判断结论由多少维度支撑。"""
    total_dimensions: int
    evaluated_dimensions: int                          # status=SUFFICIENT 的维度数
    pending_dimensions: int                            # status=INSUFFICIENT 的维度数
    missing_dimensions: int                            # status=NO_DATA 的维度数
    evaluated: tuple[FactorCode, ...]                  # 已评上的维度
    not_evaluated: tuple[FactorCode, ...]              # 没评上的维度（不足+无数据）
    coverage_ratio: float                              # evaluated / total，0~1
    confidence: ConfidenceLevel                        # 整体可信度
    sufficient_for_grade: bool                         # 覆盖是否达到分级门槛


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"  # 数据不足，不得判为安全


@dataclass(frozen=True)
class RiskScore:
    address: str
    score: float
    level: RiskLevel
    factors: tuple[FactorEvidence, ...]
    insufficient_reasons: tuple[Insufficiency, ...]
    evaluated_at_block: int
    determinism_key: str  # 输入指纹，保证同输入同结论
    # 新增：完整度汇总（旧消费方可忽略；评分入口恒会填充）
    completeness: CompletenessSummary | None = None


@dataclass(frozen=True)
class LedgerView:
    """一致性查询快照：某一时刻账本的不可变视图。"""
    balances: dict[str, float] = field(default_factory=dict)
    nonces: dict[str, int] = field(default_factory=dict)
    last_block: int = 0
    tx_count: int = 0
