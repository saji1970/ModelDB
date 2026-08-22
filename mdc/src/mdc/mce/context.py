"""QueryContext and Condition models (CLAUDE.md sections 20-21)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Operator = Literal[
    "=", "!=", ">", "<", ">=", "<=",
    "IN", "NOT IN", "BETWEEN", "LIKE", "IS NULL", "IS NOT NULL",
]

Intent = Literal[
    "FETCH", "COUNT", "SUM", "AVG", "MIN", "MAX", "SORT", "HELP", "EXPLAIN",
    "CREATE", "UPDATE", "DELETE",
]


class Condition(BaseModel):
    field: str
    operator: Operator
    value: Any = None
    datatype: str
    confidence: float = 1.0
    source: str = "user_input"


class OrderBy(BaseModel):
    field: str
    direction: Literal["ASC", "DESC"] = "DESC"


class QueryContext(BaseModel):
    session_id: str
    intent: Intent | None = None
    entity: str | None = None
    fields: list[str] = Field(default_factory=list)
    conditions: list[Condition] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    order_by: list[OrderBy] = Field(default_factory=list)
    limit: int | None = None
    temporal_context: dict[str, Any] | None = None
    geographic_context: dict[str, Any] | None = None
    confidence: float = 0.0
    complete: bool = False
    clarification_required: bool = False
