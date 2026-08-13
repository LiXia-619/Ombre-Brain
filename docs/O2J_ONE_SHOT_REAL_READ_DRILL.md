# O2-J one-shot real read drill

O2-J is an optional, short-lived restriction layered on the O2-I two-tool read
surface. It does not make a normal read credential sufficient for a real drill.

The owner host must provide, outside source and ordinary configuration history:

- a 64-hex authorization digest binding two independent approvals, one fixed
  owner / organ / vault, one query digest, and the agreed result/token limits;
- an expiry that is still valid and no more than 15 minutes in the future;
- a maximum structured-recall count of exactly one.

When any field is missing or invalid, O2-A does not mount the read credential
surface. On success, `recall_contract` carries
`ombre-read-drill-attestation-v1`. The read-only middleware atomically consumes
the only `recall_structured` allowance before the memory handler runs. A failed,
expired, concurrent, or repeated call is denied and cannot be retried in the
same process.

Enabling O2-J also selects a dedicated read-only process lifecycle:

- the embedding database must already exist, contain at least one vector, and
  have model/dimension metadata matching the configured provider;
- SQLite opens it with `mode=ro`, `immutable=1`, and `query_only=ON`; schema
  migration, metadata repair, re-indexing, and every embedding write API fail
  closed;
- decay, embedding outbox, local-model child boot, tunnel auto-start,
  keepalive, GitHub auto-sync, and boot-marker mutation do not start;
- even an otherwise valid full-access credential is rejected by the MCP
  middleware while the one-shot guard exists.

The process therefore exposes only `recall_contract` and `recall_structured`;
no write, touch, dream, reflect, checkpoint, generic resource, or resident
switch is added. The owner host must still compare vault snapshots before,
immediately after, and after a short observation window. Completing a drill is
not resident migration or runtime admission.
