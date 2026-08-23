<!--
SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
SPDX-FileContributor: Lorenzo Massaro
SPDX-License-Identifier: AGPL-3.0-only
-->

# License And Notices

Status: AGPLv3 public-core license selected; locked third-party license metadata is inventoried.

Detwin AGVM Core is licensed under `AGPL-3.0-only` in the public `LICENSE` file.
The public SDK contract packages are intended to stay under `Apache-2.0` so external clients and module authors can integrate without inheriting the core runtime license.
Paid platform and Pro module implementations are not part of the public core and are distributed under a separate proprietary Eternal Tech SRL commercial license.

## What Is Ready

- the public export gate requires `LICENSE`, `THIRD_PARTY_NOTICES.md` and this guide;
- `LICENSE` contains the GNU Affero General Public License v3 text for the public core;
- third-party dependency licenses are inventoried in `THIRD_PARTY_NOTICES.md`;
- public contribution acceptance remains blocked until the contributor policy is finalized.

## What Is Not Ready

- commercial module license terms;
- CLA or DCO policy;
- Terms of Service, Privacy Policy and billing disclosures for hosted AGVM or Pro.

## License Locations

- Public core: `LICENSE`, SPDX `AGPL-3.0-only`.
- Design tokens: `packages/detwin-design-tokens/package.json`, SPDX `AGPL-3.0-only`.
- Python SDK metadata: `sdk/python/pyproject.toml`, SPDX `Apache-2.0`.
- TypeScript SDK metadata: `sdk/typescript/package.json`, SPDX `Apache-2.0`.
- Private modules/platform: proprietary commercial terms kept outside the public export.

To change the model later, update the relevant license file or package SPDX field, regenerate this notice pack, rerun the public export gate and document the decision in the owner guide before any repository publication.

## Release Gate


Publication requires the automated source-boundary, REUSE, secret-scanner, test and container-smoke gates plus the owner's release decision.
