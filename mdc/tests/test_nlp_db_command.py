"""Phase: database-admin NLP parsing (nlp/db_command.py) - emphasis on
collision safety against both existing domains (merchants CRUD and
the object-storage NLP layer).
"""

import pytest

from mdc.nlp.command import parse_storage_command
from mdc.nlp.db_command import DatabaseIntent, parse_database_command
from mdc.storage_intelligence.strategy import StorageTier

# -- must never match merchants or object-storage phrasing ----------------------

OTHER_DOMAIN_PHRASES = [
    "Change ABC Store balance to 15000",
    "Create a merchant called ABC Store in India",
    "Delete merchant ABC Store",
    "Show all merchants",
    "Show me ABC Store",
    "store ./model.safetensors",
    "archive AIM-1234567890",
    "delete AIM-1234567890",
    "describe AIM-1234567890",
    "describe it",
    "list models",
    "list images",
    "show objects in archive",
    "search for revenue",
]


@pytest.mark.parametrize("phrase", OTHER_DOMAIN_PHRASES)
def test_never_matches_other_domains(phrase: str):
    assert parse_database_command(phrase) is None


@pytest.mark.parametrize("phrase", [
    "create database mytest", "use database mytest", "list databases",
    "describe database", "list tables",
    "create table products with sku string, price decimal",
    "describe table products", "show data in products", "insert into products sku=A",
])
def test_database_commands_never_match_the_object_storage_parser(phrase: str):
    assert parse_storage_command(phrase) is None


# -- CREATE / USE / LIST / DESCRIBE database ---------------------------------------

def test_create_database():
    cmd = parse_database_command("create database mytest")
    assert cmd.intent == DatabaseIntent.CREATE_DATABASE
    assert cmd.database_name == "mytest"


def test_create_database_with_named_called_phrasing():
    assert parse_database_command("create a database named mytest").database_name == "mytest"
    assert parse_database_command("create new database called mytest").database_name == "mytest"


def test_use_database_synonyms():
    for phrase in ("use database mytest", "switch to database mytest", "switch database mytest"):
        cmd = parse_database_command(phrase)
        assert cmd.intent == DatabaseIntent.USE_DATABASE
        assert cmd.database_name == "mytest"


def test_list_databases():
    for phrase in ("list databases", "show databases"):
        assert parse_database_command(phrase).intent == DatabaseIntent.LIST_DATABASES


def test_describe_database_synonyms():
    for phrase in ("describe database", "show database structure", "list tables", "show tables"):
        assert parse_database_command(phrase).intent == DatabaseIntent.DESCRIBE_DATABASE


# -- CREATE / DESCRIBE table --------------------------------------------------------

def test_create_table_with_field_list():
    cmd = parse_database_command("create table products with sku string, name string, price decimal")
    assert cmd.intent == DatabaseIntent.CREATE_TABLE
    assert cmd.table_name == "products"
    assert set(cmd.fields) == {"sku", "name", "price"}
    assert cmd.fields["price"].datatype == "decimal"


def test_create_table_rejects_invalid_type():
    assert parse_database_command("create table products with sku widget") is None


def test_create_table_rejects_malformed_field_list():
    assert parse_database_command("create table products with sku") is None
    assert parse_database_command("create table products with sku string extra token") is None


def test_describe_table_synonyms():
    assert parse_database_command("describe table products").table_name == "products"
    assert parse_database_command("describe collection products").table_name == "products"


# -- SHOW_DATA / INSERT --------------------------------------------------------------

def test_show_data_synonyms():
    for phrase in ("show data in products", "show rows in products", "query products"):
        cmd = parse_database_command(phrase)
        assert cmd.intent == DatabaseIntent.SHOW_DATA
        assert cmd.table_name == "products"


def test_insert_with_kv_pairs():
    cmd = parse_database_command("insert into products sku=ABC123, name=Widget, price=9.99")
    assert cmd.intent == DatabaseIntent.INSERT
    assert cmd.table_name == "products"
    assert cmd.values == {"sku": "ABC123", "name": "Widget", "price": "9.99"}


def test_insert_rejects_missing_equals():
    assert parse_database_command("insert into products sku") is None


def test_empty_and_gibberish_return_none():
    assert parse_database_command("") is None
    assert parse_database_command("do a backflip") is None
