import pytest

from cagent import db, importer, settings


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    conn = db.connect(path)
    importer.import_all(conn, settings.DATA_DIR)
    conn.close()
    return path


@pytest.fixture
def db_conn(db_path):
    conn = db.connect(db_path)
    yield conn
    conn.close()
