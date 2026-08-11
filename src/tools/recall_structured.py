"""Bounded, side-effect-free recall for organ-to-house integrations.

This module deliberately does not call ``touch()``, start decay, fire hooks, or
write audit state.  Transport authorization is enforced before this handler;
the response carries the protocol/vault binding needed by the caller to reject
stale or cross-vault material.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Optional

from ombrebrain.policy.surfacing import SurfacePolicyVM
from tools import _runtime as rt
from tools.breath.search import (
    _bucket_has_tags,
    _bucket_in_created_range,
    _is_archived,
    _parse_date_bound,
    _semantic_scores,
)
from tools.plan.core import is_letter_bucket
from utils import count_tokens_approx, parse_bool


READ_PROTOCOL_SCHEMA = "ombre-read-protocol-v1"
STRUCTURED_RECALL_SCHEMA = "ombre-structured-recall-v1"
READ_ONLY_TOOL_NAMES = frozenset({"recall_contract", "recall_structured"})
MAX_STRUCTURED_RESULTS = 20
MAX_STRUCTURED_TOKENS = 6000
_SURFACE_POLICY = SurfacePolicyVM.default()


def _vault_binding() -> str:
    configured = str(rt.config.get("vault_id") or "").strip()
    if configured:
        return configured
    vault_dir = str(rt.config.get("buckets_dir") or "").strip()
    return "vault-" + hashlib.sha256(vault_dir.encode("utf-8")).hexdigest()[:24]


def contract() -> dict[str, Any]:
    return {
        "schema": READ_PROTOCOL_SCHEMA,
        "protocol_version": 1,
        "organ": "ombre-brain",
        "organ_version": str(rt.version or "unknown"),
        "vault_binding": _vault_binding(),
        "capability": "memory-recall:read",
        "tools": sorted(READ_ONLY_TOOL_NAMES),
        "result_schema": STRUCTURED_RECALL_SCHEMA,
        "limits": {
            "max_results": MAX_STRUCTURED_RESULTS,
            "max_tokens": MAX_STRUCTURED_TOKENS,
        },
        "side_effects": {
            "vault_write": False,
            "touch": False,
            "dream": False,
            "reflect": False,
        },
    }


def _metadata(bucket: Mapping[str, Any]) -> Mapping[str, Any]:
    value = bucket.get("metadata")
    return value if isinstance(value, Mapping) else {}


def _eligible(bucket: Mapping[str, Any]) -> bool:
    if is_letter_bucket(dict(bucket)) or _is_archived(dict(bucket)):
        return False
    meta = _metadata(bucket)
    if str(meta.get("type") or "") in {"feel", "plan", "letter"}:
        return False
    return _SURFACE_POLICY.evaluate_bucket(dict(bucket), mode="search").allowed


def _item(bucket: Mapping[str, Any]) -> dict[str, Any]:
    meta = _metadata(bucket)
    content = bucket.get("content")
    if not isinstance(content, str):
        content = ""
    score = bucket.get("score", bucket.get("weight", 0.0))
    try:
        relevance = max(0.0, min(1.0, float(score)))
    except (TypeError, ValueError, OverflowError):
        relevance = 0.0
    item = {
        "bucket_id": str(bucket.get("id") or ""),
        "title": str(meta.get("title") or ""),
        "content": content,
        "source": {
            "kind": "ombre-bucket",
            "vault_binding": _vault_binding(),
        },
        "relevance": round(relevance, 6),
        "created": str(meta.get("created") or ""),
        "domains": [str(value) for value in (meta.get("domain") or [])],
        "tags": [str(value) for value in (meta.get("tags") or [])],
        "importance": int(meta.get("importance") or 0),
        "affect": {
            "valence": meta.get("valence"),
            "arousal": meta.get("arousal"),
        },
        "unresolved": not parse_bool(meta.get("resolved"), default=False),
        "protected": parse_bool(meta.get("protected"), default=False),
    }
    item["digest"] = hashlib.sha256(
        json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return item


async def dispatch(
    query: str,
    domain: Optional[str] = "",
    tags: Optional[str] = "",
    max_results: Optional[int] = 5,
    max_tokens: Optional[int] = 3500,
    date_from: Optional[str] = "",
    date_to: Optional[str] = "",
) -> dict[str, Any]:
    query = str(query or "").strip()
    if not query:
        raise ValueError("recall_structured requires a non-blank query")
    domain = str(domain or "")
    tag_filter = [value.strip() for value in str(tags or "").split(",") if value.strip()]
    result_limit = min(MAX_STRUCTURED_RESULTS, max(1, int(max_results or 5)))
    token_limit = min(MAX_STRUCTURED_TOKENS, max(1, int(max_tokens or 3500)))
    created_from = _parse_date_bound(str(date_from or ""), upper=False)
    created_to = _parse_date_bound(str(date_to or ""), upper=True)
    if created_from and created_to and created_from > created_to:
        raise ValueError("date_from cannot be later than date_to")

    vector_scores, semantic_notice = await _semantic_scores(
        query, top_k=max(result_limit, 20)
    )
    matches = await rt.bucket_mgr.search(
        query,
        limit=max(result_limit, 20),
        domain_filter=[value.strip() for value in domain.split(",") if value.strip()] or None,
        vector_scores=vector_scores,
        include_archive=False,
    )
    selected: list[dict[str, Any]] = []
    used_tokens = 0
    for bucket in matches:
        if not _eligible(bucket):
            continue
        meta = _metadata(bucket)
        if not _bucket_has_tags(dict(meta), tag_filter):
            continue
        if not _bucket_in_created_range(dict(bucket), created_from, created_to):
            continue
        candidate = _item(bucket)
        candidate_tokens = count_tokens_approx(
            json.dumps(candidate, ensure_ascii=False, sort_keys=True)
        )
        if used_tokens + candidate_tokens > token_limit:
            break
        selected.append(candidate)
        used_tokens += candidate_tokens
        if len(selected) >= result_limit:
            break

    payload: dict[str, Any] = {
        "schema": STRUCTURED_RECALL_SCHEMA,
        "protocol_version": 1,
        "organ_version": str(rt.version or "unknown"),
        "vault_binding": _vault_binding(),
        "query_digest": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "semantic_status": "degraded" if semantic_notice else "available",
        "items": selected,
        "count": len(selected),
        "estimated_tokens": used_tokens,
        "truncated": len(selected) >= result_limit or used_tokens >= token_limit,
        "semantic_mutation_permitted": False,
    }
    payload["result_digest"] = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return payload
