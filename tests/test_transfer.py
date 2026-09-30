from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from arch_upload import cli


@pytest.fixture
def receiver():
    state = {"data": bytearray(), "offsets": [], "heads": 0, "creates": 0,
             "token": "temporary-secret", "fail_after": None, "lose_ack": False, "expired": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, status, headers=None):
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, str(value))
            self.end_headers()

        def authorized(self):
            if self.headers.get("x-signature") != state["token"]:
                self.respond(401)
                return False
            return True

        def do_POST(self):
            if not self.authorized():
                return
            state["creates"] += 1
            state["data"] = bytearray()
            state["length"] = int(self.headers["Upload-Length"])
            self.respond(201, {"Location": "/upload/1"})

        def do_HEAD(self):
            if not self.authorized():
                return
            state["heads"] += 1
            if state["expired"]:
                self.respond(410)
            else:
                self.respond(200, {"Upload-Offset": len(state["data"])})

        def do_PATCH(self):
            content = self.rfile.read(int(self.headers["Content-Length"]))
            if not self.authorized():
                return
            if state["fail_after"] is not None and len(state["data"]) >= state["fail_after"]:
                self.respond(401)
                return
            offset = int(self.headers["Upload-Offset"])
            state["offsets"].append(offset)
            if offset != len(state["data"]):
                self.respond(409)
                return
            state["data"].extend(content)
            if state["lose_ack"]:
                state["lose_ack"] = False
                self.respond(503)
            else:
                self.respond(204, {"Upload-Offset": len(state["data"])})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/upload", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def prepare(tmp_path, endpoint, size=cli.CHUNK_SIZE + 37):
    file = tmp_path / "a file ' é.csv"
    content = (b"1234567890" * (size // 10 + 1))[:size]
    file.write_bytes(content)
    session_file = tmp_path / "session.json"
    session = {"uploadReference": "reference-1", "sizeBytes": size, "endpoint": endpoint,
               "token": "temporary-secret", "metadata": {"filename": file.name}}
    cli._private_json(session_file, session)
    return file, session_file, session, content


def test_transfer_chunks_and_completed_rerun_do_not_reupload(tmp_path, receiver, capsys):
    endpoint, state = receiver
    file, session, _, content = prepare(tmp_path, endpoint)
    result = cli.transfer(file, session)
    assert result["nextAction"] == "finalize_upload"
    assert bytes(state["data"]) == content
    assert state["offsets"] == [0, cli.CHUNK_SIZE]
    assert cli.transfer(file, session) == result
    assert state["creates"] == 1
    assert "temporary-secret" not in capsys.readouterr().err


def test_renew_credentials_resumes_without_retransmitting(tmp_path, receiver):
    endpoint, state = receiver
    file, session_file, session, content = prepare(tmp_path, endpoint)
    state["fail_after"] = cli.CHUNK_SIZE
    with pytest.raises(cli.UploadError, match="fresh credentials"):
        cli.transfer(file, session_file)
    assert len(state["data"]) == cli.CHUNK_SIZE
    state["fail_after"] = None
    state["token"] = session["token"] = "renewed-secret"
    cli._private_json(session_file, session)
    cli.transfer(file, session_file)
    assert state["creates"] == 1
    assert state["heads"] == 1
    assert state["offsets"] == [0, cli.CHUNK_SIZE]
    assert bytes(state["data"]) == content


def test_lost_ack_reads_offset_before_retry(tmp_path, receiver, monkeypatch):
    endpoint, state = receiver
    file, session, _, content = prepare(tmp_path, endpoint, size=100)
    state["lose_ack"] = True
    monkeypatch.setattr(cli, "RETRY_DELAYS", (0,))
    cli.transfer(file, session)
    assert state["offsets"] == [0]
    assert state["heads"] == 1
    assert bytes(state["data"]) == content


def test_changed_file_cannot_resume(tmp_path, receiver):
    endpoint, state = receiver
    file, session, _, _ = prepare(tmp_path, endpoint)
    state["fail_after"] = cli.CHUNK_SIZE
    with pytest.raises(cli.UploadError):
        cli.transfer(file, session)
    file.write_bytes(b"x" * file.stat().st_size)
    with pytest.raises(cli.UploadError, match="identity changed"):
        cli.transfer(file, session)


def test_expired_session_requires_explicit_restart(tmp_path, receiver):
    endpoint, state = receiver
    file, session, _, content = prepare(tmp_path, endpoint)
    state["fail_after"] = cli.CHUNK_SIZE
    with pytest.raises(cli.UploadError):
        cli.transfer(file, session)
    state["fail_after"] = None
    state["expired"] = True
    with pytest.raises(cli.UploadError, match="--restart"):
        cli.transfer(file, session)
    assert state["creates"] == 1
    cli.transfer(file, session, restart=True)
    assert state["creates"] == 2
    assert bytes(state["data"]) == content


def test_cli_never_prints_private_exception(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["arch-upload", "--file", "unused", "--session", "unused"])

    def fail(*args, **kwargs):
        raise RuntimeError("https://storage.test/?token=private-secret")

    monkeypatch.setattr(cli, "transfer", fail)
    assert cli.main() == 1
    output = capsys.readouterr()
    assert "private-secret" not in output.out + output.err
    assert json.loads(output.out)["status"] == "error"
