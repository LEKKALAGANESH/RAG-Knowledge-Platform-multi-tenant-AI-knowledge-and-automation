"""End-to-end against the backend's scripted serve helpers (no real LLM):
`tests.test_agent serve` (agent runs, approvals, reports) and `tests.test_rag serve` (answers, fixed
port 8079). Both use the test DB `RAG_TEST_DB` (default rag_test_b). Skipped when they cannot start.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from rag_platform import Client, PermissionDeniedError, RagError

pytestmark = pytest.mark.integration

BACKEND = Path(__file__).resolve().parents[3] / "backend"
CREDS = re.compile(r"agent serve: email=(\S+) password=(.+) kb_id=(\S+)")
ALL_SCOPES = ["kb:read", "kb:write", "documents:write", "search", "answer", "agent:run", "admin"]


@dataclass
class Server:
    proc: subprocess.Popen[str]
    url: str
    lines: list[str] = field(default_factory=list)

    def stop(self) -> None:
        if self.proc.poll() is None:  # uv does not forward signals: kill our own tree
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True
                )
            else:
                self.proc.kill()
        self.proc.wait(30)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _port_busy(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _start(module: str, port: int, *args: str) -> Server:
    env = {
        **os.environ,
        "RAG_TEST_DB": os.environ.get("RAG_TEST_DB", "rag_test_b"),
        "S3_ACCESS_KEY": os.environ.get("S3_ACCESS_KEY", "rag_dev_minio"),
        "S3_SECRET_KEY": os.environ.get("S3_SECRET_KEY", "rag_dev_minio_secret"),
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    env.pop("VIRTUAL_ENV", None)  # the backend has its own venv
    cmd = ["uv", "run", "python", "-m", module, "serve", *args]
    proc = subprocess.Popen(
        cmd,
        cwd=BACKEND,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    srv = Server(proc, f"http://127.0.0.1:{port}")
    assert proc.stdout is not None
    out = proc.stdout
    threading.Thread(target=lambda: srv.lines.extend(out), daemon=True).start()
    return srv


def _wait_healthy(srv: Server, timeout: float = 180) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if srv.proc.poll() is not None:
            break
        try:
            if httpx.get(srv.url + "/healthz", timeout=2).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(1)
    srv.stop()
    pytest.skip("serve helper did not start:\n" + "".join(srv.lines[-15:]))


@pytest.fixture(scope="module")
def agent_server() -> Iterator[tuple[Server, str, str, str]]:
    if not BACKEND.is_dir():
        pytest.skip("backend/ not found")
    port = _free_port()
    srv = _start("tests.test_agent", port, "--port", str(port))
    try:
        _wait_healthy(srv)
        match = next((m for m in map(CREDS.search, srv.lines) if m), None)
        if match is None:
            pytest.skip("agent serve helper printed no credentials")
        yield srv, match[1], match[2].strip(), match[3]
    finally:
        srv.stop()


def _login(c: Client, email: str, password: str) -> None:
    try:
        c.login(email, password)
    except RagError as e:  # known: the first login after start can 500 (redis pool on seed loop)
        if e.status != 500:
            raise
        c.login(email, password)


def test_agent_run_with_approval_and_report(agent_server: tuple[Server, str, str, str]) -> None:
    srv, email, password, kb = agent_server
    with Client(srv.url, timeout=60) as c:
        _login(c, email, password)
        key = c.api_keys.create("sdk-integration", ALL_SCOPES)  # type: ignore[arg-type]
        c.set_credentials(key["key"])  # from here on: API key auth
        assert c.me()["role"] == "owner"

        narrow = c.api_keys.create("sdk-narrow", ["search"])
        with Client(srv.url, api_key=narrow["key"]) as limited:
            with pytest.raises(PermissionDeniedError) as denied:
                limited.agent.list()
            assert denied.value.code == "insufficient_scope"
            assert limited.search("Invoice", kb_ids=[kb], mode="keyword")

        assert c.kbs.get(kb)["id"] == kb
        docs = c.documents.list(kb)["items"]
        assert len(docs) >= 4 and all(d["status"] == "ready" for d in docs)
        assert "Invoice" in c.documents.chunks(docs[0]["id"])["items"][0]["text"]
        hits = c.search("Acme", kb_ids=[kb], mode="keyword")
        assert hits and all("Acme" in h["text"] for h in hits)
        up = c.documents.upload(
            kb, b"# SDK note\nUploaded by the SDK test.", filename="sdk-note.md"
        )
        assert up["status"] in ("queued", "processing", "ready") and up["name"] == "sdk-note.md"
        c.documents.delete(up["id"])

        run = c.agent.create("Compare 2025 invoice totals with 2024", kb_ids=[kb])
        last_n, approval_id = 0, None
        for e in c.agent.events(run["id"]):
            if e.event == "step":
                last_n = e.data["n"]
            if e.event == "approval" and e.data["status"] == "pending":
                approval_id = e.data["id"]
                break  # drop the stream on purpose; resume below with ?after=
        assert approval_id is not None and last_n > 0
        assert c.approvals.decide(approval_id, "approve")["status"] == "approved"

        kinds = [e.event for e in c.agent.events(run["id"], after=last_n)]
        assert kinds[-1] == "end" and "status" in kinds
        done = c.agent.get(run["id"])
        assert done["status"] == "succeeded", done
        result = done["result"] or {}
        assert result["citations"] and result["reports"]
        xlsx = c.reports.download(result["reports"][0])
        assert xlsx[:2] == b"PK"  # zip container = xlsx


def test_answer_stream_and_ask(agent_server: tuple[Server, str, str, str]) -> None:
    if _port_busy(8079):
        pytest.skip("port 8079 (fixed in tests.test_rag serve) is busy")
    _, email, password, kb = agent_server  # same test DB: the seeded invoice KB
    srv = _start("tests.test_rag", 8079)
    try:
        _wait_healthy(srv)
        with Client(srv.url, timeout=60) as c:
            _login(c, email, password)
            c.set_credentials(c.api_keys.create("sdk-answer", ["answer", "kb:read"])["key"])
            kinds = [type(e).__name__ for e in c.answer.stream("Invoice Acme total", kb_ids=[kb])]
            assert kinds[0] == "Meta" and kinds[-1] == "Done" and "Token" in kinds
            a = c.answer.ask("Invoice Acme total", kb_ids=[kb])
            assert a.text.startswith("Refunds are issued") and "[c42]" not in a.text
            assert a.citations and a.citations[0].kb_id == kb
            conv = c.conversations.get(a.conversation_id or "")
            assert [m["role"] for m in conv["messages"]][-2:] == ["user", "assistant"]
    finally:
        srv.stop()
