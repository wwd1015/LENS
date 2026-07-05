"""Tests for the brief server (lens.brief.serve)."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from lens.brief.serve import serve


@pytest.fixture()
def server(tmp_path):
    (tmp_path / "brief.latest.html").write_text(
        "<html><body>hello brief</body></html>", encoding="utf-8"
    )
    srv = serve(tmp_path, host="127.0.0.1", port=0)  # port 0 → ephemeral
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _url(srv, path):
    host, port = srv.server_address[:2]
    return f"http://{host}:{port}{path}"


def _post_json(srv, path, payload):
    req = urllib.request.Request(
        _url(srv, path),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return resp.status, json.loads(resp.read())


def test_get_serves_brief(server):
    with urllib.request.urlopen(_url(server, "/")) as resp:
        assert resp.status == 200
        assert b"hello brief" in resp.read()
        assert resp.headers["Cache-Control"] == "no-store"


def test_get_missing_brief_404(tmp_path):
    srv = serve(tmp_path, host="127.0.0.1", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(_url(srv, "/"))
        assert exc_info.value.code == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_post_feedback_appends_jsonl(server):
    status, body = _post_json(
        server,
        "/feedback",
        {
            "finding_id": "fid-1",
            "label": "false_positive",
            "entity_id": "LN-1",
            "field_name": "balance",
            "detector_sources": "stl_residual,tabpfn_anomaly",
        },
    )
    assert status == 200
    assert body["ok"] is True

    lines = server.feedback_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["finding_id"] == "fid-1"
    assert entry["label"] == "false_positive"
    assert entry["entity_id"] == "LN-1"
    assert entry["field_name"] == "balance"
    assert entry["detector_sources"] == ["stl_residual", "tabpfn_anomaly"]


def test_post_feedback_unknown_label_400(server):
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _post_json(server, "/feedback", {"finding_id": "x", "label": "nope"})
    assert exc_info.value.code == 400
    assert not server.feedback_path.exists()


def test_post_feedback_missing_fields_400(server):
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _post_json(server, "/feedback", {"label": "real"})
    assert exc_info.value.code == 400


def test_unknown_route_404(server):
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(_url(server, "/etc/passwd"))
    assert exc_info.value.code == 404


def test_healthz(server):
    with urllib.request.urlopen(_url(server, "/healthz")) as resp:
        assert json.loads(resp.read()) == {"ok": True}


# ---------------------------------------------------------------------------
# CSRF / robustness hardening
# ---------------------------------------------------------------------------


def _post_raw(srv, path, body: bytes, headers: dict):
    req = urllib.request.Request(_url(srv, path), data=body, headers=headers, method="POST")
    return urllib.request.urlopen(req)


def test_post_feedback_rejects_non_json_content_type(server, tmp_path):
    """A no-cors browser POST (text/plain) must not land feedback — that's
    the CSRF vector for forging suppression votes."""
    body = json.dumps({"finding_id": "f1", "label": "false_positive"}).encode()
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _post_raw(server, "/feedback", body, {"Content-Type": "text/plain"})
    assert exc_info.value.code == 415
    assert not (tmp_path / "feedback.jsonl").exists()


def test_post_feedback_rejects_foreign_origin(server, tmp_path):
    body = json.dumps({"finding_id": "f1", "label": "false_positive"}).encode()
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _post_raw(
            server,
            "/feedback",
            body,
            {"Content-Type": "application/json", "Origin": "https://evil.example"},
        )
    assert exc_info.value.code == 403
    assert not (tmp_path / "feedback.jsonl").exists()


def test_post_feedback_accepts_local_origin(server):
    body = json.dumps({"finding_id": "f1", "label": "real"}).encode()
    host, port = server.server_address[:2]
    with _post_raw(
        server,
        "/feedback",
        body,
        {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"},
    ) as resp:
        assert resp.status == 200


def test_post_feedback_malformed_content_length_is_400(server):
    """`Content-Length: abc` previously crashed the handler thread with no
    HTTP response at all."""
    import http.client

    host, port = server.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.putrequest("POST", "/feedback", skip_accept_encoding=True)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", "abc")
        conn.endheaders()
        resp = conn.getresponse()
        assert resp.status == 400
    finally:
        conn.close()
