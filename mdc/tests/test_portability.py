"""Importing and exporting databases (portability/): CSV, SQL script,
Markdown design document, MDC export file in; those plus Excel and an ER
diagram out. Nothing here may ever execute SQL.
"""

import io
import json
import zipfile
from pathlib import Path

import pytest

from mdc.databases.manager import DatabaseManager
from mdc.engine.errors import DuplicateRecordError, InvalidRecordIdError
from mdc.model.operation import CreateOperation, ReadOperation
from mdc.portability.apply import ImportResult, prepare_import, resolve_database_name
from mdc.portability.plan import ExportOptionError, ImportConflictError, ImportDataError, ImportFormatError
from mdc.portability.readers import read_csv, read_import, read_markdown, read_native, read_sql
from mdc.portability.writers import export_database, export_filename, resolve_export_options
from mdc.schema.loader import load_default_registry
from mdc.schema.registry import FieldSchema
from mdc.security.roles import Permission
from mdc.storage.duckdb_store import DuckDBStore


def make_manager(root: Path) -> DatabaseManager:
    store = DuckDBStore(root / "mdc.duckdb")
    store.init_schema()
    return DatabaseManager(root / "databases", store, load_default_registry())


@pytest.fixture
def manager(tmp_path: Path) -> DatabaseManager:
    return make_manager(tmp_path / "a")


@pytest.fixture
def other(tmp_path: Path) -> DatabaseManager:
    """A second, empty instance - where an export gets imported."""
    return make_manager(tmp_path / "b")


def do_import(manager: DatabaseManager, filename: str, content: bytes | str, *, database: str | None = None, session: str | None = None) -> ImportResult:
    data = content.encode() if isinstance(content, str) else content
    plan = read_import(filename, data)
    name = resolve_database_name(plan, database, session, filename.split(".")[0])
    return prepare_import(manager, plan, name).apply()


def rows_of(manager: DatabaseManager, database: str, table: str) -> list[dict]:
    result = manager.get(database).engine.read(ReadOperation(collection=table, filters=[]))
    return sorted(({"record_id": r.record_id, **r.fields} for r in result.records), key=lambda r: r["record_id"])


# -- CSV in -------------------------------------------------------------------------

def test_csv_infers_types():
    plan = read_csv("name,age,score,active,joined,zip\nAda,36,9.5,true,2024-01-05,01234\nBob,41,7,false,2023-02-01,90210\n", "people")
    types = {c.name: c.datatype for c in plan.tables[0].columns}
    assert types == {"name": "string", "age": "integer", "score": "decimal", "active": "boolean", "joined": "datetime", "zip": "string"}


def test_csv_leading_zero_stays_text_so_zip_codes_survive():
    plan = read_csv("zip\n01234\n00501\n", "places")
    assert plan.tables[0].columns[0].datatype == "string"
    assert plan.tables[0].rows[0].values["zip"] == "01234"


def test_csv_renames_awkward_headers_and_says_so():
    plan = read_csv("Customer Name,Order #\nAda,1\n", "orders")
    assert [c.name for c in plan.tables[0].columns] == ["customer_name", "order"]
    assert any("Customer Name" in w for w in plan.warnings)


def test_csv_blank_cells_are_omitted_not_empty_strings():
    plan = read_csv("a,b\n1,\n,x\n", "t")
    assert plan.tables[0].rows[0].values == {"a": 1}
    assert plan.tables[0].rows[1].values == {"b": "x"}


def test_csv_semicolon_delimiter_detected():
    plan = read_csv("a;b\n1;2\n3;4\n", "t")
    assert [c.name for c in plan.tables[0].columns] == ["a", "b"]


def test_csv_record_id_column_is_the_row_identity_not_a_field():
    plan = read_csv("record_id,name\nX-1,Ada\n", "t")
    assert [c.name for c in plan.tables[0].columns] == ["name"]
    assert plan.tables[0].rows[0].record_id == "X-1"


def test_csv_formula_guard_is_removed_on_import():
    plan = read_csv("name\n'=SUM(A1)\n'-5 apples\nplain\n", "t")
    assert [r.values["name"] for r in plan.tables[0].rows] == ["=SUM(A1)", "-5 apples", "plain"]


@pytest.mark.parametrize("text", ["", "   \n"])
def test_csv_empty_file_is_rejected(text):
    with pytest.raises(ImportFormatError):
        read_csv(text, "t")


def test_csv_row_wider_than_header_is_rejected_with_its_line_number():
    with pytest.raises(ImportFormatError, match="Row 3"):
        read_csv("a,b\n1,2\n1,2,3\n", "t")


def test_csv_header_only_creates_the_table_and_warns():
    plan = read_csv("a,b\n", "t")
    assert plan.tables[0].rows == []
    assert any("no data rows" in w for w in plan.warnings)


# -- SQL script in (parsed, never executed) ------------------------------------------

SCRIPT = """
-- a customer table
CREATE TABLE IF NOT EXISTS public."customers" (
  id INTEGER PRIMARY KEY,
  `full name` VARCHAR(100) NOT NULL,
  balance NUMERIC(10,2) DEFAULT 0,
  active BOOLEAN,
  joined TIMESTAMP WITHOUT TIME ZONE,
  CONSTRAINT uq UNIQUE (id)
);
INSERT INTO customers (id, "full name", balance, active, joined) VALUES
  (1, 'Ada O''Neil', 10.5, TRUE, '2024-01-05'),
  (2, 'Bob', -3, FALSE, NULL);
"""


def test_sql_create_table_maps_types_and_required():
    table = read_sql(SCRIPT).tables[0]
    assert table.name == "customers" and table.if_not_exists
    assert {c.name: (c.datatype, c.required) for c in table.columns} == {
        "id": ("integer", True), "full_name": ("string", True), "balance": ("decimal", False),
        "active": ("boolean", False), "joined": ("datetime", False),
    }


def test_sql_insert_parses_escapes_nulls_booleans_and_negative_numbers():
    rows = read_sql(SCRIPT).tables[0].rows
    assert rows[0].values == {"id": 1, "full_name": "Ada O'Neil", "balance": 10.5, "active": True, "joined": "2024-01-05"}
    assert rows[1].values == {"id": 2, "full_name": "Bob", "balance": -3, "active": False}


def test_sql_insert_without_column_list_uses_create_order():
    plan = read_sql("CREATE TABLE t (a INT, b TEXT); INSERT INTO t VALUES (1, 'x');")
    assert plan.tables[0].rows[0].values == {"a": 1, "b": "x"}


def test_sql_semicolon_inside_a_string_does_not_split_the_statement():
    plan = read_sql("CREATE TABLE t (a TEXT); INSERT INTO t VALUES ('one; two');")
    assert plan.tables[0].rows[0].values == {"a": "one; two"}


def test_sql_database_name_from_create_database_use_or_header_comment():
    assert read_sql("CREATE DATABASE shop; CREATE TABLE t (a INT);").database == "shop"
    assert read_sql("USE shop; CREATE TABLE t (a INT);").database == "shop"
    assert read_sql("-- mdc-database: shop\nCREATE TABLE t (a INT);").database == "shop"


def test_sql_destructive_statements_are_skipped_and_reported_never_run(manager: DatabaseManager):
    manager.get("default").engine.create(CreateOperation(collection="merchants", data={"name": "Keep Me", "country": "IN"}))
    script = "DROP TABLE merchants; DELETE FROM merchants; UPDATE merchants SET name='x'; CREATE TABLE fresh (a INT); INSERT INTO fresh VALUES (1);"
    plan = read_sql(script)
    assert [t.name for t in plan.tables] == ["fresh"]
    assert any("Skipped 3" in w and "DROP TABLE" in w and "ever executed" in w for w in plan.warnings)
    prepare_import(manager, plan, "default").apply()
    assert [r["name"] for r in rows_of(manager, "default", "merchants")] == ["Keep Me"]


@pytest.mark.parametrize("value", ["NOW()", "1 + 1", "CURRENT_TIMESTAMP", "(SELECT 1)"])
def test_sql_expressions_and_functions_in_values_are_rejected(value):
    with pytest.raises(ImportFormatError, match="unsupported value"):
        read_sql(f"CREATE TABLE t (a TEXT); INSERT INTO t VALUES ({value});")


def test_sql_insert_select_is_rejected():
    with pytest.raises(ImportFormatError, match="INSERT ... VALUES"):
        read_sql("CREATE TABLE t (a INT); INSERT INTO t SELECT * FROM other;")


def test_sql_wrong_value_count_names_the_statement():
    with pytest.raises(ImportFormatError, match="Statement 2, row 1"):
        read_sql("CREATE TABLE t (a INT, b INT); INSERT INTO t VALUES (1);")


def test_sql_script_with_nothing_importable_is_rejected():
    with pytest.raises(ImportFormatError, match="no CREATE TABLE"):
        read_sql("DROP TABLE x; SET foo = 1;")


def test_sql_unterminated_string_is_rejected():
    with pytest.raises(ImportFormatError, match="Unterminated"):
        read_sql("CREATE TABLE t (a TEXT); INSERT INTO t VALUES ('oops);")


# -- Markdown design document in ------------------------------------------------------

DESIGN = """# Database: shop

Some prose that is ignored.

```
## Table: ignored_inside_a_fence
| Field | Type |
```

## Table: customers

| Field | Type | Required |
|---|---|---|
| name | string | yes |
| credit | decimal | no |
| active | boolean | |
| since | date | |

### Table: `orders`

| Column | Data type | Not null |
|:--|:--|:--|
| customer_id | varchar(36) | true |
| total | numeric | |
"""


def test_markdown_design_document_defines_a_database_and_tables():
    plan = read_markdown(DESIGN)
    assert plan.database == "shop"
    assert [t.name for t in plan.tables] == ["customers", "orders"]
    customers = {c.name: (c.datatype, c.required) for c in plan.tables[0].columns}
    assert customers == {"name": ("string", True), "credit": ("decimal", False), "active": ("boolean", False), "since": ("datetime", False)}
    orders = {c.name: (c.datatype, c.required) for c in plan.tables[1].columns}
    assert orders == {"customer_id": ("string", True), "total": ("decimal", False)}


def test_markdown_unfamiliar_type_is_read_as_something_sensible_and_flagged():
    plan = read_markdown("## Table: t\n\n| Field | Type |\n|---|---|\n| a | varchar(20) |\n")
    assert plan.tables[0].columns[0].datatype == "string"
    assert any("varchar(20)" in w for w in plan.warnings)


def test_markdown_ignores_a_record_id_row():
    plan = read_markdown("## Table: t\n\n| Field | Type |\n|---|---|\n| record_id | string |\n| a | integer |\n")
    assert [c.name for c in plan.tables[0].columns] == ["a"]


def test_markdown_without_tables_explains_the_expected_format():
    with pytest.raises(ImportFormatError, match="## Table:"):
        read_markdown("# Just a document\n\nNo tables.")


def test_markdown_table_without_a_field_table_is_rejected():
    with pytest.raises(ImportFormatError, match="no Field/Type table"):
        read_markdown("## Table: t\n\nNothing here.\n")


# -- MDC export file in ---------------------------------------------------------------

def test_native_rejects_foreign_json_and_future_versions():
    with pytest.raises(ImportFormatError, match="not an MDC export"):
        read_native(b'{"hello": 1}')
    with pytest.raises(ImportFormatError, match="version"):
        read_native(json.dumps({"format": "mdc-export", "version": 99, "tables": {"t": {}}}).encode())
    with pytest.raises(ImportFormatError, match="Not valid JSON"):
        read_native(b"{nope")


def test_native_rejects_unsafe_table_names():
    bundle = {"format": "mdc-export", "version": 1, "tables": {"../evil": {"fields": {}}}}
    with pytest.raises(ImportFormatError, match="Invalid table name"):
        read_native(json.dumps(bundle).encode())


def test_unknown_extension_is_rejected():
    with pytest.raises(ImportFormatError, match="Supported"):
        read_import("notes.docx", b"x")


# -- applying a plan ------------------------------------------------------------------

def test_csv_import_creates_a_table_and_rows(manager: DatabaseManager):
    result = do_import(manager, "people.csv", "name,age\nAda,36\nBob,41\n", database="crm")
    assert result.created_database and result.tables_created == ["people"] and result.rows_inserted == {"people": 2}
    assert sorted((r["name"], r["age"]) for r in rows_of(manager, "crm", "people")) == [("Ada", 36), ("Bob", 41)]
    assert sorted(r["age"] for r in rows_of(manager, "crm", "people")) == [36, 41]
    assert manager.get("crm").schema_registry.get_collection("people").fields["age"].datatype == "integer"


def test_bare_csv_goes_to_the_session_database_else_default(manager: DatabaseManager):
    manager.create("work")
    assert do_import(manager, "a.csv", "x\n1\n", session="work").database == "work"
    assert do_import(manager, "b.csv", "x\n1\n").database == "default"


def test_file_that_defines_a_database_names_its_own_unless_told_otherwise(manager: DatabaseManager):
    assert do_import(manager, "whatever.md", DESIGN).database == "shop"
    assert do_import(manager, "whatever2.md", DESIGN, database="shop2").database == "shop2"


def test_database_name_falls_back_to_the_filename(manager: DatabaseManager):
    assert do_import(manager, "my_model.sql", "CREATE TABLE t (a INT);").database == "my-model"


def test_importing_an_existing_table_is_a_conflict_and_changes_nothing(manager: DatabaseManager):
    do_import(manager, "t.csv", "a\n1\n", database="d")
    with pytest.raises(ImportConflictError, match='table "t" already exists'):
        do_import(manager, "t.csv", "a\n2\n", database="d")
    assert len(rows_of(manager, "d", "t")) == 1


def test_create_table_if_not_exists_keeps_the_existing_table_and_adds_rows(manager: DatabaseManager):
    do_import(manager, "t.csv", "a\n1\n", database="d")
    result = do_import(manager, "s.sql", "CREATE TABLE IF NOT EXISTS t (a INT); INSERT INTO t VALUES (2);", database="d")
    assert result.tables_created == [] and any("already existed" in w for w in result.warnings)
    assert sorted(r["a"] for r in rows_of(manager, "d", "t")) == [1, 2]


def test_insert_into_a_table_the_script_never_creates_needs_that_table_to_exist(manager: DatabaseManager):
    with pytest.raises(ImportFormatError, match="no structure"):
        do_import(manager, "s.sql", "INSERT INTO ghosts (a) VALUES (1);", database="default")
    result = do_import(manager, "s2.sql", "INSERT INTO merchants (name, country) VALUES ('Zed', 'US');", database="default")
    assert result.rows_inserted == {"merchants": 1}


def test_bad_values_are_all_reported_with_row_numbers_and_nothing_is_written(manager: DatabaseManager):
    script = "CREATE TABLE t (n INT, d DATE); INSERT INTO t VALUES (1, '2024-01-01'), ('abc', '2024-01-01'), (3, 'not a date');"
    with pytest.raises(ImportDataError) as caught:
        do_import(manager, "s.sql", script, database="newdb")
    assert len(caught.value.problems) == 2
    assert any("row 2" in p and "integer" in p for p in caught.value.problems)
    assert any("row 3" in p and "ISO" in p for p in caught.value.problems)
    assert not manager.exists("newdb")  # not even the database was created


def test_missing_required_field_is_reported(manager: DatabaseManager):
    with pytest.raises(ImportDataError, match="Missing required"):
        do_import(manager, "s.sql", "CREATE TABLE t (a INT NOT NULL, b INT); INSERT INTO t (b) VALUES (1);", database="d")


def test_boolean_words_are_read_not_cast(manager: DatabaseManager):
    do_import(manager, "s.sql", "CREATE TABLE t (flag BOOLEAN); INSERT INTO t VALUES ('false'), ('no'), ('true');", database="d")
    assert sorted(r["flag"] for r in rows_of(manager, "d", "t")) == [False, False, True]


def test_required_permissions_reflect_what_the_import_does(manager: DatabaseManager):
    def needs(filename: str, content: str, database: str) -> set[Permission]:
        plan = read_import(filename, content.encode())
        return prepare_import(manager, plan, database).required_permissions

    assert needs("t.csv", "a\n1\n", "brand-new") == {Permission.CREATE_DATABASE, Permission.CREATE_TABLE, Permission.WRITE}
    assert needs("t.csv", "a\n1\n", "default") == {Permission.CREATE_TABLE, Permission.WRITE}
    assert needs("s.sql", "INSERT INTO merchants (name, country) VALUES ('Q', 'US');", "default") == {Permission.WRITE}
    assert needs("d.md", "## Table: t\n\n| Field | Type |\n|--|--|\n| a | string |\n", "default") == {Permission.CREATE_TABLE}


def test_invalid_target_database_name_is_a_format_error(manager: DatabaseManager):
    with pytest.raises(ImportFormatError):
        prepare_import(manager, read_csv("a\n1\n", "t"), "../../etc")


def test_tables_imported_into_default_survive_a_restart(tmp_path: Path):
    root = tmp_path / "restart"
    first = make_manager(root)
    do_import(first, "gadgets.csv", "sku\nA1\n", database="default")
    second = make_manager(root)  # a fresh process over the same files
    assert "gadgets" in second.get("default").schema_registry.list_collections()
    assert len(rows_of(second, "default", "gadgets")) == 1


# -- record ids -----------------------------------------------------------------------

def test_engine_keeps_a_supplied_record_id_and_refuses_duplicates_and_bad_ids(manager: DatabaseManager):
    engine = manager.get("default").engine
    engine.create(CreateOperation(collection="merchants", data={"name": "A", "country": "IN"}, record_id="MER-KEEP-1"))
    assert engine.has_record("merchants", "MER-KEEP-1")
    with pytest.raises(DuplicateRecordError):
        engine.create(CreateOperation(collection="merchants", data={"name": "B", "country": "IN"}, record_id="MER-KEEP-1"))
    with pytest.raises(InvalidRecordIdError):
        engine.create(CreateOperation(collection="merchants", data={"name": "C", "country": "IN"}, record_id="bad:id"))


def test_import_conflicts_on_a_record_id_that_already_exists(manager: DatabaseManager):
    do_import(manager, "t.csv", "record_id,a\nR-1,x\n", database="d")
    with pytest.raises(ImportConflictError, match="already exists"):
        do_import(manager, "more.sql", "INSERT INTO t (record_id, a) VALUES ('R-1', 'y');", database="d")


def test_import_rejects_repeated_and_malformed_record_ids_in_the_file(manager: DatabaseManager):
    with pytest.raises(ImportConflictError, match="appears twice"):
        do_import(manager, "t.csv", "record_id,a\nR-1,x\nR-1,y\n", database="d")
    with pytest.raises(ImportDataError, match="valid record_id"):
        do_import(manager, "t.csv", "record_id,a\nbad:id,x\n", database="d")


# -- export ---------------------------------------------------------------------------

@pytest.fixture
def shop(manager: DatabaseManager) -> str:
    do_import(manager, "shop.md", DESIGN, database="shop")
    do_import(manager, "orders.sql", "INSERT INTO orders (customer_id, total) VALUES ('c1', 5.5), ('=EVIL()', 1);", database="shop")
    manager.get("shop").engine.create(CreateOperation(collection="customers", data={"name": "Ada", "credit": 10.5, "active": True}))
    return "shop"


def test_export_options_defaults_aliases_and_errors():
    assert resolve_export_options(None, None) == ("dnadb", "all")
    assert resolve_export_options("xls", None) == ("xlsx", "all")
    assert resolve_export_options("markdown", None) == ("md", "schema")
    assert resolve_export_options("csv", "all") == ("csv", "data")
    with pytest.raises(ExportOptionError, match="CSV holds data only"):
        resolve_export_options("csv", "schema")
    with pytest.raises(ExportOptionError, match="structure only"):
        resolve_export_options("md", "data")
    with pytest.raises(ExportOptionError, match="Unknown export format"):
        resolve_export_options("pdf", None)
    with pytest.raises(ExportOptionError, match="Unknown scope"):
        resolve_export_options("sql", "everything")


def test_export_filenames():
    assert export_filename("shop", "csv", "data") == "shop-csv.zip"
    assert export_filename("shop", "csv", "data", "orders") == "orders.csv"
    assert export_filename("shop", "sql", "schema") == "shop-structure.sql"
    assert export_filename("shop", "dnadb", "all") == "shop.dnadb.json"
    assert export_filename("shop", "md", "schema") == "shop-design.md"
    assert export_filename("shop", "er", "schema") == "shop-er.mmd"


def test_csv_export_single_table_and_zip(manager: DatabaseManager, shop: str):
    handle = manager.get(shop)
    single = export_database(handle, "csv", table="orders")
    text = single.content.decode()
    assert text.splitlines()[0] == "record_id,customer_id,total"
    assert "'=EVIL()" in text  # formula guard
    archive = zipfile.ZipFile(io.BytesIO(export_database(handle, "csv").content))
    assert sorted(archive.namelist()) == ["customers.csv", "orders.csv"]


def test_sql_export_scopes(manager: DatabaseManager, shop: str):
    handle = manager.get(shop)
    full = export_database(handle, "sql").content.decode()
    assert '-- mdc-database: shop' in full and 'CREATE TABLE "orders"' in full and 'INSERT INTO "orders"' in full
    assert "'=EVIL()'" in full
    structure = export_database(handle, "sql", "schema").content.decode()
    assert "CREATE TABLE" in structure and "INSERT" not in structure
    data = export_database(handle, "sql", "data").content.decode()
    assert "INSERT" in data and "CREATE TABLE" not in data


def test_sql_export_escapes_quotes_and_identifiers(manager: DatabaseManager):
    do_import(manager, "t.sql", "CREATE TABLE t (a TEXT); INSERT INTO t VALUES ('it''s');", database="q")
    text = export_database(manager.get("q"), "sql").content.decode()
    assert "'it''s'" in text


def test_xlsx_export_has_a_sheet_per_table_a_schema_sheet_and_no_live_formulas(manager: DatabaseManager, shop: str):
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(export_database(manager.get(shop), "xlsx").content))
    assert workbook.sheetnames == ["customers", "orders", "_schema"]
    orders = workbook["orders"]
    assert [c.value for c in orders[1]] == ["record_id", "customer_id", "total"]
    cells = [c for row in orders.iter_rows(min_row=2) for c in row if c.value == "=EVIL()"]
    assert cells and cells[0].data_type == "s"  # text, not a formula
    schema = [[c.value for c in row] for row in workbook["_schema"].iter_rows(min_row=2)]
    assert ["orders", "total", "decimal", "no"] in schema


def test_xlsx_schema_scope_has_only_the_schema_sheet(manager: DatabaseManager, shop: str):
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(export_database(manager.get(shop), "xlsx", "schema").content))
    assert workbook.sheetnames == ["_schema"]


def test_markdown_design_document_and_er_diagram(manager: DatabaseManager, shop: str):
    doc = export_database(manager.get(shop), "md").content.decode()
    assert doc.startswith("# Database: shop")
    assert "```mermaid\nerDiagram" in doc
    assert "## Table: customers" in doc and "| name | string | yes |" in doc
    er = export_database(manager.get(shop), "er").content.decode()
    assert er.startswith("erDiagram") and "string record_id PK" in er


def test_er_diagram_infers_relationships_from_table_id_fields(manager: DatabaseManager):
    do_import(manager, "d.md", "# Database: rel\n\n## Table: merchants\n\n| Field | Type |\n|--|--|\n| name | string |\n\n"
                              "## Table: payments\n\n| Field | Type |\n|--|--|\n| merchant_id | string |\n| amount | decimal |\n")
    er = export_database(manager.get("rel"), "er").content.decode()
    assert 'payments }o--|| merchants : "merchant_id"' in er
    assert "inferred" in export_database(manager.get("rel"), "md").content.decode()


def test_export_of_a_missing_table_is_an_error(manager: DatabaseManager, shop: str):
    from mdc.schema.registry import CollectionNotFoundError

    with pytest.raises(CollectionNotFoundError):
        export_database(manager.get(shop), "csv", table="nope")


# -- round trips between two instances -------------------------------------------------

def test_native_export_with_data_round_trips_with_record_ids(manager: DatabaseManager, other: DatabaseManager, shop: str):
    bundle = export_database(manager.get(shop), "dnadb")
    result = do_import(other, bundle.filename, bundle.content)
    assert result.database == "shop" and result.created_database
    for table in ("customers", "orders"):
        assert rows_of(other, "shop", table) == rows_of(manager, "shop", table)
    assert other.get("shop").schema_registry.to_dict() == manager.get("shop").schema_registry.to_dict()


def test_native_structure_only_export_creates_empty_tables(manager: DatabaseManager, other: DatabaseManager, shop: str):
    bundle = export_database(manager.get(shop), "dnadb", "schema")
    result = do_import(other, bundle.filename, bundle.content)
    assert sorted(result.tables_created) == ["customers", "orders"] and result.rows_inserted == {}


def test_native_data_only_export_needs_the_tables_to_exist(manager: DatabaseManager, other: DatabaseManager, shop: str):
    bundle = export_database(manager.get(shop), "dnadb", "data")
    with pytest.raises(ImportFormatError, match="no structure"):
        do_import(other, bundle.filename, bundle.content)
    structure = export_database(manager.get(shop), "dnadb", "schema")
    do_import(other, structure.filename, structure.content)
    do_import(other, bundle.filename, bundle.content)
    assert rows_of(other, "shop", "orders") == rows_of(manager, "shop", "orders")


def test_sql_export_round_trips_including_awkward_strings(manager: DatabaseManager, other: DatabaseManager, shop: str):
    manager.get(shop).engine.create(CreateOperation(collection="customers", data={"name": "O'Brien; DROP TABLE x;--"}))
    script = export_database(manager.get(shop), "sql")
    do_import(other, script.filename, script.content)
    assert rows_of(other, "shop", "customers") == rows_of(manager, "shop", "customers")
    assert rows_of(other, "shop", "orders") == rows_of(manager, "shop", "orders")


def test_markdown_export_round_trips_the_structure(manager: DatabaseManager, other: DatabaseManager, shop: str):
    doc = export_database(manager.get(shop), "md")
    do_import(other, doc.filename, doc.content)
    assert other.get("shop").schema_registry.to_dict() == manager.get("shop").schema_registry.to_dict()


def test_csv_export_round_trips_rows_and_ids_even_with_a_formula_looking_value(manager: DatabaseManager, other: DatabaseManager, shop: str):
    single = export_database(manager.get(shop), "csv", table="orders")
    do_import(other, single.filename, single.content, database="shop")
    assert rows_of(other, "shop", "orders") == rows_of(manager, "shop", "orders")


# -- Mermaid ER diagram in --------------------------------------------------------------

DIAGRAM = """erDiagram
    %% a comment
    CUSTOMER ||--o{ ORDER : places
    CUSTOMER {
        string name PK
        string email UK "required"
        int age
        float credit
        datetime joined
    }
    ORDER {
        string record_id PK
        decimal total
        bool paid
    }
    PRODUCT { string sku }
"""


def test_mermaid_entities_become_tables_with_mapped_types():
    from mdc.portability.readers import read_mermaid

    plan = read_mermaid(DIAGRAM)
    tables = {t.name: {c.name: (c.datatype, c.required) for c in t.columns} for t in plan.tables}
    assert tables["customer"] == {"name": ("string", True), "email": ("string", True), "age": ("integer", False), "credit": ("decimal", False), "joined": ("datetime", False)}
    assert "record_id" not in tables["order"]
    assert tables["product"] == {"sku": ("string", False)}  # a one-line block


def test_mermaid_relationship_becomes_an_id_field_on_the_many_side():
    from mdc.portability.readers import read_mermaid

    plan = read_mermaid(DIAGRAM)
    order = next(t for t in plan.tables if t.name == "order")
    assert ("customer_id", "string") in [(c.name, c.datatype) for c in order.columns]
    assert any("added field order.customer_id" in w for w in plan.warnings)


def test_mermaid_relationship_label_that_is_a_field_name_is_used_and_not_duplicated():
    from mdc.portability.readers import read_mermaid

    plan = read_mermaid('erDiagram\n  A { string x }\n  B { string merchant_id }\n  A ||--o{ B : "merchant_id"\n')
    assert [c.name for c in next(t for t in plan.tables if t.name == "b").columns] == ["merchant_id"]


def test_mermaid_many_to_many_is_flagged_not_invented():
    from mdc.portability.readers import read_mermaid

    plan = read_mermaid("erDiagram\n  A { string x }\n  B { string y }\n  A }o--o{ B : links\n")
    assert any("many-to-many" in w for w in plan.warnings)
    assert [c.name for t in plan.tables for c in t.columns] == ["x", "y"]


@pytest.mark.parametrize("text, message", [
    ("graph TD\n A-->B", "must start with 'erDiagram'"),
    ("erDiagram\n  A {\n    string x\n", "never closed"),
    ("erDiagram\n  A {\n    ???\n  }\n", "cannot read the attribute"),
    ("erDiagram\n  A ->> B\n", "cannot read"),
    ("erDiagram\n", "no entities"),
])
def test_mermaid_errors_are_specific(text, message):
    from mdc.portability.readers import read_mermaid

    with pytest.raises(ImportFormatError, match=message):
        read_mermaid(text)


def test_mmd_file_and_markdown_with_only_a_diagram_both_import(manager: DatabaseManager):
    assert do_import(manager, "shop.mmd", DIAGRAM).database == "shop"
    md = "# Database: viamd\n\nSome words.\n\n```mermaid\n" + DIAGRAM + "```\n"
    result = do_import(manager, "doc.md", md)
    assert result.database == "viamd" and sorted(result.tables_created) == ["customer", "order", "product"]


def test_markdown_table_sections_win_over_an_embedded_diagram(manager: DatabaseManager, shop: str):
    doc = export_database(manager.get(shop), "md").content.decode()
    plan = read_markdown(doc)
    assert [t.name for t in plan.tables] == ["customers", "orders"]


def test_er_export_imports_back_to_the_same_structure(manager: DatabaseManager, other: DatabaseManager):
    do_import(manager, "d.md", "# Database: rel\n\n## Table: merchants\n\n| Field | Type |\n|--|--|\n| name | string |\n\n"
                              "## Table: payments\n\n| Field | Type |\n|--|--|\n| merchant_id | string |\n| amount | decimal |\n")
    er = export_database(manager.get("rel"), "er")
    do_import(other, "rel.mmd", er.content)
    assert other.get("rel").schema_registry.to_dict() == manager.get("rel").schema_registry.to_dict()
