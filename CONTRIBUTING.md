<!--
SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
SPDX-FileContributor: Lorenzo Massaro
SPDX-License-Identifier: AGPL-3.0-only
-->

# Contributing

AGVM Core is planned as an open-core project. Public contributions should target
the core runtime, public MCP contracts, local setup, documentation, tests and
SDK-facing interfaces.

Do not submit paid module code, private cloud infrastructure, customer data,
local brain exports, benchmark artifacts or secrets.

## Development Rules

- keep public APIs documented and tested;
- update public docs when behavior changes;
- keep private module imports out of public core paths;
- add focused tests for boundary changes;
- never commit `.env`, local brain data, logs or generated artifacts.

## Pull Requests

- Open pull requests against `preprod` unless a maintainer asks for another
  branch.
- `main` is protected and requires passing CI plus maintainer approval.
- External contributors should work from forks.
- Maintainers merge only after public/private boundary and license checks pass.

## Licensing

By submitting a contribution, you agree that it is licensed under
`AGPL-3.0-only` for AGVM Core and that you have the right to submit it. Do not
submit code copied from private AGVM modules, proprietary platform code or
third-party sources whose license is incompatible with this repository.
