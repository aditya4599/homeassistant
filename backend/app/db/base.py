"""SQLAlchemy declarative base.

No tables exist in Part 1. When models are added, import them here (or in a
``models`` package imported from here) so Alembic autogenerate can see them.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
