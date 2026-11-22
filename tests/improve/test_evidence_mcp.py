import asyncio
import json
import os
import socket
import subprocess
import sys

import pytest

from agent_usage_tracker.improve.store import Store

pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

import httpx  # noqa: E402


@pytest.fixture
def connection(tmp_path):
    store = Store(tmp_path / "private")
    session_id = store.put("sessions", {"agent": "codex", "coverage": {"complete": True},
        "events": [{"id": "event", "kind": "prompt", "text": "password=fixture-secret"}]})
    audit_id = store.put("audits", {"session_ids": [session_id], "coverage": {}})
    args = ["-m", "agent_usage_tracker.cli", "evidence", "--root", str(store.root),
            "--audit", audit_id, "--content", "redacted"]
    return args, session_id


def test_stdio_matches_cli_and_cannot_write(connection):
    args, key = connection
    expected = json.loads(subprocess.check_output(
        [sys.executable, *args, "event", key, "event"], text=True))

    async def run():
        async with stdio_client(StdioServerParameters(
                command=sys.executable, args=[*args, "mcp"])) as streams:
            async with ClientSession(*streams) as client:
                await client.initialize()
                tools = await client.list_tools()
                assert {tool.name for tool in tools.tools} == {
                    "describe", "search", "session", "event", "compare", "commits", "case",
                    "ordinary_work"}
                result = await client.call_tool("event", {"session_id": key, "event_id": "event"})
                assert result.structuredContent == expected
                ranked = await client.call_tool("search", {"query": "password", "mode": "ranked"})
                assert ranked.structuredContent["items"][0]["event_id"] == "event"
                assert "fixture-secret" not in json.dumps(ranked.structuredContent)
                ranked_again = await client.call_tool("search", {"query": "redacted", "mode": "ranked"})
                expected_ranked = json.loads(subprocess.check_output(
                    [sys.executable, *args, "search", "--query", "redacted", "--mode", "ranked"], text=True))
                assert ranked_again.structuredContent == expected_ranked
                denied = await client.call_tool("session", {"session_id": "0" * 64})
                assert denied.isError
                expected_work = json.loads(subprocess.check_output(
                    [sys.executable, *args, "ordinary-work"], text=True))
                work = await client.call_tool("ordinary_work", {})
                assert work.structuredContent == expected_work

    asyncio.run(asyncio.wait_for(run(), timeout=15))


def test_http_auth_and_read_on_real_loopback(connection, tmp_path):
    args, key = connection
    bundle = tmp_path / "remote.json"
    subprocess.run([sys.executable, *args, "export", str(bundle), key], check=True)
    remote = ["-m", "agent_usage_tracker.cli", "evidence", "--bundle", str(bundle)]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = str(sock.getsockname()[1])
    token = "t" * 43
    url = f"http://127.0.0.1:{port}/mcp"

    async def run():
        async with httpx.AsyncClient(timeout=2) as probe:
            for _ in range(50):
                try:
                    response = await probe.get(url)
                except httpx.ConnectError:
                    await asyncio.sleep(0.1)
                else:
                    break
            else:
                pytest.fail("MCP server did not start")
            assert response.status_code == 401
            assert (await probe.get(url, headers={"Authorization": "Bearer wrong"})).status_code == 401
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
            async with streamable_http_client(url, http_client=http) as streams:
                async with ClientSession(*streams[:2]) as client:
                    await client.initialize()
                    result = await client.call_tool("event", {"session_id": key, "event_id": "event"})
                    assert result.structuredContent["text"] == "password=[redacted]"

    with subprocess.Popen([sys.executable, *remote, "mcp", "--transport", "http", "--port", port],
                          env={**os.environ, "AUT_EVIDENCE_TOKEN": token},
                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) as server:
        try:
            asyncio.run(asyncio.wait_for(run(), timeout=15))
        finally:
            server.terminate()
            server.communicate(timeout=10)


def test_code_tool_matches_cli_and_uses_connection_grant(connection, tmp_path):
    pytest.importorskip("pydantic_monty")
    args, key = connection
    source = f'read("event", session_id="{key}", event_id="event")["data"]["text"]'
    recipe = tmp_path / "recipe.py"
    recipe.write_text(source, encoding="utf-8")
    expected = json.loads(subprocess.check_output(
        [sys.executable, *args, "run-code", str(recipe)], text=True))

    async def run():
        async with stdio_client(StdioServerParameters(
                command=sys.executable, args=[*args, "mcp", "--code"])) as streams:
            async with ClientSession(*streams) as client:
                await client.initialize()
                catalog = await client.call_tool("code_catalog", {})
                assert "search" in catalog.structuredContent["operations"]
                result = await client.call_tool("run_code", {"source": source})
                assert result.structuredContent == expected
                assert expected["result"] == "password=[redacted]"
                denied = await client.call_tool("run_code", {"source": 'read("export")'})
                assert denied.isError

    asyncio.run(asyncio.wait_for(run(), timeout=15))


def test_code_tool_pins_bundle_for_life_of_connection(connection, tmp_path):
    pytest.importorskip("pydantic_monty")
    args, key = connection
    bundle, replacement = (tmp_path / name for name in ("selected.json", "updated.json"))
    for output, content in ((bundle, "metadata"), (replacement, "full")):
        subprocess.run([sys.executable, *args[:-1], content, "export", str(output), key], check=True)
    args = ["-m", "agent_usage_tracker.cli", "evidence", "--bundle", str(bundle),
            "--content", "full", "mcp", "--code"]
    source = f'read("event", session_id="{key}", event_id="event")["data"].get("text")'

    async def run():
        async with stdio_client(StdioServerParameters(command=sys.executable, args=args)) as streams:
            async with ClientSession(*streams) as client:
                await client.initialize()
                before = await client.call_tool("run_code", {"source": source})
                assert before.structuredContent["result"] is None
                replacement.replace(bundle)
                after = await client.call_tool("run_code", {"source": source})
                assert after.isError
                direct = await client.call_tool("event", {"session_id": key, "event_id": "event"})
                assert "text" not in direct.structuredContent

    asyncio.run(asyncio.wait_for(run(), timeout=15))
