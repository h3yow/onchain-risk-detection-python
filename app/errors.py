"""可区分的业务错误类型。"""
from __future__ import annotations


class IngestError(Exception):
    """摄入失败的基类。"""

    code = "INGEST_ERROR"


class DuplicateRecordError(IngestError):
    """同一标识的记录已存在且内容完全一致（幂等命中）。"""

    code = "DUPLICATE_RECORD"


class ContentConflictError(IngestError):
    """同一标识的记录已存在，但关键字段不一致。"""

    code = "CONTENT_CONFLICT"

    def __init__(self, entity_id: str, field_name: str, existing: object, incoming: object):
        self.entity_id = entity_id
        self.field_name = field_name
        self.existing = existing
        self.incoming = incoming
        super().__init__(
            f"记录 {entity_id} 字段 {field_name} 冲突: 已存在={existing!r}, 新提交={incoming!r}"
        )


class InvalidTransactionError(IngestError):
    """交易数据不合法（引用未知钱包、金额非法等）。"""

    code = "INVALID_TRANSACTION"


class LedgerNotFoundError(Exception):
    """查询的钱包或评分对象不存在。"""

    code = "NOT_FOUND"


class InsufficientDataError(Exception):
    """数据不足，无法给出风险结论（不得默认判安全）。"""

    code = "INSUFFICIENT_DATA"
