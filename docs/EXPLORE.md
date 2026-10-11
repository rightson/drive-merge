# Explore and Compare

**English** | [繁體中文](EXPLORE.zh-TW.md)

> This document adds three read-only views to the visual workspace ([WORKSPACE.md](WORKSPACE.md)): **Overview**, **Explore** and **Compare**. The first part is the design; the second part (the implementation contract) is binding for implementers.

## 1. The problem

Four clouds that grew side by side hold *similar* trees: `School` on Drive, `School (1)` on Dropbox, half of it again under `Backup/2021` on OneDrive. The two-pane workspace is good at *moving* things once you know what to move. It does not answer the questions that come first:

1. **Where is it?** "Where are all the copies of my thesis, under any name?"
2. **How do my clouds overlap?** "How much of Dropbox is already on Drive? What exists on only one cloud and would be lost with it?"
3. **How do these two folders differ?** "`gdrive:School` and `dropbox:School (1)` look alike: which files are the same, which differ, which exist on one side only, which were just renamed or moved?"

Every answer must keep the project's rule of evidence: *identical* is claimed only with a strong-hash proof (content group); everything else is "different" (proven by size or an unequal hash) or "not proven".

## 2. Information architecture

A narrow **rail** on the left switches between four views. The workspace stays what it is; the three new views are read-only and lead into it.

| View | Question | Main element |
|---|---|---|
| Overview | How do my clouds overlap? | Cloud cards, an intersection chart (UpSet style), similar-folder list |
| Explore | Where is it? | A query bar with filter chips; results grouped by *content*, one presence column per cloud |
| Compare | How do these two folders differ? | One aligned diff tree of two folders on any clouds, with status filters and a detail panel |
| Workspace | How should it be arranged? | The existing two panes, draft, preview and approval |

Why an intersection chart and not a Venn diagram: a four-set Venn diagram has 15 regions that cannot be drawn with equal-sized circles and cannot be read. An UpSet chart shows each exact combination of clouds as one bar (sorted by bytes) with a dot matrix underneath, scales to any number of clouds, and every bar is a link to the files behind it.

Why results are grouped by content: the user's question is about *content*, not about entries. Ten copies of one PDF are one result with ten locations, and the presence columns show at a glance on which clouds it lives. A copy that a search term does not match (renamed) is still listed under its content, marked as not matching.

Why Compare aligns on relative paths first and content second: two folders that were synced and then diverged mostly keep their relative paths, so path alignment explains most rows; content alignment then finds what was renamed or moved inside the folders, which a plain path diff would show as one deletion plus one addition.

### Cross-links (the views are one tool, not four)

- Overview → Explore: a bar of the intersection chart opens Explore filtered to exactly that set of clouds (`only:dropbox,onedrive`). A cloud card's "only here" number opens `only:<cloud>`.
- Overview → Compare: each similar-folder row (from the suggestion engine) has a Compare button.
- Explore → Compare: a result with copies in two different folders offers "Compare their folders" (the two parent folders).
- Explore / Compare → Workspace: "Reveal" opens the workspace with the item focused in the left pane.
- Workspace → Compare: the row menu of a folder has "Compare with…", which opens Compare with that folder on the left and the picker focused on the right.
- Workspace suggestions drawer: each card gets a Compare button next to "Add".
- Compare → draft: "Merge left into right" (and the reverse) stages one move action; a one-side-only row offers "Move to the other side". Staging goes through the same `POST /v1/drafts/{id}/actions` as dragging, with the same warnings and refusals, and the toast offers "Open workspace".

## 3. Overview

```
┌ rail ┐┌─────────────────────────────────────────────────────────────────────┐
│  ◎   ││ gdrive ● 41,203 files · 212 GB   dropbox ● 18,550 · 96 GB   …       │
│  ⌕   ││   only here: 9,120 · 51 GB         only here: 1,204 · 3.1 GB        │
│  ⇆   ││   no comparable hash: 0            no comparable hash: 0            │
│  ▦   │├─────────────────────────────────────────────────────────────────────┤
│      ││ Unique content 238 GB · Stored 401 GB · Redundant 163 GB            │
│      ││ ████████████  ████████  ███  ██  ▌ ▌ ...   (bytes per combination)  │
│      ││   ●            ●         ●   ●             gdrive                    │
│      ││                ●         ●                 dropbox                   │
│      ││                          ●   ●             onedrive                  │
│      ││ [ files | bytes ]  [ all categories ▾ ]                              │
│      │├─────────────────────────────────────────────────────────────────────┤
│      ││ Similar folders   gdrive:School ⇆ dropbox:School (1)   93%  Compare  │
└──────┘└─────────────────────────────────────────────────────────────────────┘
```

- Cloud cards: files, bytes, last completed scan, read-only badge, "only here" (contents with no copy on another cloud: the risk number), and "no comparable hash" (files without a content group: iCloud before a bridge). When that last number is not zero the card says that the overlap is *under*-counted for that cloud and shows the `drivemerge bridge` command.
- Chart: the measure toggles between contents and bytes; a category filter recomputes it (photos only, documents only). Bars are sorted by the measure, the empty combination never appears, at most 24 bars are drawn plus one "other combinations" bar. Hover and keyboard focus show the exact numbers; Enter or a click opens Explore.
- Similar folders: the first 10 suggestions of both kinds (`/v1/suggestions`), worded as in the drawer, each with Compare (the first two folders; a small menu when there are more).

## 4. Explore

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ ⌕  thesis  [type:pdf ×] [not:gdrive ×] [copies:1 ×]               ⟲  ?      │
├───────────────┬─────────────────────────────────────────────────────────────┤
│ Clouds        │ name                    gd  db  od  ic   size    modified    │
│ ☐ on gdrive 12│ thesis_final.pdf        ●   ●2  ○   ○   4.2 MB  2023-06-01  │
│ ☐ on dropbox 9│   ▸ gdrive:School/Thesis/thesis_final.pdf                    │
│ Copies        │   ▸ dropbox:School (1)/thesis_final.pdf                      │
│ ☐ only one  3 │   ▸ dropbox:Backup/thesis-v9.pdf      (name does not match)  │
│ Category      │ thesis_draft.docx       ○   ●   ○   ○   880 KB  2022-11-12  │
│ ☐ document 40 │ …                                     50 of 213 · load more  │
└───────────────┴─────────────────────────────────────────────────────────────┘
```

**Query language** (typed in one box, parsed on the page into chips; free words stay words):

| Token | Meaning | Request parameter |
|---|---|---|
| `word`, `"two words"` | name or path contains it (CJK works, as in search) | `q` |
| `in:gdrive` | the *matching copy* is on that cloud | `in` |
| `on:gdrive` | the content has a copy on that cloud (repeatable: all of them) | `on` |
| `not:gdrive` | the content has no copy on that cloud (repeatable) | `not_on` |
| `only:gdrive,dropbox` | the content is on exactly these clouds | `only_on` |
| `copies:1`, `copies:>2`, `copies:2..4` | number of live copies | `copies_min`, `copies_max` |
| `type:pdf` | extension | `ext` |
| `cat:image` | category (`categories.py`) | `category` |
| `size:>10MB`, `size:<1k`, `size:1M..1G` | size (B, K/KB, M/MB, G/GB, T/TB; powers of 1024) | `min_size`, `max_size` |
| `modified:2023`, `modified:2023-06`, `modified:>2024-01-01`, `modified:2021..2022` | modification time | `modified_after`, `modified_before` |
| `path:School/Thesis` | under this path (with `in:` on one cloud, else on any) | `path_prefix` |

An unknown key (`foo:bar`) or a malformed value stays visible as a red chip with a tooltip and is not sent. The `?` button opens this table. The facet sidebar toggles the same chips, so the box always shows the whole query. The query lives in the URL (`#/explore?q=…`), so Back works and a query can be bookmarked.

**Results**: one row per content. Presence columns, one per cloud in configuration order, each in the cloud's colour: a filled dot when the cloud holds a copy (with the count when it holds more than one), an empty ring when not; every cell has an accessible label ("on dropbox, 2 copies"). A content without a content group (not hash-identified) shows a dashed outline and the tooltip "identity not proven: copies elsewhere may exist under a different hash". Sort by relevance (default with words), size, modified, name, or copies. A row expands to all its copies, each with Reveal, and "Compare their folders" when two copies live in different folders.

## 5. Compare

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ [gdrive ▾] School           ⇄   [dropbox ▾] School (1)      similarity 87%  │
│ Same 1,204 · Probably same 12 · Differ 9 · Moved 31 · Only left 40 · Only…  │
│ [ ] show same      filter: [differ][moved][only left][only right][unproven] │
├───────────────────────────────────────────┬─────────────────────────────────┤
│ ▾ Thesis/                     ▮▮▮▮▮▯▯ 23  │ thesis_final.pdf                │
│     thesis_final.pdf      ≠ differs        │ left  4,212,331 B  2023-06-01   │
│     notes.md        → moved to old/notes.md│ right 4,198,004 B  2023-05-28   │
│   ◧ figure3.png          only left · 2 ⧉   │ evidence: sizes differ          │
│ ▸ Courses/                    ▮▮▮▮▮▮▮ 12  │ [Reveal left] [Reveal right]    │
└───────────────────────────────────────────┴─────────────────────────────────┘
```

- **Pickers**: a cloud select and a path box with folder autocomplete (from `GET /v1/tree`) on each side; ⇄ swaps. Compare runs when both are set, and the pair is in the URL (`#/compare?l=gdrive:School&r=dropbox:School%20(1)`).
- **Status of a row** (§B): `same` (proven identical), `unverified` (shown as "probably same — not proven": same path, nothing proves a difference, nothing proves identity), `differs` (proven different), `moved` (identical content at another relative path on the other side), `left_only`, `right_only`. A one-side-only row also says whether its content has copies elsewhere (outside both folders, ⧉ with a count) or none at all (`unique`, a warning tone: losing this side loses the content).
- **Tree**: rows are grouped into folders by relative path. A folder row carries a small stacked bar of its children's statuses and a count of non-same files; folders whose files are all `same` are collapsed, and with "show same" off (the default) they are hidden. The filter chips toggle statuses; counts in the header are always for the whole comparison.
- **Detail panel**: both sides' paths, sizes, modification times, the hash evidence (which hash type matched or differed, or "no comparable hash"), the content's copies elsewhere, and Reveal buttons.
- **Staging**: header buttons "Merge left into right" and "Merge right into left" stage one move of the whole folder (destination: the other folder's parent, `new_name`: the other folder's name, which makes it a merge; identical files become witnessed dedupes and same-name different files become conflicts in the preview, as always). A one-side-only row offers "Move to the other side" (destination: the same relative parent on the other side, which must exist; otherwise the button explains that the folder is missing there). Compare reads the catalog, not the draft: when the open draft has active steps, a note says that the comparison shows the clouds as last scanned.

## 6. What the views never do

They never write a cloud, never change the catalog and never create a plan. Staging from Compare adds an action to the open draft exactly like a drag; the rest of the path (preview, simulator, typed approval) is unchanged. Compare refuses rather than truncates: a diff that silently left files out would claim that they do not exist.

## 7. Known limitations (v1)

- Compare and Explore read the catalog (the clouds as last scanned), not the draft's after-view.
- Empty folders are not compared (the catalog's folders carry no content).
- Identity is exact groups only; "probable" duplicates (same name and size) are not merged into one Explore result, and Compare reports them as `unverified` only when they sit at the same relative path.
- Explore groups at most 20,000 matching files per query; beyond that the answer says it is truncated and asks for a narrower query.

---

# Implementation contract

Everything below is binding. Read first: [WORKSPACE.md](WORKSPACE.md) (especially §D, §F and §G), `src/drivemerge/search.py`, `identity.py` (`content_key`, the proof rules), `db.py` (`quarantine_dir`, `outside_quarantine`), `dirstats.py`, `ui_api.py`, `api.py`, `service.py`, and every file in `src/drivemerge/web/`.

## A. Common rules

- Read-only: no module of this feature writes the catalog.
- "Live file" = `present = 1 AND is_dir = 0` and outside the quarantine folder (`db.outside_quarantine` or its SQL equivalent). Quarantined files appear in none of these views.
- Content key = `identity.content_key(row)` (`g:<group>` or `e:<entry id>`). Two files are **proven identical** only when they share a non-null `content_group`. They are **proven different** when both sizes are known and unequal, or when some hash type in `search.HASH_KEYS` is non-null on both and unequal. Otherwise they are **not proven** either way.
- Empty files (size 0) are not content for coverage and copies counting (they are placeholders, as `dup_files` already treats them), but they are compared in Compare by path like any other file (two empty files at the same path are `same` when grouped, else `unverified`).
- Remote order is configuration order (`remotes.id`). Remote names, not ids, cross the HTTP boundary.
- Errors follow §D of WORKSPACE.md: an English `error`, a `code`, `params` for the page. New codes: `NOT_A_FOLDER` (409: the path is not a live folder of that remote; `''` is always a folder; params `{remote, path}`), `COMPARE_NESTED` (409: one folder is the other or lies inside it on the same remote; params `{left, right}` as `{remote, path}`), `COMPARE_TOO_LARGE` (409: a side has more live files than `max_files`; params `{side: "left"|"right", files, limit}`), `QUARANTINE` (409: a compared folder is at or below the quarantine folder), `BAD_QUERY` (400: an invalid explore or coverage parameter; params `{param}`). Unknown remote names are 404 `NOT_FOUND`.

## B. Compare (`src/drivemerge/compare.py`)

```python
class CompareError(ValueError): code: str; params: dict
def compare_folders(conn, left: tuple[str, str], right: tuple[str, str], *, max_files: int = 20000) -> dict
```

Inputs are `(remote_name, path)` with catalog path conventions (strip `/`). Order of checks: unknown remote (`NOT_FOUND`), `QUARANTINE`, `NOT_A_FOLDER`, `COMPARE_NESTED`, `COMPARE_TOO_LARGE` (use `dir_stats.files` to refuse before loading when available; recount from the rows otherwise).

Algorithm:
1. Load the live files under each root. `rel` = path relative to the root. Match key `k(rel)` = NFC of `rel`, casefolded when **either** remote is case-insensitive.
2. **Path pass.** For each key present on both sides, pair files. Drive can hold several files at one path: pair proven-identical ones first, then the rest in entry-id order; files left over go on to step 3. A pair's status: `same` if proven identical; `differs` if proven different; else `unverified`.
3. **Content pass.** Among unpaired files, pair a left and a right file that share a non-null `content_group` → `moved` (pair within a group in `rel` order, one to one).
4. The rest are `left_only` / `right_only`. For each, when its `content_group` is not null, count live copies of that group outside both compared roots (any remote): `elsewhere = {"count": n, "examples": [up to 3 {"remote","path"}]}`; `unique = (n == 0)` and no copy inside the other root either. An ungrouped one-side file has `elsewhere = None`, `unique = None` (unknown).
5. `evidence` for a pair: `{"kind": "group"}` for `same`; `{"kind": "size"}` or `{"kind": "hash", "hash": "<type>"}` for `differs`; `{"kind": "none"}` for `unverified`.

Result:
```json
{
  "left":  {"remote": "gdrive",  "path": "School",      "files": 1310, "bytes": 0},
  "right": {"remote": "dropbox", "path": "School (1)",  "files": 1290, "bytes": 0},
  "summary": {"same": {"files": 0, "bytes": 0}, "unverified": {...}, "differs": {...}, "moved": {...},
              "left_only": {...}, "right_only": {...}, "unique": 0, "similarity": 0.87},
  "rows": [
    {"rel": "Thesis/a.pdf", "status": "differs", "rel_right": null,
     "left":  {"entry_id": 1, "path": "School/Thesis/a.pdf", "name": "a.pdf", "size": 1, "mtime": "…",
               "category": "document", "content_group": 7, "hashes": {"md5": "…"}},
     "right": {…}, "evidence": {"kind": "size"}, "elsewhere": null, "unique": null}
  ]
}
```
- `rows` are sorted by the natural, case-insensitive order of `rel` (for `moved` and `right_only` rows `rel` is the left rel when there is one, else the right rel; `rel_right` is set only for `moved`).
- `summary` bytes count the left file for pairs and one-side-left rows, the right file for `right_only`.
- `similarity` = `(same + moved) / (same + moved + unverified + differs + left_only + right_only)` by file count of pairs and singles (a pair counts once); `1.0` for two empty folders.
- Performance: two indexed range queries (`entries_path`), one group query for `elsewhere` over the one-side groups; no per-row query. 20,000 files per side must answer in well under a second on a laptop.

## C. Coverage (`src/drivemerge/coverage.py`)

```python
def coverage(conn, *, category: str | None = None, max_bars: int = 24) -> dict
```
Over live files with `size > 0` (optionally of one category), per content key collect the set of remotes that hold a copy.
```json
{
  "remotes": [{"name", "provider", "writable", "files", "bytes", "only_here": {"contents", "bytes"},
               "unverified_files", "last_scan": {"id", "status", "finished_at"} | null}],
  "totals": {"contents", "bytes", "stored_bytes", "redundant_bytes", "copies"},
  "intersections": [{"remotes": [names], "contents", "bytes", "copies", "stored_bytes"}],
  "other": {"combinations", "contents", "bytes", "copies", "stored_bytes"} | null
}
```
- `bytes` of a content = its size once; `stored_bytes` = sum over copies. `redundant_bytes = stored_bytes − bytes`.
- `intersections` sorted by `bytes` desc, then `contents` desc, then names; the first `max_bars` combinations are listed, the rest are summed into `other`.
- `unverified_files`: live, size > 0 files of that remote without a content group (each counts as its own content: the page warns that overlap is under-counted).
- Do it in SQL with `GROUP BY` on the content key (a group's remote set via `GROUP_CONCAT(DISTINCT remote_id)`), then aggregate the sets in Python. No per-content Python loop over copies beyond that one pass.

## D. Explore (`src/drivemerge/explore.py`)

```python
def explore(conn, q: str | None = None, *, in_remote=None, category=None, ext=None, min_size=None, max_size=None,
            modified_after=None, modified_before=None, path_prefix=None, on: list[str] = (), not_on: list[str] = (),
            only_on: list[str] | None = None, copies_min=None, copies_max=None, sort="relevance",
            limit=50, offset=0, scan_cap=20000) -> dict
```
- Hits: live files that match `q` and the attribute filters with **exactly** the rules of `search.search` (refactor `search.py` so the WHERE builder is shared; `search.search` must keep its behaviour and its tests unchanged). `in_remote` restricts hits to one remote.
- Group hits by content key; for each group key load **all** live copies (renamed copies included, each with `matched: bool`). Presence filters and `copies_*` apply to the full copy set: `on` = every listed remote holds a copy; `not_on` = none does; `only_on` = the set of remotes equals it exactly; copies counts every live copy.
- At most `scan_cap` hits are read (in the hit order); `truncated: true` when there were more.
- Sort: `relevance` (best bm25 rank of a matching copy when the query has an FTS term, else name), `size` desc, `mtime` desc (newest copy), `name`, `copies` desc. Ties by name then key.
- Facets over the filtered groups (before pagination): `{"remotes": [{"name", "contents"}], "copies": {"1": n, "2": n, "3+": n}, "categories": [{"category", "contents"}], "exts": [top 12 {"ext", "contents"}]}`.
```json
{"total": 213, "truncated": false, "facets": {…},
 "results": [{"key": "g:7", "name": "thesis_final.pdf", "size": 4212331, "category": "document", "ext": "pdf",
              "mtime": "…", "verified": true, "remotes": ["gdrive", "dropbox"], "copies_count": 3,
              "copies": [{"entry_id", "remote", "path", "name", "mtime", "matched"}]}]}
```
`limit` ≤ 200. Unknown remote names in any parameter are 404 `NOT_FOUND`; contradictory presence filters (`on` ∩ `not_on`) just return nothing.

## E. Service, HTTP and MCP

- `Service.compare(left, right, max_files=…)`, `Service.coverage(category=None)`, `Service.explore(**params)`.
- Routes, on the shared router in `api.py` (so both `drivemerge serve` and `drivemerge ui` have them, same auth as `/v1/search`):

| Method & path | Query | Response |
|---|---|---|
| `GET /v1/compare` | `left_remote, left_path, right_remote, right_path, max_files` (≤ 100000) | §B |
| `GET /v1/coverage` | `category`, `max_bars` (≤ 64) | §C |
| `GET /v1/explore` | the §D parameters; list parameters are comma-separated (`on=gdrive,dropbox`) | §D |

  `CompareError` maps to its status (404 for `NOT_FOUND`, 400 for `BAD_QUERY`, else 409) with `{"error","code","params"}` in the UI app (coded handlers) and the REST app alike.
- MCP: read-only tools `compare_folders(left_remote, left_path, right_remote, right_path)` (rows capped at 300 in the answer plus the full summary, and a `rows_truncated` flag) and `coverage(category?)`. English messages only, as for every MCP tool.

## F. Frontend (`src/drivemerge/web/`)

Same rules as WORKSPACE.md §F: no build step, offline, CSP-safe (no inline script or style attributes that need `unsafe-eval`; `style=` attributes are allowed by the CSP), Preact + htm from `vendor/`, every user-visible string through `t()` with keys in **both** languages, server messages worded from `code` + `params`, `data-testid` on what tests touch.

Files:
- `shell.js`: the rail and the router. Routes in `location.hash`: `#/overview`, `#/explore?<params>`, `#/compare?l=<remote>:<path>&r=<remote>:<path>`, `#/workspace`. No route (or the token exchange's `#token=`) means the workspace, so every existing test keeps loading the workspace. The workspace stays mounted when another view shows (hidden with the `hidden` attribute), so its draft, polling and pane state survive switching. `Alt+1…4` switch views; `/` focuses the Explore box (switching to Explore) when focus is not in a text field. The rail is a `nav` with `aria-current="page"` on the active item.
- `clouds.js`: a stable colour per remote from a fixed palette of 8 hues with light and dark variants (CSS custom properties `--cloud-0` … `--cloud-7`, chosen for ≥ 3:1 contrast against the surface in both schemes), assigned by configuration order; `CloudDot`, `PresenceCells` components.
- `query.js`: `parseQuery(text, remoteNames) -> {params, chips, errors}` and `formatQuery(params) -> text`, pure functions (§4 table). Round-trip: `parseQuery(formatQuery(p)).params` equals `p`.
- `overview.js`, `explore.js`, `compare.js`: the views of §3–§5. The chart is inline SVG drawn by the component (no chart library); bars are focusable (`role="button"`, label with the numbers).
- `api.js`: `compare`, `coverage`, `explore`.
- `app.js`, `tree.js`, `panels.js`: only the hooks of §2 (rail host, "Compare with…" in the folder row menu, Compare on suggestion cards, a `reveal` entry point the other views call). Keep these edits small; do not restructure the workspace.

Compare tree rendering: build folder nodes from `rel` on the page; render only expanded nodes; cap at 2,000 visible rows with a "show more" row. Keyboard: Up/Down move, Right/Left expand/collapse, Enter opens the detail; the tree is a `role="tree"`.

Explore: debounce typing by 250 ms, cancel stale responses (ignore a response whose request is not the latest), page with "load more" (50 at a time).

New i18n namespaces: `nav.*`, `ov.*`, `ex.*`, `q.*`, `cmp.*`, `cloud.*`, plus `err.NOT_A_FOLDER`, `err.COMPARE_NESTED`, `err.COMPARE_TOO_LARGE`, `err.BAD_QUERY` (and `err.QUARANTINE` exists already). Status labels: `cmp.status.<status>`; evidence: `cmp.evidence.<kind>`.

## G. Tests

- `tests/test_compare.py`: every status with a hand-built catalog (path pass, Drive same-path duplicates, content pass for a moved file, case-insensitive matching, NFD vs NFC names, `elsewhere` / `unique`, quarantine excluded, every refusal code and their order, similarity edge cases, `max_files`).
- `tests/test_coverage.py`, `tests/test_explore.py`: intersections and `other` bucketing, `only_here`, `unverified_files`, category filter; explore grouping with renamed copies (`matched`), each presence filter, copies filters, sorts, facets, `truncated`, and that `search.search` results are unchanged.
- API tests for the three routes through both apps (bearer and session), including the coded errors.
- MCP tests for the two tools.
- `tests/test_ui_frontend.py` (mocked API): rail switching and `aria-current`, routes from the hash and Back, workspace state kept across switches, `parseQuery`/`formatQuery` round-trip and red chips, explore results and presence cells, facet toggles editing the query text, overview bars opening Explore with `only:`, compare tree statuses and filters, detail panel, merge staging request body, a one-side "Move to the other side" body, refusal wording from `code`, both languages.
- The i18n contract test must also cover the new error codes (extend `test_i18n_contract.py` so that every code `compare.py` and the routes can raise has `err.<CODE>` in both languages).
- The whole suite stays green: `pytest` (with the browser tests).
