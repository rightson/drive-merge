# drive-merge

**English** | [繁體中文](README.zh-TW.md)

Merge files from Google Drive, Dropbox, OneDrive and iCloud into one place — with deduplication and a dry-run plan before anything moves.

drive-merge runs on your machine. It catalogs **metadata** (never file contents) from every cloud into a local SQLite database via [rclone](https://rclone.org), tells you where each piece of content lives, proves which files are duplicates, proposes a clean folder layout, and — only through simulated, human-approved plans — consolidates everything into one cloud.

Design documents: [docs/DESIGN.md](docs/DESIGN.md) — architecture, safety model, and the open decisions you can adjust — [docs/WORKSPACE.md](docs/WORKSPACE.md) — the visual workspace — and [docs/EXPLORE.md](docs/EXPLORE.md) — the Overview, Explore and Compare views — [docs/PLANS.md](docs/PLANS.md) — the Plans view — and [docs/SETUP.md](docs/SETUP.md) — the first run in the browser. Traditional Chinese versions exist as `docs/*.zh-TW.md`.

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

The terminal route for clouds the page does not offer (iCloud), or for scripting: `drivemerge connect dropbox|onedrive|drive` signs in from the terminal, or configure one rclone remote per cloud yourself (`rclone config`). Recommended: a read-only remote for scanning (e.g. Google Drive with scope `drive.readonly`) and a separate writable remote only if/when you want drive-merge to change that cloud. Note: rclone's shared Google Drive client_id is being retired in 2026 — [create your own](https://rclone.org/drive/#making-your-own-client-id).

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

The first step can be the browser: `drivemerge ui` creates the config if it is missing and opens on the **Setup** screen, where you connect your clouds and run the first scan (see below). Or do the same from a terminal:

```bash
drivemerge ui                              # first step, in the browser: Setup connects clouds and scans them
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

The **Setup** screen (first item of the rail, `Alt+0`; a page opened with nothing to work on yet lands there) takes a person from `pip install` to a scanned catalog: it checks rclone, connects Google Drive, Dropbox, OneDrive or iCloud by signing in to the provider in a new tab (through rclone's own sign-in helper on `127.0.0.1:53682`, so drive-merge never sees a token), adopts rclone remotes you already have, tests them, runs the first scan with live counts, and later enables changes per cloud. Clouds are added read-only. Connecting and scanning are possible only from the page of a `drivemerge ui` started in your own terminal; otherwise the screen is read-only and shows the commands to run instead. See [docs/SETUP.md](docs/SETUP.md).

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
