# drive-merge

Merge files from Google Drive, Dropbox, OneDrive and iCloud into one place — with deduplication and a dry-run plan before anything moves.

drive-merge runs on your machine. It catalogs **metadata** (never file contents) from every cloud into a local SQLite database via [rclone](https://rclone.org), tells you where each piece of content lives, proves which files are duplicates, proposes a clean folder layout, and — only through simulated, human-approved plans — consolidates everything into one cloud.

設計文件（繁體中文）：[docs/DESIGN.md](docs/DESIGN.md) — architecture, safety model, and the open decisions you can adjust.

## Why it is built this way

- **No hash is shared by all four clouds** (Drive: md5/sha1/sha256, Dropbox: content hash, OneDrive: QuickXorHash, iCloud: none). drive-merge downloads *one* copy of an ambiguous file, computes all five hash types locally, and uses that "bridge" to prove copies on the other clouds identical without downloading them.
- **The catalog is built per remote, not through rclone `union`.** Union merges by path and shows one copy, which hides exactly the provenance a dedup tool needs. Use `combine` if you want a browsable single view (`drivemerge rclone-combine`).
- **Nothing is written to a cloud except by an approved plan.** Plans are simulated against the catalog (no content may lose its last copy, every delete needs a provably identical surviving witness, no overwrites, case collisions, ...). Approval is bound to the simulation digest and needs an interactive terminal. Deletes go to a quarantine folder and can be restored.
- **LLMs propose, humans approve.** Claude Code (via MCP) can search, read folder profiles, write layout rules, create and simulate plans. It cannot approve or execute them.

## Install

Requires Python 3.11+ and rclone ≥ 1.69 (for iCloud).

```bash
pip install -e '.[api,mcp]'      # or: uv pip install -e '.[api,mcp]'
drivemerge init                  # writes ~/.config/drive-merge/config.toml and an API token
```

Configure one rclone remote per cloud (`rclone config`). Recommended: a read-only remote for scanning (e.g. Google Drive with scope `drive.readonly`) and a separate writable remote only if/when you want drive-merge to change that cloud. Note: rclone's shared Google Drive client_id is being retired in 2026 — [create your own](https://rclone.org/drive/#making-your-own-client-id).

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

```bash
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

API and MCP:

```bash
drivemerge serve                           # REST API on 127.0.0.1:8765 (Bearer token)
claude mcp add drive-merge -- drivemerge mcp   # let Claude Code explore and propose plans
```

External databases can sync incrementally from `GET /v1/changes?cursor=...`.

## Development

```bash
uv venv && uv pip install -e '.[dev]'
pytest
```

The core (`src/drivemerge`) uses only the Python standard library; FastAPI and the MCP SDK are optional extras.
