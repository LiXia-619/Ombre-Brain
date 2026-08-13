import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from server_app import (
    MCPAuthMiddleware,
    MCPReadDrillGuard,
    MCPReadExposurePreflight,
    RuntimeLifecycle,
    assess_mcp_read_drill,
)
from embedding_engine import EmbeddingEngine
from tools import _runtime as rt
from tools import recall_structured


NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
AUTHORIZATION_DIGEST = "a" * 64
READ_TOKEN = "o2j-read-token-not-real-000000000000000000000000"


def read_exposure(go=True):
    return MCPReadExposurePreflight(
        decision="GO" if go else "NO-GO",
        reason_codes=() if go else ("master-switch-disabled",),
        binding_digest="b" * 64 if go else None,
        exact_tools=("recall_contract", "recall_structured"),
        zero_side_effects=True,
        rollback_ready=True,
    )


def drill_config(**patch):
    value = {
        "mcp_read_drill_enabled": True,
        "mcp_read_drill_authorization_digest": AUTHORIZATION_DIGEST,
        "mcp_read_drill_expires_at": (NOW + timedelta(minutes=10)).isoformat().replace(
            "+00:00", "Z"
        ),
        "mcp_read_drill_max_recalls": 1,
    }
    value.update(patch)
    return value


def test_o2j_preflight_requires_o2i_and_one_short_lived_exact_authorization():
    report = assess_mcp_read_drill(read_exposure(), drill_config(), now=NOW)
    assert report.go is True
    assert report.authorization_digest == AUTHORIZATION_DIGEST
    assert report.max_structured_recalls == 1

    cases = [
        (read_exposure(False), drill_config(), "o2i-read-exposure-not-ready"),
        (read_exposure(), drill_config(mcp_read_drill_enabled=False), "drill-disabled"),
        (
            read_exposure(),
            drill_config(mcp_read_drill_authorization_digest="not-a-digest"),
            "authorization-digest-invalid",
        ),
        (
            read_exposure(),
            drill_config(mcp_read_drill_expires_at=(NOW - timedelta(seconds=1)).isoformat()),
            "authorization-expired",
        ),
        (
            read_exposure(),
            drill_config(mcp_read_drill_expires_at=(NOW + timedelta(minutes=16)).isoformat()),
            "authorization-window-too-wide",
        ),
        (
            read_exposure(),
            drill_config(mcp_read_drill_max_recalls=2),
            "single-recall-required",
        ),
    ]
    for exposure, config, code in cases:
        rejected = assess_mcp_read_drill(exposure, config, now=NOW)
        assert rejected.go is False
        assert code in rejected.reason_codes
        assert rejected.authorization_digest is None


def test_o2j_contract_attests_only_the_digest_one_call_and_expiry(monkeypatch):
    config = {
        **drill_config(),
        "vault_id": "opaque-owner-vault",
        "buckets_dir": "/private/path-must-not-appear",
    }
    monkeypatch.setattr(rt, "config", config)
    monkeypatch.setattr(rt, "version", "2.9.1-o2j")
    value = recall_structured.contract()
    assert value["drill"] == {
        "schema": "ombre-read-drill-attestation-v1",
        "authorization_digest": AUTHORIZATION_DIGEST,
        "max_structured_recalls": 1,
        "expires_at": config["mcp_read_drill_expires_at"],
    }
    serialized = json.dumps(value, sort_keys=True)
    assert "/private/path-must-not-appear" not in serialized
    assert READ_TOKEN not in serialized


class RecordingApp:
    def __init__(self):
        self.calls = 0

    async def __call__(self, scope, receive, send):
        self.calls += 1
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})


async def call_middleware(middleware, request, *, token=READ_TOKEN):
    body = json.dumps(request).encode()
    delivered = False
    messages = []

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    await middleware(
        {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "scheme": "https",
            "client": ("127.0.0.1", 12345),
            "headers": [
                (b"host", b"memory.invalid"),
                (b"authorization", f"Bearer {token}".encode()),
            ],
        },
        receive,
        send,
    )
    return messages


@pytest.mark.asyncio
async def test_o2j_middleware_consumes_the_only_recall_before_dispatch():
    preflight = assess_mcp_read_drill(read_exposure(), drill_config(), now=NOW)
    guard = MCPReadDrillGuard(preflight, now=lambda: NOW)
    downstream = RecordingApp()
    middleware = MCPAuthMiddleware(
        downstream,
        auth_required=True,
        token_validator=lambda *_args, **_kwargs: False,
        read_only_token_validator=lambda token, **_kwargs: token == READ_TOKEN,
        read_only_tools=frozenset({"recall_contract", "recall_structured"}),
        read_only_drill_guard=guard,
        auth_mode="hybrid",
        public_origin="https://memory.invalid",
    )
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "recall_structured", "arguments": {"query": "private"}},
    }
    first = await call_middleware(middleware, request)
    second = await call_middleware(middleware, {**request, "id": 2})
    assert next(m for m in first if m["type"] == "http.response.start")["status"] == 204
    assert next(m for m in second if m["type"] == "http.response.start")["status"] == 403
    assert downstream.calls == 1
    assert AUTHORIZATION_DIGEST not in repr(second)


@pytest.mark.asyncio
async def test_o2j_process_rejects_a_valid_full_access_credential():
    full_token = "o2j-full-token-not-real-000000000000000000000000"
    preflight = assess_mcp_read_drill(read_exposure(), drill_config(), now=NOW)
    downstream = RecordingApp()
    middleware = MCPAuthMiddleware(
        downstream,
        auth_required=True,
        token_validator=lambda token, **_kwargs: token == full_token,
        read_only_token_validator=lambda token, **_kwargs: token == READ_TOKEN,
        read_only_tools=frozenset({"recall_contract", "recall_structured"}),
        read_only_drill_guard=MCPReadDrillGuard(preflight, now=lambda: NOW),
        auth_mode="token",
        public_origin="https://memory.invalid",
    )
    response = await call_middleware(
        middleware,
        {"jsonrpc": "2.0", "id": 9, "method": "tools/list"},
        token=full_token,
    )
    assert next(m for m in response if m["type"] == "http.response.start")[
        "status"
    ] == 403
    assert downstream.calls == 0


@pytest.mark.asyncio
async def test_o2j_runtime_lifecycle_starts_no_background_service():
    events = []

    class Service:
        async def start(self):
            events.append("start")

        async def stop(self):
            events.append("stop")

    class Logger:
        def info(self, *_args):
            return None

    lifecycle = RuntimeLifecycle(
        logger=Logger(),
        read_only_drill=True,
        decay_engine=Service(),
        embedding_outbox=Service(),
        restart_github_auto_task=lambda interval: events.append(f"github:{interval}"),
        github_auto_interval=5,
    )
    await lifecycle.start()
    await lifecycle.stop()
    assert events == []


def test_o2j_embedding_index_is_prebuilt_immutable_and_write_guarded(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    db = vault / "embeddings.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE embeddings (bucket_id TEXT PRIMARY KEY, embedding TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, content_hash TEXT NOT NULL DEFAULT '', "
            "meaning_embedding TEXT)"
        )
        conn.execute(
            "CREATE TABLE embeddings_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO embeddings_meta VALUES (?, ?)",
            (("model_name", "o2j-vector"), ("vector_dim", "2")),
        )
        conn.execute(
            "INSERT INTO embeddings VALUES (?, ?, ?, ?, NULL)",
            ("synthetic", "[1.0, 0.0]", "2026-08-13T00:00:00Z", "f" * 64),
        )
        conn.commit()
    finally:
        conn.close()
    before = db.read_bytes()
    engine = EmbeddingEngine(
        {
            "buckets_dir": str(vault),
            "mcp_read_drill_enabled": True,
            "embedding": {
                "enabled": True,
                "api_format": "local",
                "model": "o2j-vector",
                "dim": 2,
                "base_url": "http://127.0.0.1:9/v1",
            },
        }
    )
    assert engine.list_all_ids() == ["synthetic"]
    with pytest.raises(RuntimeError, match="forbids embedding index writes"):
        engine.delete_embedding("synthetic")
    with engine._connect_db() as read_connection:
        with pytest.raises(sqlite3.OperationalError):
            read_connection.execute("DELETE FROM embeddings")
    assert db.read_bytes() == before


def test_o2j_guard_denies_after_expiry_or_explicit_revocation():
    preflight = assess_mcp_read_drill(read_exposure(), drill_config(), now=NOW)
    expired = MCPReadDrillGuard(
        preflight,
        now=lambda: NOW + timedelta(minutes=11),
    )
    assert expired.consume_recall() is False

    revoked = MCPReadDrillGuard(preflight, now=lambda: NOW)
    revoked.revoke()
    assert revoked.consume_recall() is False
