"""Natural-language -> StorageCommand (CLAUDE-STORAGE.md sections
36-37: conversational storage operations).

Every pattern is anchored to the *whole* input (`match` + `$`), not a
substring/keyword score - deliberately, so this never fires on
merchants-CRUD text that happens to share a word ("show", "find") with
a storage command. But anchoring alone isn't enough: "delete
merchant ABC Store" syntactically matches a generic "delete <text>"
shape just as well as "delete AIM-1234567890" does. The real
disambiguator is `_looks_like_reference` - an object reference must
either look like one of our generated ids (`PREFIX-HEXDIGITS`) or be a
recognized pronoun ("it", "this model", ...) resolved via
conversational state. "merchant ABC Store" is neither, so that match
is *rejected* and parsing falls through to try other patterns -
eventually returning `None`, which callers treat as "not a storage
command" (the CLI shell then falls through to the merchants
interpreter; the HTTP chat endpoint, which has no merchants fallback,
turns `None` into a help message). This is also just honest: free-text
descriptive references ("the old model") aren't resolvable without a
name index this system doesn't have, so requiring an id-shaped token
isn't a capability loss on top of a working feature - it's the actual
capability.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mdc.classification.data_type import DataType
from mdc.nlp.intent import StorageIntent
from mdc.storage_intelligence.strategy import StorageTier

_TIER_WORDS = {"hot": StorageTier.HOT, "warm": StorageTier.WARM, "cold": StorageTier.COLD, "archive": StorageTier.ARCHIVE}

# The canonical type-word vocabulary for "list/show/find <type>" -
# deliberately NOT every DataType (no "database_record"/"unknown"/
# "time_series"/"archive" word forms - "archive" in particular would
# collide with the ARCHIVE tier/intent). An unrecognized word here (e.g.
# "merchants") must make the whole pattern fail closed, not match with a
# type_word nothing downstream understands - that's what stops
# "Show all merchants" from ever being treated as a storage command.
TYPE_WORDS: dict[str, DataType] = {
    "model": DataType.AI_MODEL,
    "image": DataType.IMAGE,
    "document": DataType.DOCUMENT,
    "video": DataType.VIDEO,
    "audio": DataType.AUDIO,
    "log": DataType.LOG,
    "tensor": DataType.TENSOR,
    "tabular": DataType.TABULAR,
    "text": DataType.TEXT,
    "binary": DataType.BINARY,
}

_OBJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.]*-[A-Za-z0-9_.-]+$")
PRONOUNS = {
    "it", "this", "that", "this one", "the current object",
    "this model", "the model", "the current model",
    "this document", "the document", "this file", "the file",
    "this image", "the image", "this tensor", "the tensor",
}


def _looks_like_reference(ref: str) -> bool:
    ref = ref.strip()
    return bool(_OBJECT_ID_RE.match(ref)) or ref.lower() in PRONOUNS


@dataclass(frozen=True)
class StorageCommand:
    intent: StorageIntent
    object_ref: str | None = None
    tensor_name: str | None = None
    tier: StorageTier | None = None
    search_term: str | None = None
    type_word: str | None = None
    path: str | None = None
    preference_text: str = ""
    raw_text: str = ""


def _store(m: re.Match, raw: str) -> StorageCommand | None:
    return StorageCommand(StorageIntent.STORE, path=m.group(1), preference_text=m.group(2) or "", raw_text=raw)


def _retrieve_tensor(m: re.Match, raw: str) -> StorageCommand | None:
    tensor_name, model_ref = m.group(1), m.group(2)
    if not _looks_like_reference(model_ref):
        return None
    return StorageCommand(StorageIntent.RETRIEVE, object_ref=model_ref, tensor_name=tensor_name, raw_text=raw)


def _retrieve_layer(m: re.Match, raw: str) -> StorageCommand | None:
    layer_no, model_ref = m.group(1), m.group(2)
    if not _looks_like_reference(model_ref):
        return None
    return StorageCommand(StorageIntent.RETRIEVE, object_ref=model_ref, tensor_name=f"layer_{layer_no}", raw_text=raw)


def _retrieve(m: re.Match, raw: str) -> StorageCommand | None:
    ref = m.group(1)
    if not _looks_like_reference(ref):
        return None
    return StorageCommand(StorageIntent.RETRIEVE, object_ref=ref, raw_text=raw)


def _list_tier(m: re.Match, raw: str) -> StorageCommand | None:
    tier = _TIER_WORDS.get(m.group(1).lower())
    if tier is None:
        return None
    return StorageCommand(StorageIntent.SEARCH, tier=tier, raw_text=raw)


def _archive(m: re.Match, raw: str) -> StorageCommand | None:
    ref = m.group(1)
    if not _looks_like_reference(ref):
        return None
    return StorageCommand(StorageIntent.ARCHIVE, object_ref=ref, raw_text=raw)


def _restore(m: re.Match, raw: str) -> StorageCommand | None:
    ref = m.group(1)
    if not _looks_like_reference(ref):
        return None
    return StorageCommand(StorageIntent.RESTORE, object_ref=ref, raw_text=raw)


def _optimize_make(m: re.Match, raw: str) -> StorageCommand | None:
    ref = m.group(1)
    if not _looks_like_reference(ref):
        return None
    return StorageCommand(StorageIntent.OPTIMIZE, object_ref=ref, preference_text=m.group(2), raw_text=raw)


def _optimize(m: re.Match, raw: str) -> StorageCommand | None:
    ref = m.group(1)
    if not _looks_like_reference(ref):
        return None
    preference_text = m.group(2) or ""
    return StorageCommand(StorageIntent.OPTIMIZE, object_ref=ref, preference_text=preference_text, raw_text=raw)


def _compress(m: re.Match, raw: str) -> StorageCommand | None:
    ref = m.group(1)
    if not _looks_like_reference(ref):
        return None
    return StorageCommand(StorageIntent.OPTIMIZE, object_ref=ref, preference_text="compact", raw_text=raw)


def _move(m: re.Match, raw: str) -> StorageCommand | None:
    ref = m.group(1)
    if not _looks_like_reference(ref):
        return None
    return StorageCommand(StorageIntent.MOVE, object_ref=ref, tier=_TIER_WORDS[m.group(2).lower()], raw_text=raw)


def _update(m: re.Match, raw: str) -> StorageCommand | None:
    ref, path = m.group(1), m.group(2)
    if not _looks_like_reference(ref):
        return None
    return StorageCommand(StorageIntent.UPDATE, object_ref=ref, path=path, raw_text=raw)


def _delete(m: re.Match, raw: str) -> StorageCommand | None:
    ref = m.group(1)
    if not _looks_like_reference(ref):
        return None
    return StorageCommand(StorageIntent.DELETE, object_ref=ref, raw_text=raw)


def _search_text(m: re.Match, raw: str) -> StorageCommand | None:
    return StorageCommand(StorageIntent.SEARCH, search_term=m.group(1).strip(), raw_text=raw)


def _list_type(m: re.Match, raw: str) -> StorageCommand | None:
    word = m.group(1).lower()
    if word not in TYPE_WORDS:
        return None
    return StorageCommand(StorageIntent.SEARCH, type_word=word, raw_text=raw)


def _inspect(m: re.Match, raw: str) -> StorageCommand | None:
    ref = m.group(1)
    if not _looks_like_reference(ref):
        return None
    return StorageCommand(StorageIntent.INSPECT, object_ref=ref, raw_text=raw)


def _describe_where(m: re.Match, raw: str) -> StorageCommand | None:
    ref = m.group(1)
    if not _looks_like_reference(ref):
        return None
    return StorageCommand(StorageIntent.DESCRIBE, object_ref=ref, raw_text=raw)


def _describe(m: re.Match, raw: str) -> StorageCommand | None:
    ref = m.group(1)
    if not _looks_like_reference(ref):
        return None
    return StorageCommand(StorageIntent.DESCRIBE, object_ref=ref, raw_text=raw)


# Order matters: more specific patterns (tensor/layer retrieval, "make X
# as Y as possible") are tried before their more general siblings, and
# the bare-type-word list pattern is tried before the generic "show/get
# <id>" pattern so a real type word ("images") doesn't get treated as a
# malformed object reference and rejected.
_PATTERNS: list[tuple[re.Pattern[str], object]] = [
    (re.compile(r"^store\s+(\S+)(?:\s+(.*))?$", re.IGNORECASE), _store),
    (re.compile(r"^retrieve\s+(?:tensor\s+)?([\w.]+)\s+from\s+(?:model\s+)?(\S+)\s*$", re.IGNORECASE), _retrieve_tensor),
    (re.compile(r"^retrieve\s+layer\s+(\d+)\s+from\s+(?:model\s+)?(\S+)\s*$", re.IGNORECASE), _retrieve_layer),
    (re.compile(r"^(?:retrieve|read|open|cat)\s+(\S+)\s*$", re.IGNORECASE), _retrieve),
    (re.compile(r"^archive\s+(?:the\s+)?(.+?)\s*$", re.IGNORECASE), _archive),
    (re.compile(r"^(?:restore|unarchive|bring\s+back)\s+(?:the\s+)?(.+?)\s*$", re.IGNORECASE), _restore),
    (re.compile(r"^make\s+(.+?)\s+as\s+(.+?)\s+as\s+possible\s*$", re.IGNORECASE), _optimize_make),
    (re.compile(r"^optimize\s+(?:the\s+storage\s+(?:of|for)\s+)?(.+?)(?:\s+(?:for|to\s+be)\s+(.+))?\s*$", re.IGNORECASE), _optimize),
    (re.compile(r"^compress\s+(?:the\s+)?(.+?)\s*$", re.IGNORECASE), _compress),
    (re.compile(r"^move\s+(\S+)\s+to\s+(hot|warm|cold|archive)\s*$", re.IGNORECASE), _move),
    (re.compile(r"^(?:update|replace)\s+(\S+)\s+with\s+(\S+)\s*$", re.IGNORECASE), _update),
    (re.compile(r"^delete\s+(?:the\s+)?(.+?)\s*$", re.IGNORECASE), _delete),
    (re.compile(r"^(?:search(?:\s+for)?|find\s+text)\s+(.+?)\s*$", re.IGNORECASE), _search_text),
    # Tried before the bare-type-word list pattern: "show objects in
    # archive" would otherwise never reach it anyway (multi-word body),
    # but keeping the tier check first mirrors how specific-before-
    # general is ordered throughout this list.
    (re.compile(r"^(?:list|show|find)\s+(?:all\s+)?(?:objects?\s+)?in\s+(hot|warm|cold|archive)\b.*$", re.IGNORECASE), _list_tier),
    (re.compile(r"^(?:list|show|find)\s+(?:all\s+|me\s+)?(\w+?)s?\s*$", re.IGNORECASE), _list_type),
    (re.compile(r"^(?:show\s+me\s+where|where\s+is|where's)\s+(?:the\s+)?(.+?)\s+(?:is\s+)?stored\??\s*$", re.IGNORECASE), _describe_where),
    (re.compile(r"^(?:describe|explain|why\s+is)\s+(?:the\s+)?(.+?)\s*$", re.IGNORECASE), _describe),
    (re.compile(r"^inspect\s+(?:the\s+)?(.+?)\s*$", re.IGNORECASE), _inspect),
    (re.compile(r"^(?:show|get)\s+(?:me\s+)?(\S+)\s*$", re.IGNORECASE), _inspect),
]


def parse_storage_command(text: str) -> StorageCommand | None:
    stripped = text.strip()
    if not stripped:
        return None
    for pattern, builder in _PATTERNS:
        match = pattern.match(stripped)
        if not match:
            continue
        command = builder(match, stripped)
        if command is not None:
            return command
    return None
