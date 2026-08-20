<!--
SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
SPDX-FileContributor: Lorenzo Massaro
SPDX-License-Identifier: AGPL-3.0-only
-->

# Connect Claude

AGVM Core can be launched by Claude Desktop as a stdio MCP server.

## 1. Start AGVM

```bash
docker compose up --build
```

Confirm `http://127.0.0.1:8010/health` returns `ok: true`.

## 2. Add The MCP Server

Use the AGVM Core checkout as `cwd`. That is how Claude can import and launch
`agvm_mcp_server`.

```json
{
  "mcpServers": {
    "agvm-local-memory-os": {
      "command": "python",
      "args": ["-m", "agvm_mcp_server"],
      "cwd": "<path-to-agvm-core>",
      "env": {
        "AGVM_API_BASE_URL": "http://127.0.0.1:8010",
        "AGVM_MCP_BRAIN_POLICY": "ai_create_if_missing",
        "AGVM_MCP_BRAIN_DISPLAY_NAME": "Claude Project Memory",
        "AGVM_MCP_BRAIN_PURPOSE": "Persistent MCP memory for this Claude workspace.",
        "AGVM_MCP_READ_ONLY": "false",
        "AGVM_MCP_ALLOWED_PERMISSION_FAMILIES": "read_only,read_only_export,registry_write,preview_only,explicit_apply",
        "AGVM_MCP_BLOCKED_PERMISSION_FAMILIES": "destructive",
        "AGVM_MCP_MODULE_VISIBILITY_POLICY": "hide_unlicensed"
      }
    }
  }
}
```

## 3. Restart And Verify

Restart Claude after editing the config and ask it to call
`get_agvm_usage_guide`. If that tool is missing, check the `cwd`, Python import
path and `AGVM_API_BASE_URL`.

Keep provider API keys in `.env` or backend-managed environment storage unless
you intentionally want Claude's MCP child process to receive the secret.

`AGVM_MCP_MODULE_VISIBILITY_POLICY=hide_unlicensed` keeps Core memory tools
visible and keeps advanced Detwin Cloud module tools out of the local tool list.
