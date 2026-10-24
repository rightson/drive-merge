# Layout Rules in the Browser

**English** | [繁體中文](RULES.zh-TW.md)

> This document adds a **Rules** view to the visual workspace ([WORKSPACE.md](WORKSPACE.md), [EXPLORE.md](EXPLORE.md), [PLANS.md](PLANS.md), [QUARANTINE.md](QUARANTINE.md)): editing the layout rules that decide where consolidate and reorganize plans put files ([DESIGN.md](DESIGN.md) §6), with live validation, a live before/after preview over the real catalog, a diff against the active rules, review of the rule changes Claude proposes, and a path from the edited rules into the plan forms. The first part is the design; the second part (the implementation contract) is binding for implementers.

## 1. The problem

The layout is the most consequential input of a consolidation: it decides where every one of a million files lands. Today it can only be changed outside the page:

- DESIGN.md §6 and PLANS.md §8 say that rules are edited in the CLI or by Claude; the consolidate form says "use the terminal or ask Claude" (`cons.rulesHelp`).
- `POST /v1/rules/validate` and `POST /v1/layout/preview` exist, but the page never calls them. The CLI has `drivemerge rules check FILE` and `drivemerge rules preview`; changing the active rules means editing the file named by `[rules] path` in an editor, which the person must first create and register in `config.toml`.
- Validation answers with one English sentence (`RuleError`), the first error only, and no line number.
- `layout/preview` walks the whole catalog on every call (seconds on a large catalog) and shows counts per rule and a sample, but not what *changes* compared with the rules in force, nor which files fall through to the fallback.
- A rule change has no history. A plan records its rules only when they were passed inline; a plan built with the configured file records nothing, so "which rules made this plan?" has no answer once the file changes.
- Claude can pass any rules inline to `propose_plan`. The plan is still simulated and approved, but nothing tells the person that it was built with rules other than their own.

Questions this view answers:

1. **Where would my files go under these rules?** Per rule: how many files and bytes, sample files before → after; which files no rule catches (they take the fallback); which units move as a whole.
2. **What changes if I change this rule?** Files whose destination differs from the active rules, which rule took them before and which takes them now.
3. **What did Claude propose, and is it in force?** A proposal is a version waiting for review, with its diff and its preview; it is never active until the person activates it.
4. **Can I go from these rules to a plan?** "Use in a consolidate plan" and "Use in a reorganize plan" open the existing forms with the edited rules applied; the plan is then estimated, created, simulated and approved exactly as today.

## 2. Principles

- **Rules never change a cloud.** Rules only shape the ops of plans built later; every such plan is still built, simulated, approved with the typed code and executed exactly as today. A plan's ops are fixed when it is created, so activating new rules never changes an existing plan.
- **Text is the source of truth.** The rule language is a small, ordered TOML file whose comments matter (the default file is half comments). The page edits that text; every structured helper (reorder, insert a rule, remove a rule) is a text transformation that keeps the comments and is verified by the parser (§4, R2).
- **Nothing becomes active by accident.** Edits are drafts; Claude's changes are proposals. Only a person, on the human channel, activates a version, after seeing its diff against the rules in force and its preview (R3). An edit of the rules file outside drive-merge is detected and shown, never adopted silently.
- **Every plan records its rules.** Consolidate and reorganize plans store the exact rules text they were built with and where it came from (active file, defaults, a version, inline), so a plan stays reproducible and the review shows when a plan was built with rules that are not the active ones.
- **Fast enough to type in.** Validation runs on every pause in typing and never reads the catalog. The preview is sampled, cached, cancellable and runs outside the catalog lock (§7).
- **The page words everything.** Validation errors, warnings and refusals are codes with params; the page writes the sentences in both languages and points at the line.

## 3. Where the rules live (decision)

**R1. The active rules are a file; their history, drafts and proposals live in the catalog.**

- The **active rules** are the text of one file: the file named by `[rules] path` when the config sets it, otherwise `rules.toml` next to `config.toml` (the path the config template already suggests), which drive-merge owns the way it owns `connections.toml`. When neither file exists the packaged `default_rules.toml` is active. `config.toml` is never edited: the convention of `config.py` stays intact, and no `[rules]` entry has to be added for the page to work.
- The catalog gains a `rule_versions` table: every text that was ever active (with who made it active and when), the person's **drafts**, and **proposals** from Claude or any API client. A version is immutable once it is not a draft.
- **Activating** a version (human channel only) writes its exact text to the active file atomically, after checking that the file still holds the text the person reviewed against (compare-and-swap on its SHA-256). The text being replaced is already in the history, so **undo** is "activate the previous version". Nothing else in drive-merge writes the file.
- The file is read on every use, as today, so an edit made in a text editor or by `drivemerge rules activate` takes effect at once. Whenever the page loads the rules state, or a plan is proposed, a file text not yet in the history is recorded as a version by `file` ("changed outside drive-merge"), so the history is complete and the person sees the outside change.

Options weighed:

| Option | Hand-edited file | History and undo | Plan reproducibility | Claude cannot slip a change in | Several tabs | Verdict |
|---|---|---|---|---|---|---|
| (a) write the `[rules] path` file, creating it and a `[rules]` entry in `config.toml` | kept as the source; but writing `config.toml` breaks the "never edit the person's config" rule, and a plain overwrite loses an outside edit | none: the previous text is gone | none by itself | only if writing is gated | last write wins | adopted for the *active* text, without touching `config.toml`, with compare-and-swap, plus the history of (b) |
| (b) named rule sets in the catalog with an "active" pointer | a second source of truth: the file and the pointer disagree, and the CLI's `--rules FILE` and a person's editor stop meaning anything; rules vanish with a rebuilt catalog | full | by version id | gating the pointer is enough | version counters | adopted for history, drafts and proposals only |
| (c) rules per plan only, save and load as files | untouched | none | full (the plan carries the text) | inline rules are visible per plan | no shared state | rejected as the model (edits die with the tab; the browser cannot write a file; "the rules I rely on" stays terminal-only); its good part, every plan carrying its text, is adopted |

What the person sees: the Rules view says where the active rules come from ("`~/.config/drive-merge/rules.toml` · active since 2 days, by you in the browser" / "packaged defaults · not yet customised" / "`~/rules/layout.toml` · changed outside drive-merge 10 min ago"), a history list, drafts, and Claude's proposals with a badge. The activation dialog names the file it will write and shows the diff.

**R3. Rules are not cloud changes, but making rules active is a human act.** Activating changes what every later plan built "with the active rules" does, including plans the CLI builds and plans Claude proposes without passing rules; a person approving a 40,000-op plan reads the totals, not each placement. So activation runs on the human channel (browser session opened by `drivemerge ui` from an interactive terminal, with CSRF and same-origin checks), like connections, and in the terminal (`drivemerge rules activate <version>`, behind the TTY gate). Drafts, proposals, previews and plans with inline rules are proposals and stay on the normal channel.

## 4. The editor (decision R2)

**R2. A text editor with live validation, plus an outline whose actions are text edits.** A structured form was weighed and rejected as the primary editor: the page has no TOML writer, round-tripping through `tomllib` drops every comment and the person's formatting, the order of rules is the language's main semantics (first match wins) and is clearest as text, and Claude writes TOML. Fields-only editing also hides `path_glob` and `name_regex`, which are text anyway. The outline gives the structured view without owning the text:

- **Outline**: the rules in order, unit rules marked, each with its match summary (categories, extensions, globs, sizes, clouds), its target template, its preview count, and a red mark when the rule has an error. Clicking a rule moves the cursor to its `[[rule]]` line. **Move up / Move down**, **Remove** and **Insert rule…** send the text to the server, which moves or removes the whole `[[rule]]` block together with the comment lines directly above it, or inserts a rendered block, and answers the new text only when it parses into exactly the expected rules (otherwise `RULES_CANNOT_EDIT`, and the person edits the text).
- **Insert rule…**: a small form (name, unit or loose files, categories as checkboxes, extensions, path glob, name pattern, sizes, clouds, target with clickable variable chips and a live rendering of one example file) that produces a TOML block; the block is inserted after the selected rule. The form never edits an existing rule.
- **Text**: a monospace `<textarea>` with a line-number gutter. Errors underline their line in the gutter and list below the text, worded from code and params ("Rule 3 (`screenshots`): unknown category `foto` — did you mean `photo`?"), each with "go to line". Warnings (a rule that can never match because an earlier rule of the same kind matches everything; a fallback that does not keep the original structure; a `remote` that is not a cloud of this catalog) show in amber. All errors of all rules are listed, not only the first.
- **Starting points**: "New from active", "New from defaults" (the packaged file, with its comments), "New from version …", and "Open a plan's rules" from a plan review.
- Drafts autosave (2 s after typing stops). The unsaved text of each tab is also kept in `sessionStorage`, so a reload keeps it.

## 5. Information architecture

An eighth rail item, **Rules** (`#/rules`, `Alt+7`), after Quarantine. Its badge counts proposals waiting for review.

```
┌ rail ┐┌──────────────────────────────────────────────────────────────────────────────┐
│  ☁   ││ Rules                                           [ New from active ] [ New from defaults ]│
│  ◎   ││ Active  ~/.config/drive-merge/rules.toml · v12 · 14 rules · active since 2 d (you)       │
│  ⌕   ││         [ Open in editor ]  [ Use in consolidate… ]  [ Use in reorganize… ]              │
│  ⇆   ││ ┌ Waiting for review ───────────────────────────────────────────────────────────┐   │
│  ☰   ││ │ v15 ✦ Claude  "Keep scanned receipts by year"  +1 rule · 2 changed   2,310 files ›│   │
│  ⛒   ││ └───────────────────────────────────────────────────────────────────────────────┘   │
│  ≡   ││ Drafts   v14 "photos by camera"  edited 3 min ago                           ›        │
│  ▦   ││ History  v12 active · v9 retired (you) · v8 changed outside drive-merge · v1 defaults  │
└──────┘└──────────────────────────────────────────────────────────────────────────────┘
```

- **Active**: source (file path, or "packaged defaults"), version, rule count, since when and by whom; an amber note when the file was changed outside drive-merge since the last activation from the page, and a red note when the active file does not parse (every consolidate and reorganize plan fails until it is fixed; "Open in editor" shows the errors).
- **Waiting for review**: proposals (Claude, API), newest first, with a one-line change summary (rules added, removed, changed, moved) and, once computed, how many files change destination. A row opens the review (§6).
- **Drafts**: the person's drafts; **History**: every version that was active, newest first, with **Restore** (activates it again) and **Open in editor**.

The editor, `#/rules/edit/<version>`:

```
┌──────────────────────────────────────────────────────────────────────────────────────┐
│ Draft v14 · from active v12 · saved          [ Diff ] [ Use in plan ▾ ] [ Activate… ]  │
├ Outline ───────────────┬ rules.toml ────────────────────────┬ Preview ────────────────┤
│ ▣ iwork-bundles   38 u │ 61 [[rule]]                        │ scope [all clouds ▾]    │
│ ▣ projects       412 u │ 62 name = "screenshots"            │   folder [        ]     │
│ ● screenshots  1,204   │ 63 category = ["photo"]            │ ≈ 1,048,576 files (2%)  │
│ ● photos     611,532 ↑ │ 64 name_regex = '(?i)^(screen…'    │ changed vs active 3,904 │
│ ● receipts ✚    2,310  │ 65 target = "Screenshots/{year}/…" │ fallback  88,210        │
│ ● videos      40,118   │ ── ⚠ 70 rule 5: unknown category   │ in place 402,118        │
│   fallback    88,210   │       "foto" (did you mean photo?) │ ┌ rule ─ active ─ new ┐ │
│ [ Insert rule… ]       │                                    │ │ photos 615,436 611,532│ │
│ [ ↑ ] [ ↓ ] [ Remove ] │                                    │ │ receipts   —   2,310│ │
│                        │                                    │ └─────────────────────┘ │
│                        │                                    │ [Changed][Fallback][All]│
│                        │                                    │ gdrive:Scans/r_0412.pdf │
│                        │                                    │  Documents/2019/… →     │
│                        │                                    │  Receipts/2019/…  receipts│
└────────────────────────┴────────────────────────────────────┴─────────────────────────┘
```

On a narrow window the three panes become tabs (Outline · Text · Preview).

## 6. Flows

**Edit and preview**

1. Typing → after 200 ms `POST /v1/rules/check`: errors, warnings, outline, diff against the active rules. Nothing reads the catalog.
2. When the text has no error → after a further 400 ms (600 ms after the last keystroke) `POST /v1/rules/preview` with the scope (clouds, folder, target root). An answer for an older text is dropped; a newer request interrupts the server's running preview.
3. The preview shows: files in scope and whether the numbers are sampled ("≈", with the sample rate) or exact; changed against the active rules; fallback (unmatched); already in place (target = current path); targets shared by several files in the sample; a table per rule with active and edited counts, bytes, and units for unit rules (a rule with 0 matches is marked); the top transitions ("documents → receipts 2,310"); and a paged file list with tabs **Changed**, **Fallback**, **All** and one per rule, each row: cloud and current path, the active rules' destination, the edited rules' destination, the rule, the unit it travels with. "Count all files" asks for exact numbers on a sampled scope.
4. **Explain** (on any file row, or by typing `cloud:path`): each rule in order with match or the first key that failed ("category is `pdf`, rule wants `photo`"), the unit the file belongs to, and the destination under the edited and the active rules.

**Activate**

1. "Activate…" (only with approvals enabled) opens a dialog: the file that will be written, the text diff against the active rules, the change summary, and the preview's "changed vs active" count for the whole catalog (an exact count is computed here when the last preview was sampled; the dialog says it is working).
2. "Activate" → `POST /v1/ui/rules/versions/<id>/activate` with the active SHA-256 the dialog showed. A version with errors cannot be activated (`RULES_INVALID`).
3. If the file changed meanwhile (another tab, an editor, the CLI): `RULES_ACTIVE_CHANGED`; the dialog reloads and shows the new diff. If the file cannot be written: `RULES_FILE_UNWRITABLE` with the path, and the dialog offers "Copy text" and the command.
4. After activation the version is active, the previous one retired, and the Rules view, the plan forms and the waiting plans see the new state. Plans already created are unchanged; the page says so.

**Claude's proposals**

1. Claude calls the MCP tool `propose_rules` (or an API client posts a version). The server validates it (a proposal with errors is refused with its codes, so Claude fixes it) and stores it as a `proposed` version by `mcp` or `api`.
2. The page, polling every 10 s while visible, raises the "proposed by Claude" toast with **Review**; the rail badge counts proposals.
3. Review (`#/rules/v/<id>`): Claude's title and note (untrusted text, shown as data), the text diff, the change summary, the preview against the active rules. Actions: **Activate…** (as above), **Edit as draft** (copies it into a new draft based on it), **Use in plan** (without activating), **Dismiss**.
4. A proposal is never edited in place and never active by itself. Claude may also build a plan with it (`rules_version`); that plan's review then shows "built with proposed rules v15 — not the active rules".

**Into a plan**

- "Use in plan ▾" → **Consolidate** or **Reorganize**. A dirty draft is saved first; the page then opens `#/plans/new/consolidate?rules=<version>` (or `reorganize`), carrying the preview's target root and folder.
- The consolidate form, and the new reorganize form (`#/plans/new/reorganize`: cloud, target root, folder, rules — the builder `build_reorganize` exists; PLANS.md had no form for it), get a **Layout rules** field: "Active rules (source)" or "Version 14 (draft) — not active", with **Edit** (opens the editor with a link back to the form) and **Use active rules**. The estimate and "Create plan" send `rules_version`. The `cons.rulesHelp` sentence is replaced.
- The plan is estimated, created, simulated and approved exactly as today. Its review shows a **Rules** box: source, version, SHA-256 prefix, "not the active rules" when they differ, "View" (the text) and "Open in editor".

**Several tabs**: each tab edits its own draft; saving a draft another tab changed is refused (`RULES_DRAFT_CHANGED`) with "Reload" or "Save as a new draft". Activation is guarded by the active SHA-256, so two tabs cannot both activate against the same base unnoticed.

**Read-only fallback** (approvals disabled): everything except activation works — drafts, check, preview, explain, plans with a draft's rules (their approval then goes through the terminal, as for every plan of a read-only page). "Activate…" and "Restore" are replaced by the command `drivemerge rules activate <version>` (the draft is saved first so it has a number), shown with a copy button, plus the note that the terminal shows the same diff and asks for confirmation.

## 7. Performance

- **Check** is pure (`tomllib` + validation, no catalog), limited to 256 KB of text, debounced 200 ms.
- **Preview** is sampled: when the scope holds at most 100,000 live files it is exact; above that it places a deterministic hash sample of 20,000 files (the same files on every keystroke, so counts do not flicker and before/after compare the same files) and scales counts, marked "≈". "Count all files" runs the exact pass on request (a cap of 2,000,000 files per request).
- **Units are cached.** Unit detection reads every live row of a remote and depends only on `unit_markers` and `bundle_patterns`; its result is cached per remote, keyed by those lists and the catalog's snapshot and `row_version`. Editing rules other than the markers costs only the sample's placement.
- **The active baseline is cached.** Placements under the active rules for the same sample and scope are cached by (active SHA-256, scope, catalog state), so only the edited side is recomputed per keystroke.
- **Results are cached and paged.** A preview's per-file rows stay in an LRU of 8 previews (by rules SHA-256, scope and catalog state); file pages (100 rows, at most 500) are served from it. An evicted preview answers `PREVIEW_EXPIRED` and the page recomputes.
- **Outside the catalog lock, cancellable.** A preview runs on a dedicated read-only connection (WAL), not under the lock that serialises the other routes, one at a time per server; a newer request interrupts the running one (`Connection.interrupt()` and a cancel flag checked every 1,000 rows), which answers `PREVIEW_SUPERSEDED` and is ignored by the page.

## 8. Cross-links

- Plans → consolidate and reorganize forms: the Layout rules field (Edit, Use active rules).
- Plan review: the Rules box; "Open in editor" creates a draft from the plan's rules.
- Rules view: "Use in consolidate…", "Use in reorganize…" on the active rules and every version.
- Rail badge and toast for proposals; the plan toast stays as it is.
- Explore: "Where would the rules put this?" on a result row opens the editor's Explain with that file.

## 9. Known limitations (v1)

- One active rule set for the catalog. Per-cloud layouts are expressed with the `remote` match key, not with several active sets.
- The text editor is a plain `<textarea>` with a gutter: no syntax highlighting or completion. The outline and the Insert form cover the common edits.
- Sampled counts are estimates; exact counts on a very large catalog take seconds (the page says it is working).
- The preview shows placements, not ops: conflicts that only the builder resolves (`LAYOUT_TAKEN`, identical content already on the target, read-only clouds) appear in the plan estimate, not in the preview. The preview counts shared targets in its sample as a hint.
- The catalog is as old as the last scan of each cloud.
- An edit of the active file outside drive-merge is detected when the Rules view loads or a plan is proposed, not the moment it happens.
- Activating rules changes no existing plan; a plan built with the previous rules must be cancelled and rebuilt to follow the new ones.

---

# Implementation contract

Everything below is binding. Read first: [DESIGN.md](DESIGN.md) §6, §7.4, [PLANS.md](PLANS.md) §B, §C, §E, [WORKSPACE.md](WORKSPACE.md) §D, §F, [QUARANTINE.md](QUARANTINE.md) §E, §F, `classify.py` (`load_rules`, `_parse_rule`, `_check_template`, `RuleError`, `detect_units`, `plan_layout`, `render`), `default_rules.toml`, `config.py` (`rules_path`, `write_private`, the "never edit config.toml" note), `service.py` (`ruleset`, `validate_rules`, `preview_layout`, `_build`, `propose_plan`, `estimate_plan`, `RESERVED_PARAMS`), `db.py` (`migrate`, `SCHEMA_VERSION`, `journal`, `snapshot`), `api.py` (`build_router`, `install_error_handlers`), `ui_api.py` (`require_human`, `_Jobs`), `mcp_server.py`, `cli.py` (`cmd_rules_check`, `cmd_rules_preview`, the TTY gate of `plan approve`), and every file in `src/drivemerge/web/`.

## A. Definitions

- **Active file** = `Config.rules_file`: `Path(rules_path).expanduser()` when `[rules] path` is set; else `config_dir / "rules.toml"` when the config was loaded from a file (`config_path` set); else `None` (a config built in memory, as in tests, never reads a file in the person's home). **Owned** = `[rules] path` is not set.
- **Active rules** = the text of the active file when it exists; else the packaged `default_rules.toml` (source `default`). A configured `[rules] path` whose file is missing is an error (`RULES_FILE_MISSING`), not a silent fall back to the defaults.
- **Version** = a `rule_versions` row. Status: `draft` (by `ui`, editable), `proposed` (by `mcp`, `api`; immutable), `active` (at most one row; its `sha256` equals the active file's), `retired` (was active), `dismissed`. `created_by`: `ui`, `mcp`, `api`, `cli`, `file` (text found in the active file that no version had), `default`.
- **Observe** = read the active file; if its SHA-256 differs from the `active` row's, retire that row and insert the file's text as a new `active` version by `file` (or reuse an existing version with that SHA-256, setting it `active`). Observing happens in `Service.rules_state()`, before an activation, and in `propose_plan` for consolidate and reorganize; never in check, preview or `estimate_plan` (they stay free of catalog writes).
- **Rules ref** of a plan = `{"source": "file"|"default"|"version"|"inline", "version": int|null, "sha256": str, "path": str|null}`.

## B. `classify.py`

- `RuleError(code, message, **params)`: `code` and `params` attributes; `str()` stays the English message (every existing message unchanged). `params` always holds `line` (1-based or `null`), `rule_index` (1-based or `null`), `rule` (name or `null`) and `key` (or `null`), plus the code's own params. `RULE_CODES` lists every code: `TOML_SYNTAX {column}`, `UNKNOWN_TOP_KEY {suggestion}`, `LAYOUT_NOT_TABLE`, `UNKNOWN_LAYOUT_KEY {suggestion}`, `MARKER_HAS_SLASH {value}`, `RULES_NOT_ARRAY`, `RULE_NOT_TABLE`, `UNKNOWN_RULE_KEY {suggestion}`, `NAME_REQUIRED`, `NAME_RESERVED`, `NAME_DUPLICATE {name}`, `TARGET_REQUIRED`, `BAD_UNIT_FLAG`, `TEMPLATE_EMPTY {where}`, `TEMPLATE_SYNTAX {where}`, `TEMPLATE_FORMAT_SPEC {where, variable}`, `UNKNOWN_VARIABLE {where, variable, suggestion}`, `TEMPLATE_DOT_SEGMENT {where}`, `UNIT_NEEDS_RELPATH`, `UNIT_VAR_NOT_UNIT`, `BAD_STRING_LIST`, `UNKNOWN_CATEGORY {value, suggestion}`, `BAD_EXT`, `BAD_GLOB`, `BAD_REGEX {value}`, `BAD_SIZE {value}`, `SIZE_RANGE_EMPTY {min, max}`, `RULES_TOO_LARGE {limit}`, `RULES_CANNOT_EDIT {reason}` (`reason` one of `unparsed`, `not_found`, `mismatch`), `TARGET_ROOT_DOT`. `where` is `target` or `fallback`; `suggestion` is a string or `null`. Values that echo the person's text (`value`, `name`) are capped at 200 characters.
- `TOML_SYNTAX`: `line` and `column` from `TOMLDecodeError.lineno`/`colno` when present, else parsed from the "(at line N, column M)" suffix, else `null`.
- `rule_spans(text) -> list[Span]` with `Span(kind: "layout"|"rule", index, comment_line, header_line, end_line)`: header lines `^\s*\[\[\s*rule\s*\]\]` and `^\s*\[\s*layout\s*\]` (trailing comment allowed), `comment_line` the first of the contiguous comment lines directly above the header, `end_line` the line before the next span's `comment_line` (or the last line). Spans are trusted only when the text parses and the number of rule headers equals the number of parsed rules; otherwise `[]`. `line` of a rule error = its `header_line`, or the line of `key =` inside the span when the error has a `key`.
- `check_rules(text) -> RulesCheck` (dataclass): `ok`, `errors` (every rule's first error and every layout error, at most 50, in text order), `warnings`, `outline`, `layout`, `ruleset` (`None` when not ok). Rules are parsed independently so one bad rule does not hide the others; duplicate names are reported on the later rule. `load_rules(text)` keeps its contract (raises the first error; same English messages).
- Warnings (`RULE_WARN_CODES`): `RULE_SHADOWED {rule, by}` (an earlier rule of the same kind, unit or loose, has no match key at all, so this rule never matches), `FALLBACK_LOSES_STRUCTURE` (the fallback uses neither `{path}` nor `{relpath}`, so unmatched unit files lose their structure), and `RULE_REMOTE_UNKNOWN {rule, value}` (a `remote` entry that is not a cloud of the catalog; added by `Service.check_rules`, which knows the catalog), each with `line`.
- `outline` items: `{"index", "name", "unit", "line", "end_line", "match": {"category", "ext", "path_glob", "name_regex", "min_size", "max_size", "remote"}, "target"}`; `layout`: `{"unit_markers", "bundle_patterns", "fallback", "defaulted": [keys not given]}`.
- `transform_rules(text, op) -> str`: `op` is `{"op": "move", "index", "to"}`, `{"op": "remove", "index"}` or `{"op": "insert", "after": index|0, "rule": {name, target, unit?, category?, ext?, path_glob?, name_regex?, min_size?, max_size?, remote?}}`. Moves and removes operate on whole spans (comment lines included); an insert renders a block (TOML basic strings with escaping; literal strings for `name_regex` and `path_glob` when they contain no `'`) followed by a blank line. The result must parse to the expected rules (same rules, moved; one fewer; the new rule at the place asked) or `RULES_CANNOT_EDIT` is raised. An inserted rule is validated like any rule.
- `plan_layout(..., sample_threshold: int | None = None, units: Mapping[int, list[Unit]] | None = None, cancel: Callable[[], bool] | None = None)`: `sample_threshold` keeps only rows with `((id * 2654435761) % 4294967296) < sample_threshold` (in SQL); `units` replaces `detect_units` per remote id when given; `cancel` is called every 1,000 rows and a true answer raises `LayoutCancelled`. Without them the output is byte-for-byte what it is today (existing tests unchanged).
- `explain(conn, ruleset, remote_id, path, *, target_root="", units=None) -> dict`: `{"remote", "path", "unit": path|null, "unit_rule": name|null, "steps": [{"rule", "unit", "result": "match"|"skip"|"not_tried", "failed_key": key|null, "wanted": value|null, "got": value|null}], "rule", "target", "in_place"}`; `NOT_FOUND` (`KeyError`) for a path that is not a live file.

## C. `rulestore.py` (new) and the catalog

- Schema (`schema.sql`), `SCHEMA_VERSION = 3` (nothing to migrate: the table is created by `CREATE TABLE IF NOT EXISTS`; `_upgrade_after` only records the version):

```sql
CREATE TABLE IF NOT EXISTS rule_versions (
    id           INTEGER PRIMARY KEY,
    status       TEXT NOT NULL CHECK (status IN ('draft', 'proposed', 'active', 'retired', 'dismissed')),
    text         TEXT NOT NULL,
    sha256       TEXT NOT NULL,                 -- of the UTF-8 text, exactly as stored
    ok           INTEGER NOT NULL,              -- the text passed check_rules when saved
    created_by   TEXT NOT NULL,                 -- 'ui' | 'mcp' | 'api' | 'cli' | 'file' | 'default'
    title        TEXT,
    note         TEXT,                          -- why: Claude's rationale, the person's note
    base_id      INTEGER REFERENCES rule_versions(id),  -- the version it was edited from
    revision     INTEGER NOT NULL DEFAULT 0,    -- drafts: bumps on every save
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    activated_at TEXT,
    activated_by TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS rule_versions_active ON rule_versions(status) WHERE status = 'active';
```

- `RulesError(status, code, message, **params)` and `CODES`: `RULES_VERSION_NOT_FOUND {version}` (404), `RULES_NOT_DRAFT {version, version_status}` (409), `RULES_DRAFT_CHANGED {version, revision}` (409), `RULES_INVALID {errors}` (400; `errors` = the first 10 `check_rules` errors as `{code, params}`), `RULES_ACTIVE_CHANGED {active_sha256, active_version}` (409), `RULES_FILE_UNWRITABLE {path}` (409), `RULES_FILE_MISSING {path}` (409), `RULES_FILE_UNREADABLE {path}` (409), `RULES_NO_FILE` (409: no config file, so nowhere to write), `RULES_NOT_ACTIVATABLE {version, version_status}` (409: dismissed), `RULES_BOTH` (400: `rules` and `rules_version` given together), `DRAFT_UI_ONLY` (403: only the browser session edits or deletes drafts), `PREVIEW_EXPIRED {preview_id}` (410), `PREVIEW_SUPERSEDED` (409), `PREVIEW_TOO_LARGE {files, limit}` (409).
- Functions (all take `conn`; text limit 256 KB → `RULES_TOO_LARGE`): `active(conn, config) -> ActiveRules` (`text, sha256, source, path, owned, version_id, error: {code, params}|null`; reads, never writes), `observe(conn, config) -> ActiveRules`, `create_version(conn, text, *, created_by, title=None, note=None, base_id=None) -> dict` (status `draft` for `ui`, `proposed` otherwise; a proposal must pass `check_rules` or `RULES_INVALID`; a draft may hold errors), `update_draft(conn, version_id, text, *, revision, title=None, note=None)`, `get_version(conn, version_id)`, `list_versions(conn, *, status=None, limit=50)`, `discard(conn, version_id, *, by)` (a draft is deleted; a proposal becomes `dismissed`), `activate(conn, config, version_id, *, base_sha256, activated_by) -> ActiveRules`, `diff(old_text, new_text) -> {"hunks": [{"old_start", "new_start", "lines": [{"op": " "|"+"|"-", "text"}]}], "added", "removed"}` (3 lines of context, `difflib`), `rule_changes(old: RuleSet, new: RuleSet) -> [{"name", "change": "added"|"removed"|"changed"|"moved"}]` (by name).
- `activate`: observe; refuse a version that is not `draft`/`proposed`/`retired`/`active` or not ok; compare `base_sha256` with the active SHA-256 (`RULES_ACTIVE_CHANGED`); write the exact text atomically next to the target (temp file, fsync, rename; a symbolic link is written through; the existing file's mode is kept, a new file is 0600; parent created for the owned file); then, in one transaction, retire the active row, set this one `active` with `activated_at`/`activated_by`, and `db.journal(conn, "rules_activate", detail={"version", "sha256", "path", "by"})`. Activating the packaged defaults is activating a version made from their text (by `default`). Writing never touches `config.toml`.

## D. Service

- `Service.ruleset(rules_text=None, *, rules_version=None)`: the version's text when given (any status), else `rules_text`, else the active rules (`rulestore.active`); `RULES_BOTH` when both are given. An invalid active file raises its `RuleError`, as today.
- `_build` pops `rules` and `rules_version`; for consolidate and reorganize it resolves the rules and returns the rules ref. `propose_plan` observes first, then stores `params["rules"]` (the exact text used, always, for these two kinds) and `params["rules_ref"]`. `rules_ref` joins `RESERVED_PARAMS` (a caller cannot claim it). `estimate_plan` accepts `rules_version` and adds `"rules_ref"` to its answer; it still writes nothing (existing `total_changes` test unchanged). Other kinds are unchanged.
- New methods (read/propose section): `rules_state()` → `{"active": ActiveRules as dict + "check": check summary, "proposals": [...], "drafts": [...], "history": [...]}` (summaries: `{id, status, title, created_by, created_at, updated_at, activated_at, activated_by, sha256, ok, base_id, revision, rules, changes}` where `changes` is `rule_changes` against the active rules); `rules_default()` → `{"text", "sha256"}`; `rule_versions(status=None, limit=50)`; `rule_version(id)` → summary + `text` + `check` + `diff` against the active rules; `save_rule_version(text, *, created_by, title, note, base_id)`; `update_rule_draft(id, text, *, revision, title, note)`; `discard_rule_version(id, *, by)`; `check_rules(text, against=None)` → `{"ok", "errors", "warnings", "outline", "layout", "diff", "changes", "sha256"}` (`against`: a version id, default the active rules); `transform_rules(text, op)` → `{"text", "check"}`; `explain_layout(...)`.
- `validate_rules(text)` keeps `{"ok", "rules"}` / `{"ok": false, "error"}` and adds `"code"`, `"params"` and `"errors"` (all of them) and `"warnings"`.
- Preview (`rulepreview.py`, new; the service methods are thin): `preview_rules(*, text=None, version_id=None, remotes=None, target_root="", path_prefix=None, mode="auto"|"exact", limit=100, filter="changed", conn=None)` and `preview_page(preview_id, *, filter, q=None, offset=0, limit=100)`. Answer:

```json
{"preview_id": "pv_3f2a9c", "rules_sha256": "…", "active_sha256": "…",
 "scope": {"remotes": ["gdrive", "dropbox"], "target_root": "", "path_prefix": null},
 "files_in_scope": 1048576, "sampled": true, "sample_files": 20000, "sample_rate": 0.0191, "elapsed_ms": 812,
 "rules": [{"name": "photos", "unit": false, "files": 11680, "bytes": 0, "units": null, "active_files": 11755,
            "estimate_files": 611532, "estimate_active_files": 615436}],
 "fallback": {"files": 1684, "bytes": 0, "estimate_files": 88210, "active_files": 1702},
 "in_place": 7680, "changed": 75, "estimate_changed": 3904, "shared_targets": 3,
 "transitions": [{"from": "documents", "to": "receipts", "files": 44, "estimate_files": 2310}],
 "page": {"filter": "changed", "q": null, "offset": 0, "limit": 100, "total": 75,
          "files": [{"entry_id": 912, "remote": "gdrive", "path": "Scans/r_0412.pdf", "size": 81234,
                     "rule": "receipts", "target": "Receipts/2019/r_0412.pdf", "unit": null,
                     "active_rule": "documents", "active_target": "Documents/2019/r_0412.pdf", "in_place": false}]}}
```

  Rules are matched across versions by name (`active_files` is `null` for a rule the active rules do not have; `from` is `null` when the active side has no such rule). `filter` is `all`, `changed`, `fallback`, `in_place` or `rule:<name>`; `q` is a case-insensitive substring of `remote:path`. `estimate_*` equal the counts when not sampled. `mode: "exact"` refuses a scope above 2,000,000 files (`PREVIEW_TOO_LARGE`). Exact up to 100,000 files in scope, else a 20,000-file sample (`sample_threshold` chosen from the scope's file count). Caches as §7: units per remote, the active baseline, an LRU of 8 previews; every cache key includes `db.snapshot` and `MAX(row_version)`.
- `preview_layout` and `POST /v1/layout/preview` stay as they are (scripts and MCP use them).
- Human-only section: `activate_rules(version_id, *, base_sha256, activated_by)`.

## E. HTTP

On the shared router (`api.py`, both apps, normal auth), plus one human-channel route in `ui_api.py`. `RuleError` and `RulesError` are answered in both apps as `{"error", "code", "params"}` (always coded): `RuleError` 400, `RulesError` with its status. Request bodies forbid extra keys. `created_by` is `ui` for a browser session and `api` for the bearer token (the existing `created_by(request)` hook).

| Method & path | Auth | Body / query | Response |
|---|---|---|---|
| `GET /v1/rules` | normal | | `rules_state()` (observes the active file) |
| `GET /v1/rules/default` | normal | | `{"text", "sha256"}` |
| `POST /v1/rules/check` | normal | `{"text", "against"?: int}` | §D `check_rules` |
| `POST /v1/rules/transform` | normal | `{"text", "op"}` | `{"text", "check"}`; 400 `RULES_CANNOT_EDIT` |
| `GET /v1/rules/versions` | normal | `status`, `limit` (≤ 200) | `{"versions": [summaries]}` |
| `POST /v1/rules/versions` | normal | `{"text", "title"?, "note"?, "base_id"?}` (title ≤ 200, note ≤ 2000 chars) | 201 the version (draft for a session, proposal for the bearer token) |
| `GET /v1/rules/versions/{id}` | normal | | `rule_version(id)` |
| `PUT /v1/rules/versions/{id}` | normal, session only | `{"text", "revision", "title"?, "note"?}` | the draft; 403 `DRAFT_UI_ONLY`, 409 `RULES_NOT_DRAFT`, 409 `RULES_DRAFT_CHANGED` |
| `DELETE /v1/rules/versions/{id}` | normal | | `{"ok"}`; a draft (session only, else 403 `DRAFT_UI_ONLY`) is deleted, a proposal dismissed (session, or the bearer token for proposals by `api`) |
| `POST /v1/rules/preview` | normal | `{"text"?, "version_id"?, "remotes"?, "target_root"?, "path_prefix"?, "mode"?: "auto"\|"exact", "filter"?, "limit"? (≤ 500)}` | §D preview; 400 `RULES_BOTH` (text and version), the text's first `RuleError` when invalid |
| `GET /v1/rules/previews/{preview_id}/files` | normal | `filter`, `q`, `offset`, `limit` (≤ 500) | the `page` object; 410 `PREVIEW_EXPIRED` |
| `POST /v1/rules/explain` | normal | `{"text"?, "version_id"?, "remote", "path", "target_root"?}` | `{"edited": explain, "active": explain}` |
| `POST /v1/ui/rules/versions/{id}/activate` | human channel | `{"base_sha256"}` | `{"active": ActiveRules}`; refusals in this order: the human channel's (`UNAUTHENTICATED`, `HUMAN_ONLY`, `APPROVALS_DISABLED`, `BAD_CSRF`, `BAD_ORIGIN`), `RULES_VERSION_NOT_FOUND`, `RULES_NOT_ACTIVATABLE`, `RULES_INVALID`, `RULES_NO_FILE`, `RULES_ACTIVE_CHANGED`, `RULES_FILE_UNWRITABLE` |

- The preview routes run on the app's dedicated reader connection (opened lazily on `config.db_path` with `PRAGMA query_only = ON`), outside the catalog lock, one at a time; a newer preview interrupts the running one, which answers 409 `PREVIEW_SUPERSEDED`. With an in-memory catalog (tests of `create_app`) they run on the service connection under the lock.
- `POST /v1/plans`, `POST /v1/plans/estimate`: `params.rules_version` accepted for `consolidate` and `reorganize`; `rules_ref` refused like `draft_id`.
- `GET /v1/ui/config` gains `"rules": {"source", "path", "owned", "active_version", "proposals": n}`.
- Nothing here returns `approval_code`, a digest or a snapshot (extend the scrub tests to the new routes).

## F. MCP and CLI

- `validate_rules` returns the new fields of §D (`code`, `params`, `errors`, `warnings`) besides `error`.
- New tool `get_rules()` (read): the active rules' source, path, version, SHA-256 and text, and the proposals waiting (id, title, created_by, created_at).
- New tool `propose_rules(rules_toml, title, note=None)` (`writes=True`): validates (errors returned as a normal result with codes, nothing stored), stores a proposal by `mcp`, returns `{version_id, rule_changes, warnings}` with the reminder: "Nothing is active. The human reviews proposal v<id> in the browser's Rules view and activates it there or with `drivemerge rules activate <id>` in their own terminal. Plans use the active rules unless you pass rules_version." Title ≤ 200, note ≤ 2,000 characters.
- `preview_layout` and `propose_plan` accept `rules_version`; the `propose_plan` docstring documents it and says a plan built with a proposal shows that in the review.
- No tool activates, edits or dismisses a version. The INSTRUCTIONS gain: rules changes are proposed with `propose_rules`, never by writing the rules file; never claim proposed rules are active.
- CLI: `drivemerge rules show [--version N]` (source, path, text), `drivemerge rules history [--json]`, `drivemerge rules activate N` (TTY gate as `plan approve`: prints the diff against the active rules and asks for the word `activate`; same refusals as the route, worded in English). `rules check` prints every error with its line. `.claude/settings.json` adds `Bash(drivemerge rules activate:*)` and `Bash(*drivemerge rules activate*)` to `deny`, and `Edit`/`Write` denials for `~/.config/drive-merge/rules.toml`.
- DESIGN.md §6 and §9 and PLANS.md §5 and §8 point to this document instead of saying "CLI or Claude only" (with their zh-TW twins).

## G. Frontend (`src/drivemerge/web/`)

Same rules as WORKSPACE.md §F, EXPLORE.md §F, PLANS.md §E and QUARANTINE.md §F (no build step, offline, CSP, Preact + htm from `vendor/`, both languages, codes worded on the page, `data-testid` on what tests touch).

- `rules.js` (new): the Rules view (`#/rules`), the editor (`#/rules/edit/<id>?back=<encoded hash>`), the version review (`#/rules/v/<id>`), the activation dialog, the read-only command box, the Insert rule form, the preview pane (summary cards, per-rule table, transitions, file list with filter tabs and "load more"), Explain. Exports `RulesField({ value, onChange, backHash })` for the plan forms and `PlanRulesBox({ plan })` for the plan review.
- `ruletext.js` (new): the text editor component (`<textarea>` + line-number gutter, error and warning marks per line, go to line, keeps the scroll position when the outline moves the cursor) and pure helpers (line ↔ offset). No third-party editor.
- `shell.js`: routes `#/rules`, `#/rules/edit/<id>`, `#/rules/v/<id>`; the rail item after Quarantine (`rules` icon), `VIEWS` gains `rules` (`Alt+7`), badge = proposals.
- `app.js`: poll `GET /v1/rules/versions?status=proposed` with the plan poll (every 10 s while visible); a new proposal by `mcp` raises the "proposed by Claude" toast with Review.
- `api.js`: `rules()`, `rulesDefault()`, `checkRules(text, against)`, `transformRules(text, op)`, `ruleVersions(query)`, `ruleVersion(id)`, `createRuleVersion(body)`, `updateRuleDraft(id, body)`, `discardRuleVersion(id)`, `previewRules(body)`, `previewFiles(id, query)`, `explainRules(body)`, `activateRules(id, body)` (human channel headers).
- `plans.js`: `RulesField` on the consolidate form (replacing `cons.rulesHelp`), the new reorganize form (`#/plans/new/reorganize?remote=&root=&path=&rules=`; only writable clouds; estimate and create as consolidate), `rules` in the URL state of both forms, `rules_version` in their params, `PlanRulesBox` on the review of consolidate and reorganize plans, and the "New: Reorganize" button on the list. Existing test ids unchanged.
- `explore.js`: the "Where would the rules put this?" row action only.
- `styles.css`, `icons.js` (`rules` icon).
- Debounces: check 200 ms, preview 600 ms after the last keystroke, draft autosave 2 s; stale answers dropped by sequence number; `PREVIEW_SUPERSEDED` ignored; `PREVIEW_EXPIRED` recomputes.
- `data-testid`s: `view-rules`, `rules-active`, `rules-active-source`, `rules-outside-change`, `rules-active-invalid`, `rules-proposal-row-<id>`, `rules-draft-row-<id>`, `rules-history-row-<id>`, `rules-restore-<id>`, `rules-new-active`, `rules-new-default`, `view-rules-edit`, `rules-text`, `rules-gutter`, `rules-error-<n>`, `rules-warning-<n>`, `rules-goto-<line>`, `rules-outline`, `rules-outline-item-<index>`, `rules-move-up`, `rules-move-down`, `rules-remove`, `rules-insert`, `rules-insert-form`, `rules-save-state`, `rules-diff`, `rules-use-plan`, `rules-use-consolidate`, `rules-use-reorganize`, `rules-activate`, `rules-activate-dialog`, `rules-activate-confirm`, `rules-activate-error`, `rules-cli`, `rules-draft-conflict`, `rules-preview`, `rules-preview-scope`, `rules-preview-sampled`, `rules-preview-exact`, `rules-preview-changed`, `rules-preview-fallback`, `rules-preview-inplace`, `rules-rule-row-<name>`, `rules-transition`, `rules-files-tab-<filter>`, `rules-file-row`, `rules-files-more`, `rules-explain`, `view-rules-version`, `rules-dismiss`, `rules-edit-as-draft`, `rules-field`, `rules-field-edit`, `rules-field-active`, `plan-rules-box`, `plans-new-reorganize`, `view-plan-new` with `data-kind="reorganize"`, `reorg-remote`, `reorg-root`, `reorg-path`, `rail-rules`, `rail-rules-badge`.
- i18n namespaces (both languages): `nav.rules`, `nav.rulesTip`, `rules.*`, `rulecheck.<CODE>` for every `RULE_CODES` entry, `rulewarn.<CODE>` for every `RULE_WARN_CODES` entry, `err.<CODE>` for every `rulestore.CODES` entry, `rsrc.<source>` (file, default, version, inline), `rstatus.<status>`, `rby.<created_by>`, `rchange.<change>`, `reorg.*`, and replacements for `cons.rulesHelp`. Every sentence uses only the params the server sends (`line`, `rule_index`, `rule`, `key` and the code's own).

## H. Tests

- `tests/test_classify.py` (extend): every `RULE_CODES` code at its raise site with `line`, `rule_index`, `rule`, `key`; `TOML_SYNTAX` line and column; all errors of several bad rules in one `check_rules`; unchanged English messages and `load_rules` behaviour; `rule_spans` (comments above headers, trailing comments, a `[[rule]]` inside a multi-line string, inline-array rules → `[]`); `transform_rules` move/remove/insert keep every comment and blank line elsewhere byte-for-byte and refuse with each `reason`; warnings; `plan_layout` with `sample_threshold`, `units` and `cancel`, and byte-for-byte unchanged output without them; `explain` steps for loose files, unit rules and the fallback.
- `tests/test_rulestore.py` (new): active-file resolution (configured, owned, none, configured but missing, in-memory config never reads home), observe (outside edit recorded once, reused SHA-256), versions lifecycle and the unique active index, draft revision conflicts, proposals refused when invalid, activation (CAS, atomic write keeping mode and symbolic links, new owned file 0600, journal row, `config.toml` byte-for-byte unchanged, unwritable file, no config file), undo by re-activating a retired version, `diff` and `rule_changes`.
- `tests/test_rules_preview.py` (new): exact vs sampled, the deterministic sample, counts and estimates, `changed`/`fallback`/`in_place`/`shared_targets`/`transitions` against hand-built catalogs, filters and paging, `PREVIEW_EXPIRED`, cache hits (units not recomputed when only targets change; recomputed when markers or the catalog change), cancellation, and that a preview writes nothing (`total_changes` unchanged).
- `tests/test_rules_api.py` (new): every route of §E through both apps where mounted; auth (bearer → proposal, session → draft, `DRAFT_UI_ONLY`, the activation route's refusal order including `APPROVALS_DISABLED` and `HUMAN_ONLY`); coded `RuleError` and `RulesError`; `rules_version` and refused `rules_ref` in estimate and create; `params.rules` and `rules_ref` stored for consolidate and reorganize; `PREVIEW_SUPERSEDED` with two concurrent previews; scrubbing; `/v1/ui/config` `rules`.
- `tests/test_service*.py`, `tests/test_plans.py` (adjust): assertions that pin consolidate and reorganize `params` gain `rules` and `rules_ref`; `estimate_plan` still writes nothing.
- `tests/test_mcp_server.py` (extend): `get_rules`, `propose_rules` (invalid text stores nothing; reminder text), `rules_version` passthrough, the service methods MCP may call (no `activate_rules`, `update_rule_draft`, `discard_rule_version`), the tool list.
- `tests/test_cli.py` (extend): `rules show`, `rules history`, `rules activate` TTY gate and refusals, `rules check` lines. `tests/test_config.py`: `rules_file`. `tests/test_migrations.py`: a version 2 catalog upgrades to 3 with the table.
- `tests/test_i18n_contract.py` (extend): every `RULE_CODES`, `RULE_WARN_CODES` and `rulestore.CODES` code has its sentence in both languages using only its params.
- `tests/test_ui_rules.py` (new, browser, mocked API importing `MockApi`/`Ui` from `test_ui_frontend.py`): rail item, badge and `Alt+7`; Rules view sections and source wording (file, owned, default, outside change, invalid active file); editor typing → check debounce → errors in the gutter and list with go to line; outline move/remove/insert request bodies and the new text; autosave and `RULES_DRAFT_CHANGED`; preview debounce, stale and superseded answers, sampled marks, filter tabs, paging, Explain; activation dialog (diff, request body with `base_sha256`, `RULES_ACTIVE_CHANGED` reload, `RULES_FILE_UNWRITABLE`); read-only command box; Claude proposal toast, review, dismiss, edit as draft; "Use in plan" → consolidate and reorganize forms with `rules` in the URL and `rules_version` in the estimate and create bodies; `PlanRulesBox` "not the active rules"; both languages. The existing browser tests stay green (update only the assertions that pin `cons.rulesHelp`).
- The whole suite stays green: `pytest` (with the browser tests), except `tests/test_docs_i18n.py` until `docs/RULES.zh-TW.md` exists.

## I. Work split (two implementers in parallel)

The HTTP shapes of §E and the codes of §B and §C are the interface; the frontend works against `MockApi` until the backend lands. No file is owned by both.

| Implementer | Owns (creates or edits) |
|---|---|
| Backend | `src/drivemerge/classify.py`, `rulestore.py` (new), `rulepreview.py` (new), `service.py`, `config.py`, `schema.sql`, `db.py`, `api.py`, `ui_api.py`, `mcp_server.py`, `cli.py`, `.claude/settings.json`; `tests/test_classify.py`, `test_rulestore.py`, `test_rules_preview.py`, `test_rules_api.py`, `test_api.py`, `test_service*.py`, `test_plans.py`, `test_mcp_server.py`, `test_cli.py`, `test_config.py`, `test_migrations.py`; the edits to `docs/DESIGN.md` and `docs/PLANS.md` (and their zh-TW twins) |
| Frontend | `src/drivemerge/web/rules.js` (new), `ruletext.js` (new), `shell.js`, `app.js`, `api.js`, `plans.js`, `explore.js`, `i18n.js`, `styles.css`, `icons.js`; `tests/test_ui_rules.py` (new), `test_ui_frontend.py` (`MockApi` routes for §E, and the `cons.rulesHelp` assertion), `test_plans_view.py`, `test_i18n_contract.py`; `docs/RULES.zh-TW.md` |

`test_i18n_contract.py` imports the code tuples (`classify.RULE_CODES`, `classify.RULE_WARN_CODES`, `rulestore.CODES`); the frontend implementer writes it against the lists in §B and §C and it turns green once both halves are merged.
