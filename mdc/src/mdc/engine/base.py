"""DataEngine interface (CLAUDE.md section 4, 28).

Every caller - CLI, a future REST API, a future SDK - is required to
go through this interface rather than touching a storage backend or
writing SQL directly (section 28: "the exact same Data Engine used by
the CLI must execute these operations").
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from mdc.model.operation import (
    CountOperation,
    CreateOperation,
    DeleteOperation,
    MDCOperation,
    ReadOperation,
    UpdateOperation,
)
from mdc.model.record import Record


@dataclass
class OperationResult:
    kind: str
    collection: str
    records: list[Record] = field(default_factory=list)
    count: int = 0


class DataEngine(ABC):
    @abstractmethod
    def create(self, operation: CreateOperation) -> OperationResult: ...

    @abstractmethod
    def read(self, operation: ReadOperation) -> OperationResult: ...

    @abstractmethod
    def update(self, operation: UpdateOperation) -> OperationResult: ...

    @abstractmethod
    def delete(self, operation: DeleteOperation) -> OperationResult: ...

    @abstractmethod
    def count(self, operation: CountOperation) -> OperationResult: ...

    def execute(self, operation: MDCOperation) -> OperationResult:
        """Dispatch by operation kind - the single entry point every
        caller (CLI, API, SDK) should actually use (section 28)."""
        handlers = {
            "CREATE": self.create,
            "READ": self.read,
            "UPDATE": self.update,
            "DELETE": self.delete,
            "COUNT": self.count,
        }
        handler = handlers.get(operation.kind)  # type: ignore[attr-defined]
        if handler is None:
            raise ValueError(f"Unsupported operation kind: {operation.kind!r}")  # type: ignore[attr-defined]
        return handler(operation)
