# Plans in the Browser

**English** | [繁體中文](PLANS.zh-TW.md)

> This document adds a **Plans** view to the visual workspace ([WORKSPACE.md](WORKSPACE.md), [EXPLORE.md](EXPLORE.md)): building dedupe and consolidation plans, reviewing any plan (including the ones Claude proposes over MCP), approving it with the typed code, running it and bridging hashless files, all from the page. The first part is the design; the second part (the implementation contract) is binding for implementers.

## 1. The problem

Overview, Explore and Compare show *what* is redundant, scattered or at risk. Acting on it at scale still needs the terminal: `drivemerge plan dedupe`, `plan consolidate`, `plan show`, `plan approve`, `bridge`. The page can only stage moves one gesture at a time, which is right for rearranging folders and wrong for "remove 40,000 redundant copies" or "gather everything onto Drive".

Questions this view answers:

1. **What would cleaning up duplicates do, and which copy stays?** Before anything exists: how many groups, how many bytes come back, which copy the keeper policy keeps, and the chance to overrule it for a group.
2. **What would consolidating onto one cloud do?** How much is already there, how much must be copied (and through this machine, across clouds), what is skipped and why, and where the files would land.
3. **What is waiting for me?** Every plan, whoever proposed it (the CLI, the API, Claude, this page), with its simulation, so a person reviews and approves in one place.

## 2. Principles

- **The safety path does not change.** Every plan made here is built by the same builders, simulated by the same simulator, approved by typing the same 8-character code and run by the same executor. The page adds no new way to change a cloud; it adds a way to *ask* for the existing one.
- **Estimate before a plan exists.** Building a plan is cheap and read-only; persisting it creates a record that has to be cancelled. So every flow shows a live estimate from an in-memory build (nothing persisted) and creates the plan only on an explicit "Create plan".
- **Defaults are the conservative ones.** Dedupe thins each cloud on its own (`cross_remote` off), so no cloud loses its own copy unless the person asks for that. Consolidation copies only (`delete_sources` off); removing the sources is a separate, later plan after the soak period of [DESIGN.md](DESIGN.md) §8, and the page says so.
- **Who proposed it is visible.** Plans made by Claude carry the "proposed by Claude" badge the workspace uses; the code to approve is never shown to anyone but the person at a browser session that may approve.
- **The page words everything.** Builders and the server send codes and params; the page writes the sentences in both languages.

## 3. Information architecture

A fifth rail item, **Plans** (`#/plans`), between Compare and Workspace.

```
┌ rail ┐┌───────────────────────────────────────────────────────────────────────┐
│  ◎   ││ Plans                                [ New: Dedupe ] [ New: Consolidate ]│
│  ⌕   ││ ┌ Waiting for review ─────────────────────────────────────────────────┐│
│  ⇆   ││ │ #14 consolidate → gdrive:Archive   ✦ Claude   8,120 ops   ok   Review││
│  ☰   ││ │ #13 dedupe (each cloud)            ui         2,310 ops   2 problems ││
│  ▦   ││ └─────────────────────────────────────────────────────────────────────┘│
│      ││ Approved · Running · Done · Failed · Cancelled   (tabs with counts)     │
└──────┘└───────────────────────────────────────────────────────────────────────┘
```

- **List**: grouped by status, "waiting for review" (`draft`, `simulated`) first. Each row: id, kind, a one-line summary worded from `params` (target, root, scope), who proposed it, op count, simulation verdict (ok / n problems / not simulated / outdated), age. Workspace previews are listed too (kind `workspace`) and open the workspace's own preview instead.
- **Review** (`#/plans/<id>`): the simulation report as the workspace preview shows it (before/after per cloud, transfer across clouds, violations and warnings with their translated sentences and reveal links), plus the ops: grouped by op kind and cloud with counts and bytes, a paged table (path from → to, witness for deletes, reason) with a filter box. Actions: **Simulate again** (when the catalog changed since the last simulation, or always), **Approve** (typed code, only when the simulation is ok and approvals are enabled), **Run** (only when approved and execution is enabled), **Cancel plan**. Running shows the same progress panel as the workspace and ends with the same "catalog is behind" banner and rescan.
- **New dedupe** (`#/plans/new/dedupe`) and **New consolidate** (`#/plans/new/consolidate`): a form on the left, the live estimate on the right, "Create plan" at the bottom, which opens the new plan's review.

## 4. Dedupe flow

```
┌ Scope ─────────────────────────────┐┌ Estimate ─────────────────────────────────┐
│ Clouds   (•) all  ( ) only [gdrive▾]││ 2,310 copies in 1,204 groups · 41.2 GB back│
│ Mode     (•) within each cloud      ││ skipped 12 (read-only 9 · ambiguous path 3)│
│          ( ) across clouds  ⚠       ││ ┌ group ───────────── keep ─────── remove ┐│
│ Path     [ Photos/          ]       ││ │ IMG_0412.jpg 4.1 MB  gdrive:Photos/…  1  ││
│ Min size [ 1 MB ]                   ││ │   ○ gdrive:Photos/2019/IMG_0412.jpg ★    ││
│ Prefer   gdrive ⠿ dropbox ⠿ …       ││ │   ○ gdrive:Backup/IMG_0412 (1).jpg  ✕    ││
│                                     ││ │   [ skip this group ]                    ││
│               [ Create plan ]       ││ └──────────────────────────────────────────┘│
└─────────────────────────────────────┘└─────────────────────────────────────────────┘
```

- Parameters map one to one onto `builders.build_dedupe`: `remote`, `cross_remote`, `path_prefix`, `min_size`, `prefer_remotes` (an orderable list of clouds).
- "Across clouds" keeps one copy *in total* and removes every other copy on every cloud. It is off by default and, when on, the page explains that a cloud will then lose its own copy.
- The estimate lists groups (biggest reclaimable bytes first, paged). In each group the policy's keeper carries ★; a click on another copy makes it the keeper of that group (an override), and "skip this group" excludes it. Overrides and skips are part of the plan's `params`, so the plan records why each copy stays. An override may only pick a copy that the builder could have kept (same pool: same cloud without "across clouds"; not quarantined, not a native doc, not at an ambiguous Drive path); the server refuses anything else.
- Skipped copies are summarised by reason (read-only cloud, ambiguous Drive path, native document) and listed on demand.

## 5. Consolidate flow

- Parameters map onto `builders.build_consolidate`: `target` (only writable clouds are offered), `target_root`, `sources` (checkboxes, all others by default), `reorganize_target`, `include_probable` (off; when on the page explains that a probable match is assumed identical without proof and offers bridging instead), `path_prefix`, `prefer_remotes`, and `delete_sources`.
- `delete_sources` is not on the main form. It sits behind "Clean up sources (after the soak period)", which explains §8 of DESIGN.md and shows, from the coverage numbers, how much content is still *not* on the target. While that number is not zero the option stays available but carries a warning, because those contents would be copied and their sources removed in one plan.
- The estimate shows: contents already on the target, contents to copy and their bytes (and the part that crosses clouds through this machine), moves on the target when reorganising, deletes when cleaning up, skipped by reason (probable match not proven, native document, read-only, ambiguous), and a sample of placements (first 50 `from → to`, grouped by destination folder), so the layout is visible before the plan exists.
- Rules: the layout uses the active rules, as the CLI does, and the plan records the exact rules text it was built with (`params.rules`) and where it came from (`params.rules_ref`). Choosing and editing the rules from the form is specified in [RULES.md](RULES.md).

## 6. Bridging from the page

The Overview card that reports "no comparable hash" gets a **Bridge** button (only with approvals enabled: bridging downloads bytes, so it is a human-channel job like a rescan). A small dialog takes a byte budget and a file limit (defaults 5 GB and 500), shows how many candidates there are, starts the job, shows progress (bridged, failed) and refreshes the coverage when done. Bridging never writes a cloud; it reads files and writes the catalog.

## 7. Cross-links

- Overview "Redundant" total → New dedupe (all clouds, each cloud).
- Overview cloud card "only here" → New consolidate with that cloud as a source and the first writable other cloud as the target.
- Overview "no comparable hash" → Bridge.
- Explore results with several copies on one cloud → New dedupe scoped to that cloud and the result's folder.
- The rail item shows a count badge of plans waiting for review; a plan Claude proposes while the page is open raises the "proposed by Claude" toast with a "Review" button (the page polls the plan list every 10 s while it is visible).

## 8. Known limitations (v1)

- Rules for the consolidation layout are edited in the Rules view ([RULES.md](RULES.md)), not in the consolidate form itself.
- Estimates rebuild the plan on each change of the form (debounced); on a catalog of a million files a build can take seconds, and the estimate says it is working.
- A plan's ops cannot be edited; a different plan is a new plan (as everywhere in drive-merge).

---

# Implementation contract

Everything below is binding. Read first: [WORKSPACE.md](WORKSPACE.md) §D, §F, §G, [EXPLORE.md](EXPLORE.md) §A, §E, §F, `builders.py` (`build_dedupe`, `build_consolidate`, `choose_keeper`, `BuildResult`, `BuildError`), `service.py` (`propose_plan`, `simulate`, `plan`, `plans`, `cancel`, `bridge`), `plans.py`, `simulate.py` (the report and its codes), `api.py` (`build_router`, `scrub_secrets`), `ui_api.py` (sessions, the human channel, jobs, rescan) and every file in `src/drivemerge/web/`.

## A. Builder changes (`builders.py`)

- `build_dedupe(..., keep: Mapping[str, int] | None = None, skip_groups: Sequence[str] = ())`: `keep` maps a group key (`g:<content_group>`) to the entry id that must be kept in that group's pool; `skip_groups` lists group keys that produce no ops. A `keep` entry whose id is not a usable copy of that group's pool (see §4) raises `BuildError` with code `BAD_KEEPER` and params `{group, entry_id}`. Unknown group keys raise `BAD_GROUP` `{group}`. Without these arguments the output is byte-for-byte what it is today (existing tests unchanged).
- `BuildError` gains `code` and `params` (default code `BUILD_ERROR`, params `{}`); give every existing `raise BuildError` a specific code and the params a page needs: at least `READ_ONLY` `{remote}` (target or remote has no write remote), `UNKNOWN_REMOTE` `{remote}`, `BAD_LOCATION` `{location}`, plus the two above. English messages stay as they are.
- `BuildResult.stats` for dedupe gains `copies_considered`; for consolidate it must report (add what is missing): `already_on_target`, `to_copy`, `bytes_to_copy`, `bytes_cross_remote`, `moves_on_target`, `deletes`, `skipped_by_reason` (`{reason_code: count}`). `skipped` entries gain a `code` (`READ_ONLY`, `AMBIGUOUS_PATH`, `NATIVE_DOC`, `PROBABLE_ONLY`, `QUARANTINE`, `LAYOUT_TAKEN`, …: one per distinct reason the builders write) next to their English `reason`.

## B. Service (`service.py`)

- `Service.estimate_plan(kind, params, *, sample: int = 50) -> dict`: the same parameter handling as `propose_plan` (reserved params refused, `rules` popped, quarantine dir injected), builds, persists nothing. Returns `{"kind", "ops": n, "stats", "skipped": [first 200], "sample": [first `sample` ops as dicts {op, src: {remote, path}, dst?: {remote, path}, witness?: {remote, path}, size, reason}], "nothing_to_do": bool}`. For `dedupe` it also returns `"groups": [...]`, paged by `group_offset` / `group_limit` (default 0 / 50) params that are not passed to the builder: each `{"key", "size", "copies": [{"entry_id", "remote", "path", "mtime", "keep": bool, "remove": bool, "usable": bool, "why_not": code | null}], "reclaim": bytes, "skipped": bool}` in the builder's order of groups. A test asserts that `conn.total_changes` does not move.
- `propose_plan` result gains `"code": "NOTHING_TO_DO"` when there are no ops (the English `message` stays).
- `Service.plan_summaries(status=None, limit=100)`: rows for the list: `{"id", "kind", "title", "status", "created_by", "created_at", "simulated_at", "approved_at", "finished_at", "ops", "ok": bool | None, "problems": n, "params": {scrubbed}, "draft_id": int | None, "outdated": bool}`. `outdated` = the snapshot of the last simulation differs from the remotes' latest complete scans (the catalog changed since; re-simulate before approving).

## C. HTTP (`api.py`, `ui_api.py`)

| Method & path | Auth | Body / query | Response |
|---|---|---|---|
| `POST /v1/plans/estimate` | normal | `{"kind", "params", "sample"?}` | §B estimate (scrubbed) |
| `GET /v1/plans/summaries` | normal | `status`, `limit` | `{"plans": [...]}` |
| `POST /v1/plans` | normal | as today | as today; `created_by` is `"ui"` for a browser session, `"api"` for the bearer token |
| `POST /v1/ui/plans/{id}/review` | normal (session or bearer) | | re-simulates the plan and returns `service.plan(id)` + `"simulation"` (the report) scrubbed of digest and snapshot, plus `"approval_code"` **only** for a browser session of a server with approvals enabled, when the report is ok and the plan is not a stale workspace preview. Refuses plans that are not `draft`/`simulated`/`approved` (409 `PLAN_CLOSED` `{plan, plan_status}`) |
| `POST /v1/ui/plans/{id}/cancel` | normal | | `{"ok"}`; 409 `PLAN_CLOSED` when it ran or is running |
| `POST /v1/ui/bridge` | human channel | `{"max_bytes"?, "limit"?}` | 202 `{"job_id"}`; 409 `JOB_BUSY` like rescan; the job reports progress `{bridged, failed, total}` and its result is `Service.bridge`'s |
| `GET /v1/ui/bridge/candidates` | normal | `max_bytes`, `limit` | `{"count", "bytes"}` |

- `BuildError` is answered in both apps as 400 `{"error", "code", "params"}` (coded), so the page words it.
- `approval_code` stays out of every other response, at any depth, for every caller (extend the existing scrub tests to the new routes).
- Approving a non-workspace plan already goes through `/v1/ui/plans/{id}/approve`; keep its checks. The execute route already handles any plan; a finished non-workspace plan does not touch drafts (unchanged).

## D. MCP

No new tools: Claude already proposes plans (`propose_plan`). Add `created_by="mcp"` to anything it creates (it does today) so the page shows the badge.

## E. Frontend (`src/drivemerge/web/`)

Same rules as WORKSPACE.md §F and EXPLORE.md §F (no build step, offline, CSP, both languages, codes worded on the page, `data-testid`).

- `plans.js`: list, review, new-dedupe and new-consolidate views; routes `#/plans`, `#/plans/<id>`, `#/plans/new/dedupe?<params>`, `#/plans/new/consolidate?<params>` (form state lives in the URL so Back and reload keep it).
- Extract from `preview.js` the report rendering (cards, before/after per cloud, problems with their wording and reveal links) and the run/progress panel into `report.js`, used by both the workspace preview and the plan review. The workspace preview must look and behave exactly as before (its browser tests stay unchanged and green).
- `shell.js`: the fifth rail item with the waiting-count badge. `Alt+1…4` keep their meaning (Overview, Explore, Compare, Workspace); Plans is `Alt+5`.
- Estimates: debounce 400 ms, show "estimating…", cancel stale responses; the dedupe group list pages with "load more".
- Overrides: clicking a non-keeper usable copy sets `keep[group] = entry_id`; a non-usable copy shows why (`why_not` worded). The form shows how many overrides and skips it carries, with "reset".
- Approval: the same typed-code dialog as the workspace; with approvals disabled, a read-only banner shows `drivemerge plan approve <id>` / `drivemerge plan execute <id>`.
- Poll `GET /v1/plans/summaries` every 10 s while the page is visible (not when hidden); a new plan with `created_by == "mcp"` raises the Claude toast with "Review".
- Overview additions (§6, §7): Bridge button and dialog, links into the flows.
- New i18n namespaces: `plans.*`, `dedupe.*`, `cons.*`, `bridge.*`, `skip.<CODE>`, `build.<CODE>` (BuildError codes), `err.PLAN_CLOSED`, `plans.NOTHING_TO_DO`. Plan kinds `kind.<kind>`, statuses `pstatus.<status>`.

## F. Tests

- Builders: `keep` and `skip_groups` (including every `BAD_KEEPER` case: other cloud's copy without across-clouds, quarantined, native doc, ambiguous path, not in the group), `BuildError` codes at every raise site, the new stats and skip codes, unchanged output without the new arguments.
- Service: `estimate_plan` persists nothing (`total_changes` unchanged; no rows in `plans`), group paging, `plan_summaries` (`ok`, `problems`, `outdated` after a new scan), `NOTHING_TO_DO`.
- HTTP: every new route through both apps where mounted, auth (bearer vs session vs human channel), `approval_code` only for an approving session and never for bearer, `created_by` `ui` vs `api`, `PLAN_CLOSED`, coded `BuildError`, bridge job busy/progress with a fake rclone.
- i18n contract: every `BuildError` code, skip code and new error code has its sentence in both languages using only the params the server sends.
- Browser (mocked API): plans list grouping and badge, review rendering (shared report component), approve flow with typed code, run progress, cancel, read-only banner, dedupe form ↔ URL, estimate debounce and stale response, keeper override and skip reflected in the create request body, `why_not` wording, consolidate form (only writable targets, cleanup section warning when coverage says contents are not yet on the target), Claude toast from polling, bridge dialog, Overview links, both languages. The existing workspace preview tests must pass unchanged.
- The whole suite stays green: `pytest` (with the browser tests).
