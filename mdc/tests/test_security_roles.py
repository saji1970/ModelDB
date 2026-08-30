"""security/roles.py: the Role/Permission vocabulary shared by user
accounts and API tokens."""

from mdc.security.roles import Permission, Role, role_has


def test_viewer_can_only_read():
    assert role_has(Role.VIEWER, Permission.READ)
    assert not role_has(Role.VIEWER, Permission.WRITE)
    assert not role_has(Role.VIEWER, Permission.CREATE_TABLE)
    assert not role_has(Role.VIEWER, Permission.CREATE_DATABASE)
    assert not role_has(Role.VIEWER, Permission.MANAGE_USERS)


def test_editor_can_write_and_create_tables_but_not_databases():
    assert role_has(Role.EDITOR, Permission.READ)
    assert role_has(Role.EDITOR, Permission.WRITE)
    assert role_has(Role.EDITOR, Permission.CREATE_TABLE)
    assert not role_has(Role.EDITOR, Permission.CREATE_DATABASE)
    assert not role_has(Role.EDITOR, Permission.MANAGE_USERS)


def test_db_admin_can_create_databases_but_not_manage_users():
    assert role_has(Role.DB_ADMIN, Permission.CREATE_DATABASE)
    assert role_has(Role.DB_ADMIN, Permission.CREATE_TABLE)
    assert role_has(Role.DB_ADMIN, Permission.WRITE)
    assert not role_has(Role.DB_ADMIN, Permission.MANAGE_USERS)


def test_admin_has_every_permission():
    for permission in Permission:
        assert role_has(Role.ADMIN, permission)


def test_every_role_covers_read():
    for role in Role:
        assert role_has(role, Permission.READ)
