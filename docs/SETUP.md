# First Run in the Browser

**English** | [繁體中文](SETUP.zh-TW.md)

> This document adds a **Setup** screen to the visual workspace ([WORKSPACE.md](WORKSPACE.md)): from `pip install` to a scanned catalog without leaving the browser. A person checks rclone, connects Google Drive, Dropbox, OneDrive or iCloud by signing in to the provider, adopts rclone remotes they already have, and runs the first scan. The first part is the design; the second part (the implementation contract) is binding for implementers.

## 1. The problem

Today the first hour happens in a terminal: `drivemerge init`, `rclone config` once per cloud (a long text questionnaire), editing `[[remotes]]` in `config.toml` by hand so that its names match rclone's, then `drivemerge scan --all` and `drivemerge identity rebuild`. Only then does `drivemerge ui` show anything. Each step is easy to get slightly wrong (a remote name that does not match, a Drive remote with full write scope used for scanning, a forgotten identity rebuild), and none of it is visible from the page.

## 2. The scenario after this change

```
pip install drive-merge            # and rclone ≥ 1.69
drivemerge ui                      # no config yet: it is created, and the page opens on Setup
```

1. **Check**: the page shows the rclone version found (or how to install it, and the `[rclone] binary` setting).
2. **Connect**: "Connect a cloud" → pick Google Drive / Dropbox / OneDrive / iCloud → a name (`gdrive`) → "Sign in". A new browser tab opens the provider's own sign-in page (served through rclone's sign-in helper on `127.0.0.1:53682`); after "Allow", the page continues by itself. Questions some providers ask after signing in (OneDrive: which drive; iCloud: the two-factor code) appear as a small form. iCloud has no browser sign-in: its form asks for the Apple ID and password up front, because rclone needs them to start. The cloud is then listed as connected, read-only.
3. **Adopt**: rclone remotes that already exist on this machine and are not yet in drive-merge are listed with "Add"; nothing needs to be signed in again.
4. **Scan**: "Scan all" (or per cloud) runs the first scan and the identity rebuild as one job, with live counts per cloud.
5. **Done**: "Open Overview".

The rail gets a sixth item, **Setup**, which stays useful after the first run: add a cloud later, see each cloud's state, rescan.

## 3. Principles

- **Read-only by default.** A cloud connected here is added to drive-merge without a `write` remote, so nothing in drive-merge can change it until the person enables writing. For Google Drive the remote is created with `scope = drive.readonly`. Enabling writing is a separate, explicit step (§6) that creates or names a writable remote.
- **rclone stays the source of truth for credentials.** drive-merge never sees or stores OAuth tokens: rclone writes them to its own config file. drive-merge writes only the `[[remotes]]` entry of its `config.toml` (name, provider, rclone path).
- **Only a person at the browser connects clouds.** Connecting a cloud grants read access to everything in it, so it goes through the human channel (session cookie, CSRF header, same origin, and a server started from an interactive terminal outside an AI agent), exactly like approving a plan. Claude over MCP and the API token cannot add clouds or start scans from here.
- **Generic over special-cased.** rclone's non-interactive configuration asks its questions as JSON (name, help, default, examples, password or not). The page renders any such question with one form, so a provider that asks something new still works, and the few provider choices drive-merge makes itself (Drive scope, read-only) are set before the questions start.
- **Secrets pass through, never stay.** A password (iCloud) or a pasted token is forwarded to rclone, which obscures it in its config; drive-merge does not log it, return it, or keep it after the request.

## 4. Layout

```
┌ rail ┐┌──────────────────────────────────────────────────────────────────────────┐
│  ⚙   ││ Setup                                                                    │
│  ◎   ││ ① rclone        ✓ rclone v1.71.1                                         │
│  ⌕   ││ ② Clouds        gdrive   Google Drive  read-only  ✓ connected  [Test]    │
│  ⇆   ││                 dropbox  Dropbox       read-only  ✓ connected  [Test]    │
│  ☰   ││                 [ + Connect a cloud ]                                     │
│  ▦   ││                 Found in rclone: work-od (onedrive)            [ Add ]    │
│      ││ ③ First scan    gdrive ✓ 41,203 files · dropbox ⟳ 12,004 files…          │
│      ││                 [ Scan all ]                                              │
│      ││ ④ Ready         [ Open Overview ]   execution is off (that is fine for now)│
└──────┘└──────────────────────────────────────────────────────────────────────────┘
```

- Each step shows its state (done, needs attention, not yet) and is never locked: a person who already has a scanned catalog sees four green steps.
- **Test** lists the root of the remote (`rclone lsjson --max-depth 1`) and reports success, or the rclone error worded from a code (expired token, not found, network).
- With approvals disabled (the server was started by an agent or without a terminal), the screen is read-only and shows the commands instead: `rclone config`, the `[[remotes]]` block to paste, `drivemerge scan --all`.

## 5. Connect flow

```
Connect a cloud
  Provider   (•) Google Drive  ( ) Dropbox  ( ) OneDrive  ( ) iCloud Drive
  Name       [ gdrive        ]   (letters, digits, - and _; must be new in rclone and drive-merge)
  ▸ Advanced (Drive): your own OAuth client ID and secret — recommended, rclone's shared ID is being retired
  [ Sign in ]
        ↓
  "A sign-in page opened in a new tab. Finish there; this page continues by itself."   [ Open it again ] [ Cancel ]
        ↓  (provider questions, if any, one at a time)
  OneDrive: Which drive?  (•) OneDrive (personal)  ( ) Documents (business)
        ↓
  ✓ gdrive connected (read-only).  [ Scan now ]
```

- The browser tab for signing in is opened by the page (a link the person clicks, so pop-up blockers do not interfere), pointing at the `http://127.0.0.1:53682/auth?state=…` URL rclone printed.
- Cancel (or 10 minutes without an answer) stops rclone's helper and removes the half-made rclone remote.
- If rclone cannot use a local browser (for example when `drivemerge ui` runs on another machine), the "use a browser on this machine?" question is answered "no" and rclone asks for a token made by `rclone authorize` elsewhere; the generic question form shows rclone's own help for that.

## 6. Enabling writing (later)

Consolidating or deduplicating needs a writable remote. On the cloud's row, **Enable changes…** explains what that means and:
- for Drive, creates a second rclone remote (`<name>-rw`, full `drive` scope) through the same sign-in flow and records it as the `write` path;
- for the others, records the same rclone remote as the `write` path (their tokens are not scope-limited).

Execution itself still needs `[safety] execution_enabled = true`, which this screen shows but does not change: turning the master switch on stays a deliberate edit of the config file.

## 7. Known limitations (v1)

- One sign-in at a time (rclone's helper listens on one fixed port).
- Removing a cloud from drive-merge, renaming it, and editing `extra_args` stay in the config file.
- The config file is rewritten only by appending or updating one `[[remotes]]` block; comments elsewhere are kept, comments inside a block that is updated are not.
- Answers and the iCloud password reach rclone as command-line arguments, as with `rclone config create` typed by hand: while that command runs, other accounts on a shared computer can read them from the process list. Use a single-user machine, or configure such clouds with `rclone config` in a terminal.

---

# Implementation contract

Everything below is binding. Read first: [WORKSPACE.md](WORKSPACE.md) §D/§F/§G, [EXPLORE.md](EXPLORE.md) §F, [PLANS.md](PLANS.md) §C/§E, `config.py`, `rclone.py`, `cli.py` (`cmd_init`, `cmd_ui`, `_write_private`), `service.py`, `ui_api.py` (sessions, `require_human`, jobs, `rescan`, `run_ui`), `providers.py`, and every file in `src/drivemerge/web/`.

## A. rclone layer (`rclone.py`)

Add to `Rclone` (all honour the configured binary and `--config`):
- `version_info() -> {"version": "v1.71.1", "ok": bool}` (`ok` = ≥ 1.69); `None` when the binary is missing.
- `list_remotes() -> list[{"name", "type"}]` from `rclone listremotes --long --json` (fall back to parsing `--long` text on older versions).
- `config_create(name, rtype, options: dict[str, str]) -> dict` runs `rclone config create NAME TYPE k=v… --non-interactive` and returns rclone's JSON (`State`, `Option`, `Error`, `Result`); `config_continue(name, state, result) -> dict` runs `rclone config update NAME --continue --state S --result R --non-interactive`. Both are **streamed**: the process runs with stdout and stderr piped; a callback receives each stderr line, so the caller can pick up `http://127.0.0.1:53682/auth?state=…` while rclone waits for the browser. Return value: the parsed JSON on stdout (an empty `State` means configuration is finished). Both take a `cancel: threading.Event` that kills the process.
- `config_delete(name)`, `test_remote(remote) -> None | RcloneError` (`lsjson --max-depth 1` on the root with a 30 s timeout).
- Never put an option value whose question `IsPassword` was true on a command line that is logged; pass the value as an argument (argv is not logged by this module) and keep it out of every exception message (`RcloneError` already carries only the stderr tail; check that rclone does not echo it).

## B. Config writing (`config.py`)

- `add_remote_block(path, remote: RemoteConfig) -> None`: appends a `[[remotes]]` block (name, provider, read, write if set, extra_args from the provider defaults below) to the TOML file, after re-reading it; refuses a duplicate name (`REMOTE_EXISTS`); writes atomically (temp file in the same directory, fsync, rename) keeping mode 600; re-parses the result with `parse_config` before replacing the file and leaves the file untouched on any error.
- `set_remote_write(path, name, write: str | None)`: updates the `write` key of that remote's block in place (the block is found by its `name = "…"` line; refuse with `REMOTE_NOT_FOUND` if absent, or `CONFIG_LAYOUT` when the block cannot be located unambiguously, e.g. remotes written as inline tables). Same atomic write and re-parse.
- Provider defaults written into new blocks: drive → `extra_args = ["--fast-list", "--drive-skip-shortcuts"]`; others none.
- rclone types → provider keys: `drive`→`drive`, `dropbox`→`dropbox`, `onedrive`→`onedrive`, `iclouddrive`→`iclouddrive`, `local`→`local`, anything else → `other`.
- `ensure_config(path) -> bool` creates the config exactly as `drivemerge init` does when the file is missing (shared helper; `cmd_init` uses it) and returns whether it created one.

## C. Service (`service.py`)

- `Service.reload_config(config)`: replaces `self.config`, re-syncs remotes into the catalog and refreshes anything derived from the config (the rclone wrapper if its settings changed). The UI server calls it after every config write, under the request lock, and uses the new config object for later jobs.
- `Service.setup_state() -> dict`: `{"config_path", "rclone": {"found", "version", "ok", "binary"}, "remotes": [{"name", "provider", "read", "write", "writable", "last_scan": {...} | None, "files", "bytes"}], "rclone_remotes": [{"name", "type", "provider", "in_config": bool}], "scanned": bool (every configured remote has a complete scan), "identity_built": bool, "execution_enabled": bool}`. The rclone remotes listing is cached for 10 s.

## D. HTTP (`ui_api.py`)

| Method & path | Channel | Body / query | Response |
|---|---|---|---|
| `GET /v1/ui/setup` | normal | | `Service.setup_state()` plus `"needed": bool` = no remotes configured, or `scanned` false |
| `POST /v1/ui/setup/connect` | human | `{"provider": "drive"\|"dropbox"\|"onedrive"\|"iclouddrive", "name", "client_id"?, "client_secret"? (Drive only), "apple_id"?, "password"? (iCloud only, both required there), "remote_auth"?: bool}` | 202 `{"job_id"}`; refuses `BAD_NAME` (regex `^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$`), `MISSING_FIELD` `{field}`, `REMOTE_EXISTS` (in rclone or in drive-merge), `RCLONE_MISSING`, `JOB_BUSY`; a field sent for the wrong provider is `BAD_REQUEST` |
| `POST /v1/ui/setup/connect/{job_id}/answer` | human | `{"result": str}` | `{"ok"}`; 409 `NOT_WAITING` when the job is not waiting for an answer |
| `POST /v1/ui/setup/connect/{job_id}/cancel` | human | | `{"ok"}` |
| `POST /v1/ui/setup/adopt` | human | `{"name"}` (an rclone remote not yet in drive-merge) | `{"remote": {...}}`; adds a read-only block |
| `POST /v1/ui/setup/test` | normal | `{"name"}` | `{"ok": true}` or `{"ok": false, "code", "params"}` with codes `AUTH_EXPIRED`, `NOT_FOUND`, `NETWORK`, `RCLONE_ERROR` (classified from rclone's stderr) |
| `POST /v1/ui/setup/enable-write` | human | `{"name"}` | Drive: 202 `{"job_id"}` (a connect job for `<name>-rw` with full scope that sets `write` when done); others: `{"remote": {...}}` |
| `POST /v1/ui/rescan` | human | as today | as today; must also work for remotes that have never been scanned |

The connect job (`kind: "connect"`) drives rclone's state machine in its own thread:
1. `config_create` with the provider's preset options: Drive `scope=drive.readonly` (or `drive` for `-rw`) plus `client_id`/`client_secret` when given; others none.
2. Loop over the returned states. For `config_is_local` answer `true` automatically (unless the request asked for remote authorisation). While rclone waits for the browser, the job's progress is `{"stage": "signin", "auth_url": "http://127.0.0.1:53682/auth?state=…"}` (only URLs on `127.0.0.1:53682` are passed through; anything else is dropped). For any other question, progress becomes `{"stage": "question", "question": {"name", "help", "default", "examples": [{"value", "help"}], "password": bool, "required": bool, "exclusive": bool, "type"}}` and the job waits (up to 10 minutes) for `/answer`; `"retry": true` is added when rclone rejected the previous answer (its error text is not forwarded). `remote_auth: true` answers `config_is_local` with `false`, so rclone asks for a token made by `rclone authorize` as an ordinary question. Every progress carries `{"stage", "name", "provider", "mode": "new" | "write"}`; stages are `starting`, `working`, `signin`, `question`, `saving`, `done`, `failed`.
3. When `State` is empty: `add_remote_block` (read-only, or `set_remote_write` for an `-rw` job), `reload_config`, result `{"remote": {...}}`.
4. On cancel, timeout, an rclone error or a config-writing error: `config_delete(name)` if this job created it, job fails with `{"code", "params"}` (`SIGNIN_TIMEOUT`, `CANCELLED`, `RCLONE_ERROR`, `CONFIG_WRITE`).

The job view never contains an answer the person typed. Every refusal is `{"error", "code", "params"}` and every new code needs `err.<CODE>` in both languages.

`run_ui` / `cmd_ui`: when the config file does not exist, `drivemerge ui` creates it with `ensure_config` (printing where), instead of failing, and passes `config_path` to `create_ui_app`, which needs it to write. `create_ui_app(..., config_path: Path | None)`: without a path the setup write routes answer 409 `NO_CONFIG_PATH`.

## E. Frontend (`src/drivemerge/web/`)

- `setup.js`: the Setup view (`#/setup`), the connect dialog (provider, name, advanced client id/secret for Drive), the sign-in waiting state with an "Open sign-in page" link (`target="_blank"`, `rel="noopener"`) and Cancel, the generic question form (radio group for exclusive examples, select-or-type for non-exclusive, password input for `password`, booleans as two buttons), adopt rows, Test buttons, Scan all / per-cloud scan with live counts (polling the job), Enable changes, the read-only fallback with copyable commands and a `[[remotes]]` block.
- `shell.js`: Setup is the first rail item (`Alt+0`); the other shortcuts keep their numbers. When `GET /v1/ui/setup` says `needed`, a page opened without a route goes to `#/setup` instead of the workspace (a page with a route goes where the route says). A mocked server that does not answer `/v1/ui/setup` (or answers without `needed`) changes nothing, so every existing test still loads the workspace.
- Overview: when no cloud is scanned, its empty state links to Setup.
- i18n: `setup.*`, `connect.*`, `q.rclone.*` (only for wording around the generic form; rclone's own help text is shown as received, in a box marked as rclone's text, since it is English and dynamic), `err.<CODE>` for every new code.

## F. Tests

- `rclone.py` against a fake rclone script (like the existing tests' fakes) that implements `config create/update --non-interactive` with states, prints an auth URL on stderr and waits, `listremotes`, `config delete`, `lsjson` errors: streaming callback, cancel kills, JSON parsing, version parsing.
- `config.py`: append and update blocks with comments elsewhere preserved, atomic write, mode 600, duplicate and missing names, inline-table layout refused, the file untouched on a failed re-parse, `ensure_config`.
- HTTP: every route's channel (bearer and agent-started servers refused on the human ones), the full connect job against the fake rclone (sign-in stage with `auth_url`, a question, an answer, completion writes the block and `reload_config` makes the remote scannable at once), cancel and timeout delete the half-made rclone remote, the answer never appears in a job view, adopt, test classification, enable-write for Drive and for Dropbox, rescan of a never-scanned remote, `drivemerge ui` without a config creates one.
- Browser (mocked API): redirect to Setup only when `needed`; the four steps' states; connect flow (sign-in link, question form for each input kind, answer body, cancel); adopt; test results worded from codes; scan progress; enable-write; read-only fallback; both languages; existing tests unchanged.
- The whole suite stays green: `pytest` (with the browser tests).
