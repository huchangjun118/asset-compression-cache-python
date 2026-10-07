"""HTTP-level tests through the FastAPI app."""
from __future__ import annotations

import importlib
import sys

import pytest
from fastapi.testclient import TestClient

BODY = ("hello world " * 100).encode()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    pub = tmp_path / "public" / "v1"
    pub.mkdir(parents=True)
    (pub / "note.txt").write_bytes(BODY)
    prot = tmp_path / "protected" / "v1"
    prot.mkdir(parents=True)
    (prot / "secret.txt").write_bytes(BODY)
    monkeypatch.setenv("ASSET_ROOT", str(tmp_path))
    sys.modules.pop("main", None)
    main = importlib.import_module("main")
    return TestClient(main.app)


def test_gzip_end_to_end(client):
    r = client.get("/assets/public/note.txt", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers["Content-Encoding"] == "gzip"
    # httpx transparently decodes the gzip body for us.
    assert r.content == BODY
    etag = r.headers["ETag"]
    r2 = client.get(
        "/assets/public/note.txt",
        headers={"Accept-Encoding": "gzip", "If-None-Match": etag},
    )
    assert r2.status_code == 304
    assert r2.content == b""


def test_identity_end_to_end(client):
    r = client.get("/assets/public/note.txt", headers={"Accept-Encoding": "identity"})
    assert r.status_code == 200
    assert "Content-Encoding" not in r.headers
    assert r.content == BODY


def test_protected_requires_token(client):
    assert client.get("/assets/protected/secret.txt").status_code == 403
    assert (
        client.get(
            "/assets/protected/secret.txt", headers={"X-Asset-Token": "bad"}
        ).status_code
        == 403
    )
    ok = client.get(
        "/assets/protected/secret.txt",
        headers={"X-Asset-Token": "protected-secret-token"},
    )
    assert ok.status_code == 200 and ok.content == BODY


def test_missing_asset_404(client):
    assert client.get("/assets/public/nope.txt").status_code == 404


def test_invalidate_endpoint(client):
    client.get("/assets/public/note.txt", headers={"Accept-Encoding": "gzip"})
    r = client.post("/admin/invalidate/public/note.txt")
    assert r.status_code == 200 and r.json()["removed"] >= 1
