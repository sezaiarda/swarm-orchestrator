"""``swarm web``'s HTTP surface, on a real ThreadingHTTPServer bound to port 0:
the machine's routes, and one swarm's under ``/s/<slug>/``. Two swarms on one
board are in ``test_web_machine.py``."""

from __future__ import annotations

import gzip
import http.client
import json
import time
import urllib.error
import urllib.request

import pytest

from swarm_orchestrator.web import server as web_server

from test_web_board import SLUG, make_run, serve

#: Where the one swarm of these tests is on the board.
S = f"/s/{SLUG}"


@pytest.fixture
def srv(tmp_path, monkeypatch):
    s = serve(make_run(tmp_path, monkeypatch))
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


@pytest.mark.parametrize("path", ["/", S + "/"])
def test_each_page_is_one_self_contained_file(srv, path):
    code, headers, body = _get(srv, path)
    assert code == 200 and headers["Content-Type"].startswith("text/html")
    html = body.decode()
    assert "<script>" in html and "<style>" in html
    # No external host of any kind: no src/href to a URL, no web font, no CDN.
    for needle in ("http://", "https://", "<link", "src=", "@import", "fonts.g"):
        assert needle not in html, needle
    # The shared parts are in the page, not asked for: no placeholder is left.
    assert "{{" not in html and "--bg:" in html and "function swarmButtons" in html
    assert "prefers-color-scheme:light" in html  # night and daylight, both
    assert "default-src 'none'" in headers["Content-Security-Policy"]


def test_the_two_pages_are_the_overview_and_a_swarms_board(srv):
    overview, board = _get(srv, "/")[2].decode(), _get(srv, S + "/")[2].decode()
    assert "/api/machine" in overview and "api/board" not in overview
    # A swarm's page asks for its data beside itself, so it shows the swarm it
    # was opened on and no other; and it carries the way to the others.
    assert '"api/board"' in board and 'S + "/api/board"' not in board
    assert 'href="/"' in board and "swarmButtons(m.swarms, HERE)" in board


def test_a_swarms_address_without_the_slash_leads_to_its_page(srv):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    conn.request("GET", S)
    r = conn.getresponse()
    r.read()
    conn.close()
    assert r.status == 308 and r.getheader("Location") == S + "/"


def test_healthz_says_whose_board_this_is(srv):
    code, _, body = _get(srv, "/healthz")
    got = json.loads(body)
    assert code == 200 and got["app"] == "swarm-web" and got["pid"] > 0
    assert got["machine"] == str(srv.hub.root)


def test_board_json_has_every_column(srv):
    code, headers, body = _get(srv, S + "/api/board")
    assert code == 200 and headers["Content-Type"].startswith("application/json")
    board = json.loads(body)
    assert [c["key"] for c in board["columns"]][:3] == ["needs_you", "blocked", "ready"]
    assert board["version"] >= 1 and "ETag" in headers


def test_board_is_gzipped_and_answers_304_on_its_etag(srv):
    code, headers, body = _get(srv, S + "/api/board", {"Accept-Encoding": "gzip"})
    assert headers.get("Content-Encoding") == "gzip"
    assert json.loads(gzip.decompress(body))["columns"]
    code, _, _ = _get(srv, S + "/api/board", {"If-None-Match": headers["ETag"]})
    assert code == 304


def test_phase_detail_and_unknown_phase(srv):
    code, _, body = _get(srv, S + "/api/phase/al-W0")
    assert code == 200 and json.loads(body)["id"] == "al-W0"
    assert _get(srv, S + "/api/phase/nope-W9")[0] == 404


def test_unknown_paths_404_and_nothing_is_served_by_path(srv):
    for path in ("/nope", "/state.json", "/static/index.html", "/static/swarm.html",
                 "/../state.json", "/%2e%2e/%2e%2e/etc/passwd", "/api/board", "/events",
                 "/s/", "/s/../state/api/board", "/s/%2e%2e/state.json", "/s/machine/",
                 S + "/state.json", S + "/config.json", S + "/../state.json",
                 S + "/api/phase/..%2F..%2Fstate.json", S + "/api/phase/%2e%2e/state.json",
                 S + "/api/board/../../state.json", S + "/%2e%2e/%2e%2e/etc/passwd"):
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        conn.request("GET", path)  # sent verbatim: no client-side normalisation
        r = conn.getresponse()
        body = r.read()
        conn.close()
        assert r.status == 404, path
        assert b'"slots"' not in body and b"root:" not in body


def test_nothing_mutates(srv):
    for path in ("/", "/api/machine", S + "/", S + "/api/board", S + "/api/phase/al-W0"):
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            conn.request(method, path, body=b"{}")
            assert conn.getresponse().status == 501, (method, path)
            conn.close()


def test_a_slug_no_swarm_has_is_404(srv):
    for path in ("/s/nope/api/board", "/s/nope/api/phase/al-W0", "/s/nope/events",
                 "/s/nope/api/resources", "/s/stat/api/board", "/s/state2/api/usage"):
        code, headers, body = _get(srv, path)
        assert code == 404 and json.loads(body) == {"error": "no such swarm"}, path
    # A person who followed an old bookmark gets a page that leads back, and the
    # name is never echoed as markup.
    for path in ("/s/nope/", "/s/nope", "/s/%3Cscript%3Ealert(1)%3C/"):
        code, headers, body = _get(srv, path)
        assert code == 404 and headers["Content-Type"].startswith("text/html"), path
        assert b'href="/"' in body and b"<script" not in body


def test_the_overview_and_every_swarm_route_are_redacted(srv):
    """The fixture's recap carries a credential, and a question can carry one."""
    cfg = srv.feed.cfg
    with (cfg.state_dir / "notifications.jsonl").open("a") as fh:
        fh.write(json.dumps({"ts": time.time(), "kind": "waiting", "phase": "al-W3",
                             "text": "use ghp_abcdefghijklmnopqrstuvwxyz0123 or not?",
                             "delivered": True, "error": None}) + "\n")
    srv.feed.refresh(force=True)
    srv.hub.refresh()
    for path in ("/api/machine", S + "/api/board", S + "/api/phase/al-W0",
                 S + "/api/phase/al-W3", S + "/api/graph?mode=all"):
        body = _get(srv, path)[2].decode()
        assert "ghp_abc" not in body and "sk-ant-api03" not in body, path
    asked = json.loads(_get(srv, "/api/machine")[2])["waiting"]["questions"]
    assert any("[redacted]" in a["q"] for a in asked)


def test_search_endpoint(srv):
    code, _, body = _get(srv, S + "/api/search?q=continuation%20line")
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
    conn.request("GET", S + "/events")
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
    board = json.loads(_get(srv, S + "/api/board")[2])
    assert board["header"]["paused"] is True and board["version"] == nxt["version"]


def test_sse_sends_heartbeats(srv, monkeypatch):
    monkeypatch.setattr(web_server, "HEARTBEAT_S", 0.1)
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    conn.request("GET", S + "/events")
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
        assert _get(srv, S + "/api/board")[0] == 200
    assert srv.feed.version == v


def test_graph_endpoint_shape_etag_and_gzip(srv):
    code, headers, body = _get(srv, S + "/api/graph?mode=all")
    assert code == 200 and headers["Content-Type"].startswith("application/json")
    g = json.loads(body)
    assert g["mode"] == "all" and {n["id"] for n in g["nodes"]} >= {"al-W0", "be-W9"}
    assert all({"id", "s", "x", "y"} <= set(n) for n in g["nodes"])
    assert _get(srv, S + "/api/graph?mode=all", {"If-None-Match": headers["ETag"]})[0] == 304
    _, gzh, gzb = _get(srv, S + "/api/graph?mode=all", {"Accept-Encoding": "gzip"})
    assert gzh.get("Content-Encoding") == "gzip" and json.loads(gzip.decompress(gzb))["nodes"]
    booked = json.loads(_get(srv, S + "/api/graph?book=be")[2])
    assert booked["book"] == "be" and all(n["c"] == "be" or n.get("k") for n in booked["nodes"])


def test_graph_endpoint_refuses_a_bad_view(srv):
    assert _get(srv, S + "/api/graph?mode=everything")[0] == 400
    assert _get(srv, S + "/api/graph?book=../../etc")[0] == 400


def test_usage_endpoint(srv):
    code, headers, body = _get(srv, S + "/api/usage")
    assert code == 200
    u = json.loads(body)
    assert [w["key"] for w in u["windows"]] == ["five", "week"] and "runs" in u
    assert _get(srv, S + "/api/usage", {"If-None-Match": headers["ETag"]})[0] == 304
