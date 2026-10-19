"""DatabaseManager errors."""

from __future__ import annotations


class DatabaseError(Exception):
    """Base class for database-management errors."""


class InvalidDatabaseNameError(DatabaseError):
    def __init__(self, name: str):
        super().__init__(f"{name!r} is not a valid database name - use letters, numbers, underscores, and hyphens, starting with a letter.")
        self.name = name


class DatabaseAlreadyExistsError(DatabaseError):
    def __init__(self, name: str):
        super().__init__(f"Database {name!r} already exists.")
        self.name = name


class DatabaseNotFoundError(DatabaseError):
    def __init__(self, name: str):
        super().__init__(f"No database named {name!r}.")
        self.name = name


class CannotDropDefaultDatabaseError(DatabaseError):
    def __init__(self):
        super().__init__('The "default" database always exists (it holds the built-in merchants table) and cannot be dropped.')


class ProtectedTableError(DatabaseError):
    def __init__(self, table: str):
        super().__init__(f'"{table}" is a built-in table of the default database and cannot be dropped.')
        self.table = table
