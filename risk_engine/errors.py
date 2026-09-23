"""摄入与评估过程中的可区分错误类型。

错误归类约定：
- ConflictError        409  同一 tx_id 内容不一致
- WalletConflictError  409  同一钱包地址类型注册不一致
- ValidationError      422  输入字段非法
- EvaluationError      400  评估无法完成
"""
from __future__ import annotations


class RiskEngineError(Exception):
    """所有引擎错误的基类。"""


class ConflictError(RiskEngineError):
    """同一交易标识 tx_id 的重复提交携带了不一致内容。"""

    def __init__(self, tx_id: str, field_name: str, existing: object, incoming: object):
        self.tx_id = tx_id
        self.field_name = field_name
        self.existing = existing
        self.incoming = incoming
        super().__init__(
            f"交易 {tx_id} 冲突：字段 {field_name} 已存在值 {existing!r}，"
            f"提交值 {incoming!r} 不一致"
        )


class WalletConflictError(RiskEngineError):
    """同一钱包地址注册了不同钱包类型。"""

    def __init__(self, address: str, field_name: str, existing: object, incoming: object):
        self.address = address
        self.field_name = field_name
        self.existing = existing
        self.incoming = incoming
        super().__init__(
            f"钱包 {address} 冲突：字段 {field_name} 已存在值 {existing!r}，"
            f"提交值 {incoming!r} 不一致"
        )


class ValidationError(RiskEngineError):
    """输入字段非法（缺字段、金额非正、区块号为负等）。"""


class EvaluationError(RiskEngineError):
    """风险评估无法完成。"""
