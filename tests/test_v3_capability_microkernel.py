from __future__ import annotations

import pytest

from ombrebrain.capabilities.catalog import register_foundation_capabilities
from ombrebrain.kernel.context import OmbreContext
from ombrebrain.kernel.errors import PolicyViolation
from ombrebrain.kernel.registry import CapabilityRegistry
from ombrebrain.microkernel import CapabilityMicrokernel, CapabilityRequest


def _registry() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    register_foundation_capabilities(registry)
    return registry


def test_capability_microkernel_authorizes_allowed_dispatch() -> None:
    context = OmbreContext(request_id="r1", actor_name="codex", permissions=("tools:search",))
    request = CapabilityRequest(name="tools.search", payload={"query": "x"}, context=context)

    decision = CapabilityMicrokernel(_registry()).authorize(request)

    assert decision.allowed is True
    assert decision.name == "tools.search"
    assert decision.side_effect_mode == "read_only"
    assert decision.missing_permissions == ()


def test_capability_microkernel_denies_missing_permissions() -> None:
    context = OmbreContext(request_id="r1", actor_name="codex", permissions=())
    request = CapabilityRequest(name="tools.breath", payload={"query": "x"}, context=context)

    decision = CapabilityMicrokernel(_registry()).authorize(request)

    assert decision.allowed is False
    assert decision.side_effect_mode == "write_side_channel"
    assert "tools:breath" in decision.missing_permissions
    assert "memory:write" in decision.missing_permissions
    assert "buckets" in decision.protected_surfaces


def test_capability_microkernel_dispatch_preserves_handler_payload_for_allowed_request() -> None:
    context = OmbreContext(request_id="r1", actor_name="codex", permissions=("tools:search",))
    request = CapabilityRequest(name="tools.search", payload={"query": "x"}, context=context)

    result = CapabilityMicrokernel(_registry()).dispatch(request)

    assert result["name"] == "tools.search"
    assert result["payload"] == {"query": "x"}


def test_capability_microkernel_dispatch_raises_policy_violation_for_denied_request() -> None:
    context = OmbreContext(request_id="r1", actor_name="codex", permissions=())
    request = CapabilityRequest(name="tools.search", payload={"query": "x"}, context=context)

    with pytest.raises(PolicyViolation):
        CapabilityMicrokernel(_registry()).dispatch(request)


