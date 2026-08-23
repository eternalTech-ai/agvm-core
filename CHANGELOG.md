<!--
SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
SPDX-FileContributor: Lorenzo Massaro
SPDX-License-Identifier: AGPL-3.0-only
-->

# Changelog

All notable AGVM Core public releases will be tracked here.

## Unreleased

- Prepare AGVM Core public repository metadata, policies and documentation.
- Document local MCP setup, local brain bootstrap and explicit Brain Sync
  boundaries.
- Keep Grow free in Local Core and expose the complete versioned MCP contract
  catalog so AI clients can plan against stable contracts.
- Add bounded Brain Bootstrap V1 and fixed-12D Brain Profile V1 public
  contracts, including shadow preview, benchmark gates and rollback metadata.
- Document the real-node-only Brain Core visualization and its strict source
  boundary from the proprietary Cloud cockpit.
- Block paid-module MCP calls before local API execution when no valid module
  access is present, returning a structured Detwin Cloud action contract.
- Make the exported test suite, source-boundary scanner, SPDX policy, secret
  scan and Docker smoke reproducible from the public repository alone.

## 0.1.0

- First public tag placeholder. Do not tag until export, license, secret scan,
  CI and maintainer approval gates pass.
