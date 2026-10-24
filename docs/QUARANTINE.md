# Quarantine in the Browser

**English** | [繁體中文](QUARANTINE.zh-TW.md)

> This document adds a **Quarantine** view to the visual workspace ([WORKSPACE.md](WORKSPACE.md), [EXPLORE.md](EXPLORE.md), [PLANS.md](PLANS.md), [ONBOARDING.md](ONBOARDING.md)): what every executed plan parked in the quarantine folders, on which cloud, how much space it holds and since when; restoring it (a whole plan, a folder or single files); and purging it for good once it is no longer needed. The first part is the design; the second part (the implementation contract) is binding for implementers.

## 1. The problem

drive-merge never deletes: a delete moves the file into `.drive-merge-quarantine/<plan id>/<original path>` on the same cloud ([DESIGN.md](DESIGN.md) §7.3), and that is what makes bold reorganising safe. The page can now run every kind of plan that quarantines files: dedupe, consolidation with source cleanup, cross-cloud moves from the workspace, and the free-space `release` plan. The two steps that close the loop are still missing from it:

- **Restoring is terminal-only.** `drivemerge plan restore <id>` has no route; every failure banner, the quarantine folder's tooltip in the tree and the `QUARANTINE` refusal tell the person to open a terminal. A person who regrets a dedupe from the page has to leave it.
- **Purging is offered only after a `release` plan**, and only for that plan, right after it ran. The quarantine of a dedupe, a consolidation or a workspace move can be purged only from the terminal, and nothing in the page says that space is held, where or since when.
- **Nothing lists the quarantine.** The tree shows the folder locked; there is no answer to "what is in there, why, and is it safe to empty?"
- **A restore does not mark the catalog as behind.** The "catalog is behind" banner (WORKSPACE.md §5) is driven by executed ops; a restore marks its ops `skipped` and is not counted, so after a restore the trees silently show the files as still quarantined.

Questions this view answers:

1. **What is in quarantine?** Per cloud: files, bytes, how many are past the retention period; per plan: who proposed it, when it ran, what it removed and why (the op's reason and its witness, the copy that vouched for it).
2. **Can I have it back?** Put a file, a folder or a whole plan's removals back where they were, never overwriting anything.
3. **Can I free that space now?** Purge what is past retention (or everything, on purpose), with every witness proven again first.

## 2. Principles

- **Only what a plan parked.** The view acts on the quarantined ops of executed plans and nothing else: every item is a `plan_ops` row whose source the executor moved into `<quarantine_dir>/<plan id>/`. There is no free-form path, no "restore this folder of the cloud", and files in the quarantine folder that no plan of this catalog put there are listed read-only as *unknown* (§6).
- **No new way to change a cloud.** Restore and purge are the two closing steps that DESIGN.md §7.3 already defines for an approved plan, run by the same executor functions as `drivemerge plan restore` and `drivemerge plan purge`, behind the same gates: the human channel (the browser session opened by `drivemerge ui` with approvals enabled), `[safety] execution_enabled`, and a typed confirmation word. A restore only moves files back from the plan's own quarantine folder to the exact path they came from, on the same cloud, and refuses an occupied path; a purge only deletes below the plan's quarantine folder and only after re-proving, live, that the witness still exists and is identical. Neither can create new content, overwrite, or reach outside the quarantine.
- **Check before acting.** Every action first shows a read-only check against the catalog (how many originals are occupied now, how many quarantined copies the last scan did not see, how many witnesses are gone or unproven, what is too recent to purge), so the person knows what will be kept before typing. The executor checks everything live again; the catalog check is advice, never a substitute.
- **Retention is visible, never automatic.** Every item shows when it was quarantined and when it is due (`quarantined_at` + `[safety] quarantine_retention_days`, default 30). Nothing is ever purged on a timer.
- **The page words everything.** The server sends codes and params; the page writes the sentences in both languages.
- **Read-only fallback.** Without approvals the view is the same listing, and each action is replaced by the exact terminal command for it.

## 3. Information architecture

A seventh rail item, **Quarantine** (`#/quarantine`, `Alt+6`), after Plans. Its badge shows the number of plans with items past retention.

```
┌ rail ┐┌──────────────────────────────────────────────────────────────────────────────┐
│  ☁   ││ Quarantine                                    retention 30 days · synced 3 min ago│
│  ◎   ││ ┌ gdrive ─────────────┐ ┌ dropbox ──────────────┐ ┌ onedrive ──────────────┐   │
│  ⌕   ││ │ 1,204 files 4.1 GB  │ │ 18,204 files 54.7 GB  │ │ nothing in quarantine  │   │
│  ⇆   ││ │ due: 0              │ │ due: 18,204 · 54.7 GB │ │                        │   │
│  ☰   ││ │ quota 212 GB / 2 TB │ │ 96 GB / 2 TB → 41.3 GB│ │                        │   │
│  ⛒   ││ └─────────────────────┘ └───────────────────────┘ └────────────────────────┘   │
│  ▦   ││                                        [ Purge everything past retention… ]   │
│      ││ ┌ plan ──────────────────── cloud ──── held ─────── quarantined ── due ──────┐ │
│      ││ │ #21 release dropbox       dropbox  18,204 · 54.7 GB  2026-09-20  due now  ›│ │
│      ││ │ #17 dedupe (each cloud) ✦ gdrive    1,204 · 4.1 GB   2026-10-20  in 26 d  ›│ │
│      ││ └────────────────────────────────────────────────────────────────────────────┘ │
│      ││ ▸ Settled (restored or purged): 3 plans                                        │
│      ││ ▸ Unknown items: 12 files on gdrive (no plan of this catalog put them there)   │
└──────┘└──────────────────────────────────────────────────────────────────────────────┘
```

- **Cloud cards**: files and bytes held, the part past retention, the cloud's quota (used / total and "after purge", when the cloud is a connection whose quota is known), a link that filters the plan list to that cloud (`#/quarantine?remote=dropbox`).
- **Plan list**: one row per plan that still holds files, oldest first: id, kind and the one-line summary the Plans list uses, the "proposed by Claude" badge, clouds, held files and bytes, when it ran, when it is due. Plans with nothing left (every item restored or purged) collapse into **Settled**. A row opens the plan's quarantine (§4).
- **Purge everything past retention…** purges, one plan after another in one job, every item whose due date has passed (optionally only on the filtered cloud). It opens the purge check first (§5).

## 4. A plan's quarantine (`#/quarantine/<id>`)

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│ Quarantine of plan #17 · dedupe (each cloud) · ran 2026-10-20 · Open plan review   │
│ [ held ▾ ]  cloud [ all ▾ ]  folder [ Photos/2019        ]      1,204 files 4.1 GB │
├──────────────────────────────────────────────┬───────────────────────────────────┤
│ ☐ ▾ Photos/2019/                 312 · 1.2 GB│ IMG_0412 (1).jpg                  │
│ ☑     IMG_0412 (1).jpg   4.1 MB   due in 26 d│ was  gdrive:Photos/2019/IMG_0412 (1).jpg│
│ ☐     IMG_0413 (1).jpg   3.9 MB   original ⚠ │ now  gdrive:.drive-merge-quarantine/17/…│
│ ☐ ▸ Backup/                      892 · 2.9 GB│ why  duplicate of the kept copy   │
│                                              │ kept copy gdrive:Photos/2019/IMG_0412.jpg ✓│
│                                              │ quarantined 2026-10-20 · due 2026-11-19│
├──────────────────────────────────────────────┴───────────────────────────────────┤
│ 1 selected · 4.1 MB          [ Restore selected… ]  [ Restore all… ]  [ Purge… ▾ ] │
└──────────────────────────────────────────────────────────────────────────────────┘
```

- Items are grouped into folders by their **original** path (that is where they go back to). Each row: name, size, state, due date, and a warning mark when the catalog check finds a problem (original path occupied, quarantined copy not seen by the last scan, witness gone). Folders show counts and bytes and a tri-state checkbox.
- **State filter**: `held` (default), `restored`, `purged`, `all`. Restored and purged items are history: no checkbox, a muted row with when it happened.
- **Detail panel**: original path, quarantine path, the op's reason, the witness (the copy that vouched for it: a delete's witness, or the destination of a move that met identical content) with its catalog status (live and identical / live, identity not proven / not in the catalog), quarantined and due dates.
- **Actions**: Restore selected, Restore all, Purge (menu: selected · past retention · everything now). Each opens its check dialog (§5).
- Cross-links: "Open plan review" (`#/plans/<id>`), Reveal the original folder in the workspace, Reveal the witness.

## 5. Flows

**Restore**

1. Selection (or "all") → `POST /v1/quarantine/plans/<id>/check` with `action: "restore"`.
2. The dialog shows: files and bytes to restore; how many originals are **occupied** in the catalog now (those will stay in quarantine: drive-merge never overwrites); how many quarantined copies the last scan **did not see** (they may be gone already); and, for cross-cloud moves and consolidations, a note that the copy on the other cloud stays, so the content will exist twice afterwards (the dedupe flow can thin it later).
3. Typing `restore` enables the button → `POST /v1/ui/plans/<id>/restore` → a job with progress (`restored`, `failed`, `total`).
4. The result lists each item that stayed, worded from its code (`ORIGINAL_TAKEN`, `QUARANTINE_GONE`, …). The "catalog is behind" banner appears for the clouds touched, with the rescan button, as after any run.

**Purge**

1. Selection, "past retention" or "everything now" → the check with `action: "purge"` and `older_than_days` (retention for "past retention", `0` for the others).
2. The dialog shows: files and bytes that will be deleted for good; how many are **too recent** and stay (only with "past retention"); how many witnesses the catalog says are **gone** (those items will be kept: the quarantined copy may be the last one); how many witnesses are present but **not proven identical** by the catalog (the executor proves them live, which can download up to the shown bytes); and the quota after the purge when known. A warning tone when anything purged was quarantined less than 7 days ago.
3. Typing `purge` → `POST /v1/ui/plans/<id>/purge` (or `POST /v1/ui/quarantine/purge` from the overview) → job → result with the items kept and why.

**Read-only fallback** (approvals disabled): the checks still run (they are read-only), and the dialog ends with the command instead of the button: `drivemerge plan restore <id>` (with `--op <op id>` per selected item, up to 20; beyond that the plan-wide command with a note), `drivemerge plan purge <id> --older-than-days <n>`, or `drivemerge quarantine purge-due` for the overview action. With approvals enabled but execution disabled, the buttons are disabled and the `[safety] execution_enabled = true` note shows, as in the plan review.

## 6. Unknown items

Live catalog files below the quarantine folder that no held op of this catalog accounts for: a plan id this catalog does not know, a path the plan never quarantined, a file someone moved there by hand, a copy restored and then put back. They are listed per cloud (count, bytes, three examples) and nothing more: there is no record of where they came from or what vouches for them, so drive-merge offers neither restore nor purge, and the simulator keeps refusing any plan that touches them (`QUARANTINE`). The page says that the cloud's own app can move them.

## 7. Cross-links

- Rail badge: plans with items past retention.
- Plan review of a `done` or `failed` plan that still holds items: a box "*N files (X) of this plan are in quarantine*" with **Restore…** and **Open in Quarantine**. The release plan keeps its existing purge box and gains the same link.
- Failure help after a run (`prog.failedHelp`), the tree's quarantine folder tooltip, and `err.QUARANTINE`: with approvals enabled they point to the view (`#/quarantine/<id>` when the plan is known, else `#/quarantine?remote=<cloud>`) instead of the terminal; the terminal wording stays for the read-only page.
- Tree: the quarantine root row gets "Open quarantine"; a `<quarantine_dir>/<id>` folder row opens that plan.
- Overview cloud cards and the Free space view: "X in quarantine on this cloud (N due)" linking to `#/quarantine?remote=<cloud>`; the Free view's "a plan has finished" note links to the plan's quarantine.

## 8. Known limitations (v1)

- A restore never overwrites: an item whose original path is occupied stays in quarantine. Restoring to another name or folder is not offered.
- Restore undoes the quarantine only. Other effects of the plan stay: copies it made on another cloud, files it moved within a cloud. Undoing a whole plan would be a new, reverse plan (simulated and approved), which is future work.
- The catalog checks are as old as the last scan of each cloud; the executor decides live.
- Purge proves each witness live; a witness on a cloud with no strong hash (OneDrive, iCloud) can mean a download per item, within no budget other than the item count shown.
- One job at a time (`JOB_BUSY`), as for every job of the page: purging several plans is one overview job that walks them in turn, not parallel jobs.
- Unknown items (§6) can only be listed.

---

# Implementation contract

Everything below is binding. Read first: [DESIGN.md](DESIGN.md) §7.3, [WORKSPACE.md](WORKSPACE.md) §D, §F, [PLANS.md](PLANS.md) §B, §C, §E, [ONBOARDING.md](ONBOARDING.md) §D, `executor.py` (`restore_plan`, `purge_quarantine`, `_quarantined_ops`, `quarantine_path`, `_inside_quarantine`, `_Run.restore`, `_Run.purge`), `plans.py`, `db.py` (`outside_quarantine`, `quarantine_dir`), `service.py`, `api.py` (`build_router`, `QuotaCache`), `ui_api.py` (`_Jobs`, `_outdated_remotes`, the purge route, `require_human`), `mcp_server.py`, `cli.py` (`cmd_plan_restore`, `cmd_plan_purge`), and every file in `src/drivemerge/web/`.

## A. Definitions

- **Quarantined op** = an op returned by `executor._quarantined_ops` for a plan of status `done` or `failed`: a `done` delete, or a `done` move with a `quarantine` journal row (it met identical content). Its **item** is the file at `quarantine_path(config, plan_id, op.src_path)` on `op.src_remote_id`.
- **State** of an item: `restored` when the op's status is `skipped` and a `restore` journal row for the op has `detail.status == "restored"`; `purged` when a `purge` journal row for the op has `detail.status == "purged"`; `held` otherwise. `restored_at` / `purged_at` are those journal rows' `ts`.
- **Witness** = `(witness_remote_id, witness_path)` for a delete, `(dst_remote_id, dst_path)` for a move.
- `quarantined_at` = the op's `executed_at`; `due_at` = `quarantined_at + retention_days` days; **due** = `due_at <= now`.
- Path matching against the catalog: NFC, casefolded when the remote is case-insensitive (as `compare.py` does).
- `retention_days` = `config.safety.quarantine_retention_days` (new, §D).

## B. `src/drivemerge/quarantine.py` (read-only)

```python
class QuarantineError(ValueError): code: str; params: dict
CODES = ("NOT_FOUND", "NOTHING_QUARANTINED", "BAD_OPS", "BAD_QUERY")

def quarantine_report(conn, *, quarantine_dir: str, retention_days: int = 30, now: datetime | None = None) -> dict
def plan_items(conn, plan_id: int, *, quarantine_dir: str, retention_days: int = 30, state: str = "held",
               remote: str | None = None, path_prefix: str | None = None, offset: int = 0, limit: int = 200,
               now: datetime | None = None) -> dict
def check(conn, plan_id: int, action: str, *, quarantine_dir: str, op_ids: Sequence[int] | None = None,
          older_than_days: int = 0, retention_days: int = 30, now: datetime | None = None) -> dict
```

No function of this module writes the catalog (a test asserts `conn.total_changes` does not move).

`quarantine_report` result:
```json
{
  "quarantine_dir": ".drive-merge-quarantine", "retention_days": 30, "now": "2026-10-24T09:00:00Z",
  "totals":  {"files": 0, "bytes": 0, "due_files": 0, "due_bytes": 0},
  "remotes": [{"remote": "dropbox", "files": 0, "bytes": 0, "due_files": 0, "due_bytes": 0, "plans": 0,
               "last_scan": {"id": 1, "finished_at": "…"} | null}],
  "plans":   [{"plan_id": 21, "title": "…", "kind": "release", "status": "done", "created_by": "ui",
               "params": {"…": "scrubbed"}, "finished_at": "…", "remotes": ["dropbox"],
               "held": {"files": 0, "bytes": 0}, "due": {"files": 0, "bytes": 0},
               "restored": 0, "purged": 0, "oldest_at": "…", "next_due_at": "…" | null}],
  "unknown": [{"remote": "gdrive", "files": 12, "bytes": 0, "examples": ["…", "…", "…"]}]
}
```
- `remotes` lists every catalog remote in configuration order (zeros included). `plans` lists every plan with at least one quarantined op, ordered by `oldest_at` of held items, then plans with nothing held (`held.files == 0`) by `finished_at` desc. Bytes are `expect_size` (0 when null).
- `unknown`: live files (`present = 1 AND is_dir = 0`) at or below `<quarantine_dir>/` on each remote whose path is not the item path of any **held** item; omitted remotes have none.
- SQL aggregates over `plan_ops` joined with the journal (one pass for states), not a Python loop per op over the database.

`plan_items` result:
```json
{"plan": {"id": 17, "title": "…", "kind": "dedupe", "status": "done", "created_by": "mcp", "finished_at": "…"},
 "total": 1204, "bytes": 0, "offset": 0, "limit": 200,
 "items": [{"op_id": 901, "seq": 12, "op": "delete", "remote": "gdrive", "path": "Photos/2019/IMG_0412 (1).jpg",
            "quarantine_path": ".drive-merge-quarantine/17/Photos/2019/IMG_0412 (1).jpg", "size": 4300000,
            "reason": "…", "witness": {"remote": "gdrive", "path": "Photos/2019/IMG_0412.jpg"},
            "state": "held", "quarantined_at": "…", "due_at": "…", "due": false,
            "restored_at": null, "purged_at": null,
            "catalog": {"seen": true, "original_taken": false, "witness_live": true, "witness_same": true}}]}
```
- `state` ∈ `held|restored|purged|all` (else `BAD_QUERY {param: "state"}`); `remote` and `path_prefix` filter on the item's remote and **original** path (segment-exact prefix); `limit` ≤ 500; items sorted by remote order then natural, case-insensitive original path.
- `catalog`: `seen` = a live catalog file exists at the quarantine path; `original_taken` = a live catalog file exists at the original path; `witness_live` = a live file at the witness path, `null` when the op names none; `witness_same` = it shares a non-null `content_group` with the quarantined file (or, when the quarantined file is not in the catalog, with the op's `expect_hash` by equal strong hash), `null` when unknown. Computed with one batched query per page per field, not one per item.

`check(action="restore")` returns `{"action", "plan_id", "files", "bytes", "original_taken": {"files", "bytes"}, "not_seen": {"files", "bytes"}, "cross_remote_copies": n, "remotes": [names], "op_ids": [the resolved held op ids]}`. `cross_remote_copies` = items whose plan also copied the same content to another cloud (the content will exist twice after the restore).

`check(action="purge")` returns `{"action", "plan_id", "files", "bytes", "too_recent": {"files", "bytes"}, "witness_gone": {"files", "bytes"}, "witness_unproven": {"files", "bytes", "download_bytes"}, "recent": {"files", "bytes"} (attempted items quarantined less than 7 days ago), "by_remote": [{"remote", "files", "bytes"}], "op_ids": [...]}`; `files`/`bytes` are those that would be attempted (older than the cutoff), `download_bytes` estimates what the live re-proof may download (sum of the quarantined sizes whose witness has no strong hash in common).

Errors: unknown plan `NOT_FOUND {plan}`; a plan with no held item `NOTHING_QUARANTINED {plan}`; `op_ids` naming anything that is not a held item of this plan `BAD_OPS {plan, op_ids: [first 20 offending]}`; an `action` other than `restore`/`purge` or a negative `older_than_days` `BAD_QUERY {param}`. `op_ids = None` means every held item.

## C. Executor (`executor.py`)

- `restore_plan(conn, rclone, config, plan_id, *, op_ids=None, progress=None)` and `purge_quarantine(..., older_than_days=30, op_ids=None, progress=None, tmpdir=None)`: `op_ids` restricts the run to those quarantined ops (the caller has validated them; an unknown id is ignored here, never acted on); `progress(done, failed, total)` is called after each item. Without the new arguments behaviour and output are unchanged (existing tests unchanged).
- `_OpFailed` gains `code` (default `OP_FAILED`) and `params`. The restore and purge raise sites set: `ORIGINAL_TAKEN {remote, path}`, `QUARANTINE_GONE {remote, path}`, `READ_ONLY {remote}`, `BAD_PATH {path}`, `VERIFY_FAILED {remote, path}` (missing or different after the move), `NO_WITNESS_RECORD {remote, path}`, `WITNESS_UNPROVEN {remote, path, witness}` (the live re-check found it gone or not identical). English messages stay as they are.
- `ExecResult` gains `failures: list[dict]`, at most 200 entries `{op_id, seq, remote, path, code, params, error}`, filled by restore and purge only.
- A plan that is `executing` keeps raising `ExecutionError`; give it `code = "PLAN_EXECUTING"` and `params = {plan}`.

## D. Config, service, CLI, MCP

- `SafetyConfig.quarantine_retention_days: int = 30` from `[safety] quarantine_retention_days` (≥ 1; else a config error naming the key). `drivemerge plan purge --older-than-days` defaults to it.
- `Service.quarantine_report()`, `Service.quarantine_items(plan_id, **kw)`, `Service.quarantine_check(plan_id, action, **kw)` (passing `quarantine_dir` and `retention_days` from the config); `Service.restore(plan_id, *, op_ids=None, progress=None)` and `Service.purge(plan_id, *, older_than_days=30, op_ids=None, progress=None)`; `Service.purge_due(remote=None, progress=None) -> dict` walks the plans of the report that have due items (on `remote` when given) and purges each with `older_than_days = retention_days`, returning `{"plans": [{plan_id, status, done, failed, skipped, failures}], "done", "failed", "skipped"}`; it continues after a plan that fails.
- CLI: `drivemerge quarantine` prints the report (table per cloud and per plan; `--json`); `drivemerge quarantine show <plan id> [--state …] [--remote …] [--prefix …]`; `drivemerge quarantine purge-due [--remote …]` (TTY gate with the word `purge`, as `plan purge`); `plan restore` and `plan purge` gain `--op ID` (repeatable). `.claude/settings.json` adds `Bash(drivemerge quarantine purge-due:*)` and `Bash(*drivemerge quarantine purge-due*)` to `deny`.
- MCP: read-only tools `quarantine_report()` and `quarantine_items(plan_id, state="held", remote=None, path_prefix=None, limit=100)` (English, same scrubbing as `get_plan`). No restore or purge tool.

## E. HTTP

| Method & path | Auth | Body / query | Response |
|---|---|---|---|
| `GET /v1/quarantine` | normal (shared router: both apps) | | §B report; each `remotes[]` row gains `"quota": QuotaCache.get(...)` (used / total / free, or `null`) |
| `GET /v1/quarantine/plans/{id}` | normal | `state`, `remote`, `path`, `offset`, `limit` | §B `plan_items` |
| `POST /v1/quarantine/plans/{id}/check` | normal | `{"action": "restore"\|"purge", "op_ids"?: [int] (≤ 5000), "older_than_days"?: int}` | §B `check` |
| `POST /v1/ui/plans/{id}/restore` | human channel | `{"confirm": "restore", "op_ids"?: [int]}` | 202 `{"job_id"}`; job kind `restore`, progress `{restored, failed, total}`, result `ExecResult` with `failures`; the job fails (status `failed`, `error` set) when any item failed |
| `POST /v1/ui/plans/{id}/purge` | human channel | as today plus `"op_ids"?: [int]` | as today; progress `{purged, failed, total}`; result gains `failures` |
| `POST /v1/ui/quarantine/purge` | human channel | `{"confirm": "purge", "remote"?: name}` | 202 `{"job_id"}`; job kind `purge`, progress `{plans_total, plans_done, purged, failed}`, result `Service.purge_due` |

- Before starting a restore or purge job the route validates with `quarantine.check` (so `NOTHING_QUARANTINED`, `BAD_OPS`, `NOT_FOUND` are answered synchronously, not as a failed job). Order of refusals: `UNAUTHENTICATED` / `HUMAN_ONLY` / `APPROVALS_DISABLED` / `BAD_CSRF` / `BAD_ORIGIN` (the human channel, unchanged), `BAD_REQUEST` (confirmation word wrong: 400, as purge today), `EXECUTION_DISABLED` (403), `NOT_FOUND` (404), `CONFLICT` for a plan that has not run (409, as purge today, params `{plan, plan_status}`), `PLAN_EXECUTING` (409), `NOTHING_QUARANTINED` (409), `BAD_OPS` (400), `JOB_BUSY` (409).
- The purge route calls `service.purge(plan_id, older_than_days=…)` exactly as today when `op_ids` is absent, so the existing purge tests stay unchanged.
- `QuarantineError` is answered in both apps as `{"error", "code", "params"}` with 404 for `NOT_FOUND`, 400 for `BAD_OPS` / `BAD_QUERY`, else 409.
- `_outdated_remotes` also counts, for the op's source remote, every `restore` journal row with `detail.status == "restored"` and every `purge` row with `detail.status == "purged"` newer than the remote's last complete scan, so the "catalog is behind" banner shows after a restore or purge from the page or the terminal.
- `approval_code`, digests and snapshots appear in none of these responses (extend the scrub tests).

## F. Frontend (`src/drivemerge/web/`)

Same rules as WORKSPACE.md §F, EXPLORE.md §F and PLANS.md §E (no build step, offline, CSP, both languages, codes worded on the page, `data-testid` on what tests touch).

- `quarantine.js` (new): the overview (`#/quarantine?remote=<name>`) and the plan view (`#/quarantine/<id>?state=&remote=&path=`; filter state lives in the URL), the check-and-confirm dialogs, the job progress (reuse `ProgressBox`-style rendering from `report.js` without changing it), the read-only command box (reuse `CliBox` patterns), and an exported `PlanQuarantineBox({ planId })` for the plan review. Item tree built on the page from original paths; render only expanded nodes; "load more" pages of 200.
- `shell.js`: route `quarantine` (`/^\/quarantine(?:\/(\d+))?(?:\?(.*))?$/`), rail item after `plans` with icon `lock`, `VIEWS` gains `quarantine` (`Alt+6`), badge = plans with `due.files > 0`, read from `GET /v1/quarantine` at start and after any `execute`, `restore` or `purge` job ends (no timed polling).
- `api.js`: `quarantine()`, `quarantineItems(id, query)`, `quarantineCheck(id, body)`, `restore(id, body)`, `purgeDue(body)`; `purge` gains `op_ids` passthrough.
- `plans.js`: render `PlanQuarantineBox` on the review of a `done`/`failed` plan whose kind is not `release`; add the "Open in Quarantine" link to `PurgeBox` (its existing behaviour and test ids unchanged).
- `report.js`, `tree.js`, `overview.js`, `free.js`: only the links of §7 (with approvals enabled the new keys `prog.failedHelpPage`, `quarantine.tipPage`, `quarantine.insidePage`, `err.QUARANTINE_PAGE`; the existing terminal sentences stay for the read-only page). Keep these edits small.
- `styles.css`: the view's styles; `icons.js` only if `lock` needs a variant.
- `data-testid`s: `view-quarantine`, `qr-cloud-<remote>`, `qr-plan-row-<id>`, `qr-settled`, `qr-unknown`, `qr-purge-due`, `view-quarantine-plan`, `qr-filter-state`, `qr-filter-remote`, `qr-filter-path`, `qr-item-<op_id>`, `qr-folder-<path>`, `qr-check-<op_id>`, `qr-detail`, `qr-restore-selected`, `qr-restore-all`, `qr-purge-menu`, `qr-purge-selected`, `qr-purge-due-plan`, `qr-purge-now`, `qr-dialog`, `qr-dialog-check`, `qr-confirm-input`, `qr-confirm`, `qr-progress`, `qr-result`, `qr-failure-<op_id>`, `qr-cli`, `qr-exec-disabled`, `plan-quarantine-box`, `rail-quarantine`, `rail-quarantine-badge`.
- i18n namespaces (both languages): `nav.quarantine`, `nav.quarantineTip`, `qr.*`, `qr.state.<held|restored|purged|all>`, `qr.fail.<CODE>` for every `_OpFailed` code of §C, `err.NOTHING_QUARANTINED`, `err.BAD_OPS`, `err.PLAN_EXECUTING`, plus the four `*Page` keys above. Every sentence uses only the params the server sends.

## G. Tests

- `tests/test_quarantine.py` (new): states from hand-built plans, ops and journal rows (held, restored, purged, move-onto-identical, a `failed` plan with some items); retention and `due`; report ordering and totals; `unknown` (unknown plan id, unaccounted path, a restored-then-reparked file); `plan_items` filters, paging, sort, every `catalog` flag including case-insensitive and NFD paths; both `check` actions; every error code; `total_changes` unchanged.
- `tests/test_executor.py` (extend): `op_ids` for restore and purge, `progress` calls, every new `_OpFailed` code at its raise site, `failures` in `ExecResult`, unchanged output without the new arguments.
- `tests/test_quarantine_api.py` (new): every route through both apps where mounted; auth (bearer vs session vs human channel), the refusal order of §E, the jobs (progress, failures fail the job, `JOB_BUSY`), `purge_due` across plans with one failing, quota passthrough, `_outdated_remotes` after a restore and after a purge, scrubbing.
- `tests/test_cli.py`, `tests/test_mcp_server.py` (extend): `quarantine`, `quarantine show`, `quarantine purge-due` TTY gate, `--op`; the two MCP tools, and that no MCP tool restores or purges.
- `tests/test_config.py` (extend): `quarantine_retention_days` default, value, invalid value.
- `tests/test_i18n_contract.py` (extend): every code in `quarantine.CODES`, every `_OpFailed` code of §C and `PLAN_EXECUTING` has its sentence in both languages using only its params.
- `tests/test_ui_quarantine.py` (new, browser, mocked API importing `MockApi`/`Ui` from `test_ui_frontend.py`): rail item, badge and `Alt+6`; cloud cards and filter; plan list with Settled and Unknown; plan view tree, selection and tri-state folders, filters in the URL and Back; detail panel flags; restore and purge check dialogs (request bodies with `op_ids`, typed word gating, progress, result wording per failure code); purge-due from the overview; the read-only command box with `--op` lines and the plan-wide fallback beyond 20; execution-disabled note; `PlanQuarantineBox` on a done dedupe review; the links from the failure help, tree and Free view; both languages. The existing browser tests stay green (update only the assertions that pin the replaced terminal sentences with approvals enabled).
- The whole suite stays green: `pytest` (with the browser tests), except `tests/test_docs_i18n.py` until `docs/QUARANTINE.zh-TW.md` exists.
