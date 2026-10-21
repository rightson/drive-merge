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
- iCloud Drive is not on the Welcome screen: its sign-in needs the Apple ID password and a 2FA code in a terminal (`rclone config`), then it can be added as a connection by name.
- Google Drive needs your own OAuth client (§2).
- Sign-in requires the browser on the same machine as drive-merge (loopback callback).

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
