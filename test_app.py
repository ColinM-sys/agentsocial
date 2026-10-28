import sqlite3

import pytest

import server


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB", str(tmp_path / "social.db"))
    server.init()
    return server.app.test_client()


def test_null_profile_names_are_rejected(client):
    for _ in range(2):
        response = client.post("/api/profiles", json={"name": None})
        assert response.status_code == 400

    assert client.get("/api/profiles").get_json() == []


def test_profile_upsert_updates_existing_row(client):
    assert client.post("/api/profiles", json={
        "name": "agent", "role": "scout", "skills": "reading", "task": "searching"
    }).status_code == 200
    original = client.get("/api/profiles").get_json()[0]

    assert client.post("/api/profiles", json={
        "name": "agent", "role": "checker", "skills": "testing", "task": "reviewing"
    }).status_code == 200
    assert client.get("/api/profiles").get_json() == [{
        "name": "agent", "role": "checker", "skills": "testing", "task": "reviewing",
        "created": original["created"],
    }]


def test_fresh_profile_schema_rejects_null_names(client):
    con = sqlite3.connect(server.DB)
    try:
        name = next(column for column in con.execute("pragma table_info(profiles)") if column[1] == "name")
        assert name[3] == 1
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("insert into profiles(name) values (?)", (None,))
    finally:
        con.close()


def test_legacy_profile_schema_migration_preserves_data(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB", str(tmp_path / "legacy.db"))
    con = sqlite3.connect(server.DB)
    con.executescript("""
        create table profiles(name text primary key, role text, skills text, task text, created real);
        insert into profiles values ('agent', 'scout', 'reading', 'searching', 1);
        insert into profiles values (null, 'first', 'one', 'task one', 2);
        insert into profiles values (null, 'second', 'two', 'task two', 3);
    """)
    con.close()

    server.init()
    server.init()

    con = sqlite3.connect(server.DB)
    try:
        name = next(column for column in con.execute("pragma table_info(profiles)") if column[1] == "name")
        assert name[3] == 1
        assert con.execute("select * from profiles").fetchall() == [
            ("agent", "scout", "reading", "searching", 1.0),
        ]
        assert con.execute("select * from profiles_null_names order by created").fetchall() == [
            (None, "first", "one", "task one", 2.0),
            (None, "second", "two", "task two", 3.0),
        ]
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("insert into profiles(name) values (?)", (None,))
    finally:
        con.close()
