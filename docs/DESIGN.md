# drive-merge Design Document

**English** | [繁體中文](DESIGN.zh-TW.md)

> Status: v0.1 draft — the "Open Decisions" in Section 12 are where you can make adjustments; each one already has a default applied, so the code can run on those defaults for now.

## 0. One-Page Summary

drive-merge is a service that **runs locally**. It aggregates the **file metadata** (not the files themselves) of Google Drive, Dropbox, OneDrive, and iCloud Drive into a local database, and then:

1. answers "which clouds hold this file (content)?";
2. finds duplicates in a provable way (not just by matching names);
3. generates a "reclassification blueprint" from folder attributes and filenames, which can be proposed and adjusted from the CLI or by an LLM (Claude Code via MCP);
4. requires every action that changes a cloud to first become a **Plan**, which is **simulated (before/after)** in the database and must pass the safety invariants, and only then is executed after **human approval**; deletion always moves files to a quarantine area first (restorable);
5. ultimately consolidates, deduplicates, and centralizes all content into the one cloud you designate (default: Google Drive).

rclone handles all cloud I/O; drive-merge handles the catalog, identity determination, planning, simulation, and auditing.

## 1. Goals and Non-Goals

**Goals**
- A unified multi-cloud catalog with fast full-text search (including Chinese filenames).
- Cross-cloud content identity determination that distinguishes "proven identical" from "probably identical".
- A change workflow that is reviewable, reproducible, and restorable.
- A REST API, MCP (for LLMs), and CLI, plus a change feed so that external databases can sync incrementally.

**Non-goals (v1)**
- No two-way sync (this is not a Dropbox replacement).
- No image similarity (near-duplicate photos) — listed as a later phase.
- File contents are not stored in the database.

## 2. First Principles: The Facts That Shape the Architecture

| Fact | Consequence |
|---|---|
| **The four clouds share no common hash**: Drive = md5/sha1/sha256, Dropbox = dropbox content hash, OneDrive = QuickXorHash, iCloud = none (rclone v1.75 overview) | Cross-cloud deduplication cannot rely on comparing listings alone. We use **hash bridging**: download **only one copy** of a given content, compute all 5 hashes locally in a single pass, and then use each of the other clouds' native hashes to prove they are the same content, without downloading those copies. |
| "Different size ⇒ different content" | Size is a free first-level filter. Only files that have "the same size but sit in different hash domains" need bridging, so the download volume is far smaller than the total. |
| **Drive allows multiple files at the same path** | The identity of a catalog entry is the provider ID (`entry_key = id:<ID>`), not the path. Multiple files at the same path are flagged `AMBIGUOUS_PATH`, and no operation on them is allowed until the ambiguity is resolved. |
| Dropbox/OneDrive are **case-insensitive**; Drive/iCloud are case-sensitive | Moving across clouds can create case collisions; the simulator checks `CASE_COLLISION`. |
| macOS/iCloud filenames are often **Unicode NFD** | Comparisons use `name_norm = NFC + casefold`. |
| **Google native documents** (Docs/Sheets) have neither bytes nor a hash | Flagged `is_native_doc`; excluded from deduplication; copying or deleting across clouds is not allowed; moving within Drive is allowed. |
| **iCloud (rclone `iclouddrive`)** is an experimental backend: no hash, no server-side copy/move, the trust token expires after 30 days, Photos is read-only | iCloud is treated as a "source" rather than a target; its files can only be "probable duplicates" until bridging proves otherwise. See D7 for alternative ways to connect. |
| The Google Drive client_id shared by rclone will be **disabled during 2026** | You need to create your own Google OAuth client_id (the README has the steps). |
| **union merges by path**: when the same path exists on several clouds, only one copy is presented/read (default search policy `ff` = first responder wins) | union is **not suitable as the catalog's data source**: it hides *which clouds hold the file*, and when the same path has different contents the result is nondeterministic. The catalog is instead built by **scanning each remote one at a time**, and the unified view is built inside the database. For a single browsable/mountable view, use **combine** (each cloud becomes a top-level folder, preserving the source); `drivemerge rclone-combine` can generate the configuration. union is only useful in the final stage (for example, to provide a read-only merged view during migration). |
| Filenames are **untrusted input** | Cloud filenames may contain prompt injection aimed at LLMs. The LLM may only "propose/simulate"; approval must be performed by a human at a TTY (see §7). |

## 3. Architecture

```
        ┌────────────── You / Claude Code / other programs ──────────────┐
        │ CLI (drivemerge)    MCP server (stdio)    REST API (127.0.0.1) │
        └────────┬────────────────────┬──────────────────────┬───────────┘
                 │   (MCP has no approve/execute)            │
┌────────────────▼────────────────────▼──────────────────────▼───────────────────┐
│                         drivemerge core (stdlib only)                          │
│ scanner ─► catalog (SQLite + FTS5 trigram) ◄─ identity (union-find + bridges)  │
│ search / stats / change feed       classify (units, rules, profiles)           │
│ builders (dedupe / consolidate / reorganize) ─► plans ─► simulate              │
│                         approve (human, TTY) ─► executor ─► journal            │
└──┬─────────────────────────────────────────────────┬───────────────────────────┘
   │ read-only remote (scan / bridging download)     │ writable remote (executor only)
  rclone ──► Google Drive / Dropbox / OneDrive / iCloud
```

- **The core has zero third-party dependencies** (Python 3.11+ standard library): sqlite3, subprocess, tomllib. The API uses FastAPI and MCP uses the official `mcp` SDK, both as optional extras.
- **Least privilege**: each remote is configured as two rclone remotes, `read` and `write`. Scanning and bridging use only `read` (ideally created with a read-only OAuth scope, for example Drive `drive.readonly`), so the catalog stage **physically cannot modify any cloud**. Only the executor uses `write`; a cloud with no `write` configured is read-only.

## 4. Data Model (`src/drivemerge/schema.sql`)

| Table | Purpose |
|---|---|
| `remotes` | Mirrors the `[[remotes]]` entries in the config (provider, read/write remote, whether case-insensitive) |
| `scans` | One row per scan; only a `complete` scan marks files it did not see as tombstones |
| `entries` | One row per file/folder: path, name, `name_norm`, extension, category, size, mtime, MIME, provider ID, the 5 hashes, `content_group`, `present`, `row_version` |
| `entries_fts` | FTS5 **trigram** index (name, path): substring search with support for Chinese and Japanese; queries shorter than 3 characters fall back to LIKE |
| `hash_bridges` | The 5 hashes computed from a local download (one row = one content), linking different hash domains together |
| `plans` / `plan_ops` | Plans and their operations (copy / move / delete); immutable once written |
| `simulations` | The full report (JSON) and digest of each simulation |
| `journal` | Append-only audit log: approvals, every actual cloud operation, and errors |

**Incremental scanning**: upsert by `(remote_id, entry_key)`; `row_version` is incremented only on an "observable change", so rescanning an unchanged cloud produces no change events. When the content (size/hash) changes, `content_group` is cleared and awaits recomputation.

## 5. Content Identity and Deduplication

Confidence levels:

| Level | Determination | Can it justify deletion? |
|---|---|---|
| **exact** | Same size and sharing any hash value of the same type (transitively), or connected through `hash_bridges` | Yes |
| **probable** | Same size + same `name_norm`, but no hash proof (for example iCloud) | **No** (default); must first be upgraded to exact through bridging |
| (size-only) | Only the size matches | Only as a bridging candidate |

Algorithm: run union-find over live files, where the edges are identical `(hash type, value, size)` and all hashes joined by a bridge row; `content_group` = the smallest entry id in the connected component (deterministic). For millions of entries this is done by sorting in SQL and then merging linearly, with no O(n²) comparison.

**Bridging workflow** (read-only): `bridge_candidates` finds the classes that are "same size, spanning hash domains, not yet proven"; for each class it picks the cheapest copy to download, ranks the classes by "space saved = size × number of unresolved copies", and stays within the `--max-bytes` budget; then download → compute all 5 hashes locally in one pass → verify they match the file's native hash → write to `hash_bridges` → recompute the groups.

**Which copy to keep (keeper policy, D5)**: target cloud > the clouds named in `prefer_remotes` (in order) > paths that do not look like copies (`Copy of`, `(1)`, `conflicted copy`, `副本`…) > provider (drive > onedrive > dropbox > local > other > iclouddrive: copies with a native hash and server-side move are the cheapest to verify and the cheapest to keep, while iCloud copies, which can only be proven through bridging, rank last) > shallower path > earlier mtime > smaller entry id.

## 6. Reclassification and Organization

Observation: **classifying by file extension alone destroys context** (the PDFs and images in a project folder should stay together). So the blueprint operates on "units":

1. **Atomic folders (units)**: folders that contain markers such as `.git`, `package.json`, or `*.xcodeproj`, or that are themselves bundles (`*.pages`, `*.key`, `*.photoslibrary`…); they are moved as a whole, preserving their internal structure. The outermost one takes priority.
2. **Loose files**: placed one by one according to rules (TOML). A rule can match on `category`, `ext`, `path_glob`, `name_regex`, size, and source cloud; the first rule that matches wins. The target path is a template, for example `Photos/{year}/{year}-{month}/{name}`; the date is taken from the filename first (`IMG_20190304_…`, `2019-03-04 …`) and from mtime second.
3. Files that match no rule go to the `fallback` (default `Unsorted/{remote}/{path}`, preserving the original structure) — **when in doubt, leave things where they are**.

**The LLM's role: propose "rules", not per-file operations.** What the LLM sees are **folder profiles** (for each folder: file count, size, category distribution, top 5 extensions, date range, 5 sample filenames, whether it is a unit, duplicate ratio), not a million filenames. It proposes rules → the service applies them deterministically → produces a Plan → simulation → you review. The benefits: it scales, it is reproducible and auditable, and the LLM can neither see nor touch the clouds.

## 7. Safety Model: Plan → Simulate → Approve → Execute

**Any action that writes to a cloud can only come from an approved Plan.** There is no exceptional path.

### 7.1 Simulation (`simulate.py`)
Applies the ops in order onto the "catalog + an in-memory overlay" (nothing is written to the database) and checks:

| Invariant | Meaning |
|---|---|
| `CONTENT_LOST` | Any content that existed before the plan must retain at least `min_copies` copies after the plan. **Deleting the last copy = rejected.** |
| `NO_WITNESS` / `WITNESS_MISMATCH` | Every delete must name a "witness copy" whose content is **exactly** identical to the deleted file and which still exists when the whole plan has finished (this prevents the case where A is witnessed by B, B is witnessed by A, and both get deleted). |
| `DST_EXISTS` | Never overwrites different content; identical content is recorded as a NOOP warning. |
| `CASE_COLLISION` | A case collision on a case-insensitive cloud. |
| `AMBIGUOUS_PATH` | Multiple files at the same path on Drive; no operation is allowed. |
| `SRC_MISSING` / `EXPECT_MISMATCH` | The source does not exist or does not match the snapshot taken when the plan was created. |
| `NATIVE_DOC` | Native documents cannot be copied across clouds or deleted. |
| `READ_ONLY_REMOTE` / `CROSS_REMOTE_MOVE` / `BAD_PATH` / `DEPENDENCY` / `QUOTA` | Permissions, paths, dependency order, and capacity (note: the quarantine does not free capacity). |

The report includes per-cloud before/after file counts and bytes, cross-cloud transfer volume, content counts before/after, and change samples.

### 7.2 Approval
- Approval is bound to the **digest = sha256(plan id + all ops + catalog snapshot)**. Any rescan (which changes the snapshot) or re-simulation invalidates the approval.
- You must type the first 8 characters of the digest to confirm, and the **CLI accepts approval only in an interactive TTY**.
- The **MCP server provides no approve/execute tools**; the `.claude/settings.json` in the repo also denies Claude Code from running `drivemerge plan approve` / `execute` / `purge`.
- The master switch `safety.execution_enabled = false` (default); while it is off, the executor refuses to run.

### 7.3 Execution (`executor.py`)
- Op by op, stopping at the first error (fail-closed). Before each op the source is **stat'ed live**; if its size/hash does not match the plan snapshot, execution stops.
- **copy**: `rclone copyto --ignore-existing`, followed by verification of the destination; across hash domains, verification uses a bridge or downloads the destination and recomputes the hashes. **A delete that depends on the copy will not run until verification has passed.**
- **delete = move into the same cloud's quarantine** `.drive-merge-quarantine/<plan-id>/<original path>` (a server-side move, restorable; `drivemerge plan restore` restores it in one step). Actual deletion is a separate command, `purge`, which by default retains items for 30 days and likewise requires TTY approval.
- Every step is written to the `journal`.
- **Files inside the quarantine do not count as live content**: a rescan will see them, but identity determination (groups, duplicate lists, bridging candidates) and folder suggestions all skip them, so a copy that has been cleared out does not turn back into a "duplicate". They still count toward file counts and capacity. The quarantine folder name (`[safety] quarantine_dir`) is recorded by `sync_remotes` in the catalog's `meta`, so that modules handed only a connection can judge consistently.

### 7.4 LLMs and Prompt Injection
What an LLM (Claude Code, other chat interfaces) can do: search, view statistics and folder profiles, propose rules, create Plans, simulate, and explain reports. What it cannot do: approve, execute, or purge. Even if a filename in the cloud tries to mislead the LLM, the worst outcome is a **proposal**, which must still pass simulation and be approved by you at a TTY.

## 8. Consolidating into a Single Cloud (Default: Google Drive)

Why Drive suits as the target (D1): it is the only one of the four that provides standard hashes (md5/sha256), so it is **the best verification anchor**; it has server-side move; and native documents already live in Drive. Drawbacks: it allows multiple files at the same path (we block this in the simulator), and capacity needs to be assessed on your own.

Staged flow:

1. **Scan** all clouds → `identity rebuild` → review `stats` and `duplicates`.
2. **Bridging** (read-only, budgeted) upgrades cross-cloud probable duplicates to exact.
3. **Blueprint**: `folder profiles` → you or Claude adjust the rules → preview the placement.
4. **Consolidate (copy only)**: for each content, if the target cloud already has it → move it within Drive to the blueprint location as needed; otherwise copy it from the best source to the blueprint path on Drive. Default `delete_sources = false`.
5. **Soak period**: rescan Drive, recompute identity, and confirm that every content is on Drive (coverage report). At least 1–2 weeks is recommended.
6. **Source cleanup**: generate a delete-only Plan in which every delete's witness is the copy on Drive → simulate → approve → move to each cloud's quarantine.
7. **Purge**: empty the quarantine after the retention period; finally, you can cancel your subscriptions to the other clouds.

Plans can be created in batches (by folder or by cloud), with each batch simulated and approved independently.

## 9. Interfaces

- **CLI** `drivemerge`: `init`, `remotes`, `scan`, `identity rebuild`, `bridge`, `search`, `stats`, `duplicates`, `profiles`, `rules check/preview`, `plan dedupe|consolidate|reorganize|show|simulate|approve|execute|restore|purge`, `changes`, `serve`, `mcp`, `rclone-combine`.
- **REST API** (bound to 127.0.0.1 only, Bearer token): `/v1/stats`, `/v1/search`, `/v1/entries/{id}`, `/v1/entries/{id}/locations`, `/v1/duplicates`, `/v1/profiles`, `/v1/changes`, `/v1/plans` (create/list/simulate/report). Approval and execution **do not go through the API** (CLI/TTY only).
- **MCP (for Claude Code)**: `search_files`, `catalog_stats`, `find_duplicates`, `file_locations`, `folder_profiles`, `validate_rules`, `preview_layout`, `propose_plan`, `simulate_plan`, `get_plan`.

## 10. Syncing to an External Database

The local SQLite database is the source of truth; external databases (Postgres, Supabase, Elasticsearch…) pull incrementally from the **change feed**: `GET /v1/changes?cursor=<row_version:id>`, ordered by `(row_version, id)` and including tombstones, so consumers also learn about deletions. This is a pull model: an outage of the external system does not affect the local machine, and the local service never needs credentials for any external database.

## 11. Scale Estimate (1 Million Files)

- Catalog: about 0.5 KB per row → SQLite of about 0.5–1 GB (including indexes and the trigram FTS).
- Scanning: Drive is fastest with `--fast-list`; Dropbox/OneDrive have no ListR, so recursive listing is slower (on the order of tens of minutes); iCloud is the slowest. Scanning can be done sub-path by sub-path.
- Identity recomputation: linear, seconds to tens of seconds.
- Bridging: downloads only the "same size, cross-domain" candidates, one download per content, with a budget cap.

## 12. Open Decisions (Defaults Applied, Adjustable Anytime)

| # | Topic | Default | Alternatives / Trade-offs |
|---|---|---|---|
| D1 | Final target cloud | Google Drive | OneDrive (Microsoft 365 storage is good value, but it only has QuickXorHash); Dropbox |
| D2 | Primary database | Local SQLite + change feed | Write directly to Postgres (one more service to operate; but it can be shared across devices) |
| D3 | Deletion semantics | Same-cloud quarantine folder (restorable, but **does not free capacity** until purge) | Each cloud's native trash (Drive 30 days, Dropbox 30–180 days, OneDrive 30 days): frees space sooner, but restoring depends on each vendor |
| D4 | Quarantine retention | 30 days | Any; purge requires TTY approval |
| D5 | Which copy to keep among duplicates | Target cloud > non-copy-style path > shallower path > older mtime | For example "keep the newest" or "keep a specific cloud" |
| D6 | Classification blueprint | `src/drivemerge/default_rules.toml` (photos by year and month, documents, project units, everything else keeps its original structure) | Rewritten by you or Claude based on the folder profiles |
| D7 | iCloud access | rclone `iclouddrive` (experimental, re-login every 30 days) | Scan the local iCloud Drive folder on a Mac (`local` backend; "Optimize Mac Storage" must be turned off to avoid reading placeholder files) |
| D8 | LLM interface | MCP server (Claude Code) | Add a chat-style Web UI; or use only the CLI |
| D9 | May probable duplicates be deleted | No; bridging proof is required | Allow it (not recommended) |
| D10 | Google native documents | Keep in Drive, allow only moves within Drive | Export to Office formats before processing |
| D11 | Drive shortcuts / Shared with me | Skipped when scanning (`--drive-skip-shortcuts`) | Include them (easily double-counted) |
| D12 | Execution concurrency / bandwidth | One at a time, with `--bwlimit` configurable | Concurrent transfers (faster, but failure states are harder to reason about) |
| D13 | Approve/execute in the web interface | Allowed, but only from the browser session of a `drivemerge ui` launched from an interactive terminal (one-time URL token + cookie + CSRF + 8-character confirmation) | Allow only the terminal `drivemerge plan approve/execute` (the most conservative, but you have to switch windows) |
| D14 | Moving a whole folder within one cloud | v1 moves file by file (simple, each file verifiable) | Add a "server-side whole-folder move" op: much faster on Drive, but the simulator and verification would have to support folder-level operations |

## 13. Implementation Phases

| Phase | Content | Status |
|---|---|---|
| P0 Foundation | schema, scanning, hashing (including QuickXor/Dropbox), identity determination, bridging, search/stats/change feed, classification engine, Plan storage, simulator, CLI/API/MCP (read-only + propose/simulate) | This PR |
| P1 Executor | Preflight checks, quarantine deletion, copy verification, restore, journal; disabled by default | This PR (disabled by default) |
| P2 Consolidation | The consolidate builder in real use, coverage report, batching | This PR includes the builder; real use requires your cloud credentials |
| P3 Interface | Visual workspace: dual-pane drag and drop, staging/undo, suggestions, preview, approval and execution progress (see §15, WORKSPACE.md) | This PR |
| P4 Advanced | Photo EXIF dates, near-duplicate images (perceptual hash), external DB syncer | TBD |

## 14. Risks and Known Limitations

- **The iCloud backend is unstable**: Apple API changes may cause scans to fail; a failed scan produces no tombstones, so files are never wrongly judged to have disappeared.
- **Eventual consistency of the clouds**: a file just written may temporarily be missing from listings; the executor relies on "live stat + verification", not on listings.
- **Cross-cloud copies go through local bandwidth** (rclone streams them without landing on disk); large migrations need planning for time and bandwidth.
- **Multiple files at the same path on Drive**: these must first be handled with `rclone dedupe` or by hand; drive-merge will not operate on them.
- **The quarantine consumes capacity**: during consolidation the target cloud must reserve space; the `QUOTA` check in the simulation report will alert you.

## 15. Visual Workspace (see [WORKSPACE.md](WORKSPACE.md))

Two tree panes, left and right (each can be set to "all clouds" or a single cloud), let you drag files/folders between any levels. Each drag is only a **staged action**; it can be undone/redone and dragged back and forth, and in the end only the **net effect** is computed. By default the panes show the virtual "after" tree, and can switch back to "before" or show where each item originally was.

- Folders with the same name = merge; when merging, files with identical content automatically become "keep one copy + witnessed deduplication", and files with the same name but different content are listed as conflicts (never overwritten).
- Cross-cloud drag = copy → verify → move the source into the quarantine.
- **Preview** converts the draft into a Plan and runs the same simulator; any further change to the draft invalidates the preview.
- **Suggestions**: automatically find related folders scattered around (by name vocabulary, internal filenames, and shared content) as well as duplicate folders with nearly identical content, and add them to staging in one click; Claude can also add moves to the same draft through MCP (but cannot approve).
- **Approval**: only the browser session of a `drivemerge ui` launched from a terminal can approve and execute (D13).

