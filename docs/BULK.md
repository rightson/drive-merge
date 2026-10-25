# Bulk Actions from Explore

**English** | [繁體中文](BULK.zh-TW.md)

> This document turns the **Explore** view ([EXPLORE.md](EXPLORE.md)) from a place that answers "where is it?" into a place to act from: select results (by hand, or every content that matches the query), then **gather** them into one folder (staged in the open workspace draft, [WORKSPACE.md](WORKSPACE.md)) or **clean up their duplicates** (a dedupe plan limited to exactly the selected contents, [PLANS.md](PLANS.md)). Explore also marks the copies that the open draft already moves. The first part is the design; the second part (the implementation contract) is binding for implementers.

## 1. The problem

Explore finds the scattered material of several overlapping clouds in one query: `type:pdf thesis`, `cat:image modified:2019 copies:>=2`, `only:dropbox`. What happens next is still one result at a time, or a detour:

- **Moving what a query found is one drag per file.** "Put every thesis PDF in `gdrive:School/Thesis`" means revealing each copy in the workspace and dragging it there. Fifty results are fifty gestures, fifty undo steps, and a good chance of missing one.
- **Cleaning up what a query found is not possible.** A result with several copies on one cloud offers "dedupe here", which opens the dedupe form scoped to that cloud and the *folder* that holds them. There is no way to say "remove the redundant copies of *these* 312 photos", only of everything under a folder, which is often more than the person looked at.
- **Explore does not know the draft.** After staging a move, Explore still shows the copy where the last scan saw it, with nothing saying that it is already on its way somewhere (EXPLORE.md §7).
- **Claude cannot query by content.** MCP has `search_files` (entries) but not Explore (contents with their copies), so a proposal like "gather these" has to be assembled file by file.

Questions this feature answers:

1. **Can I act on everything this query found?** Select results, or all of them, see what the selection holds (contents, copies, bytes, per cloud), and act once.
2. **Can I put them in one place?** One staged step that moves one copy of each selected content into a folder, previewed and approved like any drag.
3. **Can I remove the redundant copies of exactly these?** A dedupe plan whose groups are the selected contents, estimated, reviewed and approved in the Plans view.

## 2. Principles

- **No new way to change a cloud.** Gathering stages one ordinary `move` action in the open draft (the same `workspace.add_action` as a drag, the same checks, warnings and undo); the draft becomes a plan only through the workspace preview, and the plan is simulated and approved by typing its code, as always. Cleaning up builds an ordinary `dedupe` plan with the same builder, only limited to the selected groups. Both paths end in the existing simulate → approve (human only) → execute path.
- **Act on what was seen.** The page shows the resolved selection (how many contents, copies and bytes) before every action, and an action on "every matching result" carries the total the person saw; if a rescan changed the answer, the server refuses (`SELECTION_CHANGED`) instead of acting on a different set.
- **Narrow, never wider.** A selection can only *restrict* what a builder would do. A dedupe over a selection is the same dedupe (same keeper policy, same pools, same witnesses) over fewer groups; gathering moves at most one copy per content and never touches the others.
- **Conservative defaults.** Gathering takes the copy that is already on the destination cloud. Bringing a content over from another cloud (a cross-cloud move: copied through this machine, the original then quarantined) is opt-in and says so. Dedupe keeps "within each cloud" as its default.
- **The page words everything.** The server sends codes and params (skip reasons, refusals); the page writes the sentences in English and zh-TW.
- **Read-only fallback.** Nothing in this feature needs the human channel: selecting, gathering into a draft and creating a dedupe plan are proposals. With approvals disabled the page behaves the same, and the workspace preview and the plan review end, as today, with `drivemerge plan approve <id>` and `drivemerge plan execute <id>` instead of the approve button.

## 3. Information architecture

No new rail item. Explore gains a selection column and a selection bar; the dedupe form gains a selection banner.

```
┌──────────────────────────────────────────────────────────────────────────────────────┐
│ ⌕  type:pdf  thesis                                                         ⟲  ?      │
├───────────────┬──────────────────────────────────────────────────────────────────────┤
│ Clouds        │ ☑  name                      gd  db  od   size     modified           │
│ ☐ on gdrive 12│ ☑  thesis_final.pdf          ●   ●2  ○   4.2 MB   2023-06-01          │
│ ☐ on dropbox 9│ ☐  thesis_draft.docx         ○   ●   ○   880 KB   2022-11-12          │
│ Copies        │ ☑  thesis-v9.pdf             ●   ○   ○   4.1 MB   2023-05-02          │
│ ☐ only one  3 │      ▸ gdrive:Downloads/thesis-v9.pdf   ⇢ staged: gdrive:School/Thesis │
│               │ All 50 on this page are selected. Select all 213 matching              │
├───────────────┴──────────────────────────────────────────────────────────────────────┤
│ 2 selected · 3 copies · 8.3 MB (gdrive 2 · dropbox 2)                                  │
│                         [ Gather into a folder… ]  [ Clean up duplicates… ]  [ Clear ] │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

- **Selection column**: a checkbox per result (a result is a content with all its copies). The header checkbox selects the loaded page; when every loaded result is selected and more match, a line offers **Select all N matching**. In that mode unchecking a result excludes it (up to 500 exclusions), and loading more pages shows the new rows checked.
- **Selection bar** (sticky under the results, shown when something is selected): contents, copies and bytes of the selection, per cloud, and the redundant copies it holds; computed by the server from the selection (§B), so "all matching" shows exact numbers, not only the loaded page. Actions: **Gather into a folder…**, **Clean up duplicates…** (disabled with a tooltip when the selection holds no content with two copies), **Clear**.
- The selection belongs to the query: it is kept, per query text, for the visit (Back to the same query restores it) and cleared when the query changes; the bar says so when it clears one. Hand-picked selections hold at most 500 contents; "all matching" at most 5,000.
- **Staged marks**: when the open draft moves a copy, its line in the expanded result shows `⇢ staged: <where the draft puts it>`, and the result row shows a small "staged" badge. The rest of Explore still reads the catalog (§8).

## 4. Gather into a folder

```
┌ Gather 2 contents into a folder ───────────────────────────────────────────────┐
│ Into  [ gdrive ▾ ] [ School/Thesis                    ] [ New folder… ]         │
│ ☐ Also bring contents that have no copy on gdrive  ⚠ (moves them across clouds) │
│                                                                                  │
│ 1 file (4.1 MB) moves into gdrive:School/Thesis                                  │
│   gdrive:Downloads/thesis-v9.pdf → gdrive:School/Thesis/thesis-v9.pdf            │
│ 1 content is already there: thesis_final.pdf                                     │
│ ▸ Skipped 0                                                                      │
│ Its other copies stay where they are; "Clean up duplicates…" can thin them later.│
│                                               [ Cancel ]  [ Stage in the draft ] │
└──────────────────────────────────────────────────────────────────────────────────┘
```

Flow:

1. The person chooses a destination folder: a cloud (writable clouds only) and a folder with autocomplete over the draft's after-view (`GET /v1/tree?draft=`), so a folder staged a moment ago can be the target. **New folder…** stages a `mkdir` first (its own undo step, as in the workspace).
2. Every change of the destination or the option asks the server for a dry run (`POST /v1/drafts/<id>/gather` with `dry_run: true`, debounced). The server picks **one copy per content**: a copy on the destination cloud when there is one (the keeper policy decides between several); with "also bring contents from other clouds" on, the best copy elsewhere otherwise. It then reports what moves (count, bytes, the part that crosses clouds, a sample), what is **already there** (some copy of the content already sits directly in that folder) and what is **skipped**, by reason: no copy on that cloud, the name is taken in that folder, two selected contents would get the same name, read-only cloud, native document across clouds, ambiguous Drive path, gone since the selection was made.
3. **Stage in the draft** sends the same request without `dry_run`. The server stages **one** `move` action with every picked copy (one undo step, note `explore:gather`), worded in the staged drawer as "Gather N files into <folder>". A toast offers **Open workspace**. The staging warnings (cross-cloud, read-only, case collision) are shown as for a drag.
4. From there it is the workspace's path: preview, simulation, typed approval, run, rescan. The gathered files' other copies are untouched; the toast and the dialog point at "Clean up duplicates…".

Names stay as they are: gathering never renames (two different contents called `notes.txt` cannot both land in one folder, so the second is skipped with `NAME_CLASH`, and the person can rename it in the workspace).

## 5. Clean up duplicates of the selection

**Clean up duplicates…** opens the existing dedupe form with the selection attached:

```
┌ Scope ───────────────────────────────────────┐┌ Estimate ──────────────────────────────┐
│ Selection  312 contents from Explore          ││ 418 copies in 297 groups · 2.9 GB back │
│            "cat:image modified:2019 copies:>=2"│ 15 contents have no exact duplicate     │
│            [ Back to Explore ] [ Remove ]     ││ ┌ group ──────── keep ──────── remove ┐│
│ Clouds   (•) all  ( ) only [gdrive▾]          ││ │ IMG_0412.jpg 4.1 MB  ★ gdrive:…   1 ││
│ Mode     (•) within each cloud                ││ └──────────────────────────────────────┘│
│          ( ) across clouds  ⚠                 ││                                          │
│ Path, Min size, Prefer  (as before)           ││                      [ Create plan ]    │
└───────────────────────────────────────────────┘└──────────────────────────────────────────┘
```

- The selection travels in the form's URL (`#/plans/new/dedupe?selq=<query text>&selx=<excluded keys>&seln=<total seen>` for "all matching", `?selk=<keys>` for hand-picked ones), so Back and reload keep it.
- Every other field keeps its meaning and its conservative default; they further restrict the build (the path limits what may be removed, the cloud limits which cloud is thinned). Keeper overrides and skipped groups work as before, within the selection.
- The estimate says how many selected contents have no exact duplicate group (unproven or single copies: nothing to remove) and how many are gone.
- If the catalog changed since the selection was made, the estimate shows the refusal `SELECTION_CHANGED` with **Use the current N** (which updates `seln`) and **Back to Explore**.
- **Create plan** persists the plan with the selection and the resolved group keys in its `params` (so the review can say "dedupe of an Explore selection (312 contents)" and the plan records exactly what it covered), and opens its review. Approval and run are unchanged.

## 6. Staged marks

`GET /v1/explore?draft=<id>` adds, to each copy that the draft's active steps move, where the draft puts it (the after-view location). Explore shows it on the copy line and as a badge on the result; the selection bar counts how many selected copies are already staged. This is not the after-view of Explore (results, presence columns and facets still describe the catalog); it is enough to see that a gathered file is on its way and not to gather it twice.

## 7. Cross-links

- Explore selection bar → Gather (stays in Explore, toast to the workspace) and Clean up duplicates (the dedupe form).
- Dedupe form with a selection → **Back to Explore** (`#/explore?q=<selq>`).
- Plan review and Plans list: the summary of a dedupe plan made from a selection reads "dedupe of an Explore selection (N contents)".
- Workspace staged drawer: a gather step reads "Gather N files into <folder>"; its title reveals the folder, as every step does.
- The per-result "dedupe here" button stays (scoped to a cloud and folder); the selection is the way to scope by content.

## 8. Known limitations (v1)

- Explore still reads the catalog, not the draft's after-view: only the copies the draft moves are marked (§6); results, presence and facets are as last scanned.
- Gathering flattens: every picked file lands directly in the destination folder; folder structure under it is not kept. It never renames; clashing names are skipped.
- Gathering moves one copy per content and never copies (a cross-cloud gather is a move: the original is quarantined after the copy is verified). "Copy, keep the original" needs a copy action in the workspace, which does not exist.
- A selection is cleared when the query changes; selections from several queries cannot be combined, and selections are not saved (saved queries are future work).
- Limits: 500 hand-picked contents, 500 exclusions, 5,000 contents for "all matching", 2,000 files per gather. "All matching" refuses a query that Explore truncates (more than 20,000 matching files): narrow the query first.
- Probable duplicates (no exact group) are never removed by a selection dedupe, as by any dedupe; bridging is the way to prove them.
- No export of a selection (CSV, list of paths).

---

# Implementation contract

Everything below is binding. Read first: [EXPLORE.md](EXPLORE.md) §A, §D, §E, §F, [WORKSPACE.md](WORKSPACE.md) §A, §C, §D, §G, [PLANS.md](PLANS.md) §A–§E, `explore.py`, `search.py` (`build_filter`), `compare.py` (`CompareError`, `load_remotes`, `remote_named`, `chunks`), `builders.py` (`build_dedupe`, `_group_overrides`, `choose_keeper`, `_ambiguous_paths`, `BuildError`), `workspace.py` (`add_action`, `_space_for`, `_Space.fwd`, `_Cat`, `WorkspaceError`, `_stage_checked`), `service.py` (`_build`, `estimate_plan`, `propose_plan`, `plan_summaries`, `explore`, `stage_action`), `api.py` (`build_router`, the coded handlers), `ui_api.py` (draft routes, `_coded`), `mcp_server.py` (`tool`, `stage_moves`, `propose_plan`), `cli.py` (`cmd_plan_dedupe`), and in `src/drivemerge/web/`: `explore.js`, `plans.js`, `app.js`, `api.js`, `query.js`, `util.js`, `panels.js`, `compare.js` (`PathPicker`).

## A. Definitions

- **Content key**: `g:<content_group>` or `e:<entry id>`, exactly as `explore.explore` builds it (a file of size 0 or without a group is its own content `e:<id>`). Grammar: `^(g|e):[1-9][0-9]{0,17}$`.
- **Copies of a key**: for `g:N`, every live file (`present = 1 AND is_dir = 0`, size > 0, outside the quarantine folder) with `content_group = N`; for `e:N`, that entry when it is a live file outside the quarantine folder. A key with no copy is **gone**.
- **Selection** (JSON, the only form any route or tool accepts):
  - query mode: `{"query": {<explore params>}, "except"?: [keys], "expect_total"?: int}`; the explore params are the names of `GET /v1/explore`'s query (`q`, `in`, `category`, `ext`, `min_size`, `max_size`, `modified_after`, `modified_before`, `path_prefix`, `on`, `not_on`, `only_on`, `copies_min`, `copies_max`), list params as JSON arrays; `sort`, `limit`, `offset` are not allowed.
  - keys mode: `{"keys": [keys]}`.
- Limits (module constants in `explore.py`): `PICK_LIMIT = 500` (keys and `except`), `SELECT_LIMIT = 5000` (resolved contents), `explore`'s `scan_cap` default (20,000) for query mode. `GATHER_LIMIT = 2000` (files picked by one gather) in `gather.py`.
- **After-view location** of a catalog location: where the open draft's active, non-stale steps put it (`_Space.fwd`); unchanged when no step touches it.

## B. Selections (`src/drivemerge/explore.py`)

```python
class SelectionError(CompareError): code: str; params: dict
SELECTION_CODES = ("BAD_SELECTION", "BAD_KEY", "TOO_MANY_KEYS", "SELECTION_EMPTY", "SELECTION_TOO_LARGE",
                   "SELECTION_TRUNCATED", "SELECTION_CHANGED")
def select(conn, selection: dict, *, limit: int = SELECT_LIMIT, with_keys: bool = False) -> dict
```

- Refactor `explore()` so that the hit query, grouping and presence/copies filtering are one internal function returning every filtered group (before sorting and paging); `explore()` and `select()` both use it. `explore()`'s output and its tests stay unchanged.
- Query mode: resolve the filtered groups of the query (no paging); a truncated hit scan raises `SELECTION_TRUNCATED {scan_cap}`; remove `except` keys; when `expect_total` is given and differs from the count after exclusions, raise `SELECTION_CHANGED {expected, total}`. Keys mode: validate each key, drop duplicates (first wins), count gone keys.
- Validation order: shape (exactly one of `query` / `keys`; `except` and `expect_total` only with `query`; unknown top-level or query key) → `BAD_SELECTION {param}` (the offending field name, or the query key); `keys` / `except` not a list of strings → `BAD_SELECTION {param}`; longer than `PICK_LIMIT` → `TOO_MANY_KEYS {param, count, limit}`; a malformed key → `BAD_KEY {key}` (the first one); query params as in `explore()` (unknown remote `NOT_FOUND`, bad value `BAD_QUERY {param}`); then resolution errors: `SELECTION_TRUNCATED`, `SELECTION_CHANGED`, `SELECTION_TOO_LARGE {total, limit}` (more than `limit` resolved contents), `SELECTION_EMPTY {}` (no live content left).
- Result:
```json
{"mode": "query", "total": 312, "grouped": 297, "ungrouped": 15, "gone": 0,
 "copies": 731, "bytes": 3120000000,
 "by_remote": [{"remote": "gdrive", "contents": 290, "copies": 640, "bytes": 0}],
 "redundant": {"copies": 419, "bytes": 0},
 "staged": null,
 "keys": ["g:7", "e:31"]}
```
  `total` = resolved live contents; `bytes` counts each content once; `by_remote` in configuration order, every remote listed (zeros included); `redundant` = copies beyond the first of each content (all clouds), bytes likewise; `keys` only with `with_keys` (in natural order of the key); `staged` is filled by the service (§E), `null` here.
- Read-only: a test asserts `conn.total_changes` does not move. One batched query per chunk of keys, no per-key query.
- `explore(..., draft_locate=None)`: an optional callable `(list[(remote_id, path)]) -> list[(remote_id, path)]` given by the service; when set, each returned copy gains `"staged": {"remote", "path"} | None` (`None` when the after-view location equals the catalog location) and each result `"staged": n` (its copies that move). Without it the output is unchanged.

## C. Dedupe over a selection (`builders.py`, `service.py`)

- `build_dedupe(..., groups: Sequence[str] | None = None)`: when given, only exact groups whose key is in `groups` are examined; everything else (pools, keeper policy, witnesses, `remote`, `cross_remote`, `path_prefix`, `min_size`, `keep`, `skip_groups`, `limit_groups`, `group_window`) behaves as today over that subset. `groups` must be a list of `g:<int>` strings of at most `SELECT_LIMIT` items, else `BuildError` `BAD_PARAM {param: "groups"}`. Keys naming no existing exact group are ignored and counted in `stats["groups_missing"]`. A `keep` or `skip_groups` key outside `groups` is `BAD_GROUP {group}` (it names no group of this build, as today). Without `groups` the output is byte-for-byte unchanged.
- `Service._build("dedupe", params)`: when `params` has `selection`, resolve it with `explore.select(conn, selection, with_keys=True)` (its `SelectionError`s propagate), pass `groups` = its `g:` keys to the builder, and return params that keep `selection` as given plus `groups` (the resolved `g:` keys). `selection` and `groups` together → `BuildError` `BAD_PARAM {param: "selection"}`. `stats["selection"] = {"contents", "grouped", "ungrouped", "gone"}` is added for both `estimate_plan` and `propose_plan`.
- `Service.plan_summaries`: a row whose `params.groups` is a list replaces it by `params.groups_count` (int); `GET /v1/plans/{id}`, `get_plan` (MCP) and the CLI keep the full list.

## D. Gather (`src/drivemerge/gather.py`, new; `workspace.py`)

```python
# workspace.py (new public function, read-only)
def locate(conn, draft_id: int, locs: Sequence[tuple[int, str]]) -> list[tuple[int, str]]
# gather.py
GATHER_LIMIT = 2000
SKIP_CODES = ("ALREADY_THERE", "NAME_TAKEN", "NAME_CLASH", "NOT_ON_TARGET", "READ_ONLY", "NATIVE_DOC",
              "AMBIGUOUS_PATH", "GONE")
CODES = ("NOTHING_TO_GATHER", "GATHER_TOO_MANY")      # raised as workspace.WorkspaceError (409)
def plan_gather(conn, draft_id: int, keys: Sequence[str], dst: dict, *, cross_cloud: bool = False,
                prefer_remotes: Sequence[str] = ()) -> dict
def gather(conn, draft_id: int, keys: Sequence[str], dst: dict, *, cross_cloud: bool = False,
           prefer_remotes: Sequence[str] = (), note: str | None = None, created_by: str) -> dict
```

- `locate`: the after-view locations of catalog locations for the draft's active steps (`_space_for(_Cat(conn), active acts)` then `fwd`); the draft must exist (`NOT_FOUND`), any status. No write.
- `plan_gather` (read-only): `dst = {"remote", "path"}` must be a folder of the after-view (`DST_NOT_DIR`), not at or below the quarantine folder (`QUARANTINE`), on a known remote (`NOT_FOUND`); the draft must be open (`NOT_OPEN`). For each key, in natural order of key:
  1. copies (§A); none → skip `GONE`. Each copy's after-view location via `locate`.
  2. some copy's after-view parent equals `dst` (same remote; NFC+casefold on a case-insensitive remote) → skip `ALREADY_THERE {name}`.
  3. candidates = copies on `dst.remote`; if none: without `cross_cloud` → skip `NOT_ON_TARGET {remote: dst.remote}`; with it, every copy, minus native documents (if that empties it → `NATIVE_DOC`). Drop copies on read-only remotes (empties → `READ_ONLY {remote}`, the first one's) and at ambiguous Drive paths (empties → `AMBIGUOUS_PATH {remote, path}`).
  4. pick `builders.choose_keeper(candidates, target=dst.remote, prefer_remotes=prefer_remotes)`.
  5. target = `dst.path/<after-view name of the pick>`: occupied in the after-view → skip `NAME_TAKEN {name}`; equal (casefolded on a case-insensitive destination) to the target of an earlier pick → skip `NAME_CLASH {name, with: {remote, path}}` (the earlier pick's source).
  
  Result:
```json
{"contents": 2, "files": 1, "bytes": 4100000, "cross_cloud": {"files": 0, "bytes": 0},
 "picked": [{"key": "g:9", "from": {"remote": "gdrive", "path": "Downloads/thesis-v9.pdf"},
             "to": {"remote": "gdrive", "path": "School/Thesis/thesis-v9.pdf"}, "size": 4100000, "cross_cloud": false}],
 "skipped": {"by_code": {"ALREADY_THERE": 1},
             "items": [{"key": "g:7", "name": "thesis_final.pdf", "code": "ALREADY_THERE", "params": {"name": "thesis_final.pdf"}}]},
 "items": [{"remote": "gdrive", "path": "Downloads/thesis-v9.pdf"}]}
```
  `from` is the after-view location (what the move action stages); `picked` and `skipped.items` are capped at 200 in the answer (counts are exact); `items` (full list, internal) is what `gather` stages and is not sent over HTTP. More than `GATHER_LIMIT` picked files → `GATHER_TOO_MANY {files, limit}`.
- `gather`: `plan_gather`, then, when nothing is picked, `NOTHING_TO_GATHER {contents, skipped}`; else `workspace.add_action(conn, draft_id, {"kind": "move", "items": items, "dst": dst, "note": note or "explore:gather"}, created_by=created_by)` in the same lock, and return the plan result (without `items`) plus `"action"`, `"draft_version"`, `"warnings"`. A `WorkspaceError` from `add_action` propagates unchanged (it should not happen after `plan_gather`; a test makes the catalog change in between and expects the coded refusal, not a partial action).
- Performance: 2,000 keys plan and stage in well under 2 s on a laptop (a test with a generated catalog asserts < 5 s).

## E. Service, CLI, MCP

- `Service.select(selection, *, draft_id=None, with_keys=False)`: `explore.select`, plus `staged = {"copies": n}` (selected copies the draft moves) when `draft_id` names an open draft.
- `Service.explore(q, ..., draft_id=None)`: passes `draft_locate` bound to `workspace.locate` when `draft_id` is given (unknown draft → `WorkspaceError NOT_FOUND`; a draft that is not open → no marks).
- `Service.gather(draft_id, selection, dst, *, cross_cloud=False, prefer_remotes=(), dry_run=False, note=None, created_by)`: resolves the selection with `with_keys=True`, then `gather.plan_gather` (dry run) or `gather.gather`; the result gains `"selection": {"total", "gone"}`.
- CLI: `drivemerge plan dedupe --group KEY` (repeatable; passes `groups`). No other CLI change.
- MCP:
  - `explore_contents(query="", in_remote=None, on=None, not_on=None, only_on=None, copies_min=None, copies_max=None, category=None, ext=None, min_size=None, max_size=None, modified_after=None, modified_before=None, path_prefix=None, sort="relevance", limit=25, offset=0)`: read-only; `limit` ≤ 100; each result's copies capped at 10 with `copies_truncated`; English; the untrusted-data note as for `search_files`.
  - `select_contents(selection)`: read-only; `Service.select` with `with_keys=False`.
  - `gather_into_draft(draft_id, selection, dst_remote, dst_path, cross_cloud=False, dry_run=False, note=None)`: `writes=True`, `created_by="mcp"`; the docstring says it only proposes (as `stage_moves`), lists the skip codes and warnings, and that the human previews and approves.
  - `propose_plan`'s docstring documents `params.selection` for `dedupe`.
  - No approve, execute or purge tool (unchanged). `SelectionError` is in the MCP user errors.

## F. HTTP

| Method & path | Auth | Body / query | Response |
|---|---|---|---|
| `GET /v1/explore` | normal (shared router) | as today plus `draft` (int) | as today; with `draft`, copies carry `staged` and results `staged` (§B) |
| `POST /v1/explore/selection` | normal (shared router) | `{"selection", "draft"?: int, "with_keys"?: bool}` | §B result (`staged` filled with `draft`) |
| `POST /v1/plans/estimate`, `POST /v1/plans` | normal | as today; `params.selection` for `dedupe` | as today; `stats.selection`; persisted `params.selection` and `params.groups` |
| `POST /v1/drafts/{id}/gather` | normal (UI app, like the other draft routes) | `{"selection", "dst": {"remote", "path"}, "cross_cloud"?: false, "prefer_remotes"?: [names], "dry_run"?: false, "note"?: str}` | dry run: 200 §D plan result + `selection`; else 201 the same plus `action`, `draft_version`, `warnings`; `created_by` `ui` for a session, `api` for the bearer token |

- Coded errors, `{"error", "code", "params"}` in both apps: `SelectionError` through the `CompareError` handler, which maps `NOT_FOUND` → 404, `BAD_QUERY`, `BAD_SELECTION`, `BAD_KEY`, `TOO_MANY_KEYS` → 400, the other selection codes → 409. Gather refusals are `WorkspaceError` (409, `NOT_FOUND` 404, `BAD_REQUEST` 400) with codes `NOT_FOUND`, `NOT_OPEN`, `DST_NOT_DIR`, `QUARANTINE`, `NOTHING_TO_GATHER`, `GATHER_TOO_MANY`. Order for gather: body shape (`BAD_REQUEST` 422/400) → draft (`NOT_FOUND`, `NOT_OPEN`) → selection errors → destination (`NOT_FOUND`, `QUARANTINE`, `DST_NOT_DIR`) → `GATHER_TOO_MANY` → `NOTHING_TO_GATHER`.
- None of these responses carries `approval_code`, digests or snapshots (extend the scrub tests). No route of this feature is on the human channel.

## G. Frontend (`src/drivemerge/web/`)

Same rules as WORKSPACE.md §F, EXPLORE.md §F and PLANS.md §E (no build step, offline, CSP, both languages, codes worded on the page, `data-testid` on what tests touch).

- `selection.js` (new): the selection model, pure and testable: `{mode: 'keys'|'query', keys: Set, except: Set, query, seen}`; `toggle(key)`, `selectPage(keys)`, `selectAllMatching(params, total)`, `clear()`, `body()` (the §A JSON), `toDedupeParams()` / `fromDedupeParams(urlParams)` (`selq`, `selx`, `seln`, `selk`), memory per query text for the visit; enforces `PICK_LIMIT` (the 501st pick is refused with `sel.limit`).
- `gather.js` (new): `GatherDialog` (destination cloud select of writable clouds, folder box with after-view autocomplete reusing `PathPicker` from `compare.js` with `draft`, **New folder…** through `ctx.stageMkdir`, the cross-cloud checkbox and its warning, debounced 300 ms dry runs with stale-response protection, sample of up to 20 moves, skipped list by code, Stage button).
- `explore.js`: the checkbox column, header checkbox, "select all N matching" line, the selection bar (from `api.selection`, debounced 250 ms after each change), the staged marks (`api.explore` gets `draft: ctx.draft.id`), opening `GatherDialog`, and **Clean up duplicates…** → `ctx.nav(plansHash('/new/dedupe', selection.toDedupeParams()))`.
- `plans.js`: the dedupe form reads `selq`/`selx`/`seln`/`selk` (through `selection.fromDedupeParams`), shows the selection banner, sends `params.selection` in estimate and create, handles `SELECTION_CHANGED` with **Use the current N**; `planSummary` words a dedupe with `params.selection` as `plans.sum.dedupeSelection {n}` (`n` = `groups_count` or `groups.length`).
- `app.js`: `ctx.stageGather(body)` enqueued in the same staging queue as `stageMove` (calls `api.gather`, shows warnings, `syncDraft`, success toast with **Open workspace**). No other change.
- `util.js`: `describeAction` words a move whose `note` is `explore:gather` as `act.gather {n, dst}`; `panels.js` does not show that note as free text.
- `api.js`: `selection(body)`, `gather(draftId, body)`; `explore` passes `draft`.
- `styles.css`: the column, bar, marks and dialog.
- `data-testid`s: `ex-select-<key>`, `ex-select-page`, `ex-select-all-matching`, `ex-selection-bar`, `ex-sel-count`, `ex-sel-copies`, `ex-sel-bytes`, `ex-sel-remote-<remote>`, `ex-sel-staged`, `ex-sel-cleared`, `ex-sel-clear`, `ex-sel-gather`, `ex-sel-dedupe`, `ex-sel-error`, `ex-staged`, `ex-staged-badge`, `gather-dialog`, `gather-dst-remote`, `gather-dst-path`, `gather-new-folder`, `gather-cross`, `gather-cross-warning`, `gather-check`, `gather-sample`, `gather-skipped`, `gather-skip-<CODE>`, `gather-stage`, `gather-error`, `dd-selection`, `dd-selection-back`, `dd-selection-remove`, `dd-selection-changed`, `dd-selection-update`.
- i18n namespaces (both languages): `sel.*`, `gather.*`, `gather.skip.<CODE>` for every `gather.SKIP_CODES` code, `err.<CODE>` for every `explore.SELECTION_CODES` and `gather.CODES` code, `act.gather`, `plans.sum.dedupeSelection`. Every sentence uses only the params the server sends.

## H. Tests

- `tests/test_selection.py` (new): both modes; every validation code and the order of §B; `except`; `expect_total` → `SELECTION_CHANGED` after a rescan that adds a matching file; `SELECTION_TRUNCATED` with a small `scan_cap`; `SELECTION_TOO_LARGE`; gone keys; `e:` keys for empty and ungrouped files; `by_remote` and `redundant`; quarantined copies never counted; `explore()` output unchanged with and without `draft_locate`; `total_changes` unchanged.
- `tests/test_builders.py` (extend): `groups` restricts to exactly those groups with every other parameter; `BAD_PARAM` for malformed groups; `groups_missing`; `keep` / `skip_groups` outside `groups` → `BAD_GROUP`; unchanged output without `groups`.
- `tests/test_gather.py` (new): every skip code at its step; keeper choice on the destination and with `prefer_remotes`; cross-cloud pick only with the option; native documents and read-only clouds; after-view sources (a copy the draft already moved is staged from where the draft put it; a folder staged by `mkdir` as destination); case-insensitive `ALREADY_THERE` and `NAME_CLASH`; `GATHER_TOO_MANY`; `NOTHING_TO_GATHER`; one action with every item, one undo step removes it; `locate`; the 2,000-key timing.
- `tests/test_service_workspace.py`, `tests/test_plans.py` (extend): `Service.gather` dry run persists nothing; `propose_plan` with `selection` records `selection` and `groups`; `plan_summaries` replaces `groups` with `groups_count`; `selection` + `groups` → `BAD_PARAM`.
- `tests/test_bulk_api.py` (new): every route of §F through both apps where mounted, bearer and session, the coded errors with their status and the gather refusal order, `created_by`, scrubbing.
- `tests/test_mcp_server.py`, `tests/test_cli.py` (extend): the three tools (read-only hints, caps, `created_by="mcp"`, English only), `propose_plan` with a selection, `plan dedupe --group`.
- `tests/test_i18n_contract.py` (extend): every code of `explore.SELECTION_CODES`, `gather.CODES` and `gather.SKIP_CODES` has its sentence in both languages using only its params.
- `tests/test_ui_bulk.py` (new, browser, mocked API: a subclass of `MockApi` from `test_ui_frontend.py`, as `test_ui_quarantine.py` does): checkboxes, page select and "all matching" with exclusions and the bar's server numbers; clearing on a query change and restoring on Back; the 500 limit; gather dialog (dry-run request bodies, debounce and stale answers, every skip code worded, cross-cloud warning, New folder, staged request body and toast); the staged step's wording in the drawer; staged marks; Clean up duplicates → the form's URL, banner, `params.selection` in estimate and create, `SELECTION_CHANGED` and **Use the current N**; the plan summary wording; with approvals disabled everything above works and the plan review shows the commands; both languages.
- `tests/test_ui_bulk_e2e.py` (new, real rclone, real catalog over local directories, real server, real Chromium; reuse `make_world` and `Page` from `test_ui_e2e.py`). Scenario, with extra files `gd: Downloads/survey.pdf` (the bytes of `Research/Papers/survey.pdf`) and `gd: Desktop/paper-a.pdf`:
  1. Open `#/explore?q=type:pdf`; select all matching; the bar reads 3 contents (`survey.pdf` with 2 copies on Drive, `paper-a.pdf`, and `hw1.pdf` on Dropbox only), 4 copies, and their bytes.
  2. Gather into `gd:Research/Papers`: the dry run shows `paper-a.pdf` moving, `survey.pdf` already there, `hw1.pdf` skipped `NOT_ON_TARGET`; tick the cross-cloud option: `hw1.pdf` moves from Dropbox with the warning. Stage; the toast leads to the workspace, whose staged drawer shows one step "Gather 2 files into gd:Research/Papers"; back in Explore the two copies carry staged marks.
  3. Preview, approve with the typed code, run, rescan from the banner. On disk: `gd/Research/Papers/paper-a.pdf` and `gd/Research/Papers/hw1.pdf` exist, `dbx/School/NTU/Courses/CS101/hw1.pdf` is in Dropbox's quarantine, nothing else changed.
  4. Explore `type:pdf copies:>=2`; select all matching (1 content); **Clean up duplicates…**: the form shows the selection banner and an estimate of 1 removal; Create plan; the review's summary reads "dedupe of an Explore selection (1 content)"; approve, run. On disk `gd/Downloads/survey.pdf` is in quarantine and `gd/Research/Papers/survey.pdf` stays. The plan's `params` (read with a second connection) hold `selection` and `groups == ["g:<survey's group>"]`.
  5. A file added on disk and rescanned from a terminal makes the old "all matching" dedupe URL answer `SELECTION_CHANGED`, worded on the page; **Use the current N** recovers.
- The whole suite stays green: `pytest` (with the browser tests), except `tests/test_docs_i18n.py` until `docs/BULK.zh-TW.md` exists.

## I. Work split

Two implementers work in parallel; no file is owned by both. The interface between them is this contract (codes, shapes, routes); the frontend develops against the mocked API and the code lists named here.

| Backend owns | Frontend owns |
|---|---|
| `src/drivemerge/explore.py` | `src/drivemerge/web/selection.js` (new) |
| `src/drivemerge/gather.py` (new) | `src/drivemerge/web/gather.js` (new) |
| `src/drivemerge/workspace.py` (`locate` only) | `src/drivemerge/web/explore.js` |
| `src/drivemerge/builders.py` (`groups` only) | `src/drivemerge/web/plans.js` |
| `src/drivemerge/service.py` | `src/drivemerge/web/app.js` (`stageGather` only) |
| `src/drivemerge/api.py` | `src/drivemerge/web/api.js` |
| `src/drivemerge/ui_api.py` (gather route only) | `src/drivemerge/web/util.js`, `panels.js` (gather wording only) |
| `src/drivemerge/mcp_server.py` | `src/drivemerge/web/compare.js` (export `PathPicker`, accept `draft`; nothing else) |
| `src/drivemerge/cli.py` (`--group` only) | `src/drivemerge/web/i18n.js`, `styles.css` |
| `tests/test_selection.py`, `tests/test_gather.py`, `tests/test_bulk_api.py` (new) | `tests/test_ui_bulk.py`, `tests/test_ui_bulk_e2e.py` (new) |
| `tests/test_builders.py`, `test_explore.py`, `test_explore_api.py`, `test_service_workspace.py`, `test_plans.py`, `test_workspace.py`, `test_mcp_server.py`, `test_cli.py` (extend) | `tests/test_i18n_contract.py` (extend) |
| `docs/BULK.zh-TW.md` (new), `README.md` (one paragraph and the design-documents line) | |

Order: both start at once. The frontend's `test_i18n_contract.py` extension imports `explore.SELECTION_CODES`, `gather.CODES` and `gather.SKIP_CODES`, and `test_ui_bulk_e2e.py` needs the real server, so those two are run green after the backend lands; everything else of each side is green on its own.
