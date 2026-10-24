# Visual Workspace

**English** | [繁體中文](WORKSPACE.zh-TW.md)

> This document expands [DESIGN.md](DESIGN.md) §15: the first part is the design, and the second part (the implementation contract) is the precise contract for implementers.

## 1. Usage scenario

"I have some school and research material scattered across different directories (even different clouds). I want to open two panes side by side, see at which level things under different paths sit, drag files or folders to the right level, drag back and forth until I'm happy, and then preview the result; only after I've checked it and approved it should anything in the cloud actually change."

The corresponding interaction model:

1. **Two panes**, each an expandable tree view; the root can be "all clouds" (one top-level node per cloud, equivalent to the view of rclone `combine`) or a single cloud.
2. **Drag and drop = staging an action**, without touching the cloud. You can drop between different levels within one pane, or between the left and right panes (including across clouds); you can also rename items and create new folders.
3. **Dragging back and forth** does not pile up into a chain of operations: only the "net effect" is computed in the end — anything dragged back to where it started counts as not having moved.
4. Each drop is one **undo step** (Ctrl+Z / Ctrl+Shift+Z).
5. The panes show the virtual tree **after** the changes by default (staged effects are visible immediately); you can switch to the "before" view, and turn on "show original locations" to see where each item was moved to.
6. **Preview** = turn the staged net effect into a Plan → check it with the simulator → show before/after, the volume transferred across clouds, and a list of conflicts and problems.
7. **Approve and execute** requires a human to type an 8-character confirmation code in the browser (see §4); progress is shown while it runs; once it finishes, the affected clouds can be rescanned with one click.
8. **Suggestions**: the service proactively finds folders that are "scattered but related" (for example school or research folders with similar names or contents) and "duplicate folders with nearly identical contents", and adds a suggestion to the staging with one click; you can still adjust it afterwards.
9. **Claude can propose too**: through MCP it adds moves to the same draft (labelled "proposed by Claude"), and the screen updates live; but Claude cannot approve or execute.

## 2. Why it is designed this way

- **A draft is a sequence of actions, not a target tree**: what the user sees is the "after" view, and they keep dragging and dropping within it, so the source path of every action is the "virtual path at that time". Applying the actions in order (function composition) naturally supports "drag A into B, then drag B into C" as well as dragging back and forth; the net effect is computed from each file's "original location → final location" and is independent of the intermediate steps.
- **Folders with the same name = merge**: the most common need when tidying scattered data is to merge two folders with the same name or topic. On a merge, files with identical content automatically become "keep one copy, move the other to quarantine" (a witnessed dedupe); files with the same name but different content are listed as conflicts for the user to resolve by renaming — nothing is ever overwritten.
- **A cross-cloud move = copy + verify + move the source into quarantine** (the dependencies guarantee that verification happens before anything is deleted).
- **A preview always goes through the simulator**: the visual interface is just another way to produce a Plan, and the safety invariants are exactly the same ([DESIGN.md](DESIGN.md) §7).
- **The draft version is bound to the preview**: any change to the draft invalidates the earlier preview, and you must preview again before you can approve.
- **Steps that become stale after a rescan**: a rescan can make the source of a step in the draft disappear (moved away or deleted). Such a step produces no operations, so the draft and the preview both mark it as "stale" and explain why, instead of silently skipping it; the remaining steps are previewed as usual.
- **Quarantine cannot be dragged or dropped**: the quarantine folder holds deleted files, which are restored or purged in the Quarantine view ([QUARANTINE.md](QUARANTINE.md)) or with the CLI (`drivemerge plan restore` / `purge`); on screen it has its own distinct style, nothing can be dragged out of it or into it, and staging such a move is rejected (`QUARANTINE`).

## 3. Suggestion engine

| Type | Criteria | Suggestion |
|---|---|---|
| Related folders (`related`) | TF-IDF similarity of the folder-name vocabulary (English words, CJK bigrams) plus the vocabulary of the file names inside, together with evidence of shared content (duplicate files); pairs that already share a parent directory or are ancestor and descendant of each other are excluded | Take the folder with the most files (or the one on the target cloud) as the anchor; move the other folders into the anchor to become its subfolders |
| Duplicate folders (`duplicates`) | Jaccard similarity of the two folders' content groups ≥ 0.9 (counting only content that is proven identical); when the parent level is also a duplicate, only the parent is reported | Merge one into the other → identical files automatically become witnessed dedupes |

Things that are close in meaning but differ in wording (for example "論文" vs "thesis") are beyond a rule-based engine; this is exactly where Claude comes in, reading the folder profiles through MCP and then proposing moves.

## 4. Approval channel (humans only)

Threat model: prevent an LLM (or an LLM that has been prompt-injected through a file name) from changing the cloud without a human's confirmation; it does not defend against malware running with the user's privileges.

- `drivemerge ui` prints a one-time URL `http://127.0.0.1:8765/ui/#token=…` in the terminal (the token lives only in memory and is never written to a file). The browser exchanges it for an HttpOnly, SameSite=Strict session cookie.
- **Only** the UI session (cookie + CSRF header + Origin check) may call approve/execute; the REST API's Bearer token and MCP never can.
- Approval is enabled only when `drivemerge ui` is started from an interactive terminal and neither `CLAUDECODE` nor `AI_AGENT` is set in the environment; otherwise the UI is in "propose-only" mode and shows the commands that must be run in the terminal.
- Approval still requires typing the 8 characters of the simulation digest; execution still requires `[safety] execution_enabled = true`.

## 5. Known limitations (v1)

- Execution works per file (moving a folder within the same cloud moves it file by file; this is slower for large numbers of files on Drive). A future op could move a whole folder in a single server-side operation.
- Empty folders left behind after a move are not deleted automatically (the preview lists how many there are). Folders that were added but have nothing put in them are not created.
- When several browser tabs edit the same draft at the same time, the server's version wins (the page detects the version change and refreshes).
- A folder with no file under it cannot be moved: execution works per file, so nothing would create it at the new place. The preview lists such folders (`empty_dirs_skipped`) instead of dropping them silently and does not count them as changes; a draft that does nothing else has nothing to run.
- A rename that only changes the case of a name on a cloud that ignores case (`Docs` to `docs`) cannot be carried out file by file: the cloud keeps the spelling of the folder that already exists, and the executor refuses to treat a file as its own duplicate. Staging allows it with a `CASE_ONLY_RENAME` warning and the preview reports it as a conflict, so it is never offered for approval. Use a different name instead (run that, then rename again).
- The executor never touches the catalog, so after a run the trees describe the clouds as they were until a scan completes. The page says so in a banner that survives closing the dialog and reloading the page (the server reports the clouds a run changed since their last completed scan) and holds back the approval of a new preview until the rescan; a page that cannot rescan shows the `drivemerge scan` command instead. A rescan from the page never forces: if the scan guard stops it because a cloud really was emptied, run `drivemerge scan --force` in the terminal.

---

# Implementation contract

Everything below is binding for implementers. Read `src/drivemerge/schema.sql` (tables `drafts`, `draft_actions`, `dir_stats`), `models.py`, `plans.py`, `builders.py` (`op_from_entry`, `best_hash`), `simulate.py`, `service.py`.

## A. Namespace semantics (`src/drivemerge/workspace.py`)

Locations are `(remote_id, path)`; paths follow catalog conventions ('' = remote root, '/'-separated, no leading/trailing '/').

**Actions** (rows of `draft_actions` with `state='active'`, ordered by `seq`):
- `move`: items `[{"remote_id", "path"}]` (virtual paths at staging time) → into directory `(dst_remote_id, dst_path)`; `new_name` renames when exactly one item. Each item expands to a *sub-move* `src=(rs, ps) → dst=(rd, join(dst_path, new_name or basename(ps)))`. Items nested in another item of the same action are dropped (the ancestor carries them).
- `mkdir`: creates the virtual directory `(dst_remote_id, join(dst_path, new_name))`.

**Forward map** `fwd((r, p))`: apply every sub-move of every active action in order: if `r == rs and (p == ps or p.startswith(ps + "/"))` then `(r, p) = (rd, pd + p[len(ps):])`. Matching must use exact path segments (`a/b` does not match `a/bc`).

**Origin set**: live catalog entries (`present=1`, files and dirs) plus every mkdir directory (origin = its creation location, flagged virtual). The after-view namespace is `{fwd(o) : o in origins}`.

**Preimage** `pre((r, D))` = set of origin locations whose content may land at `D`: start with `{(r, D)}`; iterate actions in REVERSE; for each sub-move and each candidate `c`: if `c` is at/under `dst` add `src + c[len(dst):]` (remote `rs`); keep `c` itself unless `c` is at/under `src` in the same remote (that region was moved away). mkdir dirs are origins in their own right. This is a set because folder merges have several preimages.

**List after-view children of `(r, D)`**: for every candidate origin dir in `pre((r, D))`, list its live catalog children; for every sub-move root and every mkdir dir, take its forward image; keep items whose forward-mapped *parent* equals `(r, D)`; group by final path. A group with several directories is `merged`; a group with several files whose content keys differ is a `conflict` (same key → `dedupe`, shown once with a badge). Before-view = plain catalog listing.

**Moved set M** (for stats and plans): every live catalog entry under the preimage (at that action's time) of any sub-move's `src`, forward-mapped through all actions, keeping those whose final location differs from the origin. Optimise by mapping each distinct parent directory once; map individual files only when a file itself is a move source.

**After-view dir stats** (additive, exact): `after(r, F) = before(r, F) − Σ{size of f in M : origin(f) under F and final(f) not under F} + Σ{size of f in M : final(f) under F and origin(f) not under F}`; `before` comes from `dir_stats` (0 if absent). Same for file counts and dup_files. One refinement keeps the tree and the preview telling the same story: a moved file whose final spot already holds identical content (a *dedupe victim*: the plan deletes it instead of copying) leaves its old folders but arrives in none, so it is skipped in the second sum (and counts as leaving even folders that also hold its twin). When that leaves a content with a single live copy, the survivor stops counting in `dup_files`. The numbers then equal the simulator's per-cloud `files_after` / `bytes_after`.

**Staging validation** (`add_action`), checked against the current after-view:
- `SRC_NOT_FOUND`: an item does not exist in the after-view.
- `DST_NOT_DIR`: destination is not a directory in the after-view (a remote root `''` is always a directory).
- `INTO_ITSELF`: destination equals or lies under a moved directory item.
- `NAME_EXISTS`: a *file* item's target name is taken by a file (or a dir by a file / a file by a dir). Directory onto an existing directory of the same name is allowed and becomes a merge (returned as a warning `MERGE`).
- `NOOP`: every item would land where it already is (reject; nothing to stage).
- `BAD_NAME`: new_name empty, contains '/', is '.' or '..', or has a NUL.
- `QUARANTINE`: an item is at or below the quarantine folder (`db.quarantine_dir(conn)`, default `.drive-merge-quarantine`, on every remote; compared NFC+casefold on case-insensitive remotes), an item is a folder that contains it (only possible when `quarantine_dir` is nested), or the target path of an item (destination plus its name or `new_name`) or of a `mkdir` is at or below it. Deleted files are restored or purged only by the Quarantine view or the command line (`drivemerge plan restore` / `purge`), never by a draft. Checked first, before `SRC_NOT_FOUND`: it depends on the paths alone.
- Warnings (non-fatal): `MERGE`, `READ_ONLY` (source or destination remote has no write_remote), `CROSS_REMOTE` (bytes will be copied through this machine), `CASE_COLLISION` (case-insensitive destination remote already has a differently-cased same name), `CASE_ONLY_RENAME` (a rename that changes only the case, or the Unicode form, of a name on a remote that ignores both: staging allows it, the preview refuses it as a conflict, see §G).

Staging order of the checks: `BAD_NAME` (new_name), `BAD_REQUEST` (no items), `QUARANTINE`, `SRC_NOT_FOUND`, `DST_NOT_DIR`, `INTO_ITSELF`, `NAME_EXISTS`, `NOOP`.

Adding an action deletes all `undone` actions of the draft (redo stack truncation) and bumps `drafts.version`.

**Undo/redo/remove**: `undo` marks the highest-seq active action `undone`; `redo` re-activates the lowest-seq undone action; `remove_action(id)` marks it undone and then re-validates every later active action in order, marking the ones that no longer validate as undone too (return their ids). Each bumps `version`. A removed step keeps its old `seq`, so after removing a middle step and then undoing a later one, `redo` brings the removed step back first: Ctrl+Z always takes the last active step and Ctrl+Shift+Z the lowest-numbered undone one.

## B. Draft → plan (`workspace.build_ops`, `workspace.preview`)

For each file in M (dirs produce no ops; mkdir'd dirs produce none):
- final location occupied by a live, unmoved catalog file (or by an earlier-processed moved file): same content key (`identity.content_key`) → `delete` the moved file with that occupant as witness (dedupe on merge); different key → conflict (still emit the move/copy so the simulator reports `DST_EXISTS`, and list it under `conflicts`).
- same remote → `move` (`builders.op_from_entry(row, "move", dst_remote_id, dst_path, reason=...)`).
- different remote → `copy` to the final location, then `delete` the source with the copy as witness and `depends_on` = the copy's seq (index+1 in the list).

Order: all moves and copies first (sorted by origin remote, path), then deletes. Reason strings name the draft and action seq.

`preview(conn, draft_id, *, policy=None) -> dict`: build ops; if none, return `{"plan_id": None, "message": "nothing to do", "stale_actions": [...], "empty_dirs_skipped": [...]}` after cancelling the draft's previous preview plan (unless it already ran) and clearing `drafts.preview_plan_id` / `preview_version`, so the plan of moves the draft no longer holds is not left approvable; else `plans.create_plan(conn, f"Workspace: {title}", "workspace", ops, created_by="workspace", params={"draft_id", "draft_version"})`, `simulate.simulate_plan`, store `drafts.preview_plan_id/preview_version`, and return `{"plan_id", "draft_version", "ok", "simulation": asdict(report), "summary": {...}, "changes": [...], "conflicts": [...], "empty_dirs_left": n, "empty_dirs_skipped": [...], "stale_actions": [...]}` (`ok` = simulation ok **and** no conflicts; only then may a UI offer approval). Previewing again cancels the draft's previous preview plan unless it already ran; `changes` lists one row per moved root `{"from": {"remote", "path"}, "to": {...}, "files", "bytes", "kind": "move" | "copy+delete" | "merge" | "dedupe"}`. `empty_dirs_skipped` lists `{"from": {"remote", "path"}, "to": {...}}` for each moved folder that has no file under it (only the topmost of nested ones): plans are per file, so nothing creates them; `summary.empty_dirs_skipped` is their count.

`changes` and `conflicts` are described in §G. `stale_actions` lists the active draft actions that no longer fit the catalog (see §G, "Stale steps"); it is always present, `[]` when none, also in the `{"plan_id": None, "message": "nothing to do"}` answer (as is `empty_dirs_skipped`).

A plan whose `params.draft_version` differs from the draft's current version is stale: approval through the UI must refuse it.

## C. Python API (`workspace.py`)

```python
class WorkspaceError(ValueError): code: str          # one of the staging codes above
def create_draft(conn, title: str) -> dict
def list_drafts(conn, *, status: str | None = "open") -> list[dict]
def get_draft(conn, draft_id: int) -> dict           # {"id","title","status","version","preview_plan_id","preview_version","actions":[...],"undo_available","redo_available","summary":{"moved_files","moved_bytes","cross_remote_bytes","remotes_touched":[names],"stale_actions":n}}
def discard_draft(conn, draft_id: int) -> None
def add_action(conn, draft_id: int, action: dict, *, created_by: str) -> dict   # action as in HTTP body below (remote *names*); returns {"action", "draft_version", "warnings":[{"code","message","params"}]}
def undo(conn, draft_id: int) -> dict
def redo(conn, draft_id: int) -> dict
def remove_action(conn, draft_id: int, action_id: int) -> dict                  # {"draft_version", "removed": [ids]}
def list_dir(conn, remote: str, path: str, *, draft_id: int | None = None, view: str = "after", ghosts: bool = False, limit: int = 500, offset: int = 0) -> dict
def roots(conn, *, draft_id: int | None = None) -> list[dict]                   # one node per remote, with stats
def build_ops(conn, draft_id: int) -> tuple[list[Op], dict]                     # ops, info {"changes","conflicts","empty_dirs_left","dedupes"}
def preview(conn, draft_id: int, *, policy=None) -> dict
def mark_applied(conn, draft_id: int) -> None
```
List item shape: `{"name", "path", "remote", "is_dir", "size", "mtime", "category", "files", "dirs", "bytes", "dup_files", "entry_id" (files only, None for virtual), "origins": [{"remote","path"}], "moved_here": bool, "merged": bool, "conflict": bool, "dedupe": bool, "virtual": bool, "quarantine": bool (at or below the quarantine folder: locked, see `QUARANTINE`), "quarantine_root": bool (the quarantine folder itself)}`; sort dirs first, then natural, case-insensitive name order. Ghost shape: `{"name", "path", "is_dir", "moved_to": {"remote","path"}}`.

`summary.cross_remote_bytes` (in `get_draft` and `preview`) leaves out the files that a merge deletes instead of copying, so it equals the simulator's `bytes_cross_remote`. `discard_draft` also cancels the draft's preview plan if it has not run.

## D. HTTP contract (`src/drivemerge/ui_api.py`, all JSON)

Auth: every route accepts the API Bearer token **or** the UI session cookie, except the human channel (`/v1/ui/plans/...`, `/v1/ui/rescan`), which requires the UI session cookie **and** header `X-CSRF-Token` matching the session **and** (if present) an `Origin` equal to the server's own origin **and** approvals enabled for this server process. The Bearer token never reaches the human channel.

| Method & path | Body / query | Response |
|---|---|---|
| `POST /v1/ui/session` | `{"token"}` (the one printed by `drivemerge ui`) | sets cookie `dm_session` (HttpOnly, SameSite=Strict, Path=/); `{"csrf", "approvals_enabled"}` |
| `GET /v1/ui/config` | | `{"approvals_enabled", "execution_enabled", "remotes": [{"id","name","provider","writable","case_insensitive"}], "version", "csrf", "outdated": [names]}` (`csrf`: the session's token for a cookie caller, so a reloaded page recovers it; `null` for Bearer. `outdated`: the clouds on which a plan operation ran, or failed part way, after their last completed scan, so a reloaded page can still say that the catalog is behind) |
| `GET /v1/tree/roots` | `draft` | `{"roots": [...]}` |
| `GET /v1/tree` | `remote, path, draft, view=after\|before, ghosts=0\|1, limit, offset` | `{"remote","path","view","draft_version","items":[...],"ghosts":[...],"total"}` |
| `GET /v1/drafts` / `POST /v1/drafts` | `?status=open\|applied\|discarded\|all` (default open) / `{"title"}` | `{"drafts": [...]}` / draft (201) |
| `GET /v1/drafts/{id}` / `DELETE /v1/drafts/{id}` | | draft / `{"ok"}` |
| `POST /v1/drafts/{id}/actions` | `{"kind":"move","items":[{"remote","path"}],"dst":{"remote","path"},"new_name"?, "note"?}` or `{"kind":"mkdir","dst":{"remote","path"},"new_name"}` | 201 `{"action","draft_version","warnings"}`; 409 `{"error","code","params"?}` |
| `POST /v1/drafts/{id}/undo` / `redo` | | draft |
| `DELETE /v1/drafts/{id}/actions/{action_id}` | | `{"draft_version","removed"}` |
| `POST /v1/drafts/{id}/preview` | | preview dict without `simulation.digest` (its first 8 characters *are* the approval code), plus `"approval_code"` only for a UI session with approvals enabled and `ok` |
| `GET /v1/suggestions` | `kind=related\|duplicates (both when omitted), remote, limit` | `{"suggestions":[...]}` |
| `POST /v1/ui/plans/{id}/approve` | `{"code"}` | `{"ok"}`; refuses stale draft previews (409) |
| `POST /v1/ui/plans/{id}/execute` | | 202 `{"job_id"}`; 409 `JOB_BUSY` while any job runs (an execution and a rescan both write the catalog), 409 `NOT_APPROVED` / `STALE_PREVIEW`, 403 `EXECUTION_DISABLED`. A finished workspace plan also closes its draft (`result.draft_applied`) |
| `POST /v1/ui/rescan` | `{"remotes":[names]}` | 202 `{"job_id"}` (scan each remote, then identity rebuild; both refresh `dir_stats`) |
| `GET /v1/jobs/{job_id}` | | `{"id","kind","status":"running\|done\|failed","progress":{...},"result","error"}` |
| `GET /v1/plans/{id}/progress` | | `{"status","ops_total","done","failed","skipped","current","recent_journal":[...]}` (journal details without digest, approval code or snapshot) |
| `GET /ui/` and `/ui/static/*` | | the single-page app from `src/drivemerge/web/` |

The plan routes of the REST API that this server mounts as well (`GET /v1/plans`, `GET /v1/plans/{id}`, `POST /v1/plans`, `POST /v1/plans/{id}/simulate`) answer without `digest`, `approval_code` and `snapshot` at any depth, for a bearer token and for a browser session alike: the code reaches a human only through the workspace preview of a session that may approve, or through the CLI. `POST /v1/plans` (like the MCP tool `propose_plan` and the CLI) refuses the params `draft_id` and `draft_version` for every plan kind: only `workspace.preview` ties a plan to a draft.

Jobs run in background threads, each with its **own** SQLite connection (`db.connect(config.db_path)`, so the catalog must be a file); the request-serving connection is shared and guarded by a lock. Errors from this app are `{"error", "code"}`, plus `"params"` when the refusal has facts that a page can word it with (`error` is the English sentence): `WorkspaceError` is 409 with its code, except `NOT_FOUND` (404) and `BAD_REQUEST` (400); an invalid body is 422 with `BAD_REQUEST`. Every error of this app carries a `code`, also the ones that the REST API's shared handlers make (`BAD_REQUEST`, `NOT_FOUND`, `UNAUTHENTICATED`), so that the page never has to show an English sentence.

## E. Suggestions (`src/drivemerge/suggest.py`)

```python
def related_folders(conn, *, remote: str | None = None, min_files: int = 3, limit: int = 20) -> list[dict]
def duplicate_folders(conn, *, remote: str | None = None, min_files: int = 3, threshold: float = 0.9, limit: int = 20) -> list[dict]
```
Suggestion shape: `{"id": "<kind>:<stable hash>", "kind", "score", "title", "reason", "code", "params", "folders": [{"remote","path","files","bytes"}], "terms": [...], "actions": [<HTTP action bodies>]}` — `actions` are ready to POST to `/v1/drafts/{id}/actions`. `title` and `reason` are English sentences, which the CLI and MCP show as they are. `code` (`RELATED` or `DUPLICATES`) and `params` are the same sentences as data, so that a client can word them in its own language: `{"count", "names", "anchor", "name_terms", "content_terms", "same"}` for `RELATED` (how many folders, their distinct names, where to tidy them, shared folder-name words, shared file-name words, how many contents are identical) and `{"count", "names", "kept", "similarity", "shared", "saved_bytes"}` for `DUPLICATES`. The page always words the card itself from `code` and `params` (`sug.<CODE>.title` and `sug.<CODE>.reason`) and shows the English `title` and `reason` only for a `code` it does not know.

## F. Frontend (`src/drivemerge/web/`)

No build step: `index.html`, `app.js` (ES module using `./vendor/htm-preact-standalone.mjs`), `styles.css`, `i18n.js` (English by default, zh-TW when the browser lists Chinese; English also answers for any missing key). Must work offline (no CDN). Layout and behaviour as in §1; HTML5 drag-and-drop; keyboard undo/redo; polling the open draft's version every 3 s so changes staged by Claude appear; preview modal with violations translated to plain language; approve-and-execute flow with progress; read-only banner when approvals are disabled. Every element the e2e test needs carries a `data-testid`.

Behaviours worth keeping (each has a browser test):
- **Language.** The page picks its language once, when it loads: the `?lang=` query (`en` or `zh-TW`) if there is one, else the person's earlier choice (the `EN` / `中文` button, kept in `localStorage` under `dm-lang`; storage that throws is ignored, and the choice then lasts for the visit), else the browser's own order of preference: `navigator.languages` is walked from the top and the first entry the page has a language for decides (`en`, `en-US`, `en_GB` give English; `zh`, `zh-TW`, `zh-CN`, `zh-Hant-HK`, `zh_TW` give `zh-TW`; any other entry is skipped), and English when none does, so `en-US, zh-TW` is English while `ja, zh-TW, en` is `zh-TW`. The button switches at once, without a reload. English answers for any key that a language lacks, and the i18n completeness test requires every key in both languages.
- **Messages from the server.** The server writes no sentence for the page. Whatever a person may read is an English `message` (`reason` for a conflict, `title` and `reason` for a suggestion) for the CLI, the API and MCP, plus a `code` and `params` that the page words in its own language: suggestions (`sug.<CODE>.*`, §E), staging warnings (`warn.<CODE>`), refusals (`err.<CODE>`, worded with the `params` of the error body), stale reasons (`stale.<CODE>`, worded from `items`), conflicts (`conflict.<CODE>.title`, `.text` and `.detail`), and the simulator's violations and warnings. The `params` of a violation hold `variant` (which of the code's sentences it is, when it has several), places as `{"remote","path"}` (`at` is the one the reveal link goes to), counts and sizes, and the short kinds `why`, `problem`, `what` and `role` that have a sentence of their own. The page writes the detail line from `viol.<CODE>.detail` (or `viol.<CODE>.detail.<variant>`) and keeps the code beside it as small monospace text; a message that it has no sentence for (a newer server, or a report stored before `params` existed) is shown with the server's English `message`. Violation and conflict codes also get a translated headline and explanation (`viol.<CODE>.title` and `.text`, `conflict.<CODE>.*`). A new code, variant or warning needs its strings in both languages: the tests read them from `simulate.py`, `workspace.py`, `suggest.py` and `ui_api.py` and check that every sentence exists in both languages and uses only the parameters that the server sends. The one text still shown as received is the failure detail of a run (rclone's output and the executor's errors), under a line that says so. MCP clients get the English messages without `params`.
- **Stale steps.** The poll also notices a change of `summary.stale_actions` at an unchanged draft version (a rescan), refreshes the trees and warns. The staged list shows a stale step struck through with the reason (`stale.<CODE>`), the top bar carries a warning count, and the preview modal lists `stale_actions` (and says so instead of "nothing changed" when every step is stale).
- **Quarantine.** Items with `quarantine: true` are rendered as locked (not draggable, not drop targets, no rename, no cut, no new folder or paste into them, breadcrumbs included); the `quarantine_root` row is labelled "隔離區" / "Quarantine" with a tooltip on restore and purge by CLI.
- **Toasts.** Bottom right (beside an open drawer, not over its buttons). Errors stay until closed; info, warning, success and "from Claude" leave after about 6 s (12 s with a button), and wait while the pointer or keyboard focus is on them. The same message shown again replaces its earlier copy. The stack publishes its height as `--toast-reserve`, which modal backdrops leave free below the dialog, so a toast never covers a dialog's footer.
- **Name clashes inside a merge.** A row flagged `conflict` stands for several files, and renaming it would rename all of them, the file that was already there included. So F2 and a double-click on it do not open the editor: a dialog explains and offers to take back the step that brought the incoming file there, and a toast then leads to that file at its original place. The `DIFFERENT_CONTENT` card of the preview has the same one-click way out. One clash is one problem: the page drops the echoes the simulator adds (a `DST_EXISTS` on a spot that a conflict already explains, a `NO_WITNESS` under `READ_ONLY_REMOTE` on the same cloud, repeated sentences).
- **The catalog is behind after a run.** When a run ends (finished or failed), or a draft is applied from a terminal, a banner names the clouds and offers the rescan (a page without approvals shows `drivemerge scan <remote>`). It stays when the dialog closes, comes back after a reload while `/v1/ui/config` reports `outdated`, and can be dismissed. A preview made meanwhile shows a note with the rescan button and offers no approval until the catalog is current.
- **Narrow panes.** A pane is a CSS container: at 480px or less the file-count column is hidden (assistive technology still reads it), at 380px or less badges shrink to an icon or dot with their tooltips kept. The breadcrumb has a fixed one-line height (crumbs shrink with an ellipsis; the cloud name and the focused item stay whole), so focusing a row never moves the tree under the pointer.
- **Long folders.** A refresh after every staging action reloads as many children as were open (in pages up to the server's cap), so the pages opened with "load more" are not lost.
- **Renaming.** The box ignores Enter and Escape while an IME composition is open (`isComposing`, key code 229); leaving the box commits the typed name (not when the whole window loses focus); after Enter or Escape the keyboard stays in the tree, and the renamed row is selected and focused.
- **Suggestions.** A suggestion card is derived from the draft (the active actions whose note is the suggestion id), so one that was staged and then undone can be staged again; its text uses `detail` when present.
- **Folders without files.** The preview lists `empty_dirs_skipped` next to the stale steps, in the review and in the "nothing to run" answer alike.

## G. Where the implementation is more precise than A–F

Decided during integration; the code and its tests follow these, the sections above are the shorter contract.

**Namespace and plans (`workspace.py`)**
- A directory made by `mkdir` is mapped only by sub-moves staged *after* it (its origin is its creation spot); earlier sub-moves do not apply to it.
- Directory counts in the after-view are over distinct locations (a merged folder is one folder, a new `mkdir` folder counts) and every count is clamped at 0. A missing `dir_stats` row counts as zeros, so `dirstats.refresh` must run after scans and identity rebuilds (`Service.scan`, `Service.rebuild_identity` and `Service.bridge` do it).
- Empty files are never duplicates: they all share one hash and are placeholders (`__init__.py`, `.gitkeep`), so `dup_files`, for file rows and folder counts alike, ignores zero-size files, as search, `dir_stats` and the duplicate list do.
- On case-insensitive remotes `build_ops` finds the occupant of a final path by NFC+casefold of the full path; staging stays exact-name plus a `CASE_COLLISION` warning.
- Conflicts in `info["conflicts"]` (and `preview()["conflicts"]`) carry a `code` and the places concerned (`from`, `to`, `with`), and an English `reason`: `DIFFERENT_CONTENT`, `DIR_IN_WAY` (a catalog folder is still physically there: executing file by file never removes folders), `FILE_IN_WAY`, `OCCUPIED` (the spot belongs to a file that only leaves in the delete phase), `CYCLE` (moves that wait on each other, e.g. a swap, or a file that would have to move into a folder of its own name) and `CASE_ONLY_RENAME` (the final spot of a file or folder is its own under another spelling of case or Unicode form, on a remote that ignores both: one conflict for each renamed root, not one per file inside it). `OCCUPIED`, `CYCLE` and `CASE_ONLY_RENAME` can pass the simulator when contents are identical, which is why `preview()["ok"]` also requires no conflicts.
- Moves and copies are ordered topologically so a file lands only after whatever sits on its spot has left; ties break by origin remote id and path. For a conflicted cross-remote copy the paired delete is not emitted.
- **Stale steps.** A rescan can change the catalog under an open draft. An active action is *stale* when, replayed on the after-view of the actions before it, it has nothing to act on: a move item that no longer exists (`SRC_NOT_FOUND`; every vanished item is listed) or a destination (move or `mkdir`) that is no longer a folder (`DST_NOT_DIR`). An action that built on a stale one is stale as well. Name clashes are not staleness: the preview reports them as conflicts. Every action of `get_draft` carries `"stale": bool` and `"stale_reason": {"code", "message", "items": [{"remote","path"}]} | None` (`items` are the vanished sources, or the missing destination, and what the page words the code with); `summary.stale_actions` counts them. `preview()` returns `"stale_actions": [<action dicts as in get_draft, stale_reason set>]` and `summary.stale_actions`. Stale steps produce no ops and do not change `ok` (they cannot do harm), so the page must show them wherever it lists staged steps and in the preview. Undone actions are never stale. An untouched catalog never produces stale steps for a draft that staged cleanly.
- **Quarantine in the workspace.** `list_dir` marks every item at or below the quarantine folder with `quarantine: true` and the folder itself with `quarantine_root: true` (also in the before-view); the page draws the root distinctly (label, tooltip pointing at the CLI) and makes the whole subtree neither draggable nor a drop target. Staging against it is refused with `QUARANTINE` (§A), which `redo` and `remove_action` also apply when re-validating older steps.
- `remove_action` of an already undone action only bumps the version. `WorkspaceError` also uses `NOT_FOUND`, `NOT_OPEN`, `BAD_REQUEST`, `NOTHING_TO_UNDO`, `NOTHING_TO_REDO`.
- `list_drafts` rows are light: `id, title, status, version, preview_plan_id, preview_version, created_at, updated_at, action_count`.

**Suggestions (`suggest.py`)**
- Kinds are `"related"` and `"duplicates"`; ids are `related:<12 hex>` / `duplicates:<12 hex>`; actions carry `note` = the suggestion id. The related threshold is 0.25; a shared folder-name word or a shared file is required, file-name vocabulary alone never makes two folders related.
- Duplicate folders that mirror each other are grouped (one target, several move actions) and a mirrored pair is reported at its top. Nested folders inside one cluster are resolved to the one with the stronger edges.

**Quarantine is not live content**
- Files parked in the quarantine folder (`[safety] quarantine_dir`) are scanned like any others, but they are deleted files: `identity` leaves them out of every group, duplicate list and bridge candidate, and `suggest` skips them, so a cleaned-up copy is never a "duplicate" of the file that replaced it and the workspace never offers to merge folders out of quarantine. They still count as files and bytes (they occupy the cloud) and appear in the tree.
- The folder name is recorded in the catalog's `meta` table by `config.sync_remotes` (every `Service` start), so every module that holds only a connection (`db.quarantine_dir`, `db.outside_quarantine`) agrees with the executor.

**Server (`ui_api.py`)**
- `GET /v1/suggestions` without `kind` returns related followed by duplicates; an unknown kind is 400. A cookie-authenticated unsafe request on a normal route with a foreign `Origin` is 403 `BAD_ORIGIN`.
- `api_token` may be empty: only the browser session works then. Static HTML carries a CSP (`script-src 'self'`, no inline scripts, no other origins); `index.html` therefore uses absolute `/ui/static/` URLs.

**Service facade (`service.py`)**
- CLI, REST and MCP reach the workspace through `Service`: read `tree_roots`, `list_dir`, `get_draft`, `list_drafts`, `suggestions`; maintain `refresh_dir_stats` (also run by `scan`, `rebuild_identity` and `bridge`); propose `create_draft`, `discard_draft`, `stage_action(..., created_by)`, `undo_action`, `redo_action`, `remove_action`, `preview_draft`. `preview_draft` returns the simulation digest, so a caller that is not the human at the browser must drop it. Approving and executing stay in the human-only section. `policy()` is the one place the simulator's `SafetyPolicy` is built from the config.
- The MCP draft tools (`list_drafts`, `get_draft`, `create_draft`, `browse_tree`, `suggest_reorganization`, `stage_moves`, `undo_last`, `remove_staged`, `preview_draft`) stage with `created_by="mcp"` (the page shows its "proposed by Claude" badge and toast only for that) and never expose `approval_code` or the digest.

**Execution**
- `Service.execute` is the only place that closes a draft: after a `done` run of a plan that is a draft's own preview (kind `workspace`, created by `workspace`; `Service.propose_plan` refuses `draft_id` / `draft_version` params, and a plan that merely carries them is tied to no draft, here and in the stale-preview checks) it marks the draft applied if its version still equals `params.draft_version`; otherwise the draft stays open and the result says why (`draft_applied: false`, `draft_error`). The CLI, the browser and a rescan therefore agree.
