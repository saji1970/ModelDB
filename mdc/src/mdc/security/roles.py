"""Roles and permissions (CLAUDE.md section 50: "a user's authorization
must be evaluated before the operation reaches the Data Engine").

One shared vocabulary for both interactive users (`security/users.py`,
username + password, used at the CLI) and API bearer tokens
(`security/tokens.py`, used by external NLU/custom-UI integrations) -
a role means the same thing regardless of which door someone came
through.

Roles are scoped to what this system actually lets you do to a
database, not a generic READ/WRITE/ADMIN triad:

    VIEWER   - read/query only, nothing else
    EDITOR   - VIEWER + create tables and write rows in existing
               databases, cannot create or drop a whole database
    DB_ADMIN - EDITOR + create databases (CLAUDE.md's own example of
               an "adding Database" capability), still not the same as
               ADMIN
    ADMIN    - DB_ADMIN + manage other users' accounts and roles

Permissions, not roles, are what call sites check - `ROLE_PERMISSIONS`
is the only place role-to-permission mapping is decided, so adjusting
what a role can do never means hunting through route handlers.
"""

from __future__ import annotations

from enum import Enum


class Permission(str, Enum):
    READ = "read"                       # query/list/get - databases, tables, rows, objects, models, find
    WRITE = "write"                     # insert/update/delete rows, upload/replace/delete/move/optimize objects, chat
    CREATE_TABLE = "create_table"       # add a table/collection to an existing database
    CREATE_DATABASE = "create_database"  # create a brand-new database
    MANAGE_USERS = "manage_users"       # create/delete user accounts, change roles, issue tokens for others


class Role(str, Enum):
    VIEWER = "viewer"
    EDITOR = "editor"
    DB_ADMIN = "db_admin"
    ADMIN = "admin"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset({Permission.READ}),
    Role.EDITOR: frozenset({Permission.READ, Permission.WRITE, Permission.CREATE_TABLE}),
    Role.DB_ADMIN: frozenset({Permission.READ, Permission.WRITE, Permission.CREATE_TABLE, Permission.CREATE_DATABASE}),
    Role.ADMIN: frozenset({
        Permission.READ, Permission.WRITE, Permission.CREATE_TABLE,
        Permission.CREATE_DATABASE, Permission.MANAGE_USERS,
    }),
}


def role_has(role: Role, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS[role]
