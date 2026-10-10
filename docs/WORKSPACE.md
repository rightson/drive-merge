# 視覺化工作區（Visual Workspace）

> 本文件是 DESIGN.md §15 的展開：前半是設計（中文），後半是給實作者的精確合約（英文）。

## 1. 使用情境

「我有一些學校跟研究的東西分散在不同目錄（甚至不同雲）。我想開左右兩個窗格，看不同路徑底下的東西在哪個階層，把檔案或資料夾拖到合適的階層，來回拖到滿意，然後預覽結果；確認無誤、核准之後才真的去改雲端。」

對應的操作模型：

1. **兩個窗格**，每個都是可展開的樹狀檢視；根節點可以是「所有雲端」（每朵雲一個頂層節點，等同 rclone `combine` 的視圖）或某一朵雲。
2. **拖放 = 暫存（stage）一個動作**，不碰雲端。可在同一窗格不同階層之間、或左右窗格（含跨雲）之間拖放；可重新命名、新增資料夾。
3. **來回拖** 不會累積成一串操作：最後只計算「淨效果」——拖回原處的東西等於沒動。
4. 每次拖放是一個 **復原步驟**（Ctrl+Z / Ctrl+Shift+Z）。
5. 窗格預設顯示「**變更後**」的虛擬樹（暫存的效果立即可見），可切換「變更前」；可開「顯示原位置」看到每個東西被搬去哪。
6. **預覽** = 把暫存的淨效果轉成 Plan → 模擬器檢查 → 顯示 before/after、跨雲傳輸量、衝突與問題清單。
7. **核准並執行** 需要人在瀏覽器輸入 8 碼確認碼（見 §4）；執行中顯示進度；完成後可一鍵重掃受影響的雲端。
8. **建議**：服務會主動找出「散落各處但相關」的資料夾（例如名稱/內容相近的學校、研究資料夾）與「內容幾乎相同的重複資料夾」，一鍵把建議加入暫存，仍可再調整。
9. **Claude 也能提案**：透過 MCP 把搬移加入同一份草稿（標示「由 Claude 提出」），畫面即時更新；但 Claude 不能核准或執行。

## 2. 為什麼這樣設計

- **草稿是動作序列，不是目標樹**：使用者看到的是「變更後」視圖並在其上繼續拖放，因此每個動作的來源路徑是「當時的虛擬路徑」。依序套用（函數合成）就能自然支援「先把 A 拖進 B，再把 B 拖進 C」與來回拖放；淨效果由「每個檔案的原始位置 → 最終位置」計算，與中間過程無關。
- **資料夾同名 = 合併**：整理分散的資料最常見的需求就是把兩個同名/同主題的資料夾併在一起。合併時內容完全相同的檔案自動變成「保留一份、另一份移到隔離區」（有見證的去重）；內容不同的同名檔案列為衝突，由使用者改名解決——永不覆寫。
- **跨雲搬移 = 複製 + 驗證 + 來源移入隔離區**（依賴關係保證先驗證才刪）。
- **預覽一定經過模擬器**：視覺化介面只是另一種產生 Plan 的方式，安全不變式完全相同（DESIGN.md §7）。
- **草稿版本綁定預覽**：草稿只要有任何變動，之前的預覽就失效，必須重新預覽才能核准。

## 3. 建議引擎

| 類型 | 判定 | 建議 |
|---|---|---|
| 相關資料夾（related） | 資料夾名稱詞彙（英文詞、CJK 雙字）+ 內部檔名詞彙的 TF-IDF 相似度，加上共享內容（重複檔案）的證據；排除已在同一父目錄或互為祖孫者 | 以檔案最多（或位於目標雲）的資料夾為錨點，其他資料夾搬入錨點成為子資料夾 |
| 重複資料夾（duplicate folders） | 兩資料夾的內容群組 Jaccard ≥ 0.9（只算已證明相同的內容），父層也重複時只報父層 | 把一個合併進另一個 → 相同檔案自動變成有見證的去重 |

語意相近但字面不同（「論文」vs「thesis」）是規則引擎做不到的，這正是讓 Claude 透過 MCP 讀 folder profiles 後提出搬移的地方。

## 4. 核准管道（人類專用）

威脅模型：防止 LLM（或被檔名 prompt injection 的 LLM）在無人確認下改動雲端；不防禦以使用者權限執行的惡意程式。

- `drivemerge ui` 在終端機印出一次性網址 `http://127.0.0.1:8765/ui/#token=…`（token 只存在記憶體，不寫入檔案）。瀏覽器以它換取 HttpOnly、SameSite=Strict 的 session cookie。
- **只有** UI session（cookie + CSRF header + Origin 檢查）可以呼叫核准/執行；REST API 的 Bearer token 與 MCP 永遠不行。
- 只有在 `drivemerge ui` 由互動式終端機啟動、且環境中沒有 `CLAUDECODE` / `AI_AGENT` 時才開啟核准能力；否則 UI 為「只能提案」模式，並顯示需在終端機執行的指令。
- 核准仍需輸入模擬 digest 的 8 碼；執行仍需 `[safety] execution_enabled = true`。

## 5. 已知限制（v1）

- 執行以檔案為單位（同雲搬移資料夾時會逐檔 move；Drive 上大量檔案較慢）。未來可加「整個資料夾一次伺服器端搬移」的 op。
- 搬走後留下的空資料夾不會自動刪除（預覽會列出數量）。新增但沒有放東西的資料夾不會被建立。
- 多個瀏覽器分頁同時編輯同一份草稿時，以伺服器版本為準（頁面會偵測版本變化並重新整理）。

---

# Implementation contract (English)

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
- Warnings (non-fatal): `MERGE`, `READ_ONLY` (source or destination remote has no write_remote), `CROSS_REMOTE` (bytes will be copied through this machine), `CASE_COLLISION` (case-insensitive destination remote already has a differently-cased same name).
Adding an action deletes all `undone` actions of the draft (redo stack truncation) and bumps `drafts.version`.

**Undo/redo/remove**: `undo` marks the highest-seq active action `undone`; `redo` re-activates the lowest-seq undone action; `remove_action(id)` marks it undone and then re-validates every later active action in order, marking the ones that no longer validate as undone too (return their ids). Each bumps `version`.

## B. Draft → plan (`workspace.build_ops`, `workspace.preview`)

For each file in M (dirs produce no ops; mkdir'd dirs produce none):
- final location occupied by a live, unmoved catalog file (or by an earlier-processed moved file): same content key (`identity.content_key`) → `delete` the moved file with that occupant as witness (dedupe on merge); different key → conflict (still emit the move/copy so the simulator reports `DST_EXISTS`, and list it under `conflicts`).
- same remote → `move` (`builders.op_from_entry(row, "move", dst_remote_id, dst_path, reason=...)`).
- different remote → `copy` to the final location, then `delete` the source with the copy as witness and `depends_on` = the copy's seq (index+1 in the list).
Order: all moves and copies first (sorted by origin remote, path), then deletes. Reason strings name the draft and action seq.

`preview(conn, draft_id, *, policy=None) -> dict`: build ops; if none, return `{"plan_id": None, "message": "nothing to do"}`; else `plans.create_plan(conn, f"Workspace: {title}", "workspace", ops, created_by="workspace", params={"draft_id", "draft_version"})`, `simulate.simulate_plan`, store `drafts.preview_plan_id/preview_version`, and return `{"plan_id", "draft_version", "ok", "simulation": asdict(report), "summary": {...}, "changes": [...], "conflicts": [...], "empty_dirs_left": n}` (`ok` = simulation ok **and** no conflicts; only then may a UI offer approval). Previewing again cancels the draft's previous preview plan unless it already ran where `changes` lists one row per moved root `{"from": {"remote", "path"}, "to": {...}, "files", "bytes", "kind": "move" | "copy+delete" | "merge" | "dedupe"}`.

A plan whose `params.draft_version` differs from the draft's current version is stale: approval through the UI must refuse it.

## C. Python API (`workspace.py`)

```python
class WorkspaceError(ValueError): code: str          # one of the staging codes above
def create_draft(conn, title: str) -> dict
def list_drafts(conn, *, status: str | None = "open") -> list[dict]
def get_draft(conn, draft_id: int) -> dict           # {"id","title","status","version","preview_plan_id","preview_version","actions":[...],"undo_available","redo_available","summary":{"moved_files","moved_bytes","cross_remote_bytes","remotes_touched":[names]}}
def discard_draft(conn, draft_id: int) -> None
def add_action(conn, draft_id: int, action: dict, *, created_by: str) -> dict   # action as in HTTP body below (remote *names*); returns {"action", "draft_version", "warnings":[{"code","message"}]}
def undo(conn, draft_id: int) -> dict
def redo(conn, draft_id: int) -> dict
def remove_action(conn, draft_id: int, action_id: int) -> dict                  # {"draft_version", "removed": [ids]}
def list_dir(conn, remote: str, path: str, *, draft_id: int | None = None, view: str = "after", ghosts: bool = False, limit: int = 500, offset: int = 0) -> dict
def roots(conn, *, draft_id: int | None = None) -> list[dict]                   # one node per remote, with stats
def build_ops(conn, draft_id: int) -> tuple[list[Op], dict]                     # ops, info {"changes","conflicts","empty_dirs_left","dedupes"}
def preview(conn, draft_id: int, *, policy=None) -> dict
def mark_applied(conn, draft_id: int) -> None
```
List item shape: `{"name", "path", "remote", "is_dir", "size", "mtime", "category", "files", "dirs", "bytes", "dup_files", "entry_id" (files only, None for virtual), "origins": [{"remote","path"}], "moved_here": bool, "merged": bool, "conflict": bool, "dedupe": bool, "virtual": bool}`; sort dirs first, then natural, case-insensitive name order. Ghost shape: `{"name", "path", "is_dir", "moved_to": {"remote","path"}}`.

## D. HTTP contract (`src/drivemerge/ui_api.py`, all JSON)

Auth: every route accepts the API Bearer token **or** the UI session cookie, except the human channel (`/v1/ui/plans/...`, `/v1/ui/rescan`), which requires the UI session cookie **and** header `X-CSRF-Token` matching the session **and** (if present) an `Origin` equal to the server's own origin **and** approvals enabled for this server process. The Bearer token never reaches the human channel.

| Method & path | Body / query | Response |
|---|---|---|
| `POST /v1/ui/session` | `{"token"}` (the one printed by `drivemerge ui`) | sets cookie `dm_session` (HttpOnly, SameSite=Strict, Path=/); `{"csrf", "approvals_enabled"}` |
| `GET /v1/ui/config` | | `{"approvals_enabled", "execution_enabled", "remotes": [{"id","name","provider","writable","case_insensitive"}], "version", "csrf"}` (`csrf`: the session's token for a cookie caller, so a reloaded page recovers it; `null` for Bearer) |
| `GET /v1/tree/roots` | `draft` | `{"roots": [...]}` |
| `GET /v1/tree` | `remote, path, draft, view=after\|before, ghosts=0\|1, limit, offset` | `{"remote","path","view","draft_version","items":[...],"ghosts":[...],"total"}` |
| `GET /v1/drafts` / `POST /v1/drafts` | `?status=open\|applied\|discarded\|all` (default open) / `{"title"}` | `{"drafts": [...]}` / draft (201) |
| `GET /v1/drafts/{id}` / `DELETE /v1/drafts/{id}` | | draft / `{"ok"}` |
| `POST /v1/drafts/{id}/actions` | `{"kind":"move","items":[{"remote","path"}],"dst":{"remote","path"},"new_name"?, "note"?}` or `{"kind":"mkdir","dst":{"remote","path"},"new_name"}` | 201 `{"action","draft_version","warnings"}`; 409 `{"error","code"}` |
| `POST /v1/drafts/{id}/undo` / `redo` | | draft |
| `DELETE /v1/drafts/{id}/actions/{action_id}` | | `{"draft_version","removed"}` |
| `POST /v1/drafts/{id}/preview` | | preview dict without `simulation.digest` (its first 8 characters *are* the approval code), plus `"approval_code"` only for a UI session with approvals enabled and `ok` |
| `GET /v1/suggestions` | `kind=related\|duplicates (both when omitted), remote, limit` | `{"suggestions":[...]}` |
| `POST /v1/ui/plans/{id}/approve` | `{"code"}` | `{"ok"}`; refuses stale draft previews (409) |
| `POST /v1/ui/plans/{id}/execute` | | 202 `{"job_id"}`; 409 `JOB_BUSY` while any job runs (an execution and a rescan both write the catalog), 409 `NOT_APPROVED` / `STALE_PREVIEW`, 403 `EXECUTION_DISABLED`. A finished workspace plan also closes its draft (`result.draft_applied`) |
| `POST /v1/ui/rescan` | `{"remotes":[names]}` | 202 `{"job_id"}` (scan each remote, then identity rebuild; both refresh `dir_stats`) |
| `GET /v1/jobs/{job_id}` | | `{"id","kind","status":"running\|done\|failed","progress":{...},"result","error"}` |
| `GET /v1/plans/{id}/progress` | | `{"status","ops_total","done","failed","skipped","current","recent_journal":[...]}` |
| `GET /ui/` and `/ui/static/*` | | the single-page app from `src/drivemerge/web/` |

Jobs run in background threads, each with its **own** SQLite connection (`db.connect(config.db_path)`, so the catalog must be a file); the request-serving connection is shared and guarded by a lock. Errors from this app are `{"error", "code"}`: `WorkspaceError` is 409 with its code, except `NOT_FOUND` (404) and `BAD_REQUEST` (400); an invalid body is 422.

## E. Suggestions (`src/drivemerge/suggest.py`)

```python
def related_folders(conn, *, remote: str | None = None, min_files: int = 3, limit: int = 20) -> list[dict]
def duplicate_folders(conn, *, remote: str | None = None, min_files: int = 3, threshold: float = 0.9, limit: int = 20) -> list[dict]
```
Suggestion shape: `{"id": "<kind>:<stable hash>", "kind", "score", "title", "reason", "folders": [{"remote","path","files","bytes"}], "terms": [...], "actions": [<HTTP action bodies>]}` — `actions` are ready to POST to `/v1/drafts/{id}/actions`.

## F. Frontend (`src/drivemerge/web/`)

No build step: `index.html`, `app.js` (ES module using `./vendor/htm-preact-standalone.mjs`), `styles.css`, `i18n.js` (zh-TW default, en fallback). Must work offline (no CDN). Layout and behaviour as in §1; HTML5 drag-and-drop; keyboard undo/redo; polling the open draft's version every 3 s so changes staged by Claude appear; preview modal with violations translated to plain language; approve-and-execute flow with progress; read-only banner when approvals are disabled. Every element the e2e test needs carries a `data-testid`.

## G. Where the implementation is more precise than A–F

Decided during integration; the code and its tests follow these, the sections above are the shorter contract.

**Namespace and plans (`workspace.py`)**
- A directory made by `mkdir` is mapped only by sub-moves staged *after* it (its origin is its creation spot); earlier sub-moves do not apply to it.
- Directory counts in the after-view are over distinct locations (a merged folder is one folder, a new `mkdir` folder counts) and every count is clamped at 0. A missing `dir_stats` row counts as zeros, so `dirstats.refresh` must run after scans and identity rebuilds (`Service.scan`, `Service.rebuild_identity` and `Service.bridge` do it).
- On case-insensitive remotes `build_ops` finds the occupant of a final path by NFC+casefold of the full path; staging stays exact-name plus a `CASE_COLLISION` warning.
- Conflicts in `info["conflicts"]` (and `preview()["conflicts"]`) carry a `code`: `DIFFERENT_CONTENT`, `DIR_IN_WAY` (a catalog folder is still physically there: executing file by file never removes folders), `FILE_IN_WAY`, `OCCUPIED` (the spot belongs to a file that only leaves in the delete phase) and `CYCLE` (moves that wait on each other, e.g. a swap). `OCCUPIED` and `CYCLE` can pass the simulator when contents are identical, which is why `preview()["ok"]` also requires no conflicts.
- Moves and copies are ordered topologically so a file lands only after whatever sits on its spot has left; ties break by origin remote id and path. For a conflicted cross-remote copy the paired delete is not emitted.
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
- MCP has no draft tools yet. When it gets them, they must stage with `created_by="mcp"` (the page shows its "proposed by Claude" badge and toast only for that) and must never expose `approval_code` or the digest.

**Execution**
- `Service.execute` is the only place that closes a draft: after a `done` run of a plan with `params.draft_id` it marks the draft applied if its version still equals `params.draft_version`; otherwise the draft stays open and the result says why (`draft_applied: false`, `draft_error`). The CLI, the browser and a rescan therefore agree.
