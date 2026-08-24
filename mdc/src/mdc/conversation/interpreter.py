"""Conversational storage turn orchestration (CLAUDE-STORAGE.md
sections 36-40) - the real NLP integration for the polymorphic object
system, as opposed to `api.chat`'s original stateless slice (which now
delegates here; see its module docstring).

`process_turn` is the single place STORE/RETRIEVE/ARCHIVE/RESTORE/
OPTIMIZE/MOVE/DELETE/SEARCH/INSPECT/DESCRIBE actually execute, plus
database/table administration (`conversation.db_interpreter`, tried
first - see its module docstring for why) - callers (the CLI shell,
the HTTP chat endpoint) just supply text and get back a message. Every
turn resolves `state.current_database` through a `DatabaseManager`
fresh, so switching databases (`create database`/`use database`)
immediately redirects every subsequent object-storage command without
callers needing to do anything - the merchants-CRUD interpreter
(`cql.interpreter`) is a deliberate exception and always stays on the
original default database (see `cli.shell`'s module docstring for why
the two conversations don't share this).

`read_file` is deliberately optional and CLI-only: letting a remote
HTTP caller ask this process to read an arbitrary local path would be
a path-traversal-shaped hole, so the HTTP-facing caller never wires
it, and STORE-by-path there degrades to a clear "use the Upload
button" message instead of silently trying (or silently failing) a
filesystem read for someone who never should have gotten one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from mdc.api.service import ObjectService
from mdc.classification.data_type import DataType
from mdc.conversation.db_interpreter import handle_database_command
from mdc.conversation.result import TurnResult
from mdc.conversation.state import StorageConversationState
from mdc.databases.manager import DatabaseManager
from mdc.models.errors import ModelNotFoundError, TensorNotFoundError
from mdc.nlp.command import PRONOUNS, TYPE_WORDS, StorageCommand, parse_storage_command
from mdc.nlp.db_command import parse_database_command
from mdc.nlp.intent import StorageIntent
from mdc.nlp.preference import OptimizationPreference, extract_preference
from mdc.storage_intelligence.analyzer import AccessProfile
from mdc.storage_intelligence.policy import DEFAULT_POLICY
from mdc.storage_intelligence.router import DataIntegrityError, ObjectNotFoundError, ObjectUnavailableError
from mdc.storage_intelligence.strategy import StorageTier

ReadFile = Callable[[Path], bytes]
_TEXTLIKE_TYPES = {"TEXT", "DOCUMENT", "LOG", "TABULAR", "DATABASE_RECORD", "TIME_SERIES"}

__all__ = ["TurnResult", "process_turn"]


def process_turn(
    state: "StorageConversationState", text: str, manager: "DatabaseManager", *, read_file: ReadFile | None = None
) -> TurnResult | None:
    stripped = text.strip()
    service = manager.get(state.current_database).object_service

    if state.pending_delete is not None:
        return _resolve_delete_confirmation(state, stripped, service)

    db_command = parse_database_command(stripped)
    if db_command is not None:
        return handle_database_command(state, manager, db_command)

    command = parse_storage_command(stripped)
    if command is None:
        return None

    handler = _HANDLERS[command.intent]
    return handler(state, service, command, read_file)


def _resolve(ref: str, state: "StorageConversationState") -> str | None:
    if ref.strip().lower() in PRONOUNS:
        return state.last_object_id
    return ref.strip()


def _resolve_delete_confirmation(state: "StorageConversationState", answer: str, service: "ObjectService") -> TurnResult:
    target = state.pending_delete
    state.pending_delete = None
    assert target is not None
    if answer.lower() not in ("yes", "y", "confirm"):
        return TurnResult("Cancelled - nothing was deleted.")
    try:
        service.delete(target)
    except ObjectNotFoundError:
        return TurnResult(f'"{target}" was already gone.')
    if state.last_object_id == target:
        state.last_object_id = None
    return TurnResult(f'Deleted "{target}".')


def _handle_store(state, service, command: StorageCommand, read_file: ReadFile | None) -> TurnResult:
    if read_file is None:
        return TurnResult("Storing by file path isn't available here - use the Upload button.")
    path = Path(command.path)
    try:
        content = read_file(path)
    except OSError as exc:
        return TurnResult(f"Couldn't read {path}: {exc}")

    preference = extract_preference(command.preference_text)
    result = service.upload(content, filename=path.name, access=_access_from_preference(preference))
    state.last_object_id = result["object_id"]
    note = _preference_note(preference)
    return TurnResult(f'Stored "{path.name}" as {result["object_id"]} ({result["type"]}).{note}', data=result)


def _handle_update(state, service, command: StorageCommand, read_file: ReadFile | None) -> TurnResult:
    if read_file is None:
        return TurnResult("Replacing content by file path isn't available here - use the Upload button.")
    object_id = _resolve(command.object_ref, state)
    if object_id is None:
        return TurnResult("Update which object? Give me an object id.")
    path = Path(command.path)
    try:
        content = read_file(path)
    except OSError as exc:
        return TurnResult(f"Couldn't read {path}: {exc}")
    result = service.replace(object_id, content, filename=path.name)
    state.last_object_id = result["object_id"]
    return TurnResult(f'Replaced "{object_id}" with "{path.name}" ({result["type"]}).', data=result)


def _handle_retrieve(state, service, command: StorageCommand, read_file: ReadFile | None) -> TurnResult:
    if command.tensor_name:
        model_ref = _resolve(command.object_ref, state) if command.object_ref else state.last_object_id
        if model_ref is None:
            return TurnResult("Retrieve a tensor from which model? Give me a model id.")
        try:
            data = service.model_store.retrieve_tensor(model_ref, command.tensor_name)
        except (ModelNotFoundError, TensorNotFoundError) as exc:
            return TurnResult(str(exc))
        state.last_object_id = model_ref
        return TurnResult(
            f'Retrieved "{command.tensor_name}" from {model_ref}: {len(data)} bytes.',
            data={"model_id": model_ref, "tensor_name": command.tensor_name, "size": len(data)},
        )

    object_id = _resolve(command.object_ref, state)
    if object_id is None:
        return TurnResult("Retrieve which object? Give me an object id.")
    try:
        metadata = service.get_metadata(object_id)
        content = service.read(object_id)
    except ObjectNotFoundError:
        return TurnResult(f'No object found with id "{object_id}".')
    except DataIntegrityError as exc:
        return TurnResult(f"Integrity check failed: {exc}")
    state.last_object_id = object_id

    if metadata["type"] in _TEXTLIKE_TYPES:
        try:
            text_content = content.decode("utf-8")
            preview = text_content if len(text_content) <= 2000 else text_content[:2000] + "…"
            return TurnResult(preview, data=metadata)
        except UnicodeDecodeError:
            pass
    return TurnResult(f"{object_id} is {len(content)} bytes of {metadata['type']} content (not shown as text).", data=metadata)


def _handle_archive(state, service, command: StorageCommand, read_file: ReadFile | None) -> TurnResult:
    object_id = _resolve(command.object_ref, state)
    if object_id is None:
        return TurnResult("Archive which object? Give me an object id.")
    try:
        result = service.move(object_id, StorageTier.ARCHIVE)
    except ObjectNotFoundError:
        return TurnResult(f'No object found with id "{object_id}".')
    state.last_object_id = object_id
    return TurnResult(f'Archived "{object_id}".', data=result)


def _handle_restore(state, service, command: StorageCommand, read_file: ReadFile | None) -> TurnResult:
    object_id = _resolve(command.object_ref, state)
    if object_id is None:
        return TurnResult("Restore which object? Give me an object id.")
    try:
        result = service.move(object_id, StorageTier.WARM)
    except ObjectNotFoundError:
        return TurnResult(f'No object found with id "{object_id}".')
    state.last_object_id = object_id
    return TurnResult(f'Restored "{object_id}" to WARM.', data=result)


def _handle_optimize(state, service, command: StorageCommand, read_file: ReadFile | None) -> TurnResult:
    object_id = _resolve(command.object_ref, state)
    if object_id is None:
        return TurnResult("Optimize which object? Give me an object id.")
    preference = extract_preference(command.preference_text)
    try:
        result = service.optimize(object_id, access=_access_from_preference(preference))
    except ObjectNotFoundError:
        return TurnResult(f'No object found with id "{object_id}".')
    state.last_object_id = object_id

    note = _preference_note(preference)
    if not result["changed"]:
        return TurnResult(f'No change needed - "{object_id}" is already at {result["storage_tier"]}.{note}', data=result)
    return TurnResult(f'Moved "{object_id}" from {result["previous_tier"]} to {result["storage_tier"]}.{note}', data=result)


def _handle_move(state, service, command: StorageCommand, read_file: ReadFile | None) -> TurnResult:
    object_id = _resolve(command.object_ref, state)
    if object_id is None:
        return TurnResult("Move which object? Give me an object id.")
    assert command.tier is not None
    try:
        metadata = service.get_metadata(object_id)
        state.last_object_id = object_id

        if metadata["type"] == DataType.AI_MODEL.value:
            # A model is really its manifest plus one index entry per
            # tensor block, each with its own independently-decided tier
            # - a plain `service.move()` here would only relocate the
            # small manifest JSON and silently leave every tensor's
            # actual weight bytes wherever they already were.
            # `move_model` cascades to all of them so "move <model_id>
            # to hot" does what it sounds like it does.
            moved = service.model_store.move_model(object_id, command.tier)
            tensor_blocks = moved - 1
            return TurnResult(
                f'Moved "{object_id}" and its {tensor_blocks} tensor block(s) to {command.tier.value}.',
                data={"object_id": object_id, "storage_tier": command.tier.value, "objects_moved": moved},
            )

        result = service.move(object_id, command.tier)
    except ObjectUnavailableError as exc:
        return TurnResult(str(exc))
    except (ObjectNotFoundError, ModelNotFoundError):
        # `move_model` confirms the model exists via `get_manifest` first,
        # which raises `ModelNotFoundError` (not `ObjectNotFoundError`) if
        # its content can't be read back - e.g. indexed at HOT but that
        # tier's in-memory backend lost it across a restart.
        return TurnResult(f'No object found with id "{object_id}".')
    return TurnResult(f'Moved "{object_id}" to {command.tier.value}.', data=result)


def _handle_delete(state, service, command: StorageCommand, read_file: ReadFile | None) -> TurnResult:
    object_id = _resolve(command.object_ref, state)
    if object_id is None:
        return TurnResult("Delete which object? Give me an object id.")
    try:
        service.get_metadata(object_id)
    except ObjectNotFoundError:
        return TurnResult(f'No object found with id "{object_id}".')
    state.pending_delete = object_id
    return TurnResult(f'Delete "{object_id}"? (yes/no)')


def _handle_search(state, service, command: StorageCommand, read_file: ReadFile | None) -> TurnResult:
    if command.tier is not None:
        results = service.list_objects(storage_tier=command.tier)
        return TurnResult(f"{len(results)} object(s) in {command.tier.value}.", data=results)

    if command.type_word is not None:
        data_type = TYPE_WORDS[command.type_word]
        results = service.list_objects(object_type=data_type)
        return TurnResult(f"{len(results)} {data_type.value} object(s).", data=results)

    term = (command.search_term or "").strip()
    if not term:
        return TurnResult("Search for what? Try \"search for <text>\".")
    results = service.search_documents(term)
    if not results:
        return TurnResult(f'No documents matched "{term}".')
    return TurnResult(f'{len(results)} document(s) matched "{term}".', data=results)


def _handle_inspect(state, service, command: StorageCommand, read_file: ReadFile | None) -> TurnResult:
    object_id = _resolve(command.object_ref, state)
    if object_id is None:
        return TurnResult("Inspect which object? Give me an object id.")
    try:
        metadata = service.get_metadata(object_id)
    except ObjectNotFoundError:
        return TurnResult(f'No object found with id "{object_id}".')
    state.last_object_id = object_id
    return TurnResult(f'{object_id}: {metadata["type"]}, {metadata["storage_tier"]}, {metadata["size"]} bytes.', data=metadata)


def _handle_describe(state, service, command: StorageCommand, read_file: ReadFile | None) -> TurnResult:
    object_id = _resolve(command.object_ref, state)
    if object_id is None:
        return TurnResult("Describe which object? Give me an object id.")
    try:
        explanation = service.explain(object_id)
    except ObjectNotFoundError:
        return TurnResult(f'No object found with id "{object_id}".')
    state.last_object_id = object_id
    return TurnResult(explanation["explanation"], data=explanation)


def _access_from_preference(preference: OptimizationPreference) -> AccessProfile:
    # Only FAST_ACCESS has a legitimate decision input to feed
    # (StorageStrategyEngine's tier selection reads access_frequency,
    # section 27-29) - MAX_COMPRESSION has no corresponding input
    # anywhere in the engine, honestly, because compression is decided
    # from the content's own measured entropy (Phase B/F), not from a
    # request. Reporting that gap in `_preference_note` is the point,
    # not a bug to paper over.
    if preference is OptimizationPreference.FAST_ACCESS:
        return AccessProfile(access_frequency=DEFAULT_POLICY.hot_access_frequency + 1)
    return AccessProfile()


def _preference_note(preference: OptimizationPreference) -> str:
    if preference is OptimizationPreference.NONE:
        return ""
    label = preference.value.replace("_", " ").lower()
    return f" (requested: {label} - the storage engine decides the actual representation/compression from the object's measured content, not from the request directly)"


_HANDLERS: dict[StorageIntent, Callable[["StorageConversationState", "ObjectService", StorageCommand, ReadFile | None], TurnResult]] = {
    StorageIntent.STORE: _handle_store,
    StorageIntent.UPDATE: _handle_update,
    StorageIntent.RETRIEVE: _handle_retrieve,
    StorageIntent.ARCHIVE: _handle_archive,
    StorageIntent.RESTORE: _handle_restore,
    StorageIntent.OPTIMIZE: _handle_optimize,
    StorageIntent.MOVE: _handle_move,
    StorageIntent.DELETE: _handle_delete,
    StorageIntent.SEARCH: _handle_search,
    StorageIntent.INSPECT: _handle_inspect,
    StorageIntent.DESCRIBE: _handle_describe,
}
