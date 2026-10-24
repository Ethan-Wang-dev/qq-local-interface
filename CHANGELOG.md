# Changelog

## 0.2.1 - 2026-10-23

### Fixes

- Bound decoded card JSON by depth and node count independently of Python's JSON decoder recursion limits. Deep or wide payloads retain exact raw access instead of breaking later serialization or indexing.
- Bump the normalizer revision so existing snapshots rebuild their message index with the same bounds.

### Validation

- Add an interpreter-independent regression for accepted shallow cards and oversized trees; the synthetic suite now contains 56 tests.
- The initial GitHub CI passed Python 3.10 and exposed the deep-card edge case on Python 3.12. This patch fixes that case without replacing the published v0.2.0 tag.

## 0.2.0 - 2026-10-23

First public release. This publishes the existing 0.2.0 implementation; no earlier version was published from this repository.

### Features

- Read authenticated macOS QQ database keys from an explicitly selected, logged-in account without saving a heap dump.
- Decrypt detached SQLCipher pages, preserve SQLite reserved space, and replay authenticated committed WAL frames.
- Publish immutable, account-isolated database and media snapshots with durable change cursors.
- Provide Python, CLI, versioned JSON/NDJSON, and MCP stdio interfaces for local messages, contacts, conversations, members, raw tables, large cells, assets, and exports.
- Preserve exact SQLite storage types and original binary/text bytes; expose unknown message fields through lossless raw access.
- Allow explicit database exclusions so optional channel databases do not block the selected scope.
- Expose locally cached favorite summaries with separate detail availability; full online favorite retrieval remains deferred.

### Fixes

- Classify image, file-preview, video, sticker, quote, card, and system elements by their native QQNT element types.
- Read group names from the correct native column and provide complete pagination for local actors, conversations, and member observations.
- Detach cached asset bytes so later QQ cache changes do not alter an existing snapshot.

### Validation and scope

- 55 synthetic regression tests passed on the development Mac, including real MCP stdio transport.
- The real-account audit compared every selected table and cell, all cached asset bytes, and complete table exports; see `docs/coverage.md` for dated counts and limitations.
- Validated client: Intel macOS 12.7.4, QQ 6.9.85 (36362). Apple Silicon and other QQ versions have not been validated.
- Channel content and online favorite full text are outside the current accepted scope. Uncached remote media and deleted history are not reconstructed.
- Third-party method references and retained MIT notices are included in both distribution archives.
