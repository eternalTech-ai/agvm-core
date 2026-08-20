# License And Notices

Status: AGPL-3.0 public-core license selected; publication remains gated by notice and launch review.

Detwin AGVM Core is licensed under `AGPL-3.0-only` in the public `LICENSE` file.
The public core repository is released under `AGPL-3.0-only`. SDK contract licensing can be split later only after explicit legal/product review.
Paid platform and Pro module implementations are not part of the public core and are distributed under a separate proprietary Eternal Tech SRL commercial license.

## What Is Ready

- the public export gate requires `LICENSE`, `THIRD_PARTY_NOTICES.md` and this guide;
- `LICENSE` now contains the AGPL-3.0 text for the public core;
- third-party dependency licenses are inventoried in `THIRD_PARTY_NOTICES.md`;
- public contribution acceptance remains blocked until the contributor policy is finalized.

## What Is Not Ready

- commercial module license terms;
- CLA or DCO policy;
- Terms of Service, Privacy Policy and billing disclosures for hosted AGVM or Pro.
- final legal approval for third-party notices and launch documentation.

## License Locations

- Public core: `LICENSE`, SPDX `AGPL-3.0-only`.
- TypeScript SDK metadata included in the core snapshot: `sdk/typescript/package.json`, SPDX `AGPL-3.0-only`.
- Private modules/platform: proprietary commercial terms kept outside the public export.

To change the model later, update the relevant license file or package SPDX field, regenerate this notice pack, rerun the public export gate and document the decision in the owner guide before any repository publication.

## Current Release Blockers

- `third_party_notice_pack_legal_review_pending`
- `counsel_approval_required_before_public_push`

Do not push a public `agvm-core` repository until these blockers are cleared.
