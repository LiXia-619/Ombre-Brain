"""Testable HTTP application and runtime lifecycle assembly.

This module deliberately has no Ombre engine construction at import time. The
CLI entry point creates the concrete services, then passes them into the small
factory and lifecycle objects below. A future desktop host can use the same
boundary without importing the side-effectful ``server`` module.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping
import httpx
from starlette.middleware.cors import CORSMiddleware

from ombrebrain.security.public_origin import (
    configured_public_origin,
    normalize_public_origin,
)
from utils import parse_bool
from web.request_limits import (
    MCPRequestBodyLimitMiddleware,
    ManagementRequestBodyLimitMiddleware,
    is_mcp_endpoint_path,
)


DEFAULT_MAX_MCP_REQUEST_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_MANAGEMENT_REQUEST_BYTES = 4 * 1024 * 1024
DEFAULT_HEALTH_PROBE_TIMEOUT_SECONDS = 5.0
DEFAULT_KEEPALIVE_INITIAL_DELAY_SECONDS = 10.0
DEFAULT_KEEPALIVE_INTERVAL_SECONDS = 60.0

TokenValidator = Callable[..., bool]
AsyncCallback = Callable[[], Awaitable[Any]]


@dataclass(frozen=True)
class HTTPRuntimeSettings:
    """Normalized settings used while assembling an HTTP MCP application."""

    auth_required: bool
    max_request_bytes: int
    max_management_request_bytes: int = DEFAULT_MAX_MANAGEMENT_REQUEST_BYTES
    # "oauth"（默认）、"token" 或 "hybrid"；仅在 auth_required=True 时生效。
    auth_mode: str = "oauth"
    # Canonical external origin captured from the same startup config snapshot
    # used by OAuth route registration.  An empty value means request-derived
    # trusted-proxy/Host fallback.
    public_origin: str = ""
    # O2-I defaults the separate organ-read surface closed even when a token
    # happens to exist in the process environment.
    read_only_enabled: bool = False

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
        *,
        default_max_request_bytes: int = DEFAULT_MAX_MCP_REQUEST_BYTES,
    ) -> "HTTPRuntimeSettings":
        limits = config.get("limits")
        if not isinstance(limits, Mapping):
            limits = {}
        def body_limit(key: str, default: int) -> int:
            try:
                value = int(limits.get(key, default))
            except (TypeError, ValueError, OverflowError):
                return default
            return default if value < 0 else value

        max_request_bytes = body_limit(
            "max_mcp_request_bytes", default_max_request_bytes
        )
        max_management_request_bytes = body_limit(
            "max_management_request_bytes",
            DEFAULT_MAX_MANAGEMENT_REQUEST_BYTES,
        )
        auth_mode = str(config.get("mcp_auth_mode", "oauth")).strip().lower()
        if auth_mode not in ("oauth", "token", "hybrid"):
            auth_mode = "oauth"
        return cls(
            auth_required=parse_bool(
                config.get("mcp_require_auth", True), default=True
            ),
            max_request_bytes=max_request_bytes,
            max_management_request_bytes=max_management_request_bytes,
            auth_mode=auth_mode,
            public_origin=configured_public_origin(config),
            read_only_enabled=parse_bool(
                config.get("mcp_read_enabled", False), default=False
            ),
        )


@dataclass(frozen=True)
class MCPReadExposurePreflight:
    """Sanitized O2-I startup verdict; contains no token, path, or memory text."""

    decision: str
    reason_codes: tuple[str, ...]
    binding_digest: str | None
    exact_tools: tuple[str, ...]
    zero_side_effects: bool
    rollback_ready: bool

    @property
    def go(self) -> bool:
        return self.decision == "GO"


@dataclass(frozen=True)
class MCPReadDrillPreflight:
    """Sanitized O2-J one-shot authorization verdict."""

    decision: str
    reason_codes: tuple[str, ...]
    authorization_digest: str | None
    expires_at: str | None
    max_structured_recalls: int

    @property
    def go(self) -> bool:
        return self.decision == "GO"


def assess_mcp_read_drill(
    read_exposure: MCPReadExposurePreflight,
    config: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> MCPReadDrillPreflight:
    """Require a short-lived, exact one-shot authorization on top of O2-I."""

    reasons: set[str] = set()
    enabled = parse_bool(config.get("mcp_read_drill_enabled", False), default=False)
    authorization_digest = str(
        config.get("mcp_read_drill_authorization_digest", "") or ""
    ).strip()
    expires_text = str(config.get("mcp_read_drill_expires_at", "") or "").strip()
    try:
        max_recalls = int(config.get("mcp_read_drill_max_recalls", 1))
    except (TypeError, ValueError, OverflowError):
        max_recalls = 0
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    expires = _parse_utc_instant(expires_text)

    if not enabled:
        reasons.add("drill-disabled")
    if not read_exposure.go:
        reasons.add("o2i-read-exposure-not-ready")
    if not re.fullmatch(r"[a-f0-9]{64}", authorization_digest):
        reasons.add("authorization-digest-invalid")
    if expires is None:
        reasons.add("authorization-expiry-invalid")
    elif expires <= current:
        reasons.add("authorization-expired")
    elif expires > current + timedelta(minutes=15):
        reasons.add("authorization-window-too-wide")
    if max_recalls != 1:
        reasons.add("single-recall-required")

    ordered = tuple(sorted(reasons))
    return MCPReadDrillPreflight(
        decision="GO" if not ordered else "NO-GO",
        reason_codes=ordered,
        authorization_digest=authorization_digest if not ordered else None,
        expires_at=(expires.isoformat().replace("+00:00", "Z") if expires and not ordered else None),
        max_structured_recalls=1,
    )


class MCPReadDrillGuard:
    """Atomically consume the only O2-J structured recall before dispatch."""

    def __init__(
        self,
        preflight: MCPReadDrillPreflight,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not preflight.go or not preflight.authorization_digest or not preflight.expires_at:
            raise ValueError("O2-J drill guard requires a GO preflight")
        expires = _parse_utc_instant(preflight.expires_at)
        if expires is None:
            raise ValueError("O2-J drill guard expiry is invalid")
        self.authorization_digest = preflight.authorization_digest
        self.expires_at = expires
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._consumed = False
        self._revoked = False
        self._lock = threading.Lock()

    def consume_recall(self) -> bool:
        with self._lock:
            current = self._now().astimezone(timezone.utc)
            if self._revoked or self._consumed or current >= self.expires_at:
                return False
            self._consumed = True
            return True

    def revoke(self) -> None:
        with self._lock:
            self._revoked = True


def _parse_utc_instant(value: str) -> datetime | None:
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def assess_mcp_read_exposure(
    settings: HTTPRuntimeSettings,
    config: Mapping[str, Any],
    *,
    environment: Mapping[str, str] | None = None,
    read_only_tools: frozenset[str] = frozenset(),
) -> MCPReadExposurePreflight:
    """Fail closed before mounting the O2-A read credential surface."""

    env = os.environ if environment is None else environment
    reasons: set[str] = set()
    expected_tools = frozenset({"recall_contract", "recall_structured"})
    read_token = str(env.get("OMBRE_MCP_READ_TOKEN", "") or "").strip()
    full_tokens = {
        token
        for token in (
            str(env.get("OMBRE_MCP_TOKEN", "") or "").strip(),
            str(config.get("mcp_token", "") or "").strip(),
        )
        if token
    }
    vault_id = str(config.get("vault_id", "") or "").strip()
    vault_id_is_opaque = (
        bool(vault_id)
        and vault_id not in {".", ".."}
        and not vault_id.startswith("~")
        and "/" not in vault_id
        and "\\" not in vault_id
        and not (len(vault_id) >= 2 and vault_id[0].isalpha() and vault_id[1] == ":")
    )

    if not settings.read_only_enabled:
        reasons.add("master-switch-disabled")
    if not settings.auth_required:
        reasons.add("mcp-auth-required")
    if str(config.get("transport", "")).strip() != "streamable-http":
        reasons.add("streamable-http-required")
    if not vault_id_is_opaque:
        reasons.add("opaque-vault-binding-required")
    if len(read_token) < 32:
        reasons.add("read-credential-missing-or-short")
    if read_token and read_token in full_tokens:
        reasons.add("read-credential-reuses-full-token")
    if read_only_tools != expected_tools:
        reasons.add("read-tool-surface-mismatch")

    binding_digest = (
        hashlib.sha256(vault_id.encode("utf-8")).hexdigest()
        if vault_id_is_opaque
        else None
    )
    ordered_reasons = tuple(sorted(reasons))
    return MCPReadExposurePreflight(
        decision="GO" if not ordered_reasons else "NO-GO",
        reason_codes=ordered_reasons,
        binding_digest=binding_digest,
        exact_tools=("recall_contract", "recall_structured"),
        zero_side_effects=True,
        rollback_ready=True,
    )


def _first_forwarded_value(value: str) -> str:
    return value.split(",", 1)[0].strip()


def _header_text(headers: Mapping[bytes, bytes], name: bytes) -> str:
    try:
        return headers.get(name, b"").decode("latin-1").strip()
    except (AttributeError, UnicodeError):
        return ""


def _scope_peer_host(scope: Mapping[str, Any]) -> str:
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client:
        return str(client[0] or "").strip()
    return ""


def _scope_peer_is_trusted_proxy(scope: Mapping[str, Any]) -> bool:
    """Use the Dashboard's existing trusted-proxy CIDR policy.

    Keeping one policy avoids accepting forwarding headers here while rejecting
    them in login rate limiting and secure-cookie detection (or vice versa).
    The import is lazy so importing this testable assembly module remains free
    of Dashboard runtime initialization.
    """

    from web._shared import _is_trusted_proxy

    return _is_trusted_proxy(_scope_peer_host(scope))


def _normalize_http_origin(value: str) -> str | None:
    """Return a canonical HTTP(S) origin, or ``None`` when malformed."""

    return normalize_public_origin(value, allow_mcp_endpoint=False) or None


def _request_base(scope: Mapping[str, Any], headers: Mapping[bytes, bytes]) -> str:
    proto = str(scope.get("scheme", "http")).strip().lower()
    if proto not in ("http", "https"):
        proto = "http"
    host = _first_forwarded_value(_header_text(headers, b"host"))

    if _scope_peer_is_trusted_proxy(scope):
        forwarded_proto = _first_forwarded_value(
            _header_text(headers, b"x-forwarded-proto")
        ).lower()
        if forwarded_proto in ("http", "https"):
            proto = forwarded_proto
        forwarded_host = _first_forwarded_value(
            _header_text(headers, b"x-forwarded-host")
        )
        if forwarded_host:
            host = forwarded_host

    return _normalize_http_origin(f"{proto}://{host}") or ""


def _canonical_mcp_base(
    scope: Mapping[str, Any],
    headers: Mapping[bytes, bytes],
    public_origin: str,
) -> str:
    """Use the configured public origin, then the trusted request fallback."""

    return normalize_public_origin(public_origin) or _request_base(scope, headers)


def _extract_bearer_token(value: str) -> str:
    """Parse an RFC 7235 auth scheme (scheme names are case-insensitive)."""

    parts = str(value or "").strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()


class MCPAuthMiddleware:
    """Require a bearer token for every endpoint of the selected MCP transport."""

    def __init__(
        self,
        app: Any,
        *,
        auth_required: bool,
        token_validator: TokenValidator,
        static_token_validator: TokenValidator | None = None,
        read_only_token_validator: TokenValidator | None = None,
        read_only_tools: frozenset[str] = frozenset(),
        read_only_drill_guard: MCPReadDrillGuard | None = None,
        auth_mode: str = "oauth",
        path_matcher: Callable[[object], bool] = is_mcp_endpoint_path,
        resource_path: str = "/mcp",
        public_origin: str = "",
    ) -> None:
        self.app = app
        self.auth_required = bool(auth_required)
        self.token_validator = token_validator
        self.static_token_validator = static_token_validator
        self.read_only_token_validator = read_only_token_validator
        self.read_only_tools = frozenset(read_only_tools)
        self.read_only_drill_guard = read_only_drill_guard
        self.auth_mode = (
            auth_mode if auth_mode in ("oauth", "token", "hybrid") else "oauth"
        )
        self.path_matcher = path_matcher
        self.resource_path = "/" + str(resource_path or "mcp").strip("/")
        self.public_origin = normalize_public_origin(public_origin)

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        path = str(scope.get("path", ""))
        if (
            scope.get("type") == "http"
            and str(scope.get("method", "")).upper() != "OPTIONS"
            and self.auth_required
            and self.path_matcher(path)
        ):
            headers = {key.lower(): value for key, value in scope.get("headers", [])}
            auth = headers.get(b"authorization", b"").decode("latin-1")
            base = _canonical_mcp_base(scope, headers, self.public_origin)
            # OAuth discovery currently exposes one canonical MCP resource.
            resource = f"{base}{self.resource_path}"
            bearer_token = _extract_bearer_token(auth)
            valid = False
            read_only = False
            if bearer_token:
                primary_valid = bool(
                    self.token_validator(bearer_token, resource=resource)
                )
                static_valid = False
                if self.auth_mode == "hybrid" and self.static_token_validator:
                    # 两个校验器都执行后再合并结果，避免凭响应时延泄露 Token 类型。
                    static_valid = bool(
                        self.static_token_validator(bearer_token, resource=resource)
                    )
                valid = primary_valid | static_valid
                if self.read_only_token_validator:
                    read_only = bool(
                        self.read_only_token_validator(bearer_token, resource=resource)
                    )
                    valid = valid | read_only
            if not valid and self.auth_mode in ("token", "hybrid"):
                # Fallback header for MCP clients that can't customize Authorization.
                alt_token = headers.get(b"ombre-mcp-token", b"").decode(
                    "latin-1"
                ).strip()
                if alt_token:
                    static_validator = self.static_token_validator
                    if static_validator is None and self.auth_mode == "token":
                        # 保留旧调用方只注入一个 validator 的兼容行为。
                        static_validator = self.token_validator
                    valid = bool(static_validator) and static_validator(
                        alt_token, resource=resource
                    )
            if not valid:
                endpoint = self.resource_path.strip("/")
                if self.auth_mode == "token":
                    # No OAuth server exists in token mode — a resource_metadata
                    # challenge pointing at a 404'd discovery endpoint would mislead.
                    challenge = 'Bearer realm="Ombre Brain"'
                    body = json.dumps({"error": "Unauthorized"}).encode()
                else:
                    metadata_url = (
                        f"{base}/.well-known/oauth-protected-resource/{endpoint}"
                    )
                    challenge = (
                        'Bearer realm="Ombre Brain",'
                        f' resource_metadata="{metadata_url}", scope="mcp"'
                    )
                    body = json.dumps(
                        {
                            "error": "Unauthorized",
                            "resource_metadata": metadata_url,
                        }
                    ).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            [b"content-type", b"application/json"],
                            [b"www-authenticate", challenge.encode()],
                            [b"content-length", str(len(body)).encode()],
                        ],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": body,
                        "more_body": False,
                    }
                )
                return
            if self.read_only_drill_guard is not None and not read_only:
                await self._send_read_only_denial(
                    send,
                    None,
                    "one-shot drill process accepts only the read-only credential",
                )
                return
            if read_only:
                await self._call_read_only(scope, receive, send)
                return
        await self.app(scope, receive, send)

    async def _call_read_only(self, scope: dict, receive: Any, send: Any) -> None:
        chunks: list[bytes] = []
        while True:
            message = await receive()
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        try:
            request = json.loads(body)
        except (TypeError, ValueError, UnicodeDecodeError):
            await self._send_read_only_denial(send, None, "invalid JSON-RPC request")
            return
        method = request.get("method") if isinstance(request, dict) else None
        allowed_methods = {"initialize", "notifications/initialized", "ping", "tools/list"}
        if method == "tools/call":
            params = request.get("params") if isinstance(request.get("params"), dict) else {}
            tool_name = params.get("name")
            if tool_name not in self.read_only_tools:
                await self._send_read_only_denial(
                    send, request.get("id"), "read-only credential cannot call this tool"
                )
                return
            if (
                tool_name == "recall_structured"
                and self.read_only_drill_guard is not None
                and not self.read_only_drill_guard.consume_recall()
            ):
                await self._send_read_only_denial(
                    send, request.get("id"), "one-shot drill authorization unavailable"
                )
                return
        elif method not in allowed_methods:
            await self._send_read_only_denial(
                send, request.get("id") if isinstance(request, dict) else None,
                "read-only credential cannot use this MCP method",
            )
            return

        sent: list[dict] = []

        async def capture(message: dict) -> None:
            sent.append(message)

        delivered = False

        async def replay() -> dict:
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, capture)
        if method == "tools/list":
            response_body = b"".join(
                message.get("body", b"")
                for message in sent
                if message.get("type") == "http.response.body"
            )
            try:
                response = json.loads(response_body)
                tools = response["result"]["tools"]
                response["result"]["tools"] = [
                    tool for tool in tools if tool.get("name") in self.read_only_tools
                ]
                filtered = json.dumps(response, ensure_ascii=False).encode("utf-8")
            except (KeyError, TypeError, ValueError, UnicodeDecodeError):
                await self._send_read_only_denial(send, request.get("id"), "invalid tools/list response")
                return
            for message in sent:
                if message.get("type") == "http.response.start":
                    headers = [
                        header for header in message.get("headers", [])
                        if header[0].lower() != b"content-length"
                    ]
                    headers.append((b"content-length", str(len(filtered)).encode()))
                    await send({**message, "headers": headers})
                elif message.get("type") == "http.response.body":
                    if response_body:
                        await send({"type": "http.response.body", "body": filtered, "more_body": False})
                        response_body = b""
                else:
                    await send(message)
            return
        for message in sent:
            await send(message)

    @staticmethod
    async def _send_read_only_denial(send: Any, request_id: Any, reason: str) -> None:
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32003, "message": reason},
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})


class MCPJSONAcceptShim:
    """为省略或通配 ``Accept`` 的 Streamable HTTP 客户端选择 JSON。

    FastMCP 会按 MCP 规范拒绝没有明确声明响应媒体类型的 POST。部分简化
    客户端只发送浏览器/HTTP 库默认的 ``*/*``，虽然它在 HTTP 语义上本就
    接受 JSON，却会因此在 ``tools/list`` 前收到 406。这里只把“缺失”补成
    JSON、把通配范围补充为 JSON；显式只接受 SSE 等媒体类型时保持原样，
    避免向客户端返回它没有声明能够解析的响应。
    """

    def __init__(
        self,
        app: Any,
        *,
        path_matcher: Callable[[object], bool] = is_mcp_endpoint_path,
    ) -> None:
        self.app = app
        self.path_matcher = path_matcher

    @staticmethod
    def _wildcard_allows_json(media_range: bytes) -> bool:
        parts = [part.strip().lower() for part in media_range.split(b";")]
        if not parts or parts[0] not in (b"*/*", b"application/*"):
            return False
        for parameter in parts[1:]:
            key, separator, value = parameter.partition(b"=")
            if separator and key.strip() == b"q":
                try:
                    quality = float(value.strip())
                except ValueError:
                    return False
                return 0 < quality <= 1
        return True

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and self.path_matcher(
            scope.get("path")
        ):
            headers = list(scope.get("headers", []))
            accept_values = [
                value
                for key, value in headers
                if key.lower() == b"accept"
            ]
            combined = b", ".join(accept_values).strip()
            raw_media_ranges = [
                item.strip()
                for item in combined.lower().split(b",")
                if item.strip()
            ]
            media_ranges = [
                item.split(b";", 1)[0].strip()
                for item in raw_media_ranges
            ]
            has_json = b"application/json" in media_ranges
            accepts_wildcard = any(
                self._wildcard_allows_json(item)
                for item in raw_media_ranges
            )

            if not combined or (accepts_wildcard and not has_json):
                normalized = (
                    b"application/json"
                    if not combined
                    else combined + b", application/json"
                )
                headers = [
                    (key, value)
                    for key, value in headers
                    if key.lower() != b"accept"
                ]
                headers.append((b"accept", normalized))
                scope = dict(scope)
                scope["headers"] = headers

        await self.app(scope, receive, send)


class OriginCSRFGuardMiddleware:
    """Reject cross-origin state-changing requests to the cookie-session surface.

    CORS below is wide open (``allow_origins=["*"]``) only so browser-based MCP
    clients (e.g. claude.ai) can call ``/mcp`` cross-origin with a Bearer token.
    That has no business relaxing the Origin for the cookie-session dashboard
    (``/auth/*``, ``/api/*``, ...): a POST/PUT/DELETE whose ``Origin`` doesn't
    match its own ``Host`` has no legitimate reason to mutate password/session
    state there, no matter what the CORS preflight allowed. ``/mcp``,
    ``/oauth/*`` and ``/.well-known/*`` are exempt — they authenticate via
    bearer tokens / proof-of-possession (PKCE), not ambient cookies, so a
    mismatched Origin there isn't a CSRF risk.
    """

    _SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
    _EXEMPT_PREFIXES = ("/oauth/", "/.well-known/")

    def __init__(
        self,
        app: Any,
        *,
        mcp_path_matcher: Callable[[object], bool] = is_mcp_endpoint_path,
        public_origin: str = "",
    ) -> None:
        self.app = app
        self.mcp_path_matcher = mcp_path_matcher
        # Optional second proof for browsers/proxies that strip Fetch Metadata:
        # the persisted external origin is operator-controlled and shared with
        # OAuth/MCP resource binding. It never overrides an explicit cross-site
        # Fetch Metadata signal below.
        self.public_origin = normalize_public_origin(public_origin)

    def _is_exempt(self, path: str) -> bool:
        if self.mcp_path_matcher(path):
            return True
        return any(path.startswith(prefix) for prefix in self._EXEMPT_PREFIXES)

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", ""))
        if method in self._SAFE_METHODS or self._is_exempt(path):
            await self.app(scope, receive, send)
            return
        raw_headers = [
            (key.lower(), value) for key, value in scope.get("headers", [])
        ]
        headers = dict(raw_headers)
        origin = _header_text(headers, b"origin")
        fetch_site = _header_text(headers, b"sec-fetch-site").lower()
        singular_security_headers = {
            b"origin",
            b"host",
            b"sec-fetch-site",
            b"forwarded",
            b"x-forwarded-for",
            b"x-forwarded-host",
            b"x-forwarded-proto",
        }
        duplicate_security_header = any(
            sum(1 for key, _value in raw_headers if key == name) > 1
            for name in singular_security_headers
        )
        normalized_origin = _normalize_http_origin(origin) if origin else None
        # Browsers do not let page JavaScript forge Sec-Fetch-Site.  Its
        # same-origin value therefore remains useful when a reverse proxy
        # rewrites Host/scheme but omits X-Forwarded-Host/Proto.  Missing,
        # navigation-only ("none"), and unknown values retain the Origin
        # fallback for older clients; non-same-origin browser contexts fail
        # closed even if they omit Origin.
        reject = (
            duplicate_security_header
            or (bool(origin) and normalized_origin is None)
            or fetch_site in ("same-site", "cross-site")
        )
        if fetch_site != "same-origin" and not reject and origin:
            own_base = _request_base(scope, headers)
            allowed_origins = {own_base}
            if self.public_origin:
                allowed_origins.add(self.public_origin)
            reject = (
                normalized_origin is None or normalized_origin not in allowed_origins
            )
        if reject:
            body = json.dumps({"error": "Cross-origin request rejected"}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [
                        [b"content-type", b"application/json"],
                        [b"content-length", str(len(body)).encode()],
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body, "more_body": False})
            return
        await self.app(scope, receive, send)


class NgrokHeaderMiddleware:
    """Stamp every response with ``ngrok-skip-browser-warning`` (issue #16).

    Deployments tunneled through ngrok's free tier serve an HTML browser-
    warning interstitial instead of proxying to the origin. ngrok recognizes
    this header (request *or* response side) as an explicit opt-out for
    non-browser clients, which is what an MCP client like claude.ai always
    is. Applied unconditionally so it also lands on auth-rejected and
    error responses, not just successful tool calls.
    """

    _HEADER_NAME = b"ngrok-skip-browser-warning"
    _HEADER_VALUE = b"true"

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_header(message: dict) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                if not any(key.lower() == self._HEADER_NAME for key, _ in headers):
                    headers.append((self._HEADER_NAME, self._HEADER_VALUE))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_header)


class SecurityHeadersMiddleware:
    """Apply browser hardening headers to success and error responses."""

    _HEADERS = (
        (b"content-security-policy", b"frame-ancestors 'none'"),
        (b"x-frame-options", b"DENY"),
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"no-referrer"),
        (
            b"permissions-policy",
            b"camera=(), geolocation=(), microphone=(), payment=(), usb=()",
        ),
    )

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: dict) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {key.lower() for key, _value in headers}
                headers.extend(
                    (key, value)
                    for key, value in self._HEADERS
                    if key not in existing
                )
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


@dataclass
class RuntimeLifecycle:
    """Own background service startup and shutdown for one HTTP app lifespan."""

    logger: Any
    read_only_drill: bool = False
    decay_engine: Any = None
    embedding_outbox: Any = None
    ensure_ollama_child: AsyncCallback | None = None
    stop_ollama_child: AsyncCallback | None = None
    load_tunnel_config: Callable[[], Mapping[str, Any]] | None = None
    start_tunnel: Callable[[str], tuple[bool, str]] | None = None
    stop_tunnel: Callable[[], Any] | None = None
    restart_github_auto_task: Callable[[int], Any] | None = None
    github_auto_interval: int = 0
    boot_marker_path: str = ""
    keepalive_url: str = ""
    keepalive_initial_delay: float = DEFAULT_KEEPALIVE_INITIAL_DELAY_SECONDS
    keepalive_interval: float = DEFAULT_KEEPALIVE_INTERVAL_SECONDS
    health_probe_timeout: float = DEFAULT_HEALTH_PROBE_TIMEOUT_SECONDS
    _keepalive_task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _started: bool = field(default=False, init=False, repr=False)

    async def _run_async_step(self, label: str, callback: AsyncCallback | None) -> None:
        if callback is None:
            return
        try:
            await callback()
        except Exception as exc:
            self.logger.warning("%s failed: %s", label, exc)

    def _start_optional_services(self) -> None:
        if self.load_tunnel_config is not None and self.start_tunnel is not None:
            try:
                tunnel_config = self.load_tunnel_config()
                if tunnel_config.get("auto_start") and tunnel_config.get("token"):
                    _ok, message = self.start_tunnel(str(tunnel_config["token"]))
                    self.logger.info("Tunnel auto-start: %s", message)
            except Exception as exc:
                self.logger.warning("tunnel auto-start failed: %s", exc)

        if self.github_auto_interval > 0 and self.restart_github_auto_task is not None:
            try:
                self.restart_github_auto_task(self.github_auto_interval)
            except Exception as exc:
                self.logger.warning("github auto-sync start failed: %s", exc)

    def _reset_boot_marker(self) -> None:
        if not self.boot_marker_path or not os.path.exists(self.boot_marker_path):
            return
        try:
            with open(self.boot_marker_path, "w", encoding="utf-8") as marker:
                marker.write("0")
            self.logger.info("boot ok -> reset .boot_fails")
        except Exception as exc:
            self.logger.warning("reset .boot_fails failed: %s", exc)

    async def _keepalive_loop(self) -> None:
        await asyncio.sleep(max(0.0, self.keepalive_initial_delay))
        async with httpx.AsyncClient() as client:
            while True:
                try:
                    await client.get(
                        self.keepalive_url,
                        timeout=self.health_probe_timeout,
                    )
                    self.logger.debug("Keepalive ping OK")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.logger.warning("Keepalive ping failed: %s", exc)
                await asyncio.sleep(max(0.01, self.keepalive_interval))

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        if self.read_only_drill:
            self.logger.info(
                "O2-J read-only lifecycle active; all background writers are disabled"
            )
            return
        self._start_optional_services()
        await self._run_async_step(
            "decay engine start",
            getattr(self.decay_engine, "start", None),
        )
        await self._run_async_step("ollama child boot", self.ensure_ollama_child)
        await self._run_async_step(
            "embedding outbox start",
            getattr(self.embedding_outbox, "start", None),
        )
        if self.keepalive_url:
            self._keepalive_task = asyncio.create_task(
                self._keepalive_loop(),
                name="ombre-health-keepalive",
            )
        self._reset_boot_marker()

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        if self.read_only_drill:
            return

        task = self._keepalive_task
        self._keepalive_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        if self.restart_github_auto_task is not None:
            try:
                self.restart_github_auto_task(0)
            except Exception as exc:
                self.logger.warning("github auto-sync stop failed: %s", exc)

        await self._run_async_step(
            "embedding outbox stop",
            getattr(self.embedding_outbox, "stop", None),
        )
        await self._run_async_step(
            "decay engine stop",
            getattr(self.decay_engine, "stop", None),
        )
        await self._run_async_step("ollama child stop", self.stop_ollama_child)
        if self.stop_tunnel is not None:
            try:
                self.stop_tunnel()
            except Exception as exc:
                self.logger.warning("tunnel stop failed: %s", exc)


def install_runtime_lifespan(app: Any, lifecycle: RuntimeLifecycle) -> Any:
    """Compose Ombre runtime services with an app's existing lifespan."""

    parent_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def managed_lifespan(lifespan_app: Any):
        async with parent_lifespan(lifespan_app):
            await lifecycle.start()
            try:
                yield
            finally:
                await lifecycle.stop()

    app.router.lifespan_context = managed_lifespan
    return app


def build_http_app(
    mcp: Any,
    transport: str,
    *,
    settings: HTTPRuntimeSettings,
    token_validator: TokenValidator,
    lifecycle: RuntimeLifecycle,
    static_token_validator: TokenValidator | None = None,
    read_only_token_validator: TokenValidator | None = None,
    read_only_tools: frozenset[str] = frozenset(),
    read_only_drill_guard: MCPReadDrillGuard | None = None,
) -> Any:
    """Build the HTTP (streamable-http) ASGI app with one consistent middleware stack."""

    if transport == "streamable-http":
        app = mcp.streamable_http_app()
    else:
        raise ValueError(f"HTTP app cannot be built for transport: {transport}")

    mcp_path_matcher = is_mcp_endpoint_path

    install_runtime_lifespan(app, lifecycle)
    app.add_middleware(
        OriginCSRFGuardMiddleware,
        mcp_path_matcher=mcp_path_matcher,
        public_origin=settings.public_origin,
    )
    app.add_middleware(
        MCPRequestBodyLimitMiddleware,
        max_bytes=settings.max_request_bytes,
        path_matcher=mcp_path_matcher,
    )
    app.add_middleware(
        ManagementRequestBodyLimitMiddleware,
        max_bytes=settings.max_management_request_bytes,
        mcp_path_matcher=mcp_path_matcher,
    )
    if transport == "streamable-http":
        app.add_middleware(
            MCPJSONAcceptShim,
            path_matcher=mcp_path_matcher,
        )
    app.add_middleware(
        MCPAuthMiddleware,
        auth_required=settings.auth_required,
        token_validator=token_validator,
        static_token_validator=static_token_validator,
        read_only_token_validator=read_only_token_validator,
        read_only_tools=read_only_tools,
        read_only_drill_guard=read_only_drill_guard,
        auth_mode=settings.auth_mode,
        path_matcher=mcp_path_matcher,
        resource_path="/mcp",
        public_origin=settings.public_origin,
    )
    # Starlette wraps middleware in reverse registration order.  CORS must be
    # outside MCP auth so browser preflights never receive a bare 401 and auth
    # challenges/errors still carry the appropriate CORS response headers.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        # 携带凭据时，部分浏览器不把通配符视为可读响应头。
        # 显式暴露 MCP/OAuth 排障需要的字段。
        expose_headers=[
            "Mcp-Session-Id",
            "WWW-Authenticate",
            "Ngrok-Skip-Browser-Warning",
        ],
    )
    # Outermost: must still fire on auth-rejected/error responses, not just
    # successful tool calls, so add it last (see NgrokHeaderMiddleware).
    app.add_middleware(NgrokHeaderMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.state.ombre_http_settings = settings
    app.state.ombre_runtime_lifecycle = lifecycle
    return app
