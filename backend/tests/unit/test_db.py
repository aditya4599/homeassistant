from sqlalchemy import text

from app.db.base import Base
from app.db.session import create_db_engine


def test_sqlite_engine_can_connect():
    engine = create_db_engine("sqlite://")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1


def test_base_metadata_exists_and_is_empty_in_part_1():
    assert len(Base.metadata.tables) == 0
