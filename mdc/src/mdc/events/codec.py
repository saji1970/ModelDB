"""How a change of state is recorded, and replayed.

A `StateCodec` is the part that decides *how* an agent's state changes are
written down - `diff(old, new)` turns a change into an event payload, and
`apply(state, payload)` replays one. The store itself treats payloads as
opaque sealed JSON; only the codec reads them. That is the seam an AI
model's own state encoding plugs into (a model that wants to record
"decisions" or compacted summaries instead of key-by-key edits supplies a
codec that does so) - while `apply` stays deterministic code, so a state
is always rebuilt by running the log, never by asking a model to remember.

The REST API and chat only ever use the built-in `PathOpsCodec`: a codec
is Python code, and this server does not load code from a request.
"""

from __future__ import annotations

import copy
import json
from abc import ABC, abstractmethod
from typing import Any

from mdc.events.errors import InvalidEventError

_MAX_PATH_DEPTH = 32


class StateCodec(ABC):
    name: str

    @abstractmethod
    def diff(self, old: dict[str, Any], new: dict[str, Any]) -> Any:
        """The event payload that turns `old` into `new` (JSON-serializable)."""

    @abstractmethod
    def apply(self, state: dict[str, Any], payload: Any) -> dict[str, Any]:
        """A NEW state with `payload` applied. Must not mutate `state` and
        must be deterministic: replay has to give the same answer every time."""

    def validate(self, payload: Any) -> None:
        """Raise InvalidEventError if `payload` isn't something `apply` accepts."""


def same_json(a: Any, b: Any) -> bool:
    """JSON equality that does not confuse 1, 1.0 and true."""
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


class PathOpsCodec(StateCodec):
    """Payload: a list of `{"op": "set", "path": ["a", "b"], "value": ...}` and
    `{"op": "unset", "path": ["a", "b"]}`. Nested objects are edited key by
    key; anything that is not an object (a list, a number, a string) is
    replaced whole. Unlike a JSON merge patch, a value may be set to null."""

    name = "path-ops"

    def diff(self, old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
        ops: list[dict[str, Any]] = []
        self._diff(old, new, [], ops)
        return ops

    def _diff(self, old: dict[str, Any], new: dict[str, Any], path: list[str], ops: list[dict[str, Any]]) -> None:
        for key, value in new.items():
            if key not in old:
                ops.append({"op": "set", "path": [*path, key], "value": value})
            elif isinstance(old[key], dict) and isinstance(value, dict):
                self._diff(old[key], value, [*path, key], ops)
            elif not same_json(old[key], value):
                ops.append({"op": "set", "path": [*path, key], "value": value})
        for key in old:
            if key not in new:
                ops.append({"op": "unset", "path": [*path, key]})

    def validate(self, payload: Any) -> None:
        if not isinstance(payload, list):
            raise InvalidEventError('A path-ops payload is a list of {"op": "set"|"unset", "path": [...]} operations.')
        for n, op in enumerate(payload, start=1):
            where = f"operation {n}"
            if not isinstance(op, dict) or op.get("op") not in ("set", "unset"):
                raise InvalidEventError(f'{where}: "op" must be "set" or "unset".')
            path = op.get("path")
            if not isinstance(path, list) or not path or not all(isinstance(p, str) and p for p in path):
                raise InvalidEventError(f'{where}: "path" must be a non-empty list of non-empty strings.')
            if len(path) > _MAX_PATH_DEPTH:
                raise InvalidEventError(f"{where}: path is deeper than {_MAX_PATH_DEPTH} levels.")
            if op["op"] == "set" and "value" not in op:
                raise InvalidEventError(f'{where}: "set" needs a "value".')

    def apply(self, state: dict[str, Any], payload: Any) -> dict[str, Any]:
        self.validate(payload)
        result = copy.deepcopy(state)
        for op in payload:
            *parents, leaf = op["path"]
            node = result
            for key in parents:
                child = node.get(key)
                if child is None and op["op"] == "unset":
                    break
                if child is None:
                    child = node[key] = {}
                if not isinstance(child, dict):
                    raise InvalidEventError(f'"{".".join(op["path"])}" goes through "{key}", which is not an object.')
                node = child
            else:
                if op["op"] == "set":
                    node[leaf] = copy.deepcopy(op["value"])
                else:
                    node.pop(leaf, None)
        return result
