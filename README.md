# drive-merge

**English** | [繁體中文](README.zh-TW.md)

Merge files from Google Drive, Dropbox, OneDrive and iCloud into one place — with deduplication and a dry-run plan before anything moves.

drive-merge runs on your machine. It catalogs **metadata** (never file contents) from every cloud into a local SQLite database via [rclone](https://rclone.org), tells you where each piece of content lives, proves which files are duplicates, proposes a clean folder layout, and — only through simulated, human-approved plans — consolidates everything into one cloud.

Design documents: [docs/DESIGN.md](docs/DESIGN.md) — architecture, safety model, and the open decisions you can adjust — [docs/WORKSPACE.md](docs/WORKSPACE.md) — the visual workspace — and [docs/EXPLORE.md](docs/EXPLORE.md) — the Overview, Explore and Compare views — [docs/PLANS.md](docs/PLANS.md) — the Plans view — [docs/QUARANTINE.md](docs/QUARANTINE.md) — the Quarantine view — and [docs/RULES.md](docs/RULES.md) — the Rules view. Traditional Chinese versions exist as `docs/*.zh-TW.md`.

## Why it is built this way

- **No hash is shared by all four clouds** (Drive: md5/sha1/sha256, Dropbox: content hash, OneDrive: QuickXorHash, iCloud: none), and only a collision-resistant hash proves two files identical: sha256 and the Dropbox hash. md5, sha1 (practical collision attacks exist, and every cloud that reports it reports sha256 too) and QuickXorHash can show that two files differ, never that they match. drive-merge downloads *one* copy of an ambiguous file, computes all five hash types locally, and uses that "bridge" to prove copies that carry a strong hash on the other clouds identical without downloading them. A OneDrive or iCloud file with no strong hash becomes exact only through a bridge made from its own download.
- **The catalog is built per remote, not through rclone `union`.** Union merges by path and shows one copy, which hides exactly the provenance a dedup tool needs. Use `combine` if you want a browsable single view (`drivemerge rclone-combine`).
- **Nothing is written to a cloud except by an approved plan.** Plans are simulated against the catalog (no content may lose its last copy, every delete needs a provably identical surviving witness, no overwrites, case collisions, ...). Approval is bound to the simulation digest and needs an interactive terminal. Deletes go to a quarantine folder and can be restored.
- **LLMs propose, humans approve.** Claude Code (via MCP) can search, read folder profiles, write layout rules, create and simulate plans. It cannot approve or execute them.

## Quick start

Requires Python 3.11+ and [rclone](https://rclone.org/install/) ≥ 1.69 (the page can install it for you). No config file and no `rclone config`.

```bash
pip install 'drive-merge[api]'
drivemerge ui                    # first run creates ~/.config/drive-merge/ and opens the page
```

1. **Connect.** The page opens on a Welcome screen with one card per cloud. Dropbox and OneDrive are one button each; Google Drive needs your own OAuth client, and the dialog walks you through it (about 5 minutes, once). A connection is read-only: the card shows the account, the quota and "connected, read-only".
2. **Sync.** One button lists every connected cloud in parallel into the local catalog, with progress per cloud. Everything after that (Overview, Explore, Compare) is a local query.
3. **Free space.** Open **Free space…** on a cloud: it shows how much of it is *proven* to exist on your other clouds (identical bytes), what is only *probable* (one click, **Verify**, settles it) and what exists only there. **Allow changes** (a second sign-in) lets drive-merge create a plan that frees the proven part; you review it, type the approval code, run it, and **Purge now** returns the space. Nothing is changed before that. See [docs/ONBOARDING.md](docs/ONBOARDING.md).

## Install

Requires Python 3.11+ and rclone ≥ 1.69 (for iCloud).

```bash
pip install -e '.[api,mcp]'      # or: uv pip install -e '.[api,mcp]'
drivemerge init                  # writes ~/.config/drive-merge/config.toml and an API token
```

The terminal route, for scripting or when the page is read-only: `drivemerge connect dropbox|onedrive|drive|iclouddrive` signs in from the terminal, or configure one rclone remote per cloud yourself (`rclone config`). Recommended: a read-only remote for scanning (e.g. Google Drive with scope `drive.readonly`) and a separate writable remote only if/when you want drive-merge to change that cloud. Note: rclone's shared Google Drive client_id is being retired in 2026 — [create your own](https://rclone.org/drive/#making-your-own-client-id).

Edit the config (see [examples/config.toml](examples/config.toml)):

```toml
[[remotes]]
name = "gdrive"
provider = "drive"
read = "gdrive-ro:"
write = "gdrive:"
extra_args = ["--fast-list", "--drive-skip-shortcuts"]

[[remotes]]
name = "dropbox"
provider = "dropbox"
read = "dropbox:"
```

## Use

The first step can be the browser: `drivemerge ui` creates the config if it is missing and opens on the **Clouds** view, where you connect your clouds and sync them (see below). Or do the same from a terminal:

```bash
drivemerge ui                              # first step, in the browser: Clouds connects clouds and syncs them
drivemerge scan --all                      # list every cloud into the catalog (read-only)
drivemerge identity rebuild                # group identical content
drivemerge stats
drivemerge search "報告 2023"               # substring search, works for CJK names
drivemerge duplicates --cross-remote
drivemerge bridge --max-bytes 5G           # download+hash ambiguous files once, then rebuild
drivemerge profiles --remote dropbox --depth 2
drivemerge rules preview --rules my-rules.toml --target-root Archive

drivemerge plan consolidate --target gdrive --root Archive   # copy-only by default
drivemerge plan show 1                     # simulation report: before/after per cloud
drivemerge plan approve 1                  # interactive: type the 8-char code
drivemerge plan execute 1                  # needs [safety] execution_enabled = true
drivemerge plan restore 1                  # undo deletes (moves them back from quarantine)
```

### Visual workspace

```bash
drivemerge ui                              # opens http://127.0.0.1:8765/ui/ (one-time token in the URL)
```

Two tree panes show any two places side by side, across clouds. Drag files and folders to better levels, rename, undo and redo, flip between the "after" and "before" views, and take suggestions for related or duplicate folders. Nothing touches a cloud while you drag: the staged moves are previewed through the same simulator as every other plan (before/after per cloud, conflicts, dedupes), and only a typed 8-character approval code starts the execution — and only when `drivemerge ui` runs in your own interactive terminal, outside an AI agent. The page is in English, or in Traditional Chinese when your browser lists a Chinese language; the `EN` / `中文` button switches and is remembered. See [docs/WORKSPACE.md](docs/WORKSPACE.md).

The rail on the left of the page also opens three read-only views that lead into the workspace: **Overview** (how your clouds overlap: an intersection chart of which content lives on which clouds, and what exists on only one), **Explore** (find a file by name or by a query such as `type:pdf not:gdrive copies:1`; identical copies are grouped, with one presence column per cloud) and **Compare** (an aligned diff of any two folders on any clouds: same, different, moved, one side only, with a button that stages a merge into your draft). They never write a cloud or change the catalog. See [docs/EXPLORE.md](docs/EXPLORE.md).

The **Plans** view builds and reviews whole-library plans from the page: a dedupe plan (with a live estimate and a per-group choice of which copy stays), a consolidate plan (copy-only by default; cleaning up sources sits behind a guarded section), and the review of any plan, including the ones Claude proposes, with the same simulation and the same typed approval code. The Overview can also bridge files that have no comparable hash. See [docs/PLANS.md](docs/PLANS.md).

The **Quarantine** view (after Plans, `Alt+6`) lists what every executed plan parked in the quarantine folders: per cloud and per plan, how much space it holds and since when. A whole plan, a folder or single files can be put back where they were (never overwriting anything), and what is past its retention (`[safety] quarantine_retention_days`, default 30) can be purged for good, after a read-only check against the catalog and with a typed confirmation (`restore` or `purge`), exactly as `drivemerge plan restore` and `drivemerge plan purge` do. Without approvals the page shows the terminal command instead. See [docs/QUARANTINE.md](docs/QUARANTINE.md).

The **Rules** view (after Quarantine, `Alt+7`) edits the layout rules that decide where consolidate and reorganize plans put files: a text editor with live validation (every error with its line, worded in your language), a live before/after preview over the real catalog (sampled and marked "≈" on large catalogs), a diff against the rules in force, and the review of rule changes Claude proposes, which are never active until you activate them (on the page with approvals, otherwise `drivemerge rules activate <id>` in your own terminal). "Use in plan" opens the consolidate and reorganize forms with the edited rules, and every plan records the rules it was built with. See [docs/RULES.md](docs/RULES.md).

The **Clouds** view (first item of the rail; a page opened with nothing to work on yet lands there) takes a person from `pip install` to a synced catalog. Dropbox, OneDrive and Google Drive sign in to the provider in a new tab through rclone's own helper on `127.0.0.1:53682`, so drive-merge never sees a token; **iCloud Drive** takes your Apple ID and password and then asks for the two-factor code in a form. If the browser is not on the computer that runs drive-merge, **Sign in on another computer** shows the exact `rclone authorize` command to run there and takes the token you paste back. **Already use rclone?** lists the remotes of your own rclone config and imports one as a read-only connection without touching that file, and a cloud you set up by hand in `config.toml` gets a card too. Test results are worded by cause (sign-in expired, no network, not found). Connecting and syncing are possible only from the page of a `drivemerge ui` started in your own terminal; otherwise the view is read-only and shows the `drivemerge connect` commands to run instead. See [docs/ONBOARDING.md](docs/ONBOARDING.md).

API and MCP:

```bash
drivemerge serve                           # REST API on 127.0.0.1:8765 (Bearer token)
claude mcp add drive-merge -- drivemerge mcp   # let Claude Code explore and propose plans
```

External databases can sync incrementally from `GET /v1/changes?cursor=...`.

## Development

```bash
uv venv && uv pip install -e '.[dev]'
pytest                  # everything, including the browser tests (headless Chromium via Playwright)
pytest -m "not ui"      # without them
```

The core (`src/drivemerge`) uses only the Python standard library; FastAPI and the MCP SDK are optional extras.
