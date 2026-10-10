# drive-merge 設計文件

[English](DESIGN.md) | **繁體中文**

> 狀態：v0.1 草案 — 第 12 節的「待決事項」是留給你調整的地方，每項都已先採用一個預設值，程式碼可以照預設先跑。

## 0. 一頁摘要

drive-merge 是一個**跑在本機**的服務，把 Google Drive、Dropbox、OneDrive、iCloud Drive 的**檔案中繼資料**（不是檔案本身）彙整到本機資料庫，之後：

1. 回答「這個檔案（內容）在哪幾朵雲上」；
2. 用可證明的方式找出重複（不只是同名）；
3. 依資料夾屬性與檔名產生「重新分類藍圖」，可由 CLI 或 LLM（Claude Code 透過 MCP）提出與調整；
4. 所有會改變雲端的動作都必須先成為一份 **Plan**，在資料庫中**模擬 before/after** 並通過安全不變式，再經**人類核准**後才執行；刪除一律先移到隔離區（可還原）；
5. 最終能把所有內容收斂、去重、集中到你指定的一朵雲（預設 Google Drive）。

rclone 負責所有雲端 I/O；drive-merge 負責目錄、身分判定、規劃、模擬與稽核。

## 1. 目標與非目標

**目標**
- 多雲統一目錄（catalog），可高速全文檢索（含中文檔名）。
- 跨雲內容身分判定，區分「已證明相同」與「疑似相同」。
- 可審閱、可重現、可還原的變更流程。
- 對外提供 REST API、MCP（給 LLM）、CLI；並提供變更串流（change feed）讓外部資料庫增量同步。

**非目標（v1）**
- 不做雙向同步（不是 Dropbox 替代品）。
- 不做影像相似度（近似重複照片）——列為後續階段。
- 不把檔案內容存進資料庫。

## 2. 第一性原理：決定架構的幾個事實

| 事實 | 後果 |
|---|---|
| **四朵雲沒有共同的 hash**：Drive = md5/sha1/sha256、Dropbox = dropbox content hash、OneDrive = QuickXorHash、iCloud = 無（rclone v1.75 overview） | 跨雲去重無法單靠清單比對。採 **hash bridging**：同一內容只下載**一份**，本機一次算出 5 種 hash，即可用其他雲各自原生的 hash 證明它們是同一內容，不必再下載它們。 |
| 「大小不同 ⇒ 內容必不同」 | 大小是免費的第一層篩選。只有「同大小且跨 hash 網域」的檔案才需要 bridging，下載量遠小於總量。 |
| **Drive 允許同一路徑有多個檔案** | 目錄中 entry 的身分是 provider ID（`entry_key = id:<ID>`），不是路徑；同路徑多檔會被標為 `AMBIGUOUS_PATH`，在解決前不允許對它下任何操作。 |
| Dropbox/OneDrive **不分大小寫**，Drive/iCloud 分 | 跨雲搬移會產生大小寫衝突；模擬器檢查 `CASE_COLLISION`。 |
| macOS/iCloud 檔名常為 **Unicode NFD** | 比對用 `name_norm = NFC + casefold`。 |
| **Google 原生文件**（Docs/Sheets）沒有位元組也沒有 hash | 標記 `is_native_doc`；不參與去重；不允許跨雲複製或刪除；在 Drive 內移動可以。 |
| **iCloud（rclone `iclouddrive`）** 屬實驗性後端：無 hash、無伺服器端複製/搬移、trust token 30 天到期、Photos 唯讀 | iCloud 視為「來源」而非目標；其檔案只能是「疑似重複」，直到被 bridging 證明。替代接入方式見 D7。 |
| rclone 共用的 Google Drive client_id **於 2026 年內停用** | 你需要建立自己的 Google OAuth client_id（README 有步驟）。 |
| **union 依路徑合併**：同路徑存在多朵雲時只呈現/讀取其中一份（預設 search policy `ff` = 誰先回應用誰） | union **不適合當目錄的資料來源**：它隱藏了「哪朵雲有這份」，而且同路徑不同內容時結果不確定。目錄改為**逐一 remote 掃描**，統一視圖在資料庫裡建立。要一個可瀏覽/掛載的單一視圖，用 **combine**（每朵雲是一個頂層資料夾，保留來源），`drivemerge rclone-combine` 可產生設定。union 只在最後階段有用（例如遷移期間提供唯讀合併視圖）。 |
| 檔名是**不可信輸入** | 雲端檔名可能含有對 LLM 的 prompt injection。LLM 只能「提案/模擬」，核准必須由人類在 TTY 進行（見 §7）。 |

## 3. 架構

```
        ┌────────────── You / Claude Code / other programs ──────────────┐
        │ CLI (drivemerge)    MCP server (stdio)    REST API (127.0.0.1) │
        └────────┬────────────────────┬──────────────────────┬───────────┘
                 │   (MCP has no approve/execute)            │
┌────────────────▼────────────────────▼──────────────────────▼───────────────────┐
│                         drivemerge core (stdlib only)                          │
│ scanner ─► catalog (SQLite + FTS5 trigram) ◄─ identity (union-find + bridges)  │
│ search / stats / change feed       classify (units, rules, profiles)           │
│ builders (dedupe / consolidate / reorganize) ─► plans ─► simulate              │
│                         approve (human, TTY) ─► executor ─► journal            │
└──┬─────────────────────────────────────────────────┬───────────────────────────┘
   │ read-only remote (scan / bridging download)     │ writable remote (executor only)
  rclone ──► Google Drive / Dropbox / OneDrive / iCloud
```

- **核心零第三方依賴**（Python 3.11+ 標準函式庫）：sqlite3、subprocess、tomllib。API 用 FastAPI、MCP 用官方 `mcp` SDK，皆為 optional extras。
- **最小權限**：每個 remote 在設定中分 `read` 與 `write` 兩個 rclone remote。掃描與 bridging 只用 `read`（建議以唯讀 OAuth scope 建立，例如 Drive `drive.readonly`），因此目錄階段**在物理上無法修改雲端**。只有 executor 會用 `write`，沒設定 `write` 的雲就是唯讀。

## 4. 資料模型（`src/drivemerge/schema.sql`）

| 表 | 用途 |
|---|---|
| `remotes` | 對應 config 中的 `[[remotes]]`（provider、read/write remote、是否不分大小寫） |
| `scans` | 每次掃描一筆；只有 `complete` 的掃描會把沒看到的檔案標為 tombstone |
| `entries` | 每個檔案/資料夾一筆：路徑、名稱、`name_norm`、副檔名、類別、大小、mtime、MIME、provider ID、5 種 hash、`content_group`、`present`、`row_version` |
| `entries_fts` | FTS5 **trigram** 索引（name, path）：子字串搜尋、支援中日文；少於 3 字元的查詢退回 LIKE |
| `hash_bridges` | 由本機下載計算的 5 種 hash（一列 = 一份內容），把不同 hash 網域串起來 |
| `plans` / `plan_ops` | 計畫與其操作（copy / move / delete），寫入後不可變 |
| `simulations` | 每次模擬的完整報告（JSON）與 digest |
| `journal` | 只增不改的稽核紀錄：核准、每次實際雲端操作、錯誤 |

**增量掃描**：upsert by `(remote_id, entry_key)`；只有「可觀察的變化」才遞增 `row_version`，所以重掃一個沒變的雲端不會產生任何變更事件。內容（大小/hash）改變時 `content_group` 清空，等待重算。

## 5. 內容身分與去重

信心等級：

| 等級 | 判定 | 可以作為刪除依據？ |
|---|---|---|
| **exact** | 同大小且共享任一同類 hash 值（遞移），或經 `hash_bridges` 連通 | 可以 |
| **probable** | 同大小 + 同 `name_norm`，但沒有 hash 證明（例如 iCloud） | **不行**（預設）；需先 bridging 升級為 exact |
| （size-only） | 只有大小相同 | 僅作為 bridging 候選 |

演算法：對 live 檔案做 union-find，邊為 `(hash 類型, 值, 大小)` 相同，以及 bridge 列連接的所有 hash；`content_group` = 連通分量中最小的 entry id（決定性）。百萬級 entries 以 SQL 排序後線性合併，不做 O(n²) 比對。

**Bridging 工作流**（只讀）：`bridge_candidates` 找出「同大小且跨 hash 網域、尚未被證明」的類別，每類挑一個最便宜下載的副本，依「可省空間 = 大小 × 未決副本數」排序，受 `--max-bytes` 預算限制；下載 → 本機一次算 5 種 hash → 驗證與該檔原生 hash 一致 → 寫入 `hash_bridges` → 重算群組。

**保留哪一份（keeper policy，D5）**：目標雲 > `prefer_remotes` 指定的雲（依序）> 非「副本樣式」路徑（`Copy of`、`(1)`、`conflicted copy`、`副本`…）> 提供者（drive > onedrive > dropbox > local > other > iclouddrive：有原生 hash 與伺服器端搬移的副本最便宜驗證、也最便宜保留，靠 bridging 才證明的 iCloud 副本墊底）> 較淺路徑 > 較早 mtime > 較小 entry id。

## 6. 重新分類與歸納

觀察：**只按副檔名分類會破壞脈絡**（一個專案資料夾裡的 PDF 與圖片應該在一起）。因此藍圖以「單元（unit）」為操作對象：

1. **原子資料夾（unit）**：含 `.git`、`package.json`、`*.xcodeproj` 等標記的資料夾，或本身是 bundle（`*.pages`、`*.key`、`*.photoslibrary`…）；整個搬移、保留內部結構。最外層優先。
2. **散檔**：依規則（TOML）逐一放置。規則可比對 `category`、`ext`、`path_glob`、`name_regex`、大小、來源雲；第一條命中者生效。目標路徑是模板，例如 `Photos/{year}/{year}-{month}/{name}`；日期優先取自檔名（`IMG_20190304_…`、`2019-03-04 …`），其次 mtime。
3. 沒命中的檔案走 `fallback`（預設 `Unsorted/{remote}/{path}`，保留原結構）——**不確定就不亂動**。

**LLM 的角色：提出「規則」，不是逐檔操作。** LLM 看到的是 **folder profiles**（每個資料夾的檔案數、大小、類別分布、前 5 副檔名、日期範圍、5 個樣本檔名、是否為 unit、重複比例），而不是百萬筆檔名。它提出規則 → 服務以決定性方式套用 → 產生 Plan → 模擬 → 你審閱。好處：可擴展、可重現、可審計，且 LLM 看不到也碰不到雲端。

## 7. 安全模型：Plan → Simulate → Approve → Execute

**任何寫入雲端的動作都只能來自已核准的 Plan。** 沒有例外路徑。

### 7.1 模擬（`simulate.py`）
把 ops 依序套用在「目錄 + 記憶體覆蓋層」上（不寫資料庫），檢查：

| 不變式 | 意義 |
|---|---|
| `CONTENT_LOST` | 任何在計畫前存在的內容，計畫後至少保留 `min_copies` 份。**刪掉最後一份 = 拒絕。** |
| `NO_WITNESS` / `WITNESS_MISMATCH` | 每個 delete 必須指名一個「見證副本」，其內容與被刪檔 **exact** 相同，且在整個計畫結束時仍存在（防止 A 以 B 為證、B 以 A 為證而雙雙刪除）。 |
| `DST_EXISTS` | 永不覆寫不同內容；同內容則記為 NOOP 警告。 |
| `CASE_COLLISION` | 不分大小寫的雲上大小寫衝突。 |
| `AMBIGUOUS_PATH` | Drive 同路徑多檔，不可操作。 |
| `SRC_MISSING` / `EXPECT_MISMATCH` | 來源不存在或與計畫建立時的快照不符。 |
| `NATIVE_DOC` | 原生文件不可跨雲複製或刪除。 |
| `READ_ONLY_REMOTE` / `CROSS_REMOTE_MOVE` / `BAD_PATH` / `DEPENDENCY` / `QUOTA` | 權限、路徑、相依順序、容量（注意：隔離區不釋放容量）。 |

報告含每朵雲 before/after 的檔案數與位元組、跨雲傳輸量、內容數 before/after、變更樣本。

### 7.2 核准
- 核准綁定 **digest = sha256(plan id + 所有 ops + 目錄快照)**。任何重掃（快照改變）或重新模擬都會讓核准失效。
- 必須輸入 digest 前 8 碼確認，且 **CLI 只在互動式 TTY 接受核准**。
- **MCP server 不提供 approve/execute 工具**；repo 內的 `.claude/settings.json` 也拒絕 Claude Code 執行 `drivemerge plan approve` / `execute` / `purge`。
- 總開關 `safety.execution_enabled = false`（預設），未開啟時 executor 拒絕執行。

### 7.3 執行（`executor.py`）
- 逐 op、遇錯即停（fail-closed）。每個 op 執行前**即時 stat** 來源，大小/hash 與計畫快照不符就停止。
- **copy**：`rclone copyto --ignore-existing`，完成後驗證目的地；跨 hash 網域時用 bridge 或下載目的地重算來驗證，**驗證通過前，依賴它的 delete 不會執行**。
- **delete = 移到同一朵雲的隔離區** `.drive-merge-quarantine/<plan-id>/<原路徑>`（伺服器端搬移，可還原；`drivemerge plan restore` 一鍵還原）。真正刪除是另一個指令 `purge`，預設保留 30 天，同樣需要 TTY 核准。
- 每一步寫入 `journal`。
- **隔離區內的檔案不算 live 內容**：重掃會看到它們，但身分判定（群組、重複清單、bridging 候選）與資料夾建議一律略過，所以被清掉的副本不會又變成「重複檔」。它們仍計入檔案數與容量。隔離資料夾名稱（`[safety] quarantine_dir`）由 `sync_remotes` 記在 catalog 的 `meta`，讓只拿到連線的模組也能一致判斷。

### 7.4 LLM 與 prompt injection
LLM（Claude Code、其他聊天介面）能做的：搜尋、看統計與 folder profiles、提出規則、建立 Plan、模擬、解釋報告。不能做的：核准、執行、purge。即使雲端裡某個檔名試圖誘導 LLM，最壞結果是一份**提案**，它仍需通過模擬並由你在 TTY 核准。

## 8. 收斂到單一雲端（預設 Google Drive）

為什麼 Drive 適合作為目標（D1）：它是四者中唯一提供標準 hash（md5/sha256）的，因此**最適合作為驗證錨點**；有伺服器端搬移；原生文件本來就在 Drive。缺點：允許同路徑多檔（我們以模擬器阻擋）、容量需自行評估。

分階段流程：

1. **掃描**全部雲端 → `identity rebuild` → 檢視 `stats`、`duplicates`。
2. **Bridging**（只讀、有預算）把跨雲疑似重複升級成 exact。
3. **藍圖**：`folder profiles` → 你或 Claude 調整規則 → 預覽 placement。
4. **Consolidate（只複製）**：對每份內容，若目標雲已有 → 視需要在 Drive 內搬到藍圖位置；否則從最佳來源複製到 Drive 的藍圖路徑。預設 `delete_sources = false`。
5. **驗證期（soak）**：重掃 Drive、重算身分、確認每份內容都在 Drive（覆蓋率報告）。建議至少 1–2 週。
6. **來源清理**：產生只含 delete 的 Plan，每個 delete 的見證都是 Drive 上的副本 → 模擬 → 核准 → 移到各雲的隔離區。
7. **Purge**：保留期過後清空隔離區；最後你可以關掉其他雲的訂閱。

可分批（依資料夾或依雲）建立 Plan，每批獨立模擬與核准。

## 9. 介面

- **CLI** `drivemerge`：`init`、`remotes`、`scan`、`identity rebuild`、`bridge`、`search`、`stats`、`duplicates`、`profiles`、`rules check/preview`、`plan dedupe|consolidate|reorganize|show|simulate|approve|execute|restore|purge`、`changes`、`serve`、`mcp`、`rclone-combine`。
- **REST API**（只綁 127.0.0.1，Bearer token）：`/v1/stats`、`/v1/search`、`/v1/entries/{id}`、`/v1/entries/{id}/locations`、`/v1/duplicates`、`/v1/profiles`、`/v1/changes`、`/v1/plans`（建立/列出/模擬/報告）。核准與執行**不經 API**（只能在 CLI/TTY）。
- **MCP（給 Claude Code）**：`search_files`、`catalog_stats`、`find_duplicates`、`file_locations`、`folder_profiles`、`validate_rules`、`preview_layout`、`propose_plan`、`simulate_plan`、`get_plan`。

## 10. 同步到外部資料庫

本機 SQLite 是 source of truth；外部資料庫（Postgres、Supabase、Elasticsearch…）以 **change feed** 增量拉取：`GET /v1/changes?cursor=<row_version:id>`，依 `(row_version, id)` 排序，含 tombstone，因此消費端也能得知刪除。這是 pull 模式：外部系統掛掉不影響本機，也不需要給本機服務任何外部資料庫的憑證。

## 11. 規模估計（100 萬檔案）

- 目錄：每列約 0.5 KB → SQLite 約 0.5–1 GB（含索引與 trigram FTS）。
- 掃描：Drive 用 `--fast-list` 最快；Dropbox/OneDrive 無 ListR，遞迴列表較慢（數十分鐘級）；iCloud 最慢。掃描可分子路徑進行。
- 身分重算：線性，秒到十秒級。
- Bridging：只下載「同大小跨網域」的候選，一份內容一次，並有預算上限。

## 12. 待你決定的事項（已套用預設值，可隨時調整）

| # | 議題 | 預設 | 其他選項 / 取捨 |
|---|---|---|---|
| D1 | 最終目標雲 | Google Drive | OneDrive（Microsoft 365 容量划算，但只有 QuickXorHash）；Dropbox |
| D2 | 主資料庫 | 本機 SQLite + change feed | 直接寫 Postgres（多一個服務要維運；但多裝置可共用） |
| D3 | 刪除語意 | 同雲隔離資料夾（可還原，但**不釋放容量**直到 purge） | 各雲原生垃圾桶（Drive 30 天、Dropbox 30–180 天、OneDrive 30 天）：釋放較快但還原依賴各家 |
| D4 | 隔離保留期 | 30 天 | 任意；purge 需 TTY 核准 |
| D5 | 重複時保留哪份 | 目標雲 > 非副本樣式路徑 > 較淺路徑 > 較舊 mtime | 例如「保留最新」或「保留特定雲」 |
| D6 | 分類藍圖 | `src/drivemerge/default_rules.toml`（照片按年月、文件、專案單元、其他保留原結構） | 由你或 Claude 依 folder profiles 改寫 |
| D7 | iCloud 接入 | rclone `iclouddrive`（實驗性、30 天重新登入） | 在 Mac 上掃描本機 iCloud Drive 資料夾（`local` 後端，需關閉「最佳化 Mac 儲存空間」以免讀到佔位檔） |
| D8 | LLM 介面 | MCP server（Claude Code） | 另加 Web UI 聊天；或只用 CLI |
| D9 | 疑似重複可否刪 | 否，需 bridging 證明 | 允許（不建議） |
| D10 | Google 原生文件 | 保留在 Drive、只允許 Drive 內移動 | 匯出成 Office 格式再處理 |
| D11 | Drive 捷徑 / 與我共用 | 掃描時略過（`--drive-skip-shortcuts`） | 納入（容易重複計算） |
| D12 | 執行併發 / 頻寬 | 逐一執行、可設 `--bwlimit` | 併發傳輸（較快，較難推理失敗狀態） |
| D13 | 在網頁介面核准/執行 | 允許，但僅限由互動式終端機啟動的 `drivemerge ui` 的瀏覽器 session（一次性網址 token + cookie + CSRF + 8 碼確認） | 只允許終端機 `drivemerge plan approve/execute`（最保守，但要切換視窗） |
| D14 | 同雲搬移整個資料夾 | v1 逐檔 move（簡單、每檔可驗證） | 新增「資料夾伺服器端一次搬移」op：Drive 上快很多，但模擬器與驗證需支援資料夾層級 |

## 13. 實作分期

| 階段 | 內容 | 狀態 |
|---|---|---|
| P0 基礎設施 | schema、掃描、hash（含 QuickXor/Dropbox）、身分判定、bridging、搜尋/統計/change feed、分類引擎、Plan 儲存、模擬器、CLI/API/MCP（唯讀 + 提案/模擬） | 本 PR |
| P1 執行器 | 前置檢查、隔離區刪除、複製驗證、還原、journal；預設關閉 | 本 PR（預設停用） |
| P2 收斂 | consolidate builder 實戰、覆蓋率報告、分批 | 本 PR 有 builder；實戰需你的雲端憑證 |
| P3 介面 | 視覺化工作區：左右雙窗格拖放、暫存/復原、建議、預覽、核准與執行進度（見 §15、WORKSPACE.zh-TW.md） | 本 PR |
| P4 進階 | 照片 EXIF 日期、近似重複影像（perceptual hash）、外部 DB 同步器 | 待定 |

## 14. 風險與已知限制

- **iCloud 後端不穩定**：Apple API 變動可能讓掃描失敗；掃描失敗不會產生 tombstone，所以不會誤判檔案消失。
- **雲端最終一致性**：剛寫入的檔案可能暫時列不到；executor 以「即時 stat + 驗證」為準，不以清單為準。
- **跨雲複製會經過本機頻寬**（rclone 串流，不落地）；大量遷移需時間與頻寬規劃。
- **Drive 同路徑多檔**：需先用 `rclone dedupe` 或人工處理，drive-merge 不會對其操作。
- **隔離區佔用容量**：收斂期間目標雲需預留空間；模擬報告的 `QUOTA` 檢查會提示。

## 15. 視覺化工作區（詳見 [WORKSPACE.zh-TW.md](WORKSPACE.zh-TW.md)）

左右兩個樹狀窗格（可各自選「所有雲端」或單一雲），在任意階層之間拖放檔案/資料夾；每次拖放只是**暫存動作**，可復原/重做、可來回拖，最後只計算**淨效果**。窗格預設顯示「變更後」的虛擬樹，也能切回「變更前」或顯示每樣東西的原位置。

- 同名資料夾 = 合併；合併時內容相同的檔案自動變成「保留一份＋有見證的去重」，內容不同的同名檔案列為衝突（永不覆寫）。
- 跨雲拖放 = 複製 → 驗證 → 來源移入隔離區。
- **預覽** 把草稿轉成 Plan 並跑同一個模擬器；草稿再有任何變動，預覽即失效。
- **建議**：自動找出散落各處的相關資料夾（名稱詞彙、內部檔名、共享內容）與內容幾乎相同的重複資料夾，一鍵加入暫存；Claude 也可透過 MCP 把搬移加入同一份草稿（但不能核准）。
- **核准**：只有由終端機啟動的 `drivemerge ui` 瀏覽器 session 可以核准與執行（D13）。

