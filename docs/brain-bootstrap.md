<!--
SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
SPDX-FileContributor: Lorenzo Massaro
SPDX-License-Identifier: AGPL-3.0-only
-->

# Brain Bootstrap

AGVM Core needs an active local brain before Context, Grow and MCP calls can
produce useful results. A fresh checkout should make brain creation explicit
instead of failing with a generic request error.

## Bootstrap Paths

- UI: open the brain selector, create a new local brain or import an existing
  sanitized export.
- MCP: set `AGVM_MCP_BRAIN_POLICY=ai_create_if_missing` so the bridge can create
  a scoped brain for the client.
- API: use the public brain registry endpoints exposed by the local runtime.

## Recommended First Brain

Use a descriptive brain name and purpose:

```bash
AGVM_MCP_BRAIN_DISPLAY_NAME="Project Memory"
AGVM_MCP_BRAIN_PURPOSE="Persistent local MCP memory for this project."
```

Avoid personal data in names if screenshots, logs or issue reports may be
shared publicly.

## Import Safety

Only import brain archives you trust. Treat local brain exports as sensitive:
they can contain project history, prompts, documents, source paths and private
notes. Do not attach raw brain exports to public issues.

## Switching Brains

The Core UI should expose brain switching in the top bar on every primary
surface. Switching a brain changes the scope for Context, Grow, Health and MCP
inspection. It does not upload data to Detwin Cloud.
