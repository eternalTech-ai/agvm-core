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

## Bounded V1 Workflow

The V1 bootstrap contract is review-first:

1. start a scoped session for one selected brain;
2. answer bounded setup questions;
3. add manual source material;
4. preview reviewable candidates;
5. approve the exact candidate identifiers;
6. apply once with confirmation and an idempotency key;
7. resume, recover or cancel the session explicitly when required.

The MCP tools are `brain_bootstrap_start`, `brain_bootstrap_status`,
`brain_bootstrap_answer`, `brain_bootstrap_add_source`,
`brain_bootstrap_preview`, `brain_bootstrap_apply`,
`brain_bootstrap_resume`, `brain_bootstrap_recover` and
`brain_bootstrap_cancel`.

Interview answers and sources do not become memory before the explicit apply.
Manual interview and local Grow review are Core operations. The **AI interview**
option is also a Local Core workflow: it uses the provider key saved in Local
Settings to generate a bounded, attested question set, while the human still
answers, reviews and explicitly applies the resulting memories. It uses the
user's provider quota and does not consume Detwin credits.

The reviewed guided-seed preview also uses that local provider to decide the
semantic memory candidates and routing weights. Manual questions replace only
question generation; they do not turn semantic candidate formation into a
heuristic or provider-free process.

The Bootstrap capability named `ai_research` is different from the local AI
interview. `ai_research`, profile fitting, backfill and activation are
cloud-backed paid capabilities and return a structured action contract when
Detwin authority is absent.

If AI interview start returns `bootstrap_question_generation_unavailable`, add
and verify the provider key in Local Settings and retry. A
`platform_memory_credit_unavailable` response belongs to a hosted/cloud action;
it must not be required for local interview, local Grow review or local apply.
The same is true of `platform_memory_outbox_worker_unavailable`: it reports a
hosted persistence worker boundary, not a missing component of local Bootstrap.
For either error during local onboarding, verify that the UI/MCP client targets
the local API at `http://127.0.0.1:8010` and is not using Cloud runtime mode.

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

After the baseline brain is built, [Brain Profile V1](brain-profile-v1.md) can
preview a bounded personalization over the existing 12 routing dimensions.
