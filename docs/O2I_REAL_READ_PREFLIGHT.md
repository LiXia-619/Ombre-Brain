# O2-I real-organ read preflight

O2-A's separate organ-read credential surface is default closed. A configured
`OMBRE_MCP_READ_TOKEN` does not expose anything unless all startup checks pass:

- `OMBRE_MCP_READ_ENABLED=true` is explicit;
- MCP authentication is enabled and transport is `streamable-http`;
- `OMBRE_VAULT_ID` is a stable opaque binding, not a filesystem path;
- the read token is long and distinct from every full-access static token;
- the mounted allowlist is exactly `recall_contract` and `recall_structured`.

The startup verdict contains only fixed reason codes, a binding digest, and
boolean exposure facts. It never contains token values, vault paths, queries,
memory text, or provider error strings. `NO-GO` means the read-only validator is
not mounted. Setting `OMBRE_MCP_READ_ENABLED=false` also makes the validator
reject immediately; restart then removes the surface from assembly.

This preflight does not deploy O2-A and does not authorize connection to a real
vault, credential, memory set, resident, room checkpoint, or any write, touch,
dream, or reflect path.
