<!--
SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
SPDX-FileContributor: Lorenzo Massaro
SPDX-License-Identifier: AGPL-3.0-only
-->

# Connect Codex

Use this flow for a single Codex chat or project.

## 1. Start AGVM

Run the local stack first:

```bash
docker compose up --build
```

Confirm `http://127.0.0.1:8010/health` returns `ok: true`.

## 2. Add The MCP Server To Codex

Codex must launch the stdio bridge from the AGVM checkout. `cwd` is the AGVM
Core directory, not the Codex project directory. It lets Python import
`agvm_mcp_server`.

Example TOML:

```toml
[mcp_servers."agvm-local-memory-os"]
command = "python"
args = ["-m", "agvm_mcp_server"]
cwd = "<path-to-agvm-core>"
startup_timeout_sec = 30
tool_timeout_sec = 180

[mcp_servers."agvm-local-memory-os".env]
AGVM_API_BASE_URL = "http://127.0.0.1:8010"
AGVM_MCP_BRAIN_POLICY = "ai_create_if_missing"
AGVM_MCP_BRAIN_DISPLAY_NAME = "Codex Project Memory"
AGVM_MCP_BRAIN_PURPOSE = "Persistent MCP memory for this Codex project."
AGVM_MCP_READ_ONLY = "false"
AGVM_MCP_ALLOWED_PERMISSION_FAMILIES = "read_only,read_only_export,registry_write,preview_only,explicit_apply"
AGVM_MCP_BLOCKED_PERMISSION_FAMILIES = "destructive"
AGVM_MCP_MODULE_VISIBILITY_POLICY = "hide_unlicensed"
```

Do not put provider API keys in the MCP bridge config unless you intentionally
want that client process to own the secret. Prefer `.env` or backend-managed
environment storage.

## 3. Restart Codex

The server is loaded when Codex starts a new session. After restart, ask:

```text
First verify whether the AGVM MCP server is available.
If yes, call get_agvm_usage_guide and list the AGVM tools you see.
Do not modify files and do not call apply tools.
```

If Codex sees `get_agvm_usage_guide`, the MCP bridge is connected. If it does
not, check:

- `cwd` points to the AGVM Core checkout;
- Python can run `python -m agvm_mcp_server` from that directory;
- `AGVM_API_BASE_URL` points to the running API;
- Codex was restarted after editing the config.

## Permission Families

- `read_only`: recall and inspection;
- `read_only_export`: larger read/export payloads;
- `registry_write`: create/select/ensure brains;
- `preview_only`: non-mutating previews;
- `explicit_apply`: mutation tools that require explicit apply;
- `destructive`: destructive operations, blocked by default.

For normal local memory usage, include `explicit_apply` and keep `destructive`
blocked.

## Module Tool Visibility

`AGVM_MCP_MODULE_VISIBILITY_POLICY=hide_unlicensed` is the recommended local
default. Core memory tools remain visible. Advanced Clone, Teach and Maintain
tools are Detwin Cloud capabilities for this release and should not appear as
local installable tools in the public Core checkout.
