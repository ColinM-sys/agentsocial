import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

import server


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = str(tmp_path / "social.db")
    monkeypatch.setattr(server, "DB", path)
    monkeypatch.setitem(server.app.config, "TESTING", True)
    server.init()
    return path


def make_request(path, payload):
    with server.app.test_client() as client:
        return client.post(path, json=payload)


def test_post_commits_while_a_reader_holds_a_snapshot(database):
    reader = sqlite3.connect(database)
    reader.execute("BEGIN")
    reader.execute("select count(*) from posts").fetchone()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(make_request, "/api/posts", {"author": "a", "body": "hello"})
            try:
                response = future.result(timeout=2)
                assert response.status_code == 200
                assert reader.execute("select count(*) from posts").fetchone()[0] == 0
            finally:
                reader.rollback()
    finally:
        reader.close()


def test_writers_are_serialized_across_endpoints(database, monkeypatch):
    entered = threading.Event()
    second_entered = threading.Event()
    release = threading.Event()
    original_db = server.db
    original_upsert = server.app.view_functions["upsert_profile"]

    def hold_first_write():
        entered.set()
        if not release.wait(timeout=10):
            raise RuntimeError("timed out waiting to release the first writer")
        return 0

    def db_with_no_busy_wait():
        con = original_db()
        con.execute("pragma busy_timeout=0")
        con.create_function("hold_first_write", 0, hold_first_write)
        return con

    def observe_upsert():
        second_entered.set()
        return original_upsert()

    with closing(sqlite3.connect(database)) as con:
        con.execute("create trigger hold_post after insert on posts begin select hold_first_write(); end")
        con.commit()
    monkeypatch.setattr(server, "db", db_with_no_busy_wait)
    monkeypatch.setitem(server.app.view_functions, "upsert_profile", observe_upsert)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(make_request, "/api/posts", {"author": "a", "body": "hello"})
        try:
            assert entered.wait(timeout=5)
            second = pool.submit(make_request, "/api/profiles", {"name": "a"})
            assert second_entered.wait(timeout=5)
            with pytest.raises(TimeoutError):
                second.result(timeout=0.5)
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 200
        assert second.result(timeout=5).status_code == 200


def test_requests_wait_for_an_external_writer_longer_than_default_timeout(database):
    with server.app.app_context():
        assert server.db().execute("pragma busy_timeout").fetchone()[0] > 5000
    blocker = sqlite3.connect(database)
    blocker.execute("begin immediate")
    start = threading.Barrier(3)

    def request_after_barrier(path, payload):
        start.wait(timeout=5)
        return make_request(path, payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        requests = [
            pool.submit(request_after_barrier, "/api/posts", {"author": "a", "body": "hello"}),
            pool.submit(request_after_barrier, "/api/profiles", {"name": "a"}),
        ]
        try:
            start.wait(timeout=5)
            time.sleep(5.5)
            assert all(not future.done() for future in requests)
        finally:
            blocker.rollback()
            blocker.close()
        assert all(future.result(timeout=10).status_code == 200 for future in requests)
    with server.app.test_client() as client:
        assert client.get("/api/stats").get_json() == {"profiles": 1, "posts": 1, "by_kind": {"post": 1}}
