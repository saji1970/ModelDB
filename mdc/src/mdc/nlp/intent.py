"""StorageIntent (CLAUDE-STORAGE.md section 37).

A separate vocabulary from `mce.context.Intent` (the merchants-CRUD
intent model), on purpose: both domains use the same surface words for
different things ("show" means "run an analytics query" for merchants,
"look up a stored object" here), so sharing one classifier/enum risked
exactly the kind of silent cross-domain misrouting section 3's "LLM
proposes, deterministic software decides" principle exists to prevent.
`nlp.command.parse_storage_command` is what actually assigns one of
these labels, from anchored sentence patterns - not a bag-of-keywords
score - specifically so it never fires on a merchants query that merely
happens to share a word.
"""

from __future__ import annotations

from enum import Enum


class StorageIntent(str, Enum):
    STORE = "STORE"
    RETRIEVE = "RETRIEVE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    SEARCH = "SEARCH"
    ARCHIVE = "ARCHIVE"
    RESTORE = "RESTORE"
    OPTIMIZE = "OPTIMIZE"
    MOVE = "MOVE"
    INSPECT = "INSPECT"
    DESCRIBE = "DESCRIBE"
