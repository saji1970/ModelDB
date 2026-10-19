"""Moving a database in and out of MDC: import from Markdown / CSV / SQL
script / MDC export file, export to CSV / SQL / Excel / Markdown design
document / ER diagram / MDC export file.

Nothing here runs SQL. An SQL script is *parsed* (`readers.read_sql`) into
a plan of tables and rows and applied through the schema registry and
the Data Engine, the same way chat's `create table` and `insert into`
are - CLAUDE.md's rule that MDC never executes free-form SQL holds for a
file exactly as it does for a chat message.
"""
