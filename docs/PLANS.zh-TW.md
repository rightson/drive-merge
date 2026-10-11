# 瀏覽器裡的計畫

[English](PLANS.md) | **繁體中文**

> 這份文件為視覺化工作區（[WORKSPACE.md](WORKSPACE.zh-TW.md)、[EXPLORE.md](EXPLORE.zh-TW.md)）新增一個 **計畫（Plans）** 檢視：在頁面上建立去重與整併計畫、審閱任何計畫（包含 Claude 透過 MCP 提出的）、輸入確認碼核准、執行，以及橋接沒有雜湊的檔案。第一部分是設計；第二部分（實作合約）對實作者具有約束力。

## 1. 問題

總覽、探索與比較告訴你哪些東西重複、分散或有風險。要大規模處理，仍然得回到終端機：`drivemerge plan dedupe`、`plan consolidate`、`plan show`、`plan approve`、`bridge`。頁面一次只能暫存一個手勢的搬移，這很適合重新安排資料夾，卻不適合「移除 40,000 個多餘副本」或「把所有東西集中到 Drive」。

這個檢視要回答的問題：

1. **清掉重複會做什麼？哪一份會留下？** 在任何計畫存在之前：有多少群組、能回收多少位元組、保留策略會留哪一份，以及針對單一群組改選的機會。
2. **整併到單一雲端會做什麼？** 已經在那裡的有多少、必須複製的有多少（其中有多少要經過這台電腦跨雲傳輸）、哪些被略過與原因，以及檔案會落在哪裡。
3. **有什麼在等我？** 每一份計畫，不論是誰提出的（CLI、API、Claude、這個頁面），連同它的模擬結果，讓人在同一個地方審閱並核准。

## 2. 原則

- **安全路徑不變。** 在這裡建立的每一份計畫，都由同樣的建構器建立、同樣的模擬器模擬、輸入同樣 8 碼確認碼核准，並由同樣的執行器執行。頁面沒有增加任何改動雲端的新方法；它只是多了一種「請求」既有流程的方式。
- **計畫存在之前先預估。** 建立計畫很便宜而且唯讀；但把它存起來就會產生一筆必須被取消的紀錄。因此每個流程都顯示記憶體內建構（不寫入任何東西）的即時預估，只在明確按下「建立計畫」時才建立計畫。
- **預設值是保守的。** 去重各雲端各自處理（`cross_remote` 關閉），除非使用者要求，否則沒有任何雲端會失去它自己的那一份。整併只複製（`delete_sources` 關閉）；移除來源是之後另一份計畫，要等過了 [DESIGN.md](DESIGN.zh-TW.md) §8 的觀察期，頁面也會這麼說明。
- **誰提出的看得見。** Claude 提出的計畫帶有工作區使用的「由 Claude 提出」徽章；確認碼只會顯示給在可以核准的瀏覽器工作階段前面的那個人。
- **頁面負責用文字表達。** 建構器與伺服器只送出代碼與參數；兩種語言的句子由頁面撰寫。

## 3. 資訊架構

第五個側邊欄項目 **計畫（Plans）**（`#/plans`），位於比較與工作區之間。

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

- **清單**：依狀態分組，「等待審閱」（`draft`、`simulated`）排在最前面。每一列：編號、種類、依 `params` 寫成的一行摘要（目標、根目錄、範圍）、提出者、操作數、模擬結論（通過／n 個問題／尚未模擬／已過期）、時間。工作區預覽也會列出（種類 `workspace`），並開啟工作區自己的預覽。
- **審閱**（`#/plans/<id>`）：和工作區預覽一樣的模擬報告（各雲端的前後對照、跨雲傳輸、附上譯文與顯示位置連結的違規與警告），加上操作清單：依操作種類與雲端分組的統計與位元組，以及可分頁的表格（來源路徑 → 目的地、刪除的見證副本、原因），附篩選框。動作有：**重新模擬**（自上次模擬以來目錄有變動時，或隨時）、**核准**（輸入確認碼，僅限模擬通過且核准功能已啟用時）、**執行**（僅限已核准且執行功能已啟用時）、**取消計畫**。執行時顯示和工作區相同的進度面板，結束時也有同樣的「目錄資料已落後」橫幅與重新掃描。
- **新增去重**（`#/plans/new/dedupe`）與 **新增整併**（`#/plans/new/consolidate`）：左邊是表單、右邊是即時預估，底部是「建立計畫」，按下後開啟新計畫的審閱。

## 4. 去重流程

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

- 參數與 `builders.build_dedupe` 一一對應：`remote`、`cross_remote`、`path_prefix`、`min_size`、`prefer_remotes`（可排序的雲端清單）。
- 「跨雲端」表示 *全部只保留一份*，並移除每個雲端上的其他所有副本。預設關閉；開啟時頁面會說明某個雲端將因此失去它自己的那一份。
- 預估會列出群組（可回收位元組最多的在前，可分頁）。每個群組裡，策略選出的保留副本帶有 ★；點選另一份副本會讓它成為該群組的保留副本（覆寫），「略過這一組」則把整組排除。覆寫與略過都是計畫 `params` 的一部分，所以計畫會記錄每一份副本為什麼留下。覆寫只能選建構器本來也能保留的副本（同一個池：沒有「跨雲端」時是同一個雲端；不在隔離區、不是原生文件、不在有歧義的 Drive 路徑）；伺服器會拒絕其他任何選擇。
- 被略過的副本會依原因彙整（唯讀雲端、有歧義的 Drive 路徑、原生文件），並可依需求列出。

## 5. 整併流程

- 參數對應 `builders.build_consolidate`：`target`（只提供可寫入的雲端）、`target_root`、`sources`（核取方塊，預設為其他全部）、`reorganize_target`、`include_probable`（預設關閉；開啟時頁面會說明「很可能相同」是在沒有證明的情況下假設內容一致，並建議改用橋接）、`path_prefix`、`prefer_remotes`，以及 `delete_sources`。
- `delete_sources` 不在主表單上。它藏在「清理來源（等過了觀察期之後）」後面，那裡會說明 DESIGN.md 的 §8，並根據覆蓋率的數字顯示還有多少內容 *不在* 目標上。這個數字不為零的時候，選項仍然可用，但會附上警告，因為那些內容會在同一份計畫裡被複製、其來源也被移除。
- 預估顯示：已在目標上的內容、要複製的內容與位元組（以及其中經過這台電腦跨雲傳輸的部分）、重新整理時目標上的搬移、清理時的刪除、依原因分類的略過（很可能相同但未證明、原生文件、唯讀、有歧義），以及落點範例（前 50 筆 `from → to`，依目的資料夾分組），讓版面在計畫存在之前就看得到。
- 規則：版面使用設定的規則，和 CLI 一樣。目前編輯規則仍然只能在 CLI 與 MCP 進行。

## 6. 從頁面橋接

總覽中回報「沒有可比對的雜湊」的卡片會多一個 **橋接** 按鈕（僅在核准功能已啟用時：橋接會下載位元組，所以和重新掃描一樣屬於人類通道的工作）。一個小對話框接受位元組預算與檔案數上限（預設 5 GB 與 500），顯示有多少候選檔案，啟動工作，顯示進度（已橋接、失敗），完成後更新覆蓋率。橋接從不寫入雲端；它只讀取檔案並寫入目錄。

## 7. 交叉連結

- 總覽的「重複佔用」總計 → 新增去重（所有雲端、各雲端各自處理）。
- 總覽雲端卡片的「僅存在這裡」 → 新增整併，以該雲端為來源、第一個可寫入的其他雲端為目標。
- 總覽的「沒有可比對的雜湊」 → 橋接。
- 探索結果在同一個雲端有多份副本 → 範圍限定在該雲端與該結果所在資料夾的新增去重。
- 側邊欄項目顯示等待審閱的計畫數徽章；頁面開著時 Claude 提出的計畫會跳出「由 Claude 提出」的提示，附上「審閱」按鈕（頁面可見時每 10 秒輪詢一次計畫清單）。

## 8. 已知限制（v1）

- 整併版面的規則要在 CLI 或由 Claude 編輯，不在頁面上。
- 表單每次變動都會重新建構預估（有防抖動）；在一百萬個檔案的目錄上，一次建構可能要好幾秒，預估會說明它正在處理中。
- 計畫的操作不能編輯；不同的計畫就是新的計畫（drive-merge 處處如此）。

---

# 實作合約

以下全部具有約束力。請先閱讀：[WORKSPACE.md](WORKSPACE.zh-TW.md) §D、§F、§G，[EXPLORE.md](EXPLORE.zh-TW.md) §A、§E、§F，`builders.py`（`build_dedupe`、`build_consolidate`、`choose_keeper`、`BuildResult`、`BuildError`）、`service.py`（`propose_plan`、`simulate`、`plan`、`plans`、`cancel`、`bridge`）、`plans.py`、`simulate.py`（報告與它的代碼）、`api.py`（`build_router`、`scrub_secrets`）、`ui_api.py`（工作階段、人類通道、工作、重新掃描）以及 `src/drivemerge/web/` 裡的每個檔案。

## A. 建構器變更（`builders.py`）

- `build_dedupe(..., keep: Mapping[str, int] | None = None, skip_groups: Sequence[str] = ())`：`keep` 把群組鍵（`g:<content_group>`）對應到必須在該群組的池裡保留的項目編號；`skip_groups` 列出不產生任何操作的群組鍵。若 `keep` 的某個編號不是該池可用的副本（見 §4），會丟出 `BuildError`，代碼為 `BAD_KEEPER`，參數 `{group, entry_id}`。未知的群組鍵則丟出 `BAD_GROUP` `{group}`。沒有這些參數時，輸出與現在逐位元組相同（既有測試不變）。
- `BuildError` 新增 `code` 與 `params`（預設代碼 `BUILD_ERROR`，參數 `{}`）；每個既有的 `raise BuildError` 都要有具體代碼，以及頁面需要的參數：至少 `READ_ONLY` `{remote}`（目標或遠端沒有可寫入的遠端）、`UNKNOWN_REMOTE` `{remote}`、`BAD_LOCATION` `{location}`，以及上面兩個。英文訊息維持原樣。
- `BuildResult.stats` 在去重時新增 `copies_considered`；整併必須回報（缺少的要補上）：`already_on_target`、`to_copy`、`bytes_to_copy`、`bytes_cross_remote`、`moves_on_target`、`deletes`、`skipped_by_reason`（`{reason_code: count}`）。`skipped` 的項目在英文 `reason` 旁新增 `code`（`READ_ONLY`、`AMBIGUOUS_PATH`、`NATIVE_DOC`、`PROBABLE_ONLY`、`QUARANTINE`、`LAYOUT_TAKEN`、…：建構器寫出的每一種不同原因各一個）。

## B. 服務（`service.py`）

- `Service.estimate_plan(kind, params, *, sample: int = 50) -> dict`：參數處理和 `propose_plan` 相同（保留參數會被拒絕、彈出 `rules`、注入隔離區目錄），建構但不寫入任何東西。回傳 `{"kind", "ops": n, "stats", "skipped": [first 200], "sample": [first `sample` ops as dicts {op, src: {remote, path}, dst?: {remote, path}, witness?: {remote, path}, size, reason}], "nothing_to_do": bool}`。`dedupe` 另外回傳 `"groups": [...]`，以不傳給建構器的 `group_offset` / `group_limit`（預設 0 / 50）分頁：每筆是 `{"key", "size", "copies": [{"entry_id", "remote", "path", "mtime", "keep": bool, "remove": bool, "usable": bool, "why_not": code | null}], "reclaim": bytes, "skipped": bool}`，順序為建構器處理群組的順序。測試要斷言 `conn.total_changes` 沒有變動。
- 沒有任何操作時，`propose_plan` 的結果新增 `"code": "NOTHING_TO_DO"`（英文 `message` 保留）。
- `Service.plan_summaries(status=None, limit=100)`：清單用的資料列：`{"id", "kind", "title", "status", "created_by", "created_at", "simulated_at", "approved_at", "finished_at", "ops", "ok": bool | None, "problems": n, "params": {scrubbed}, "draft_id": int | None, "outdated": bool}`。`outdated` 表示上次模擬的快照和各遠端最新完成的掃描不同（目錄在那之後有變動；核准前請重新模擬）。

## C. HTTP（`api.py`、`ui_api.py`）


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

- 兩個應用程式都以 400 `{"error", "code", "params"}`（附代碼）回應 `BuildError`，讓頁面能用文字表達。
- `approval_code` 不會出現在任何其他回應裡，不論深度、不論呼叫者（把既有的清除機密測試延伸到新路由）。
- 核准非工作區的計畫已經走 `/v1/ui/plans/{id}/approve`；保留它的檢查。執行路由已經能處理任何計畫；完成的非工作區計畫不會動到草稿（不變）。

## D. MCP

沒有新工具：Claude 已經能透過 `propose_plan` 提出計畫。它建立的任何東西都加上 `created_by="mcp"`（現在就是這樣），讓頁面顯示徽章。

## E. 前端（`src/drivemerge/web/`）

規則和 WORKSPACE.md §F 與 EXPLORE.md §F 相同（無建置步驟、離線、CSP、兩種語言、由頁面用文字表達代碼、`data-testid`）。

- `plans.js`：清單、審閱、新增去重與新增整併的檢視；路由 `#/plans`、`#/plans/<id>`、`#/plans/new/dedupe?<params>`、`#/plans/new/consolidate?<params>`（表單狀態放在網址裡，讓「上一頁」與重新整理都能保留）。
- 從 `preview.js` 抽出報告的呈現（卡片、各雲端前後對照、附文字與顯示位置連結的問題）與執行／進度面板，放進 `report.js`，讓工作區預覽與計畫審閱共用。工作區預覽必須看起來與行為都和以前完全一樣（它的瀏覽器測試維持不變且通過）。
- `shell.js`：第五個側邊欄項目，附等待數徽章。`Alt+1…4` 維持原本的意義（總覽、探索、比較、工作區）；計畫是 `Alt+5`。
- 預估：防抖動 400 ms，顯示「預估中…」，取消過期的回應；去重的群組清單用「載入更多」分頁。
- 覆寫：點選非保留但可用的副本會設定 `keep[group] = entry_id`；不可用的副本會顯示原因（`why_not` 以文字表達）。表單會顯示它帶有多少覆寫與略過，並提供「重設」。
- 核准：和工作區相同的輸入確認碼對話；核准功能停用時，唯讀橫幅會顯示 `drivemerge plan approve <id>` / `drivemerge plan execute <id>`。
- 頁面可見時每 10 秒輪詢 `GET /v1/plans/summaries`（隱藏時不輪詢）；`created_by == "mcp"` 的新計畫會跳出附「審閱」的 Claude 提示。
- 總覽的新增項目（§6、§7）：橋接按鈕與對話框、通往各流程的連結。
- 新的 i18n 命名空間：`plans.*`、`dedupe.*`、`cons.*`、`bridge.*`、`skip.<CODE>`、`build.<CODE>`（BuildError 代碼）、`err.PLAN_CLOSED`、`plans.NOTHING_TO_DO`。計畫種類 `kind.<kind>`，狀態 `pstatus.<status>`。

## F. 測試

- 建構器：`keep` 與 `skip_groups`（包含 `BAD_KEEPER` 的每一種情況：沒有跨雲端時選了別的雲端的副本、在隔離區、原生文件、有歧義的路徑、不在群組裡）、每個 raise 位置的 `BuildError` 代碼、新的統計與略過代碼、沒有新參數時輸出不變。
- 服務：`estimate_plan` 不寫入任何東西（`total_changes` 不變；`plans` 沒有新增列）、群組分頁、`plan_summaries`（新的掃描之後 `ok`、`problems`、`outdated`）、`NOTHING_TO_DO`。
- HTTP：每個新路由在所有掛載它的應用程式上、驗證（bearer 對工作階段對人類通道）、`approval_code` 只給能核准的工作階段而從不給 bearer、`created_by` 的 `ui` 對 `api`、`PLAN_CLOSED`、附代碼的 `BuildError`、用假的 rclone 測試橋接工作忙碌／進度。
- i18n 合約：每個 `BuildError` 代碼、略過代碼與新的錯誤代碼，都在兩種語言裡有句子，且只使用伺服器送出的參數。
- 瀏覽器（模擬 API）：計畫清單分組與徽章、審閱呈現（共用的報告元件）、輸入確認碼的核准流程、執行進度、取消、唯讀橫幅、去重表單 ↔ 網址、預估防抖動與過期回應、保留副本覆寫與略過反映在建立請求的內容、`why_not` 的文字、整併表單（只有可寫入的目標、覆蓋率顯示內容還沒到目標時的清理區警告）、由輪詢觸發的 Claude 提示、橋接對話框、總覽連結、兩種語言。既有的工作區預覽測試必須原封不動通過。
- 整個測試套件維持全綠：`pytest`（包含瀏覽器測試）。
