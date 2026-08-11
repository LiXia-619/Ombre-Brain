import json

import pytest

from server_app import MCPAuthMiddleware
from tools import _runtime as rt
from tools import recall_structured


class FakeEmbedding:
    enabled = False


class FakeLogger:
    def warning(self, *_args, **_kwargs):
        pass


class FakeBucketManager:
    def __init__(self):
        self.search_calls = []
        self.touch_calls = []

    async def search(self, query, **kwargs):
        self.search_calls.append((query, kwargs))
        return [
            {
                "id": "bucket-1",
                "score": 0.875,
                "content": "A remembered event.",
                "metadata": {
                    "title": "Event",
                    "created": "2026-08-01T10:00:00+00:00",
                    "domain": ["life"],
                    "tags": ["home"],
                    "importance": 8,
                    "valence": 0.7,
                    "arousal": 0.4,
                    "resolved": False,
                },
            }
        ]

    async def touch_many(self, ids, **kwargs):
        self.touch_calls.append((ids, kwargs))


@pytest.mark.asyncio
async def test_structured_recall_returns_bounded_provenance_without_touch(monkeypatch):
    manager = FakeBucketManager()
    monkeypatch.setattr(rt, "config", {"buckets_dir": "/vault/resident-a"})
    monkeypatch.setattr(rt, "version", "2.test")
    monkeypatch.setattr(rt, "bucket_mgr", manager)
    monkeypatch.setattr(rt, "embedding_engine", FakeEmbedding())
    monkeypatch.setattr(rt, "embedding_outbox", None)
    monkeypatch.setattr(rt, "logger", FakeLogger())

    result = await recall_structured.dispatch(
        "home",
        tags="home",
        max_results=5,
        max_tokens=3500,
        date_from="2026-08-01",
        date_to="2026-08-01",
    )

    assert result["schema"] == "ombre-structured-recall-v1"
    assert result["semantic_mutation_permitted"] is False
    assert result["count"] == 1
    assert result["items"][0]["bucket_id"] == "bucket-1"
    assert result["items"][0]["source"]["vault_binding"] == result["vault_binding"]
    assert result["items"][0]["unresolved"] is True
    assert len(result["items"][0]["digest"]) == 64
    assert len(result["result_digest"]) == 64
    assert manager.touch_calls == []


class JSONRPCApp:
    def __init__(self):
        self.calls = []

    async def __call__(self, scope, receive, send):
        request = await receive()
        payload = json.loads(request["body"])
        self.calls.append(payload)
        if payload["method"] == "tools/list":
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "tools": [
                            {"name": "hold"},
                            {"name": "recall_contract"},
                            {"name": "recall_structured"},
                        ]
                    },
                }
            ).encode()
        else:
            body = json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": {}}).encode()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": body})


def scope(body, token="read-key"):
    return {
        "type": "http",
        "method": "POST",
        "scheme": "https",
        "path": "/mcp",
        "headers": [
            (b"host", b"ombre.example"),
            (b"authorization", f"Bearer {token}".encode()),
        ],
        "body": json.dumps(body).encode(),
    }


async def receive_from_scope(value):
    return {"type": "http.request", "body": value["body"], "more_body": False}


def collect(messages):
    async def send(message):
        messages.append(message)
    return send


@pytest.mark.asyncio
async def test_read_token_lists_only_read_protocol_tools():
    app = JSONRPCApp()
    middleware = MCPAuthMiddleware(
        app,
        auth_required=True,
        token_validator=lambda *_args, **_kwargs: False,
        read_only_token_validator=lambda token, **_kwargs: token == "read-key",
        read_only_tools=recall_structured.READ_ONLY_TOOL_NAMES,
    )
    request = scope({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    messages = []

    await middleware(request, lambda: receive_from_scope(request), collect(messages))

    response = json.loads(messages[-1]["body"])
    assert [tool["name"] for tool in response["result"]["tools"]] == [
        "recall_contract",
        "recall_structured",
    ]


@pytest.mark.asyncio
async def test_read_token_rejects_write_before_handler():
    app = JSONRPCApp()
    middleware = MCPAuthMiddleware(
        app,
        auth_required=True,
        token_validator=lambda *_args, **_kwargs: False,
        read_only_token_validator=lambda token, **_kwargs: token == "read-key",
        read_only_tools=recall_structured.READ_ONLY_TOOL_NAMES,
    )
    request = scope(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "hold", "arguments": {"content": "must not write"}},
        }
    )
    messages = []

    await middleware(request, lambda: receive_from_scope(request), collect(messages))

    assert app.calls == []
    assert messages[0]["status"] == 403
    assert json.loads(messages[-1]["body"])["error"]["code"] == -32003
