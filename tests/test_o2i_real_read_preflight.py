import json

from server_app import HTTPRuntimeSettings, assess_mcp_read_exposure
from tools import recall_structured
from web import oauth as oauth_mod


READ_TOKEN = "o2i-read-token-not-real-000000000000000000000000"
FULL_TOKEN = "o2i-full-token-not-real-000000000000000000000000"


def config(**patch):
    value = {
        "transport": "streamable-http",
        "mcp_require_auth": True,
        "mcp_auth_mode": "hybrid",
        "mcp_token": FULL_TOKEN,
        "mcp_read_enabled": True,
        "vault_id": "opaque-owner-a-vault",
    }
    value.update(patch)
    return value


def test_o2i_defaults_closed_even_when_a_read_token_exists():
    value = config(mcp_read_enabled=False)
    report = assess_mcp_read_exposure(
        HTTPRuntimeSettings.from_config(value),
        value,
        environment={"OMBRE_MCP_READ_TOKEN": READ_TOKEN},
        read_only_tools=recall_structured.READ_ONLY_TOOL_NAMES,
    )
    assert report.decision == "NO-GO"
    assert report.reason_codes == ("master-switch-disabled",)


def test_o2i_go_report_is_sanitized_and_exactly_two_tool_read_only():
    value = config()
    report = assess_mcp_read_exposure(
        HTTPRuntimeSettings.from_config(value),
        value,
        environment={
            "OMBRE_MCP_READ_TOKEN": READ_TOKEN,
            "OMBRE_MCP_TOKEN": FULL_TOKEN,
        },
        read_only_tools=recall_structured.READ_ONLY_TOOL_NAMES,
    )
    assert report.go is True
    assert report.exact_tools == ("recall_contract", "recall_structured")
    assert report.zero_side_effects is True
    assert report.rollback_ready is True
    serialized = json.dumps(report.__dict__, sort_keys=True)
    assert READ_TOKEN not in serialized
    assert FULL_TOKEN not in serialized
    assert "opaque-owner-a-vault" not in serialized


def test_o2i_rejects_no_auth_unsafe_binding_short_or_reused_credentials():
    cases = [
        (config(mcp_require_auth=False), {"OMBRE_MCP_READ_TOKEN": READ_TOKEN}),
        (config(vault_id=""), {"OMBRE_MCP_READ_TOKEN": READ_TOKEN}),
        (config(vault_id="/private/real-vault"), {"OMBRE_MCP_READ_TOKEN": READ_TOKEN}),
        (config(vault_id=r"C:\\private\\real-vault"), {"OMBRE_MCP_READ_TOKEN": READ_TOKEN}),
        (config(), {"OMBRE_MCP_READ_TOKEN": "short"}),
        (
            config(mcp_token=READ_TOKEN),
            {"OMBRE_MCP_READ_TOKEN": READ_TOKEN},
        ),
        (
            config(mcp_token=READ_TOKEN),
            {
                "OMBRE_MCP_READ_TOKEN": READ_TOKEN,
                "OMBRE_MCP_TOKEN": FULL_TOKEN,
            },
        ),
    ]
    for value, environment in cases:
        report = assess_mcp_read_exposure(
            HTTPRuntimeSettings.from_config(value),
            value,
            environment=environment,
            read_only_tools=recall_structured.READ_ONLY_TOOL_NAMES,
        )
        assert report.decision == "NO-GO"


def test_o2i_rejects_any_widened_tool_surface():
    value = config()
    report = assess_mcp_read_exposure(
        HTTPRuntimeSettings.from_config(value),
        value,
        environment={"OMBRE_MCP_READ_TOKEN": READ_TOKEN},
        read_only_tools=frozenset({"recall_contract", "recall_structured", "hold"}),
    )
    assert report.reason_codes == ("read-tool-surface-mismatch",)


def test_o2i_live_switch_revokes_the_read_validator_without_exposing_values(monkeypatch):
    monkeypatch.setattr(oauth_mod.sh, "config", {"mcp_read_enabled": True})
    monkeypatch.setenv("OMBRE_MCP_READ_TOKEN", READ_TOKEN)
    monkeypatch.setenv("OMBRE_MCP_READ_ENABLED", "true")
    assert oauth_mod._is_valid_read_only_mcp_token(READ_TOKEN) is True
    monkeypatch.setenv("OMBRE_MCP_READ_ENABLED", "false")
    assert oauth_mod._is_valid_read_only_mcp_token(READ_TOKEN) is False
