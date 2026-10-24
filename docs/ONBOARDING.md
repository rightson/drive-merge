# Connect, Sync, Free Space

**English** | [繁體中文](ONBOARDING.zh-TW.md)

> This document makes drive-merge usable out of the box: connect Google Drive, OneDrive and Dropbox from the page, sync every cloud's file list into the local catalog with one button, and find out, with proof, how much space one cloud can give back. The first part is the design; the second part (the implementation contract) is binding for implementers.

## 1. The target experience

1. `pip install drive-merge` then `drivemerge ui`. No config file, no `rclone config`, no TOML editing: the page opens on a **Welcome** screen with one card per cloud.
2. **Connect**: a click on a card starts the provider's sign-in in a new tab; when it completes the card shows the account, the quota (used / total) and a green "connected, read-only" badge.
3. **Sync**: one button in the header lists every connected cloud through its API, in parallel, into the local SQLite catalog, with live progress per cloud. Everything after that (Overview, Explore, Compare, Plans, the workspace, the free-space report) is a local query.
4. **Free space**: pick a cloud and get one answer: "*N GB of this cloud are proven to exist on your other clouds*", split into what is proven, what is only probable (one click verifies it), and what exists only here. One more click creates the plan that frees the proven part; it then goes through the same review, typed approval and run as every other plan.

## 2. Facts that shape the design

| Fact | Consequence |
|---|---|
| rclone's shared Google client id stops working in 2026, and Google only lets an unverified app request Drive scopes for its owner's own use. | Google Drive needs **your own OAuth client** (a one-time, ~5 minute setup in Google Cloud Console). The connect dialog walks through it step by step and checks the result. |
| An OAuth client whose consent screen is in **Testing** gets refresh tokens that **expire after 7 days**. | The wizard tells you to set the publishing status to **In production** (it stays unverified; you see one warning screen when you sign in). Otherwise Drive would silently disconnect every week. |
| rclone's built-in apps for OneDrive and Dropbox work without any setup. OneDrive accepts a narrower scope list (`access_scopes`); rclone's Dropbox app always asks for full access. | OneDrive connects **read-only** (`Files.Read Files.Read.All Sites.Read.All offline_access`). Dropbox via rclone's app gets a token that *could* write; drive-merge never uses it to write until you allow changes, and the dialog says so. For a token that truly cannot write, the dialog offers "use my own Dropbox app" (scopes `files.metadata.read files.content.read account_info.read`). |
| rclone's sign-in uses a local callback on `127.0.0.1:53682`. | Sign-in works when the browser and drive-merge run on the same machine, which is the supported setup; only one sign-in can run at a time. |
| Deleting into drive-merge's quarantine moves the file on the **same** cloud, and Google Drive trash and OneDrive's recycle bin both count against quota. | Space is returned only when the quarantine is **purged**. The free-space view says so in plain words, shows used / total before and after purge, and lets you purge from the page (typed confirmation). |
| A full listing of a large cloud takes minutes; the providers also offer change feeds (Drive `changes.list`, Dropbox `list_folder/continue`, OneDrive `delta`). | v1 syncs with full parallel listings (rclone `lsjson`, `--fast-list` on Drive). Incremental sync from change feeds is phase 2 (§7): it makes the second and later syncs take seconds. |

## 3. Security model of a connection

- **Read-only by default.** A connection creates one rclone remote, `<name>-ro`, with the narrowest scope the provider allows (Drive `drive.readonly`, OneDrive `Files.Read…`). Scanning, comparing, the free-space report and bridging use only it.
- **Changes need a second, explicit consent.** "Allow drive-merge to change this cloud" runs a second sign-in with write scope and creates `<name>-rw`, the `write` remote the executor uses. Without it, plans touching that cloud are refused by the simulator (`READ_ONLY_REMOTE`), as today.
- **Tokens stay local.** drive-merge keeps its own rclone config file, `~/.config/drive-merge/rclone.conf` (mode 600), separate from any personal `rclone.conf`. Tokens are never sent to the page, the API or MCP; the page sees only the account name and quota.
- **Only a person connects.** Starting a sign-in, enabling writes, disconnecting and installing rclone are human-channel routes (the browser session opened from the URL `drivemerge ui` prints in a terminal), like approving a plan. The API token and MCP cannot add or change connections. Syncing is also human-channel (it uses your tokens and bandwidth); reading the results is not.
- **Disconnect** removes the rclone remotes and the connection entry; the catalog keeps the files as last seen (marked as from a disconnected cloud).

## 4. Welcome and connect

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Welcome to drive-merge                                       rclone 1.75 ✓   │
│ Connect the clouds you want to compare. Nothing is changed until you approve │
│ a plan; connections are read-only.                                           │
│ ┌ Google Drive ──────────┐ ┌ OneDrive ──────────────┐ ┌ Dropbox ───────────┐ │
│ │ needs your own Google  │ │ ✓ connected, read-only │ │ [ Connect ]        │ │
│ │ client · 5 min, once   │ │ rightson@outlook.com   │ │                    │ │
│ │ [ Set up and connect ] │ │ 412 GB / 1 TB          │ │                    │ │
│ └────────────────────────┘ └────────────────────────┘ └────────────────────┘ │
│                                   [ Sync all and continue → ]                │
└──────────────────────────────────────────────────────────────────────────────┘
```

- The Welcome screen shows when no cloud is connected, and stays reachable from a **Clouds** rail item. Each card: state (not connected / signing in / connected read-only / connected with changes allowed / needs sign-in again / error), account, quota, last sync, and actions (Connect, Test, Allow changes, Disconnect).
- **Google Drive dialog**: numbered steps with links (create a project, enable the Drive API, configure the consent screen as External and **In production**, create an OAuth client of type *Desktop app*), two fields for client id and secret, and "Connect". Errors are explained (wrong client type, consent screen in Testing, API not enabled).
- **OneDrive / Dropbox dialog**: one button; Dropbox has the "use my own app" disclosure.
- A connection's name defaults to `gdrive`, `onedrive`, `dropbox` (`-2`, `-3` for a second account).
- **rclone missing or too old** (< 1.69): the header of the Welcome screen offers "Install rclone": it downloads the official build for this platform from `downloads.rclone.org`, verifies it against the published `SHA256SUMS`, and installs it under `~/.local/share/drive-merge/bin/`. Or install it yourself; the page re-checks.

## 5. Sync

- A **Sync** button in the page header (and "Sync all and continue" on Welcome). It lists all connected clouds in parallel, each with its own catalog connection (writes are serialised by SQLite), then rebuilds identity and folder stats once.
- The header shows a compact progress pill (`Syncing 2/3 · 184,203 files`); a popover lists each cloud with files, bytes, rate and state, and failures per cloud (one failing cloud does not stop the others; its old data stays and is marked stale).
- Each cloud shows its **data age** everywhere it matters (Overview cards, the free-space view): "synced 3 min ago", and a warning tone after 24 hours.
- The suspicious-scan guard (DESIGN.md) still applies: a listing that lost most of a cloud is refused and reported.

## 6. Free space

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Free space on  [ dropbox ▾ ]   folder [ (whole cloud)      ]   synced 3 min ago│
│ Used 96.0 GB of 2 TB ███████░░░░░░░░░░░░░░░░░  after purge: 41.3 GB           │
├──────────────────────────────────────────────────────────────────────────────┤
│ ✓ Proven elsewhere      54.7 GB  18,204 files   copies on gdrive 52.1 · od 2.6 │
│ ? Probably elsewhere     3.2 GB     411 files   [ Verify (downloads 3.2 GB) ]  │
│ ✕ Only on dropbox       37.9 GB   9,120 files   [ Copy to … first ]            │
│ – Cannot be judged       0.2 GB      35 files   (Paper docs, unknown size)     │
├──────────────────────────────────────────────────────────────────────────────┤
│ Keep copies on: gdrive ⠿ onedrive ⠿                                            │
│                         [ Create plan to free 54.7 GB → ]                      │
└──────────────────────────────────────────────────────────────────────────────┘
```

- **Proven elsewhere**: every live file of the chosen cloud (or folder) whose content group has a live copy on another cloud, outside quarantine. Proof is the catalog's: strong hash or a bridge (DESIGN.md §5). The row breaks the bytes down by where the copies are.
- **Probably elsewhere**: no proof, but a candidate on another cloud (same size and name, or an equal weak hash: MD5, SHA-1, QuickXorHash). "Verify" runs bridging restricted to exactly these files (it downloads them once, hashes them locally, and then they move to "proven" or "only here").
- **Only on this cloud**: no copy anywhere else. Freeing them would lose them, so the plan never touches them; "Copy to … first" opens New consolidate with this cloud as the source.
- **Cannot be judged**: native documents and objects without a size.
- Duplicates *within* the chosen cloud are not counted twice: the report is per file, and a file is "proven elsewhere" only through a copy on another cloud. (Thinning duplicates inside one cloud is the dedupe flow.)
- **Freshness gate**: when the chosen cloud or any cloud holding the witnesses was synced more than 24 hours ago, or never, the view shows "Sync first" and the create button syncs before building. The executor re-checks every source and witness live anyway.
- **Create plan** builds a `release` plan: one quarantine delete per proven file, each with a witness on another cloud chosen by "Keep copies on" order (then the keeper policy). It opens in the Plans review; nothing runs before the typed approval.
- **After the run**: the plan page and this view show "N GB in quarantine on dropbox; space comes back when it is purged", with "Purge now" (typed confirmation `purge`; purge re-proves every witness first) or "in 30 days" (the default retention).

## 7. Phase 2: incremental sync (not in this version)

Store a change cursor per cloud after a full listing and apply only the changes on the next sync: Drive `changes.list` with a start page token, Dropbox `files/list_folder/continue` with the cursor, OneDrive `/drive/root/delta`. A cursor that expires or errors falls back to a full listing; a full listing still runs weekly to catch anything a feed missed. This turns repeated syncs of a large cloud from minutes into seconds. It needs the OAuth tokens rclone stores, used directly over HTTPS, and is tested against recorded API responses.

## 8. Known limitations (v1)

- Every sync is a full listing (§7).
- iCloud Drive is connected with the Apple ID and password plus a two-factor code (§G); rclone receives the password on its command line for that one call, so other accounts on a shared computer could read it from the process list while it runs.
- Google Drive needs your own OAuth client (§2).
- Sign-in in the page requires the browser on the same machine as drive-merge (loopback callback); otherwise the token is made with `rclone authorize` on another computer and pasted (§G).

---

# Implementation contract

Everything below is binding. Read first: [DESIGN.md](DESIGN.md), [WORKSPACE.md](WORKSPACE.md) §D, [EXPLORE.md](EXPLORE.md), [PLANS.md](PLANS.md), `config.py`, `rclone.py`, `scanner.py`, `service.py`, `ui_api.py`, `builders.py`, `identity.py` (`STRONG_TYPES`, `WEAK_TYPES`, `content_key`), `coverage.py`, `executor.py` (`purge_quarantine`), and `src/drivemerge/web/`.

## A. Connections (`src/drivemerge/connections.py`, `config.py`)

Files, all mode 600, next to the main config (default `~/.config/drive-merge/`):
- `rclone.conf`: drive-merge's own rclone config. `Config.rclone_config` defaults to it when the main config does not set `[rclone] config`.
- `connections.toml`: `[[remotes]]` entries written by drive-merge (same schema as the main config's `[[remotes]]`, plus `connection = true`, `account`, `created_at`). `load_config` merges them after the main config's remotes; a duplicate name is an error naming both files. Writes are atomic (temp file + rename).

```python
PROVIDER_SPECS: dict[str, ProviderSpec]   # 'drive', 'onedrive', 'dropbox'
@dataclass(frozen=True)
class ProviderSpec:
    key: str; rclone_type: str; label: str
    needs_client: bool                       # drive: True
    read_options: dict[str, str]             # drive {'scope': 'drive.readonly'}; onedrive {'access_scopes': 'Files.Read Files.Read.All Sites.Read.All offline_access'}; dropbox {}
    write_options: dict[str, str]            # drive {'scope': 'drive'}; onedrive default scopes; dropbox {}
    extra_args: tuple[str, ...]              # drive ('--fast-list', '--drive-skip-shortcuts')

class ConnectError(ValueError): code: str   # NO_RCLONE, RCLONE_TOO_OLD, FLOW_BUSY, FLOW_FAILED, FLOW_TIMEOUT, NAME_TAKEN, BAD_NAME, NEEDS_CLIENT, TEST_FAILED, NOT_FOUND

def rclone_status(config) -> dict            # {'found', 'path', 'version', 'ok' (>= 1.69), 'managed' (installed by drive-merge)}
def install_rclone(dest_dir: Path, *, progress=None) -> Path   # official zip for this OS/arch + SHA256SUMS verification; raises on mismatch
def list_connections(config) -> list[dict]   # name, provider, account, read, write, writable, created_at (never tokens)

class AuthFlow:  # one rclone `authorize` subprocess
    id: str; provider: str; purpose: str ('read'|'write'); status: str ('waiting'|'succeeded'|'failed'|'cancelled'|'timeout')
    auth_url: str | None; error: str | None
def start_flow(config, provider, *, purpose='read', client_id=None, client_secret=None, timeout=600) -> AuthFlow
def get_flow(flow_id) -> AuthFlow
def cancel_flow(flow_id) -> None
def finish_connect(config, flow_id, *, name: str | None = None) -> dict      # creates '<name>-ro', tests it, writes connections.toml; returns the connection
def finish_enable_writes(config, flow_id, name: str) -> dict                  # creates '<name>-rw', sets write in connections.toml
def test_connection(config, name) -> dict    # lsjson --max-depth 1 + about: {'ok', 'account', 'quota': {'used','total','free'} | None, 'error'}
def disconnect(config, name) -> None         # deletes '<name>-ro'/'<name>-rw' from drive-merge's rclone.conf and the entry
```
- `start_flow` runs `rclone authorize <type> [client_id client_secret] --auth-no-open-browser` (plus the purpose's scope options as `--<type>-<option>` flags where rclone accepts them) with drive-merge's rclone config, reads the auth URL from its output (≤ 15 s), and keeps the process; the token is read from stdout when it exits 0 and kept in memory only. One flow at a time (`FLOW_BUSY`); `timeout` kills it.
- `finish_connect` creates the remote with `rclone config create <name>-ro <type> token=<json> <read_options…> [client_id=… client_secret=…] --non-interactive`, following rclone's non-interactive state machine where it asks questions (OneDrive drive choice: the account's default drive). Then `test_connection`; on failure the remote is removed again and `TEST_FAILED` raised with rclone's message.
- Names: `^[a-z][a-z0-9-]{0,30}$`, not already a configured remote (`NAME_TAKEN`).
- `account` comes from the provider through rclone (`rclone about` / `backend` info where available); best effort, may be None.
- Nothing in this module logs or returns a token. Tests use a fake `rclone` executable (a script on PATH) that implements `version`, `authorize` (prints the URL, then a token after a trigger file appears), `config create/delete`, `lsjson`, `about`.

CLI: `drivemerge connect drive|onedrive|dropbox [--name N] [--client-id … --client-secret …]` runs the same flow in a terminal (prints the URL, waits), `drivemerge connect --enable-writes NAME`, `drivemerge disconnect NAME`, `drivemerge connections`. `drivemerge init` no longer writes example remotes; `drivemerge ui` without a config runs `init` first.

## B. Sync (`src/drivemerge/syncall.py`)

```python
def sync_all(config, *, remotes: list[str] | None = None, progress=None, max_parallel: int = 4) -> dict
```
Lists the given (default: all) remotes in parallel threads; each thread opens its own `db.connect(config.db_path)` and calls `scanner.scan_remote`. `progress(name, state, files, dirs, bytes)` per batch (`state`: queued, listing, done, failed). Then one `identity.rebuild_groups` and `dirstats.refresh` on a fresh connection. Returns `{'remotes': {name: {'status', 'files', 'bytes', 'seconds', 'error'}}, 'seconds', 'identity': …}`. A failing remote does not stop the others. `Service.sync(remotes=None, progress=None)` wraps it. The data age of a remote is the `finished_at` of its last complete scan.

## C. Free space (`src/drivemerge/release.py`)

```python
def release_report(conn, remote: str, *, path_prefix: str | None = None, keep_on: list[str] | None = None,
                   stale_after_hours: int = 24) -> dict
def build_release(conn, *, remote: str, path_prefix: str | None = None, keep_on: list[str] | None = None,
                  quarantine_dir: str | None = None) -> builders.BuildResult
```
- Live files of `remote` under `path_prefix` (outside quarantine). Buckets: `proven` (content group has a live copy on another remote — restricted to `keep_on` when given — outside quarantine), `probable` (no proof; a live file on another remote with the same size and name_norm, or an equal weak hash), `only_here`, `unjudged` (native docs, size unknown). Each bucket: `files`, `bytes`; `proven` also `by_remote: {name: bytes}` (the remote its chosen witness is on); `probable` also `entry_ids` (capped at 5000, with `entry_ids_total`) for verification.
- `freshness`: `{name: {'synced_at', 'stale'}}` for the remote and every remote that holds witnesses or candidates; `stale` when older than `stale_after_hours` or never synced.
- `quota`: not computed here (needs the network); the HTTP layer adds the cached `test_connection` quota.
- `build_release`: one `delete` op per proven file (`builders.op_from_entry`, `reason='release <remote>: copy on <w_remote>:<w_path>'`), witness = the live copy on another remote ordered by `keep_on` then `choose_keeper`. Never native docs, ambiguous Drive paths, or quarantine. Refuses (BuildError, code `READ_ONLY`) when `remote` has no write remote. `stats`: the report's buckets. Plan kind `release`; `Service.propose_plan('release', {...})` dispatches to it.
- Bridging restricted to given entries: `Service.bridge(entry_ids=[…])` (extend the existing method) downloads exactly those files' bytes (or the cheaper candidate on the other side when that settles the same question) and rebuilds groups.

## D. HTTP (`ui_api.py`, `api.py`) and MCP

| Route | Channel | Body / query → response |
|---|---|---|
| `GET /v1/setup` | read | `{'first_run', 'config_path', 'rclone': rclone_status, 'connections': [...], 'providers': [{key,label,needs_client}]}` |
| `POST /v1/ui/connect/start` | human | `{provider, purpose:'read', client_id?, client_secret?}` → `{flow_id, auth_url}` |
| `GET /v1/ui/connect/flows/{id}` | human | flow status (no token) |
| `POST /v1/ui/connect/flows/{id}/finish` | human | `{name?}` → connection; `{name}` for purpose `write` |
| `POST /v1/ui/connect/flows/{id}/cancel` | human | `{ok}` |
| `POST /v1/ui/connections/{name}/test` | human | test result (cached quota for 10 min) |
| `DELETE /v1/ui/connections/{name}` | human | `{ok}` |
| `POST /v1/ui/rclone/install` | human | job |
| `POST /v1/ui/sync` | human | `{remotes?}` → job (kind `sync`, progress per remote); replaces the old rescan route's body (keep `/v1/ui/rescan` as an alias) |
| `GET /v1/release/{remote}` | read | `path`, `keep_on` (comma list) → report + `quota` |
| `POST /v1/ui/plans/{id}/purge` | human | `{confirm:'purge', older_than_days?: 0}` → job |

Errors carry `code` and `params` (as everywhere). Connections created while the server runs are visible to it without a restart (the service reloads the merged config). MCP: a read-only `free_space_report` tool; `propose_plan` accepts kind `release`. No connection or sync tool in MCP.

## E. Frontend (`src/drivemerge/web/`)

- Rail: a **Clouds** item first (Welcome when nothing is connected; the cards otherwise). On load with `first_run` or no connection, the page opens on it.
- Connect dialogs as §4; the page opens `auth_url` in a new tab and polls the flow every second; success refreshes the card; errors are translated by code.
- Header: Sync button + progress pill + popover (§5); data age chips on Overview cards.
- **Free space** view (`#/free/<remote>?path=…&keep=…`): §6 layout; linked from each Overview cloud card ("Free space…") and from Plans ("New: Free space"). Verify starts the restricted bridge job; Create plan proposes `release` and opens its review; after a run the plan page offers Purge.
- All text through `i18n.js` in English and Traditional Chinese; every new element the tests need has a `data-testid`.

## F. Tests

- Fake-rclone tests for every connection path, including a slow sign-in, a failed one, cancel, timeout, a busy flow, name clashes, token never appearing in any HTTP response or log line, the 600 file modes, and the merged config.
- `sync_all` with three local remotes in parallel, one of them failing.
- `release_report`/`build_release` with proven, probable (name+size and weak hash), only-here, unjudged, keep_on, quarantine, read-only and stale cases; every built plan simulates ok with `contents_lost == 0`.
- A Playwright end-to-end run: first start without config → Welcome → connect a fake Dropbox and a fake Drive (fake rclone backed by local folders) → Sync → Free space on one → Verify → Create plan → approve → run → Purge now → the bytes are gone from that folder and still present on the other.

## G. Consolidation with the Setup screen

PR #5 (`claude/setup-screen`, docs `SETUP.md`) built a second onboarding next to this one: a **Setup** rail item, `setup.js`, `/v1/ui/setup/*`, a streamed `rclone config` state machine in `rclone.py`, and `[[remotes]]` blocks written into the person's own `config.toml`. After the merge a new user sees two ways to connect a cloud. This section is binding and supersedes anything in PR #5's design: **one** onboarding remains, the **Clouds** view of this document with its storage model (`connections.toml` + drive-merge's own `rclone.conf`, `<name>-ro`/`<name>-rw`). PR #5's capabilities that this document lacked move into it; everything else of PR #5's onboarding is deleted. Where this section contradicts §1–§8 or A–F, this section wins.

### G1. Why the Clouds model is the base

| Question | Clouds (this document) | Setup (PR #5) | Kept |
|---|---|---|---|
| Where a connection is written | `connections.toml`, a file drive-merge owns and rewrites whole; tokens in its own `rclone.conf` | line surgery on the person's hand-edited `config.toml` (`add_remote_block`, `set_remote_write`, re-parse, inline-table refusals); tokens in whichever rclone config is in use | Clouds: no edits to a human file, no `CONFIG_LAYOUT` failure class, disconnect is possible |
| Read-only by default | narrow scopes per provider (Drive `drive.readonly`, OneDrive `Files.Read…`); writes need a second sign-in creating `<name>-rw` | Drive `drive.readonly`; others unscoped, "Enable changes" just copies `read` into `write` | Clouds (OneDrive is truly read-only only here) |
| Sign-in mechanics | `rclone authorize` → token in memory → `config create … token=…`; nothing is written to rclone before the sign-in succeeds | `config create` without a token, streamed while rclone waits for the browser; the half-made remote must be deleted on every failure | Clouds for OAuth; PR #5's question loop survives only where `authorize` cannot work (iCloud, a person-only question) |
| Sync / first scan | parallel `sync_all`, header pill and popover, identity rebuilt once | serial rescan job polled by the Setup page | Clouds |
| rclone missing | verified install into `~/.local/share/drive-merge/bin` | link to rclone.org | Clouds |
| Disconnect, quota, account, Google guide, Dropbox own app | yes | no | Clouds |
| iCloud, generic question form, token paste for another computer, adopting existing rclone remotes, read-only fallback, Test failures worded by code, `needed` redirect, `lsjson` test timeout, loopback-only sign-in URL, i18n contract for the codes | no | yes | ported from PR #5 (G3–G6) |

### G2. Capability matrix (what each side had; what the merged view does)

| Capability | Main (PR #6) | PR #5 | Merged |
|---|---|---|---|
| rclone detection / install | `connections.rclone_status`, `install_rclone` (SHA256SUMS), `POST /v1/ui/rclone/install`, `RcloneBanner` | `Rclone.version_info`, install link only | main |
| OAuth sign-in on `127.0.0.1:53682` | `start_flow` (`rclone authorize`), polled flow, `_URL_RE` accepts any host | streamed `config create`, `AUTH_URL` = `127.0.0.1:53682` only | main, with PR #5's loopback-only URL pattern |
| Generic rclone question form | none: every question auto-answered (`_create_remote`) | `_question`, `QuestionForm` (radio, select-or-type, password, bool) | known questions auto-answered (main's table); any other question, and OneDrive's drive choice when rclone offers more than one drive, go to PR #5's form |
| iCloud (Apple ID, password, 2FA) | not offered (terminal only) | `apple_id`/`password` up front, 2FA via the form | ported; spec `iclouddrive`, kind `config` |
| Token from another computer | not offered | `remote_auth` answers `config_is_local=false`, token pasted as a question | `POST /v1/ui/connect/paste` with the exact `rclone authorize` command shown |
| Adopt existing rclone remotes | none | lists remotes of the rclone config in use and writes a block | **import**: copies a remote from the person's own rclone config into drive-merge's `rclone.conf` as `<name>-ro` (G4.4) |
| Read-only + "Enable changes" | second sign-in → `<name>-rw` for all providers | Drive second sign-in; others reuse `read` | main; providers without a scoped sign-in (iCloud, imported) reuse `read` after a confirmation |
| Test connection | `POST /v1/ui/connections/{name}/test` (human), raw error text, no timeout | `POST /v1/ui/setup/test` (normal), coded `AUTH_EXPIRED`/`NETWORK`/`NOT_FOUND`/`RCLONE_ERROR`, 30 s | main's route, plus PR #5's codes and 30 s timeout; also for remotes from `config.toml` |
| First scan / sync with progress + identity rebuild | `sync_all`, `/v1/ui/sync`, pill + popover | Setup step 3 polling `/v1/ui/rescan`, extra `remote_index`/`scans` fields | main; PR #5's extra progress fields deleted |
| Purge / remove | purge route, `disconnect` | none (removal "stays in the config file") | main |
| Where config is written | `connections.toml` + own `rclone.conf` | `config.toml [[remotes]]` | main |
| Human channel | `require_human` on connect/finish/cancel/test/delete/install/sync/purge | same mechanism on its routes | main's list plus the new routes (G4.5) |
| Secret scrubbing | token/client secret kept in `AuthFlow`, `_scrub`, fake logs masked | answers never in a job view, `_config_call` masks argv and stderr | main's, extended to answers, Apple password and pasted tokens (G4.2) |
| Read-only fallback (approvals off) | none: the cards even auto-run Test and show `APPROVALS_DISABLED` | read-only panel with commands | ported to Clouds, with `drivemerge connect` commands |
| First-run redirect | routeless page → `#/clouds` when `first_run` or nothing connected | routeless → `#/setup` when `needed` | one rule: routeless → `#/clouds` when `needed` (G4.5) |
| i18n | `conn.*`, `welcome.*`, `rclone.*`, `err.<ConnectError>` (no contract test) | `setup.*`, `connect.*`, `err.<code>` with a contract test | `conn.*` only; PR #5's contract test retargeted to `ConnectError` |

### G3. The resulting experience

The rail is `Clouds, Overview, Explore, Compare, Plans, Workspace` (as on main; `Alt+1…5` unchanged, no `Alt+0`). A page opened without a route goes to `#/clouds` when `needed` (no cloud configured, or a configured cloud without a complete scan); `#/setup` from an old bookmark is replaced by `#/clouds`. The Clouds view shows: the rclone banner or chip; one card per connection **and** per remote configured by hand in `config.toml`; one tile per provider (Google Drive, OneDrive, Dropbox, **iCloud Drive**); an "Already use rclone?" list when the person's own rclone config has remotes that can be imported; "Sync all and continue". Every connect path ends in the same card, read-only.

### G4. Backend contract

#### G4.1 `connections.py`

```python
@dataclass(frozen=True)
class ProviderSpec:
    ...                                   # existing fields unchanged
    kind: str = "authorize"               # 'authorize': rclone authorize + token; 'config': rclone config asks (iCloud)
    fields: tuple[str, ...] = ()          # what `start_flow` needs up front for kind 'config'
    scoped_write: bool = True             # False: the read sign-in can already write; Enable changes reuses `read`

PROVIDER_SPECS["iclouddrive"] = ProviderSpec("iclouddrive", "iclouddrive", "iCloud Drive", False, {}, {}, (),
                                             "icloud", kind="config", fields=("apple_id", "password"),
                                             scoped_write=False)

class ConnectError(ValueError):
    def __init__(self, code: str, message: str, **params: str)   # params added (MISSING_FIELD {field}, ...)
# codes: NO_RCLONE, RCLONE_TOO_OLD, FLOW_BUSY, FLOW_FAILED, FLOW_TIMEOUT, NAME_TAKEN, BAD_NAME, NEEDS_CLIENT,
#        TEST_FAILED, NOT_FOUND, plus MISSING_FIELD {field}, NOT_WAITING, TOKEN_UNREADABLE, CONFIG_WRITE,
#        IMPORT_UNSUPPORTED {type}, IMPORT_ENCRYPTED, BAD_REQUEST

class AuthFlow:
    status: str      # waiting | question | working | succeeded | done | failed | cancelled | timeout
    name: str | None                 # reserved connection name once known
    question: dict | None            # question_view(...) while status == 'question'
    retry: bool                      # rclone refused the previous answer (its text is never forwarded)
    connection: dict | None          # the public entry once status == 'done'
    code: str | None; params: dict   # coded failure
    # private: _token, _client_id, _client_secret, _secrets (every value to scrub), _state (rclone State),
    #          _remote (the rclone remote this flow created and must remove on failure), _timer
    def public(self) -> dict         # id, provider, purpose, status, auth_url, error, code, params, name,
                                     # question, retry, connection; never a token, password, answer or secret

def question_view(opt: dict) -> dict  # moved from ui_api._question, same shape:
                                      # {name, help, default, examples:[{value, help}], password, required, exclusive, type}
def start_flow(config, provider, *, purpose='read', name=None, client_id=None, client_secret=None,
               apple_id=None, password=None, timeout=600, url_wait=15) -> AuthFlow
def paste_token(config, provider, token: str, *, purpose='read', name=None,
                client_id=None, client_secret=None) -> AuthFlow
def authorize_command(provider: str, purpose: str, client_id: str | None = None) -> list[str]
def finish_connect(config, flow_id, *, name=None) -> dict          # connection | question result
def finish_enable_writes(config, flow_id, name) -> dict            # connection | question result
def answer_flow(config, flow_id, result: str) -> dict              # connection | question result
def importable(config) -> dict
def import_remote(config, source: str, *, name: str | None = None) -> dict   # connection | question result
def test_connection(config, name) -> dict
```

A **question result** is `{"flow_id", "status": "question", "question", "retry"}`; a connection never has a `flow_id` key, which is how callers tell them apart.

- `_URL_RE` becomes `http://127\.0\.0\.1:53682/auth\?state=[A-Za-z0-9_.~%-]+` (PR #5's `AUTH_URL`). Any other URL rclone prints is never stored in a flow.
- **One question loop**, `_advance(config, flow)`, replaces the inline loop of `_create_remote` and is used by every path that runs `rclone config create … --non-interactive`. Auto answers: `config_refresh_token` → `false`, `config_change_team_drive` → `false`, `config_type` → `onedrive`, `config_drive_ok` → `true`, `config_driveid` → the default when rclone offers at most one example. Every other question (OneDrive with several drives, iCloud's `config_2fa`, anything rclone adds later) sets `status = 'question'`, stores `question_view(Option)` and rclone's `State`, and returns a question result. An `Error` with an `Option` on an auto-answered question is `FLOW_FAILED`; on a person's question it re-asks with `retry = True`. At most 30 rounds (`FLOW_FAILED`).
- `answer_flow`: `NOT_WAITING` unless `status == 'question'`; `result` at most 65 536 characters (`BAD_REQUEST`). It runs `config create <remote> <type> --non-interactive --continue --state S --result R` (timeout 120 s) and continues `_advance`. The answer is appended to `_secrets` before rclone runs, scrubbed from every stderr tail and message, and dropped when the call returns.
- A question waits at most 600 s (`threading.Timer`): then `status = 'timeout'`, `code = 'FLOW_TIMEOUT'`, the remote is deleted and the name released. `cancel_flow` works in `waiting`, `question` and `succeeded` and also deletes `_remote`.
- `FLOW_BUSY` when any flow is `waiting`, `question` or `working`.
- **Names**: `NAME_RE` stays `^[a-z][a-z0-9-]{0,30}$`. `_taken` also counts names reserved by live flows (`_RESERVED`, under `_FLOWS_LOCK`), so a person answering a question cannot lose the name to a second sign-in. Released on `done`, `failed`, `cancelled`, `timeout`.
- **Rollback**: every failure after `_remote` exists deletes it (`_delete_remote`), including a failing `_write_entries` (new code `CONFIG_WRITE`; main currently leaves an orphaned `<name>-ro` there). `TEST_FAILED` as today.
- **kind `config` (iCloud)**: `start_flow` requires `apple_id` and `password` (`MISSING_FIELD {field}`), refuses them for any other provider and refuses `client_id`/`client_secret` for providers other than drive/dropbox (`BAD_REQUEST`). It picks and reserves the name (`name` or `icloud`, `icloud-2`, …), runs `config create <name>-ro iclouddrive apple_id=… password=… --non-interactive` synchronously (timeout 120 s; the password goes to `_secrets` and is dropped afterwards), then `_advance`; the flow is returned in `question` (the 2FA code) or `done`. `finish_connect` is not used for it; the last answer runs the test and writes the entry.
- **paste_token**: for kind `authorize` only. Accepts either rclone's whole paste block (`--->` … `<---End paste`) or the bare JSON (`_parse_token`); at most 16 384 characters; anything else is `TOKEN_UNREADABLE` (the message never quotes the input). Drive still needs the client id and secret (`NEEDS_CLIENT`), because the token is bound to the client that made it. Returns a flow already `succeeded`, with no process; the page then calls finish as for a browser sign-in. `authorize_command` returns the argv the dialog shows: `["rclone", "authorize", <type>, <client_id>, "CLIENT_SECRET", <scope flags of the purpose>]` (client pair only when given; the secret is a placeholder and is typed by the person). A pasted token is presented as read-only by drive-merge's policy: its scope cannot be checked, and the dialog says the command must be run exactly as shown.
- **Enable changes** for an entry whose provider has `scoped_write = False`, or that was imported (`imported_from` set): `start_flow(purpose='write', name=…)` returns a flow already `succeeded`; `finish_enable_writes` sets `write` to the entry's `read` (no second remote). Drive, OneDrive, Dropbox connections keep the second sign-in.
- **Entries** gain two optional keys in `connections.toml`: `scoped = false` (iCloud, imported) and `imported_from = "<remote name in the person's rclone config>"`. `list_connections` returns them as `scoped` (default true) and `imported_from` (default null).
- **disconnect** deletes exactly the rclone remotes named by the entry's `read` and `write` that are sections of drive-merge's `rclone.conf` (instead of guessing `-ro`/`-rw`). It never opens the person's own rclone config.

#### G4.2 Secrets (merged guarantees)

Nothing below ever appears in an HTTP response, a flow view, a job view, a log line, an exception message or `FAKE_RCLONE_LOG`: OAuth tokens, a pasted token, the Drive/Dropbox client secret, the Apple ID password, any answer to a question whose `password` is true, and any answer at all in a flow view. All of them are scrubbed with `_scrub(text, *flow._secrets)` from every rclone stderr tail before it becomes `error` or a message. rclone receives the token, password and answers as command-line arguments for one call (§8 limitation).

#### G4.3 `test_connection`

`test_connection(config, name)` also accepts a remote from `config.toml` (non-connection), using its `read`. It runs `lsjson --max-depth 1` through `_run` with a 30 s timeout (PR #5's `TEST_TIMEOUT_S`), then `about` and the account as today. Result: `{'ok', 'account', 'quota', 'error', 'code', 'params'}`; on failure `code = rclone.classify_failure(returncode, stderr)` (`AUTH_EXPIRED`, `NETWORK`, `NOT_FOUND`, `RCLONE_ERROR` with `params.detail`), `error` the scrubbed tail as today. `classify_failure` and its regular expressions stay in `rclone.py`.

#### G4.4 Import (replaces PR #5's adopt)

- The person's own rclone config is `RCLONE_CONFIG` if set, else the last line of `rclone config file` run **without** `--config`. It is ignored when it resolves to drive-merge's `rclone.conf`.
- `importable(config) -> {'config_path': str | None, 'encrypted': bool, 'remotes': [{'name', 'type', 'provider', 'supported', 'imported'}]}`: section names and `type` read directly from that file (never through a subprocess that prints tokens). A file whose first non-blank line is `# Encrypted rclone configuration File` gives `encrypted: true` and no remotes. `provider` is `config.provider_of(type)`. `supported` is false for remotes that wrap other remotes or rotate single-use tokens: `alias`, `crypt`, `union`, `combine`, `chunker`, `hasher`, `compress`, `cache`, `box`. `imported` is true when an entry already has `imported_from == name`.
- `import_remote(config, source, *, name=None)`: `IMPORT_ENCRYPTED`, `NOT_FOUND`, `IMPORT_UNSUPPORTED {type}`; name = `name` or the source name lowercased into `NAME_RE` (`BAD_NAME`/`NAME_TAKEN` otherwise). It creates `<name>-ro` in drive-merge's `rclone.conf` with `config create <name>-ro <type> <every key=value of the source section except type> --no-obscure --non-interactive` (values are copied as stored, so obscured passwords stay obscured once), runs `_advance`, tests, and writes the entry with `scoped = false`, `imported_from = source`, `write` unset. Every value of the section goes to `_secrets`. The person's file is never written.

#### G4.5 HTTP

New and changed routes (all others of §D unchanged):

| Route | Channel | Body / query → response |
|---|---|---|
| `GET /v1/setup` | read | as §D plus `needed: bool` (no remote configured, or a configured remote without a complete scan), `manual_remotes: [{name, provider, writable}]` (remotes of `config.toml`), each connection's `scoped` and `imported_from`, and per provider `kind`, `fields`, `authorize: {read: [...], write: [...]}` (`authorize_command` without a client id). Before answering, the service reloads the merged config when `connections.toml`'s mtime changed since it was last loaded (connections made with `drivemerge connect` while the server runs). |
| `POST /v1/ui/connect/start` | human | `{provider, purpose, name?, client_id?, client_secret?, apple_id?, password?}` → `{flow_id, auth_url, status}`; for iCloud the response is a question result with `auth_url: null` (HTTP 202) or the connection (200) |
| `GET /v1/ui/connect/flows/{id}` | human | `AuthFlow.public()` |
| `POST /v1/ui/connect/flows/{id}/finish` | human | `{name?}` → 200 connection, or 202 question result |
| `POST /v1/ui/connect/flows/{id}/answer` | human | `{result}` → 200 connection, or 202 question result; 409 `NOT_WAITING` |
| `POST /v1/ui/connect/flows/{id}/cancel` | human | `{ok}` |
| `POST /v1/ui/connect/paste` | human | `{provider, purpose, name?, client_id?, client_secret?, token}` → `{flow_id, status: "succeeded"}` |
| `GET /v1/ui/connect/importable` | normal | `importable(config)` |
| `POST /v1/ui/connect/import` | human | `{source, name?}` → 200 connection, or 202 question result |
| `POST /v1/ui/connections/{name}/test` | human | G4.3 result; also for `manual_remotes` |
| `POST /v1/ui/sync`, `/v1/ui/rescan` | human | as §D; the progress keys `remote`, `remote_index` and the result key `scans` that PR #5 added are removed |

Every successful finish, answer or import calls `quotas.drop(name)` and `service.reload_config()` under the request lock, as `connect_finish` does today. `ConnectError` keeps rendering through `api.install_error_handlers` with `code` and `params`; `_CONNECT_STATUS` gains `MISSING_FIELD: 400`, `BAD_REQUEST: 400`, `TOKEN_UNREADABLE: 400`, `NOT_WAITING: 409`, `IMPORT_UNSUPPORTED: 400`. Flow `public()` for a failed flow carries `code`/`params`.

**Deleted from `ui_api.py`:** `GET /v1/ui/setup`, `POST /v1/ui/setup/test`, `/v1/ui/setup/connect`, `/v1/ui/setup/connect/{job_id}/answer`, `/v1/ui/setup/connect/{job_id}/cancel`, `/v1/ui/setup/adopt`, `/v1/ui/setup/enable-write`; `ConnectBody`, `AnswerBody` (re-created as `{result}` for the new route), `NameBody`, `EnableWriteBody`; `_Connect`, `_question` (moved), `_connect_failure`, `_remote_view`; `need_path`, `swap_config`, `remote_view`, `check_new_name`, `start_connect`, `connect_job`, the `connects` dict; `SIGNIN_TIMEOUT_S`, `ANSWER_TIMEOUT_S`, `CREATE_TIMEOUT_S`, `NEW_REMOTE_NAME`, `ADOPT_NAME`, `MAX_ANSWER` (moved to `connections.py`), `CONNECT_TYPES`; the `ConfigError` handler; `create_ui_app(config_path=…)` and `NO_CONFIG_PATH` (`run_ui` still hands `config_path` to the `Service`).

#### G4.6 Other modules

- `config.py`: delete `ConfigError`, `PROVIDER_EXTRA_ARGS`, `_write_lock`, `_HEADER`, `_NAME_LINE`, `_toml_str`, `_toml_list`, `_read_text`, `_commit`, `add_remote_block`, `set_remote_write`. Keep PR #5's `write_private` (fsync, writes through a symbolic link), `provider_of`/`RCLONE_PROVIDERS` (import), `write_new_config`, `ensure_config` (`cmd_init` and `cmd_ui` use them).
- `rclone.py`: delete `RcloneCancelled`, `RcloneTimeout`, `_REMOTE_NAME`, `MIN_RCLONE`, `TEST_TIMEOUT_S` (moves to `connections.py`), `AUTH_URL`, `_scrub`, `version_info`, `list_remotes`, `_check_name`, `config_create`, `config_continue`, `config_delete`, `test_remote`, `_config_call`, `_config_result`. Keep `classify_failure` and its patterns.
- `service.py`: delete `SETUP_CACHE_S`, `_setup_cache`, `_setup_lock`, `forget_rclone`, `rclone_facts`, `setup_state`, the `config` argument of `reload_config`, and the `identity_built_at` meta writes in `sync` and `rebuild_identity`. `setup_status` gains `needed` and `manual_remotes` (G4.5).
- `cli.py`: `cmd_ui` prints one line when it creates the config (`Created <path> … connect your clouds on the page that opens.`); the duplicated message from the merge goes. `drivemerge connect iclouddrive --apple-id ID` asks the password with `getpass` and every question on the terminal (`input`, or `getpass` when `password`), through `answer_flow`. `drivemerge connect --paste-token PROVIDER` reads a token from stdin. `drivemerge import SOURCE [--name N]` wraps `import_remote`. All behind `_connect_gate`.

### G5. Frontend contract

- **Delete** `web/setup.js`; the `setup` entries of `RAIL`, `VIEW_ICON`, `ROUTE`, `shortcutOf` and `Alt+0` in `shell.js` (a `#/setup` hash is replaced by `#/clouds`); the `setup` icon; `api.setupState`, `api.connect`, `api.answer`, `api.cancelConnect`, `api.adopt`, `api.testRemote`, `api.enableWrite`; the second redirect block and `ctx.reloadConfig` in `app.js` (`refreshSetup` already refetches `/v1/ui/config`); the `.su-*` styles not used by the moved form; the `connect.field.*` special case in `util.wordParams` (the Apple ID field is worded through `conn.field.*`).
- **Redirect** (`app.js`): a page without a route goes to `#/clouds` when `GET /v1/setup` answers `needed: true`; a server that does not answer, or answers without `needed`, changes nothing.
- **Move** `QuestionForm` and `RcloneText` from `setup.js` to a new `web/question.js`, unchanged in behaviour (radio group for exclusive examples, select-or-type, password input that empties itself before the request, booleans as two buttons, `retry` notice, rclone's help in a box marked as rclone's text), keeping their `data-testid`s (`connect-question`, `connect-q-name`, `connect-q-options`, `connect-q-option-<i>`, `connect-q-yes`, `connect-q-no`, `connect-q-select`, `connect-q-input`, `connect-q-continue`, `connect-q-retry`, `connect-q-help`).
- **`ConnectDialog`** (`onboarding.js`) gains the phase `question`: a 202 from finish, start, import or answer shows `QuestionForm`; submitting calls `api.connectAnswer(flowId, value)`. Cancel in this phase calls `connectCancel`. The page opens only an `auth_url` matching `^http://127\.0\.0\.1:53682/auth\?`.
- **iCloud tile and dialog** (`provider-iclouddrive`, `connect-iclouddrive`): Apple ID (`connect-apple-id`), password (`connect-apple-password`, emptied when the request is sent), the 2FA and process-list notes, then the question phase.
- **"Sign in on another computer"** disclosure for kind `authorize` (`conn-paste-toggle`): the command from `providers[].authorize[purpose]` with the client id filled in (`conn-paste-command`, copy button), a textarea (`conn-paste-token`) and `conn-paste-submit` → `api.connectPaste` → finish as after a browser sign-in. The token leaves the component state before the request and is never put in the address, storage or a toast.
- **Import** section under the tiles when `GET /v1/ui/connect/importable` lists remotes: `import-list`, one row per remote (`import-<name>`, button `import-add-<name>`, disabled with `conn.import.unsupported` when not supported, hidden when imported), `import-encrypted` note.
- **Cards**: `manual_remotes` get a card too (`conn-card-<name>`, `data-source="config"`): state, quota from Test, last sync, Test and Sync only, and the note `conn.manual`. Connections with `scoped: false` show `conn.unscoped`; their Allow changes asks for a confirmation (`conn.allowSameTitle`/`conn.allowSameText`) instead of a sign-in. A Test failure with `code: AUTH_EXPIRED` puts the card in the `relogin` state.
- **Read-only fallback** (`config.approvals_enabled === false`): no Test runs on load, every connect/import/allow/disconnect/sync button is disabled, and a panel (`clouds-readonly`) shows the commands to run in a terminal (`drivemerge connect drive|onedrive|dropbox|iclouddrive`, `drivemerge scan --all`, `drivemerge identity rebuild`) with copy buttons; reloading the page then shows the new connections (G4.5 mtime reload).
- **Overview** empty state: `ov-setup-link` becomes `ov-clouds-link` → `#/clouds`.

### G6. i18n

- **Delete** every `setup.*` and `nav.setup*` key, `ov.setupLink`, the `connect.*` keys not listed below, and `err.REMOTE_EXISTS`, `err.REMOTE_NOT_FOUND`, `err.CONFIG_LAYOUT`, `err.RCLONE_MISSING`, `err.NOT_WAITING` (re-added below with new text), `err.NO_CONFIG_PATH`, `err.SIGNIN_TIMEOUT`, `err.CANCELLED`. `err.BAD_NAME` goes back to main's cloud-name rule (lowercase letters, digits and `-`, starting with a letter, at most 31).
- **Rename** PR #5's question-form keys `connect.q.*` → `conn.q.*` (same texts).
- **Add** in both languages: `conn.q.*` (from PR #5), `conn.field.apple_id`, `conn.field.password`, `conn.tile.iclouddrive`, `conn.icloud.intro`, `conn.icloud.appleId`, `conn.icloud.password`, `conn.icloud.twoFactor`, `conn.icloud.processList`, `conn.paste.toggle`, `conn.paste.text`, `conn.paste.command`, `conn.paste.token`, `conn.paste.submit`, `conn.paste.scope`, `conn.import.heading`, `conn.import.text`, `conn.import.add`, `conn.import.unsupported`, `conn.import.encrypted`, `conn.import.done`, `conn.manual`, `conn.unscoped`, `conn.allowSameTitle`, `conn.allowSameText`, `conn.ro.title`, `conn.ro.text`, `conn.ro.connect`, `conn.ro.scan`, `ov.cloudsLink`, and `err.MISSING_FIELD {field}`, `err.NOT_WAITING`, `err.TOKEN_UNREADABLE`, `err.CONFIG_WRITE`, `err.IMPORT_UNSUPPORTED {type}`, `err.IMPORT_ENCRYPTED`, `err.AUTH_EXPIRED` ("…disconnect it and connect it again"), `err.NETWORK`, `err.RCLONE_ERROR`.

### G7. Tests

Coverage of every surviving capability is kept; only tests of deleted mechanisms go.

**Delete** (the feature is gone or main's model replaces it, and main already covers the capability):
- `tests/test_setup_api.py` as a file, after porting the tests listed below. Not ported: setup_state shape, rclone listing cache and `reload_config(config)` (gone); `NO_CONFIG_PATH`; bad/good names and name clash (main's `test_default_names_count_up_and_explicit_names_are_checked`, `test_name_clash_with_a_remote_of_the_main_config`); connect without rclone, one at a time, Dropbox end to end, Drive scope and secret, Drive without a client id (main requires one), cancel while waiting for the browser, sign-in timeout, refused access (main's `test_busy_slow_cancel_and_failure`, `test_flow_times_out`, `test_connect_dropbox_end_to_end`, `test_drive_needs_client_and_uses_the_read_scope`, `test_tokens_never_leave_and_files_are_private`); inline-table layout; Dropbox enable-write reusing `read` and Drive enable-write (main's second sign-in, `test_enable_writes_and_disconnect`); enable-write refusals and the `-rw` length check; the full first run (main's `test_ui_onboarding_e2e.py`).
- `tests/fake_rclone.py`, once its iCloud 2FA, several-drives and answer-echo states are in `tests/fakes/rclone`.
- `tests/test_config.py`: every `add_remote_block`/`set_remote_write` test (append, provider defaults, quoting, empty file, duplicate, inline array, not TOML, missing file, set/insert/remove write, other blocks, unknown remote, inline tables, change-more refusal, CRLF, re-parse failure).
- `tests/test_rclone.py`: `version_info`, `list_remotes`, `config_create`/`config_continue` argv, name/type flag refusals, `config_delete` argv, `test_remote` returning errors, every streaming/cancel/timeout test against the PR #5 fake, `test_listing_the_fake_remote_reports_what_rclone_reports`.
- `tests/test_ui_frontend.py`: the PR #5 Setup tests and the `MockApi` Setup routes (rail with Setup first, four steps, Setup scan progress and failure, Setup screenshots, refused-connect reason (main covers)).
- `tests/test_i18n_contract.py`: `test_the_setup_job_failures_send_the_name_so_the_page_can_say_which_cloud`.

**Port** (same assertion, new home):
- to `tests/test_connections.py` (fake `tests/fakes/rclone`): OneDrive with several drives asks which drive and uses the answer, with one drive asks nothing; answer validation (length, `NOT_WAITING`); iCloud needs Apple ID and password up front (`MISSING_FIELD`), then the 2FA question, a wrong code re-asks with `retry`, never echoes; token paste (block and bare JSON, `TOKEN_UNREADABLE` without quoting the input, Drive `NEEDS_CLIENT`) never shows the token back; cancel while a question waits deletes the remote; cancel is idempotent; no answer in time times out and cleans up; rclone failing on create and on continue removes what it wrote; an rclone that echoes the answer cannot leak it (message, flow view, logs, `capsys`, `caplog`); a `connections.toml` write error deletes the new remote (`CONFIG_WRITE`); a name taken meanwhile is refused at the end, and a name reserved by a waiting question blocks a second flow; import adds a read-only `scoped = false` entry and leaves the person's config byte for byte; import refusals (encrypted, unknown, unsupported types, already imported); unknown types become provider `other`; Test classification (`AUTH_EXPIRED`, `NETWORK`, `NOT_FOUND`, `RCLONE_ERROR` with `detail`), Test of a manual remote, Test timeout at 30 s; enable changes on iCloud and on an imported remote reuses `read`; a cancelled write flow changes nothing; `_URL_RE` takes only the helper URL (PR #5's `test_auth_url_pattern_*` parametrisations); `classify_failure` parametrisation stays in `test_rclone.py`; PR #5's `test_real_rclone_setup_calls` becomes a real-rclone test of `importable`/`import_remote` on a `local` remote plus `classify_failure` of a missing remote (skipped without rclone).
- to `tests/test_onboarding_api.py`: the new routes in the human-channel, CSRF/origin and approvals-off parametrisations (`answer`, `paste`, `import`), `importable` readable by bearer and session but not anonymously; `GET /v1/setup` `needed` true with nothing configured and with a never-scanned cloud, false once every cloud is scanned (PR #5's `test_setup_route_adds_needed`, `test_needed_is_false_once_every_cloud_is_scanned`); a sync of a never-scanned connection (PR #5's rescan test); a connection made by `drivemerge connect` while the server runs is visible on the next `GET /v1/setup`; iCloud start → 202 question → answer → 200 connection; finish → 202 for OneDrive with several drives; body validation (`apple_id` for a non-iCloud provider, client fields for iCloud → `BAD_REQUEST`).
- to `tests/test_cli.py`: `drivemerge ui` creates a missing config and hands its path to the server; other commands still hint at `init` when the config is missing; `run_ui` passes the config path to the service; `drivemerge connect iclouddrive` and `drivemerge import` through the fake.
- to `tests/test_config.py`: `write_private` mode 600, through a symbolic link, atomic replace, temp file in the same directory and fsynced (retargeted from the block tests); `ensure_config` and `write_new_config`; `provider_of` parametrisation.
- to `tests/test_ui_onboarding.py` (mocked API): routeless redirect to `#/clouds` only when `needed`, never with a route, nothing changes without `needed`; `#/setup` lands on Clouds; the rail starts with `rail-clouds` and has no `rail-setup` (revert the merge's edit); `Alt+0` does nothing; the question form for every input kind; a sign-in link that is not rclone's helper is never opened or shown; cancel during a question; failed connects worded from the new codes; iCloud fields, with the password and the 2FA code kept out of the URL, storage and toasts; the paste disclosure (command with scope flags, token sent only on submit, never stored); import rows, unsupported and encrypted; Test results worded from codes and `AUTH_EXPIRED` → relogin; Allow changes by confirmation for `scoped: false`; manual-remote cards; the read-only panel with commands and no automatic Test; the Overview empty state links to Clouds; the question form and iCloud dialog in Traditional Chinese; `FLOW_BUSY` and `MISSING_FIELD` worded; every key the new elements use exists in both languages.
- to `tests/test_i18n_contract.py`: PR #5's `test_every_setup_code_has_a_sentence…` becomes `test_every_connect_code_has_a_sentence_in_both_languages_that_uses_only_what_every_site_sends`, collecting `ConnectError(code, …)` sites in `connections.py`, `ui_api.py` and `cli.py` plus the codes of `classify_failure`.

**Add**: the Playwright end-to-end run of §F also connects a fake iCloud (Apple ID, password, 2FA in the question form) and imports one remote from a fake personal rclone config, then syncs all three clouds. `tests/fakes/rclone` gains `config create … iclouddrive` (contacts nothing; asks `config_2fa`, accepts `123456`, re-asks with `Error` otherwise), an OneDrive account with several drives (switched by an environment variable), `--no-obscure`, and `config file`.

### G8. Documentation

- Delete `docs/SETUP.md` and `docs/SETUP.zh-TW.md`.
- `README.md` and `README.zh-TW.md`: remove the SETUP links in the design-documents sentence and rewrite the Setup paragraph as a Clouds paragraph (iCloud, sign-in on another computer, import from your rclone config, read-only fallback) linking to `docs/ONBOARDING.md` / `docs/ONBOARDING.zh-TW.md`.
- `docs/ONBOARDING.zh-TW.md` mirrors this section and the §8 edits with the same headings, tables, code blocks and codes (`tests/test_docs_i18n.py`).

### G9. Risks

- **Imported tokens are copies.** drive-merge's copy and the person's own remote refresh independently. Google and Dropbox refresh tokens do not rotate; Microsoft issues new refresh tokens but keeps old ones valid, so both copies keep working; providers with single-use refresh tokens (Box) are refused. Disconnect deletes only drive-merge's copy.
- **Pasted tokens cannot be checked for scope.** A token made without the shown `--drive-scope=drive.readonly` could write; drive-merge still never writes without a `write` remote, but the "read-only" badge would overstate the token. The dialog says so next to the command.
- **rclone rewriting `rclone.conf`.** A sync refreshing a token while a connection is being created writes the same file from another process. Verify on the pinned rclone version that its config storage reloads a file changed on disk before saving; otherwise run connect/import only while no sync job runs (`FLOW_BUSY`-style refusal).
- **iCloud trust expires** (about 30 days, Apple's rule): the card then shows `relogin` through `AUTH_EXPIRED`; reconnecting is Disconnect then Connect.
- **Existing PR #5 users** may already have `[[remotes]]` blocks written by Setup in `config.toml`; they keep working as manual remotes (cards with Test and Sync) and are not migrated.
