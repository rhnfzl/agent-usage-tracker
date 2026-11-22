import json
from http.server import BaseHTTPRequestHandler
from threading import Thread

from agent_usage_tracker.cli import main
from agent_usage_tracker.improve.store import Store
from agent_usage_tracker.net import LoopbackHTTPServer


def test_session_delivery_redacts_and_can_be_disabled(tmp_path, monkeypatch, capsys):
    store = Store(tmp_path / "private")
    key = store.put("sessions", {"agent": "codex", "coverage": {}, "events": [
        {"id": "one", "kind": "prompt", "text": "password=fixture-secret"},
    ]})
    audit = store.put("audits", {"session_ids": [key], "coverage": {}})
    captured = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            captured.append((self.headers["Idempotency-Key"], json.loads(body)))
            self.send_response(204)
            self.end_headers()

    with LoopbackHTTPServer(("127.0.0.1", 0), Receiver) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            monkeypatch.setenv("AUT_SESSION_OBSERVABILITY_URL", f"http://127.0.0.1:{server.server_port}")
            args = ["evidence", "--root", str(store.root), "--audit", audit, "observe"]
            assert main(args) == 0
            assert json.loads(capsys.readouterr().out)["status"] == "sent"
            identity, payload = captured[0]
            assert identity == payload["sha256"]
            assert payload["evidence"]["content"] == "redacted"
            assert payload["evidence"]["sessions"][key]["events"][0]["text"] == "password=[redacted]"
            monkeypatch.setenv("AUT_SESSION_OBSERVABILITY", "off")
            assert main(args) == 0
            assert json.loads(capsys.readouterr().out)["status"] == "disabled"
            assert len(captured) == 1
        finally:
            server.shutdown()
            worker.join()
