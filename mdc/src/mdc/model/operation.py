"""The MDC Operation AST (CLAUDE.md sections 14-15).

This is what the conversational layer, a future REST API, and a future
SDK all converge on producing - and the *only* thing `MDCDataEngine`
accepts (section 28: "the exact same Data Engine used by the CLI must
execute these operations"). Nothing upstream of an `MDCOperation`
(NLP, CLI parsing) is trusted to touch storage directly; nothing
downstream of it (the engine, storage backends) knows or cares that
some of these operations were ever expressed in English.

`UpdateOperation` and `DeleteOperation` require at least one filter -
enforced here, not left to caller discipline - so a mistranslated
natural-language command can narrow scope to nothing but can never
default to "match everything" (section 13, financial safety).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

FilterOperator = Literal[
    "=", "!=", ">", "<", ">=", "<=",
    "IN", "NOT IN", "BETWEEN", "LIKE", "IS NULL", "IS NOT NULL",
]


class Filter(BaseModel):
    field: str
    operator: FilterOperator
    value: Any = None


class Sort(BaseModel):
    field: str
    direction: Literal["ASC", "DESC"] = "DESC"


class MDCOperation(BaseModel):
    collection: str


class CreateOperation(MDCOperation):
    kind: Literal["CREATE"] = "CREATE"
    data: dict[str, Any]


class ReadOperation(MDCOperation):
    kind: Literal["READ"] = "READ"
    filters: list[Filter] = Field(default_factory=list)
    sort: list[Sort] = Field(default_factory=list)
    limit: int | None = None


class UpdateOperation(MDCOperation):
    kind: Literal["UPDATE"] = "UPDATE"
    filters: list[Filter]
    data: dict[str, Any]

    @field_validator("filters")
    @classmethod
    def _require_at_least_one_filter(cls, filters: list[Filter]) -> list[Filter]:
        if not filters:
            raise ValueError("UpdateOperation requires at least one filter - refusing to update an entire collection")
        return filters


class DeleteOperation(MDCOperation):
    kind: Literal["DELETE"] = "DELETE"
    filters: list[Filter]

    @field_validator("filters")
    @classmethod
    def _require_at_least_one_filter(cls, filters: list[Filter]) -> list[Filter]:
        if not filters:
            raise ValueError("DeleteOperation requires at least one filter - refusing to delete an entire collection")
        return filters


class CountOperation(MDCOperation):
    kind: Literal["COUNT"] = "COUNT"
    filters: list[Filter] = Field(default_factory=list)
