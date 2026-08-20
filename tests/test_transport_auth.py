import asyncio
import json
import os
from uuid import uuid4

import httpx
from starlette.responses import JSONResponse

os.environ["ADMIN_TOKEN"] = "test-token"

import server


class _MessageWriter:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(message)


def _request(method, url, **kwargs):
    async def run():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, url, **kwargs)

    return asyncio.run(run())


def test_sse_message_session_does_not_need_api_key_after_authenticated_connect(monkeypatch):
    session_id = uuid4()
    writer = _MessageWriter()
    monkeypatch.setitem(server.transport._read_stream_writers, session_id, writer)

    try:
        response = _request(
            "POST",
            f"/messages/?session_id={session_id.hex}",
            content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            headers={"Content-Type": "application/json"},
        )
    finally:
        server.transport._read_stream_writers.pop(session_id, None)

    assert response.status_code == 202
    assert len(writer.messages) == 1


def test_unknown_sse_message_session_still_requires_api_key():
    response = _request(
        "POST",
        f"/messages/?session_id={uuid4().hex}",
        content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 401


def test_mcp_session_id_does_not_bypass_unrelated_routes(monkeypatch):
    session_id = uuid4()
    monkeypatch.setitem(server.transport._read_stream_writers, session_id, _MessageWriter())

    try:
        response = _request("GET", f"/health/detail?session_id={session_id.hex}")
    finally:
        server.transport._read_stream_writers.pop(session_id, None)

    assert response.status_code == 401


def test_streamable_http_session_does_not_need_api_key_after_authenticated_initialize(monkeypatch):
    session_id = uuid4().hex
    monkeypatch.setitem(server.session_manager._server_instances, session_id, object())

    async def fake_handle_request(scope, receive, send):
        await JSONResponse({"accepted": True})(scope, receive, send)

    monkeypatch.setattr(server.session_manager, "handle_request", fake_handle_request)

    try:
        response = _request(
            "POST",
            "/mcp",
            content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            headers={
                "Content-Type": "application/json",
                "Mcp-Session-Id": session_id,
            },
        )
    finally:
        server.session_manager._server_instances.pop(session_id, None)

    assert response.status_code == 200
    assert response.json() == {"accepted": True}


def test_unknown_streamable_http_session_still_requires_api_key():
    response = _request(
        "POST",
        "/mcp",
        content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
        headers={
            "Content-Type": "application/json",
            "Mcp-Session-Id": uuid4().hex,
        },
    )

    assert response.status_code == 401
