"""``swarm web``'s HTTP surface, on a real ThreadingHTTPServer bound to port 0."""

from __future__ import annotations

import gzip
import http.client
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from swarm_orchestrator.web import server as web_server

from test_web_board import make_run


@pytest.fixture
def srv(tmp_path, monkeypatch):
    cfg = make_run(tmp_path, monkeypatch)
    s = web_server.make_server(cfg, "127.0.0.1", 0, poll_s=0.05)
    t = threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    try:
        yield s
    finally:
        web_server.close(s)


def _url(srv, path: str) -> str:
    return f"http://127.0.0.1:{srv.server_address[1]}{path}"


def _get(srv, path: str, headers: dict | None = None):
    req = urllib.request.Request(_url(srv, path), headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_the_page_is_one_self_contained_file(srv):
    code, headers, body = _get(srv, "/")
    assert code == 200 and headers["Content-Type"].startswith("text/html")
    html = body.decode()
    assert "<script>" in html and "<style>" in html
    # No external host of any kind: no src/href to a URL, no web font, no CDN.
    for needle in ("http://", "https://", "<link", "src=", "@import", "fonts.g"):
        assert needle not in html, needle
    assert "default-src 'none'" in headers["Content-Security-Policy"]


def test_board_json_has_every_column(srv):
    code, headers, body = _get(srv, "/api/board")
    assert code == 200 and headers["Content-Type"].startswith("application/json")
    board = json.loads(body)
    assert [c["key"] for c in board["columns"]][:3] == ["needs_you", "blocked", "ready"]
    assert board["version"] >= 1 and "ETag" in headers


def test_board_is_gzipped_and_answers_304_on_its_etag(srv):
    code, headers, body = _get(srv, "/api/board", {"Accept-Encoding": "gzip"})
    assert headers.get("Content-Encoding") == "gzip"
    assert json.loads(gzip.decompress(body))["columns"]
    code, _, _ = _get(srv, "/api/board", {"If-None-Match": headers["ETag"]})
    assert code == 304


def test_phase_detail_and_unknown_phase(srv):
    code, _, body = _get(srv, "/api/phase/al-W0")
    assert code == 200 and json.loads(body)["id"] == "al-W0"
    assert _get(srv, "/api/phase/nope-W9")[0] == 404


def test_unknown_paths_404_and_nothing_is_served_by_path(srv):
    for path in ("/nope", "/state.json", "/static/index.html", "/../state.json",
                 "/api/phase/..%2F..%2Fstate.json", "/api/phase/%2e%2e/state.json",
                 "/api/board/../../state.json", "/%2e%2e/%2e%2e/etc/passwd"):
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        conn.request("GET", path)  # sent verbatim: no client-side normalisation
        r = conn.getresponse()
        body = r.read()
        conn.close()
        assert r.status == 404, path
        assert b'"slots"' not in body and b"root:" not in body


def test_nothing_mutates(srv):
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        conn.request(method, "/api/board", body=b"{}")
        assert conn.getresponse().status == 501, method
        conn.close()


def test_search_endpoint(srv):
    code, _, body = _get(srv, "/api/search?q=continuation%20line")
    assert code == 200 and json.loads(body)["ids"] == ["al-W2"]


def _settled(srv) -> None:
    """Let the first forecast land and the feed take it in: a forecast landing is
    a real change, and after it nothing in this fixture moves."""
    assert srv.feed.dash.eta.wait(30)
    v = -1
    while v != srv.feed.version:
        v = srv.feed.version
        time.sleep(0.3)


def _read_event(resp, deadline: float) -> dict | None:
    """The next ``event: board`` payload off an SSE stream, or None at the deadline."""
    event = None
    while time.monotonic() < deadline:
        line = resp.fp.readline()
        if not line:
            return None
        line = line.decode().rstrip("\n")
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: ") and event == "board":
            return json.loads(line[6:])
    return None


def test_sse_pushes_a_new_version_when_state_changes(srv):
    _settled(srv)
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    conn.request("GET", "/events")
    resp = conn.getresponse()
    assert resp.status == 200 and resp.getheader("Content-Type").startswith("text/event-stream")
    first = _read_event(resp, time.monotonic() + 5)
    assert first is not None
    cfg = srv.feed.cfg
    st = json.loads(cfg.state_path.read_text())
    st["paused"] = True
    cfg.state_path.write_text(json.dumps(st))
    nxt = _read_event(resp, time.monotonic() + 10)
    conn.close()
    assert nxt is not None and nxt["version"] > first["version"]
    board = json.loads(_get(srv, "/api/board")[2])
    assert board["header"]["paused"] is True and board["version"] == nxt["version"]


def test_sse_sends_heartbeats(srv, monkeypatch):
    monkeypatch.setattr(web_server, "HEARTBEAT_S", 0.1)
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    conn.request("GET", "/events")
    resp = conn.getresponse()
    seen = b""
    deadline = time.monotonic() + 5
    while b": hb" not in seen and time.monotonic() < deadline:
        seen += resp.fp.readline()
    conn.close()
    assert b": hb" in seen


def test_many_clients_share_one_board(srv):
    """Ten clients cost one build: the version does not move per request."""
    _settled(srv)
    v = srv.feed.version
    for _ in range(10):
        assert _get(srv, "/api/board")[0] == 200
    assert srv.feed.version == v
