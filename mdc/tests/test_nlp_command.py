"""Phase J: natural-language -> StorageCommand parsing (CLAUDE-STORAGE.md
sections 36-37), with an emphasis on the collision-safety guarantee -
this must never fire on merchants-CRUD phrasing.
"""

import pytest

from mdc.nlp.command import parse_storage_command
from mdc.nlp.intent import StorageIntent
from mdc.storage_intelligence.strategy import StorageTier

# -- must NEVER match: real phrasing from the merchants CRUD/analytics tests --

MERCHANT_PHRASES = [
    "Change ABC Store balance to 15000",
    "Count merchants in India",
    "Create a merchant called ABC Store in India",
    "Create a merchant in India",
    "Delete merchant ABC Store",
    "Delete merchant XYZ Retail",
    "Only India",
    "Show all merchants",
    "Show me ABC Store",
    "Show merchants with available balance above 5000",
    "Show merchants with balance above 5000",
    "Show merchants with balance",
    "Show merchants with settlement balance above 10000 USD",
    "Show merchants with settlement balnce above 10000 USD",
    "Show the maximum settlement balance",
    "Show the minimum settlement balance",
    "Show top 20 merchants by settlement balance",
    "Sort highest first",
    "What is the average settlement balance",
    "What is the total settlement amount",
    "explain the last query",
    "explain",
    "help",
]


@pytest.mark.parametrize("phrase", MERCHANT_PHRASES)
def test_never_matches_merchants_domain_phrasing(phrase: str):
    assert parse_storage_command(phrase) is None


def test_free_text_object_reference_is_rejected_not_guessed():
    # "the old model" isn't resolvable without a name index this system
    # doesn't have - the parser must fail closed, not guess an id.
    assert parse_storage_command("archive the old model") is None
    assert parse_storage_command("delete the old version") is None


# -- STORE -------------------------------------------------------------------

def test_store_with_path():
    cmd = parse_storage_command("store ./model.safetensors")
    assert cmd.intent is StorageIntent.STORE
    assert cmd.path == "./model.safetensors"
    assert cmd.preference_text == ""


def test_store_with_preference_text():
    cmd = parse_storage_command("store ./model.safetensors as efficiently as possible")
    assert cmd.path == "./model.safetensors"
    assert "efficiently" in cmd.preference_text


# -- UPDATE --------------------------------------------------------------------

def test_update_with_path():
    cmd = parse_storage_command("update AIM-1234567890 with ./new.safetensors")
    assert cmd.intent is StorageIntent.UPDATE
    assert cmd.object_ref == "AIM-1234567890"
    assert cmd.path == "./new.safetensors"


def test_update_rejects_non_id_reference():
    assert parse_storage_command("update the old model with ./new.safetensors") is None


# -- RETRIEVE (whole object, tensor, layer) --------------------------------------

def test_retrieve_whole_object():
    cmd = parse_storage_command("retrieve AIM-1234567890")
    assert cmd.intent is StorageIntent.RETRIEVE
    assert cmd.object_ref == "AIM-1234567890"
    assert cmd.tensor_name is None


def test_retrieve_tensor_from_model():
    cmd = parse_storage_command("retrieve tensor layer_27.attention.q from model-001")
    assert cmd.intent is StorageIntent.RETRIEVE
    assert cmd.tensor_name == "layer_27.attention.q"
    assert cmd.object_ref == "model-001"


def test_retrieve_layer_shorthand():
    cmd = parse_storage_command("retrieve layer 27 from AIM-1234567890")
    assert cmd.tensor_name == "layer_27"
    assert cmd.object_ref == "AIM-1234567890"


def test_read_open_cat_are_retrieve_synonyms():
    for verb in ("read", "open", "cat"):
        cmd = parse_storage_command(f"{verb} AIM-1234567890")
        assert cmd.intent is StorageIntent.RETRIEVE
        assert cmd.object_ref == "AIM-1234567890"


# -- ARCHIVE / RESTORE ------------------------------------------------------------

def test_archive_by_id():
    cmd = parse_storage_command("archive AIM-1234567890")
    assert cmd.intent is StorageIntent.ARCHIVE
    assert cmd.object_ref == "AIM-1234567890"


def test_archive_by_pronoun():
    cmd = parse_storage_command("archive it")
    assert cmd.intent is StorageIntent.ARCHIVE
    assert cmd.object_ref == "it"


def test_restore_synonyms():
    for phrase in ("restore AIM-1234567890", "unarchive AIM-1234567890", "bring back AIM-1234567890"):
        cmd = parse_storage_command(phrase)
        assert cmd.intent is StorageIntent.RESTORE
        assert cmd.object_ref == "AIM-1234567890"


# -- OPTIMIZE / preference extraction --------------------------------------------

def test_optimize_by_id():
    cmd = parse_storage_command("optimize AIM-1234567890")
    assert cmd.intent is StorageIntent.OPTIMIZE
    assert cmd.object_ref == "AIM-1234567890"


def test_make_x_as_y_as_possible():
    cmd = parse_storage_command("make this model as compact as possible")
    assert cmd.intent is StorageIntent.OPTIMIZE
    assert cmd.object_ref == "this model"
    assert cmd.preference_text == "compact"


def test_compress_maps_to_optimize_with_compact_preference():
    cmd = parse_storage_command("compress this document")
    assert cmd.intent is StorageIntent.OPTIMIZE
    assert cmd.object_ref == "this document"
    assert cmd.preference_text == "compact"


# -- MOVE ------------------------------------------------------------------------

def test_move_to_tier():
    cmd = parse_storage_command("move AIM-1234567890 to hot")
    assert cmd.intent is StorageIntent.MOVE
    assert cmd.object_ref == "AIM-1234567890"
    assert cmd.tier is StorageTier.HOT


# -- DELETE ------------------------------------------------------------------------

def test_delete_by_id():
    cmd = parse_storage_command("delete AIM-1234567890")
    assert cmd.intent is StorageIntent.DELETE
    assert cmd.object_ref == "AIM-1234567890"


def test_delete_by_pronoun():
    cmd = parse_storage_command("delete it")
    assert cmd.object_ref == "it"


# -- SEARCH (text, type, tier) -----------------------------------------------------

def test_search_for_text():
    cmd = parse_storage_command("search for revenue")
    assert cmd.intent is StorageIntent.SEARCH
    assert cmd.search_term == "revenue"


def test_find_text():
    cmd = parse_storage_command("find text quarterly")
    assert cmd.search_term == "quarterly"


def test_list_by_type():
    cmd = parse_storage_command("list models")
    assert cmd.intent is StorageIntent.SEARCH
    assert cmd.type_word == "model"


def test_unrecognized_type_word_does_not_match():
    assert parse_storage_command("list unicorns") is None


def test_list_by_tier():
    cmd = parse_storage_command("show objects in archive")
    assert cmd.intent is StorageIntent.SEARCH
    assert cmd.tier is StorageTier.ARCHIVE


# -- INSPECT / DESCRIBE -----------------------------------------------------------

def test_inspect_by_id():
    cmd = parse_storage_command("inspect AIM-1234567890")
    assert cmd.intent is StorageIntent.INSPECT
    assert cmd.object_ref == "AIM-1234567890"


def test_show_get_are_inspect_synonyms():
    assert parse_storage_command("show AIM-1234567890").intent is StorageIntent.INSPECT
    assert parse_storage_command("get it").intent is StorageIntent.INSPECT


def test_describe_and_explain_and_why_is():
    for phrase in ("describe AIM-1234567890", "explain AIM-1234567890", "why is AIM-1234567890"):
        cmd = parse_storage_command(phrase)
        assert cmd.intent is StorageIntent.DESCRIBE
        assert cmd.object_ref == "AIM-1234567890"


def test_where_is_x_stored():
    cmd = parse_storage_command("where is AIM-1234567890 stored")
    assert cmd.intent is StorageIntent.DESCRIBE
    assert cmd.object_ref == "AIM-1234567890"


def test_show_me_where_x_is_stored():
    cmd = parse_storage_command("show me where this document is stored")
    assert cmd.intent is StorageIntent.DESCRIBE
    assert cmd.object_ref == "this document"


# -- empty / unrecognized -----------------------------------------------------------

def test_empty_text_returns_none():
    assert parse_storage_command("") is None
    assert parse_storage_command("   ") is None


def test_gibberish_returns_none():
    assert parse_storage_command("do a backflip") is None
