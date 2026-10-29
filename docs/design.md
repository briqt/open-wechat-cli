# Design notes

## Scope

`open-wechat-cli` is a read-only local-history reader. The first release targets Windows Enterprise WeChat (WXWork) 5.x and exposes a CLI plus a thin Agent Skill description.

The release is a compatibility prototype, not a promise of support for every client version, architecture, database layout, or Windows security configuration.

## Runtime pipeline

```text
locate
  └─ discover WXWork installation, process, account directories, and database inventory
key scan
  └─ read candidate bytes from WXWork.exe and validate candidates against encrypted page 1
snapshot
  └─ copy database and WAL with shared-read handles into a user-selected work directory
 decrypt
  └─ decrypt pages, replay committed WAL frames, run SQLite quick_check, then publish output
query/export
  └─ open decrypted databases read-only and return paginated JSON/text results
```

### Read-only boundary

The live client process and its database files are read-only inputs. Generated snapshots, decrypted databases, key records, manifests, and exports are written only below the output/cache paths selected by the caller. The default key record is a local application-state file and contains sensitive material; it is never printed unless the caller explicitly requests `--reveal-key`.

The implementation does not attach a debugger, inject code, hook the target, write the target process, change the client installation, or send messages.

## Format adapter

The current adapter implements the observed WXWork 5.x wxSQLite3-style pager:

- 4096-byte pages;
- raw 16-byte key;
- page key `MD5(raw_key || page_no_le32 || "sAlT")`;
- page-number-derived IV;
- special page-one layout with a plaintext header fragment;
- plaintext WAL header and encrypted WAL pages.

Page-one validation, SQLite `quick_check`, and committed-WAL replay are correctness gates. A format mismatch must produce an error instead of a plausible-looking database.

All format-specific assumptions belong in `codec.py`; query code should not depend on cipher details.

## Public command contract

- stdout contains successful results;
- stderr contains diagnostics; in `--json` mode diagnostics are JSON lines and the final error line is a compact JSON error envelope;
- errors have stable `code`, user-actionable `repair`, and process exit codes;
- list commands use opaque cursors;
- timestamps include an explicit timezone offset;
- account selection is explicit in live-reading commands, with a documented recent-activity fallback.

The JSON envelope is versioned with `schema: 1`. Adding fields is preferred over changing existing field meanings; incompatible changes require a schema version or a major release decision.

## Compatibility policy

The only live verification recorded for this release is Windows WXWork 5.0.13.6005, PE32, with 4096-byte databases. A new client version must pass, at minimum:

1. installation/process/account discovery;
2. page-one key verification;
3. one small database decrypt and `quick_check`;
4. WAL replay validation;
5. message schema discovery and a bounded query.

A failed page-one or SQLite gate is a compatibility failure, not permission to emit unverified data.

## Security and privacy expectations

The repository contains source, tests, public documentation, and no live snapshots, decrypted databases, keys, process dumps, or real exports. Runtime data is ignored by Git. Public releases must be checked for account identifiers, key material, chat text, host paths, and private project documents before publication.

The tool is designed for local history access. Sending, account mutation, and other external side effects are outside this release.

## Known trade-offs

- Shared-read copying avoids elevation but cannot guarantee a point-in-time snapshot while a database is being written. The manifest records source changes, and `quick_check` gates publication. An administrator-provided VSS workflow remains an operational enhancement rather than a default dependency.
- Message pagination is bounded and cursor-based, but deep scans can still be expensive on very large databases.
- Media files, recall reconstruction, and structured rich-card fields are not part of this release.
- macOS Enterprise WeChat and personal WeChat use separate adapters and are not claimed as supported here.
