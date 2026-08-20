<!--
SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
SPDX-FileContributor: Lorenzo Massaro
SPDX-License-Identifier: AGPL-3.0-only
-->

# Connect Cursor

Cursor should connect to AGVM through the same local stdio MCP bridge used by
other clients.

## 1. Start AGVM

```bash
docker compose up --build
```

Confirm `http://127.0.0.1:8010/health` returns `ok: true`.

## 2. Configure Cursor

Use these values in Cursor's MCP configuration UI or JSON config:

- command: `python`
- args: `-m`, `agvm_mcp_server`
- working directory: `<path-to-agvm-core>`
- API URL: `http://127.0.0.1:8010`
- brain policy: `ai_create_if_missing` for a dedicated Cursor memory brain, or
  `fixed` when you want to lock Cursor to a known brain id.
- module visibility policy: `hide_unlicensed`.

The working directory is required because Cursor launches a new process and
Python must be able to import `agvm_mcp_server`.

## 3. Restart And Verify

After restart, ask Cursor to call `get_agvm_usage_guide`. If that tool is
missing, check the working directory, Python path and `AGVM_API_BASE_URL`.

For normal local memory usage, allow `read_only`, `read_only_export`,
`registry_write`, `preview_only` and `explicit_apply`, while keeping
`destructive` blocked.

`hide_unlicensed` keeps Core memory tools visible and keeps advanced Detwin
Cloud module tools out of the local tool list.
