# Changelog

## 0.1.0a2 — 2026-10-29

- Establish the complete source distribution under the MIT License, with installation, development and release instructions.
- Add Windows/Linux Python CI, build checks and repository/distribution boundary tests.
- Include the CLI, tests, Agent Skill and public documents in reproducible source builds; keep generated artifacts out of Git.
- Clarify local key storage, shared-read copy consistency limits, and supported client scope.
- Use source tags as the release entry point; wheels remain buildable locally rather than a default Release attachment.

## 0.1.0a1 — 2026-10-29

- Initial Windows WXWork local-history reader: discovery, key verification, database copy/decode, session/contact/message queries and JSONL/TXT export.
- JSON result envelopes, structured runtime errors, cursors and explicit timestamps.
- Offline synthetic tests and a live compatibility observation for WXWork 5.0.13.6005 / PE32 / 4096-byte pages.
