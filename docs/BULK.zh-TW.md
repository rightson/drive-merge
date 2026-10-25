# 從探索進行批次操作（Bulk Actions from Explore）

[English](BULK.md) | **繁體中文**

> 本文件把**探索（Explore）**檢視（[EXPLORE.zh-TW.md](EXPLORE.zh-TW.md)）從回答「它在哪裡？」的地方，變成可以直接動手的地方：選取結果（手動挑選，或是符合查詢的每一筆內容），然後把它們**集中（gather）**到同一個資料夾（暫存在已開啟的工作區草稿中，見 [WORKSPACE.zh-TW.md](WORKSPACE.zh-TW.md)），或**清理它們的重複檔**（一份只限於所選內容的去重計畫，見 [PLANS.zh-TW.md](PLANS.zh-TW.md)）。探索也會標出已開啟的草稿正在搬動的副本。前半是設計；後半（實作合約）對實作者具有約束力。

## 1. 問題

探索能用一次查詢找出幾個重疊雲端裡散落的資料：`type:pdf thesis`、`cat:image modified:2019 copies:>=2`、`only:dropbox`。但接下來要做的事，仍然是一次處理一筆結果，或是繞一大圈：

- **搬動查詢找到的東西，得一個檔案拖一次。**「把每份論文 PDF 都放進 `gdrive:School/Thesis`」意味著要在工作區逐一顯示每個副本再拖過去。五十筆結果就是五十次手勢、五十個復原步驟，而且很可能漏掉一個。
- **清理查詢找到的東西，目前做不到。**一筆結果在同一個雲端有多個副本時，會提供「在此去重」，它打開的去重表單範圍是那個雲端與容納這些副本的*資料夾*。沒有辦法說「移除*這* 312 張照片多餘的副本」，只能對資料夾底下的全部動手，而那常常比使用者看到的還多。
- **探索不知道草稿的存在。**暫存一個搬動之後，探索仍然顯示上次掃描時那個副本所在的位置，完全沒有提示它已經在前往別處的路上（EXPLORE.md §7）。
- **Claude 無法依內容查詢。**MCP 有 `search_files`（項目），卻沒有探索（附帶各副本的內容），所以像「把這些集中起來」這樣的提案，只能一個檔案一個檔案地拼湊。

本功能要回答的問題：

1. **我能對這次查詢找到的所有東西動手嗎？**選取結果，或選取全部；看看選取內容含有什麼（內容數、副本數、位元組數，以及各雲端的分布），然後一次動手。
2. **我能把它們放在同一個地方嗎？**一個暫存的步驟，把每個所選內容的一個副本搬進一個資料夾，像任何一次拖放一樣先預覽、再核准。
3. **我能只移除*這些*內容多餘的副本嗎？**一份去重計畫，它的群組就是所選的內容，在計畫檢視中估算、審閱並核准。

## 2. 原則

- **沒有新的方式可以更動雲端。**集中只會在已開啟的草稿裡暫存一個一般的 `move` 動作（與拖放使用同一個 `workspace.add_action`，同樣的檢查、警告與復原）；草稿只有透過工作區預覽才會變成計畫，而計畫要經過模擬，並由人輸入計畫代碼核准，一如既往。清理則是用同一個建構器建出一份一般的 `dedupe` 計畫，只是限定在所選的群組。兩條路徑都終結於既有的模擬 → 核准（僅限人類）→ 執行流程。
- **只對看過的東西動手。**頁面在每個動作之前，都會顯示解析後的選取內容（多少內容、副本與位元組）；對「每一筆符合的結果」下的動作會帶著使用者看到的總數，如果重新掃描改變了答案，伺服器會拒絕（`SELECTION_CHANGED`），而不是對另一組東西動手。
- **只能縮小，絕不擴大。**選取只能*限縮*建構器原本會做的事。對選取內容去重，就是同一套去重（相同的保留策略、相同的集區、相同的見證檔），只是群組更少；集中最多只搬動每個內容的一個副本，並且絕不碰其他副本。
- **保守的預設值。**集中時採用已經在目的地雲端上的副本。從另一個雲端把內容帶過來（跨雲端搬動：經由這台機器複製，原檔再進隔離區）需要使用者主動選擇，並且會明說。去重則維持「在各雲端內」作為預設。
- **由頁面負責措辭。**伺服器只送出代碼與參數（略過原因、拒絕原因）；英文與繁體中文的句子由頁面撰寫。
- **唯讀退路。**本功能的任何部分都不需要人類通道：選取、集中到草稿、建立去重計畫都只是提案。核准功能停用時，頁面的行為相同，而工作區預覽與計畫審閱一如既往，以 `drivemerge plan approve <id>` 與 `drivemerge plan execute <id>` 收尾，而不是按核准按鈕。

## 3. 資訊架構

不新增導覽列項目。探索新增一個選取欄與一條選取列；去重表單新增一個選取橫幅。

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

- **選取欄**：每筆結果一個核取方塊（一筆結果就是一個內容連同它所有的副本）。表頭的核取方塊會選取已載入的那一頁；當已載入的每筆結果都被選取、而且還有更多符合時，會出現一行提供**選取全部 N 筆符合者（Select all N matching）**。在這個模式下，取消勾選某筆結果就是把它排除（最多 500 個排除），載入更多頁時，新列會是勾選狀態。
- **選取列**（黏在結果下方，有選取時顯示）：所選內容的內容數、副本數與位元組數（按雲端分列），以及其中多餘的副本；由伺服器依選取內容計算（§B），所以「全部符合」顯示的是精確數字，而不只是已載入的那一頁。動作有：**集中到資料夾…（Gather into a folder…）**、**清理重複檔…（Clean up duplicates…）**（選取內容中沒有任何內容有兩個副本時停用，並附工具提示），以及**清除（Clear）**。
- 選取內容屬於查詢本身：依查詢文字，在這次造訪期間保留（回到同一個查詢會還原它），查詢改變時清除；清除時選取列會告知。手動挑選的選取內容最多 500 個內容；「全部符合」最多 5,000 個。
- **暫存標記**：當已開啟的草稿搬動某個副本時，展開結果中該副本那一行會顯示 `⇢ staged: <草稿放置它的位置>`，結果列上會有一個小小的「已暫存」徽章。探索的其餘部分仍然讀取 catalog（§8）。

## 4. 集中到資料夾

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

流程：

1. 使用者選擇目的地資料夾：一個雲端（僅限可寫入的雲端）與一個資料夾，資料夾以草稿的後視圖（`GET /v1/tree?draft=`）自動完成，所以剛才暫存的資料夾也能當作目標。**新增資料夾…（New folder…）**會先暫存一個 `mkdir`（自己是一個復原步驟，與工作區相同）。
2. 每次更改目的地或選項，都向伺服器要一次試跑（`POST /v1/drafts/<id>/gather`，帶 `dry_run: true`，並做防抖）。伺服器為**每個內容挑選一個副本**：有的話挑目的地雲端上的副本（有數個時由保留策略決定）；開啟「也帶入其他雲端的內容」時，沒有的話挑別處最好的副本。然後回報會搬動什麼（數量、位元組、跨雲端的部分、範例）、哪些**已經在那裡**（該內容的某個副本已經直接位於那個資料夾），以及哪些被**略過**及原因：該雲端上沒有副本、該資料夾裡名稱已被占用、兩個所選內容會得到相同名稱、唯讀雲端、跨雲端的原生文件、有歧義的 Drive 路徑、自選取之後已不存在。
3. **暫存到草稿（Stage in the draft）**送出相同的請求，但不帶 `dry_run`。伺服器暫存**一個** `move` 動作，內含每個挑出的副本（一個復原步驟，備註為 `explore:gather`），在暫存抽屜中措辭為「Gather N files into <資料夾>」。吐司通知提供**開啟工作區（Open workspace）**。暫存警告（跨雲端、唯讀、大小寫衝突）的顯示方式與拖放相同。
4. 之後就是工作區的路徑：預覽、模擬、輸入代碼核准、執行、重新掃描。集中的檔案其他副本原封不動；吐司與對話框都會指向「清理重複檔…」。

名稱維持原樣：集中絕不重新命名（兩個名為 `notes.txt` 的不同內容不能同時落在一個資料夾裡，所以第二個會以 `NAME_CLASH` 略過，使用者可以在工作區裡重新命名它）。

## 5. 清理所選內容的重複檔

**清理重複檔…**會打開既有的去重表單，並附上選取內容：

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

- 選取內容隨表單的 URL 傳遞（`#/plans/new/dedupe?selq=<查詢文字>&selx=<排除的鍵>&seln=<看到的總數>` 用於「全部符合」，`?selk=<鍵>` 用於手動挑選的），所以返回與重新載入都會保留它。
- 其他每個欄位都保持原本的意義與保守的預設值；它們會進一步限縮建構（路徑限制可被移除的範圍，雲端限制要精簡哪個雲端）。保留者覆寫與略過的群組如前運作，只在選取範圍內。
- 估算會說明有多少所選內容沒有精確的重複群組（未證明或單一副本：沒有可移除的東西），以及有多少已不存在。
- 如果自做出選取之後 catalog 已經改變，估算會顯示拒絕 `SELECTION_CHANGED`，並附上**改用目前的 N（Use the current N）**（更新 `seln`）與**回到探索（Back to Explore）**。
- **建立計畫（Create plan）**會把計畫連同選取內容與解析出的群組鍵存進它的 `params`（讓審閱能寫成「探索選取內容的去重（312 個內容）」，且計畫記錄了它確切涵蓋的範圍），然後打開它的審閱。核准與執行不變。

## 6. 暫存標記

`GET /v1/explore?draft=<id>` 會為草稿的有效步驟所搬動的每個副本，加上草稿放置它的位置（後視圖位置）。探索在副本那一行顯示它，並在結果上顯示徽章；選取列會計算所選副本中有多少已經暫存。這不是探索的後視圖（結果、存在狀態欄與篩選標籤仍然描述 catalog）；它足以看出集中的檔案正在路上，而不必再集中一次。

## 7. 交叉連結

- 探索選取列 → 集中（停留在探索，吐司通往工作區）與清理重複檔（去重表單）。
- 帶有選取內容的去重表單 → **回到探索**（`#/explore?q=<selq>`）。
- 計畫審閱與計畫清單：由選取內容建立的去重計畫，摘要讀作「探索選取內容的去重（N 個內容）」。
- 工作區暫存抽屜：集中步驟讀作「Gather N files into <資料夾>」；它的標題會顯示該資料夾，與每個步驟一樣。
- 每筆結果的「在此去重」按鈕保留（限定在某個雲端與資料夾）；選取內容則是依內容界定範圍的方式。

## 8. 已知限制（v1）

- 探索仍然讀取 catalog，而不是草稿的後視圖：只有草稿搬動的副本會被標記（§6）；結果、存在狀態與篩選標籤都停留在上次掃描時的樣子。
- 集中會攤平：每個挑出的檔案都直接落在目的地資料夾；其下的資料夾結構不會保留。它絕不重新命名；名稱衝突的會被略過。
- 集中只搬動每個內容的一個副本，且絕不複製（跨雲端的集中是一次搬動：複製驗證通過後，原檔進隔離區）。「複製、保留原檔」需要工作區裡的複製動作，而它並不存在。
- 查詢改變時選取內容會被清除；無法合併多個查詢的選取內容，選取內容也不會儲存（儲存查詢是未來的工作）。
- 限制：手動挑選 500 個內容、500 個排除、「全部符合」5,000 個內容、每次集中 2,000 個檔案。「全部符合」會拒絕探索會截斷的查詢（符合的檔案超過 20,000 個）：請先縮小查詢範圍。
- 疑似重複（沒有精確群組）絕不會被選取內容的去重移除，任何去重皆然；要證明它們，請用橋接（bridging）。
- 不支援匯出選取內容（CSV、路徑清單）。

---

# 實作合約（Implementation contract）

以下全部對實作者具有約束力。請先閱讀：[EXPLORE.zh-TW.md](EXPLORE.zh-TW.md) §A、§D、§E、§F，[WORKSPACE.zh-TW.md](WORKSPACE.zh-TW.md) §A、§C、§D、§G，[PLANS.zh-TW.md](PLANS.zh-TW.md) §A–§E，`explore.py`、`search.py`（`build_filter`）、`compare.py`（`CompareError`、`load_remotes`、`remote_named`、`chunks`）、`builders.py`（`build_dedupe`、`_group_overrides`、`choose_keeper`、`_ambiguous_paths`、`BuildError`）、`workspace.py`（`add_action`、`_space_for`、`_Space.fwd`、`_Cat`、`WorkspaceError`、`_stage_checked`）、`service.py`（`_build`、`estimate_plan`、`propose_plan`、`plan_summaries`、`explore`、`stage_action`）、`api.py`（`build_router`、有代碼的處理器）、`ui_api.py`（草稿路由、`_coded`）、`mcp_server.py`（`tool`、`stage_moves`、`propose_plan`）、`cli.py`（`cmd_plan_dedupe`），以及 `src/drivemerge/web/` 中的：`explore.js`、`plans.js`、`app.js`、`api.js`、`query.js`、`util.js`、`panels.js`、`compare.js`（`PathPicker`）。

## A. 定義

- **內容鍵（Content key）**：`g:<content_group>` 或 `e:<entry id>`，與 `explore.explore` 建構的完全一致（大小為 0 或沒有群組的檔案自成一個內容 `e:<id>`）。文法：`^(g|e):[1-9][0-9]{0,17}$`。
- **鍵的副本**：對 `g:N`，是每個 `content_group = N` 的有效檔案（`present = 1 AND is_dir = 0`，大小 > 0，位於隔離資料夾之外）；對 `e:N`，當該項目是隔離資料夾之外的有效檔案時，就是該項目。沒有副本的鍵是**已不存在（gone）**。
- **選取（Selection）**（JSON，任何路由或工具唯一接受的形式）：
  - 查詢模式：`{"query": {<explore params>}, "except"?: [keys], "expect_total"?: int}`；explore 參數就是 `GET /v1/explore` 查詢的名稱（`q`、`in`、`category`、`ext`、`min_size`、`max_size`、`modified_after`、`modified_before`、`path_prefix`、`on`、`not_on`、`only_on`、`copies_min`、`copies_max`），清單參數為 JSON 陣列；不允許 `sort`、`limit`、`offset`。
  - 鍵模式：`{"keys": [keys]}`。
- 限制（`explore.py` 中的模組常數）：`PICK_LIMIT = 500`（keys 與 `except`）、`SELECT_LIMIT = 5000`（解析出的內容）、查詢模式使用 `explore` 的 `scan_cap` 預設值（20,000）。`GATHER_LIMIT = 2000`（一次集中挑出的檔案）在 `gather.py`。
- **目錄位置的後視圖位置（After-view location）**：已開啟草稿的有效且未過時的步驟放置它的位置（`_Space.fwd`）；沒有步驟碰到它時不變。

## B. 選取（`src/drivemerge/explore.py`）

```python
class SelectionError(CompareError): code: str; params: dict
SELECTION_CODES = ("BAD_SELECTION", "BAD_KEY", "TOO_MANY_KEYS", "SELECTION_EMPTY", "SELECTION_TOO_LARGE",
                   "SELECTION_TRUNCATED", "SELECTION_CHANGED")
def select(conn, selection: dict, *, limit: int = SELECT_LIMIT, with_keys: bool = False) -> dict
```

- 重構 `explore()`，使命中查詢、分組與存在狀態／副本數篩選成為一個內部函式，回傳每個篩選後的群組（在排序與分頁之前）；`explore()` 與 `select()` 都使用它。`explore()` 的輸出及其測試保持不變。
- 查詢模式：解析查詢篩選後的群組（不分頁）；命中掃描被截斷時拋出 `SELECTION_TRUNCATED {scan_cap}`；移除 `except` 的鍵；給了 `expect_total` 而且與排除後的數量不同時，拋出 `SELECTION_CHANGED {expected, total}`。鍵模式：驗證每個鍵、丟棄重複（先出現者優先）、計算已不存在的鍵。
- 驗證順序：形狀（`query` / `keys` 恰好一個；`except` 與 `expect_total` 只能搭配 `query`；未知的頂層鍵或查詢鍵）→ `BAD_SELECTION {param}`（有問題的欄位名稱，或查詢鍵）；`keys` / `except` 不是字串清單 → `BAD_SELECTION {param}`；長度超過 `PICK_LIMIT` → `TOO_MANY_KEYS {param, count, limit}`；格式錯誤的鍵 → `BAD_KEY {key}`（第一個）；查詢參數如同 `explore()`（未知的遠端 `NOT_FOUND`，值不良 `BAD_QUERY {param}`）；然後是解析錯誤：`SELECTION_TRUNCATED`、`SELECTION_CHANGED`、`SELECTION_TOO_LARGE {total, limit}`（解析出的內容超過 `limit`）、`SELECTION_EMPTY {}`（沒有任何有效內容剩下）。
- 結果：
```json
{"mode": "query", "total": 312, "grouped": 297, "ungrouped": 15, "gone": 0,
 "copies": 731, "bytes": 3120000000,
 "by_remote": [{"remote": "gdrive", "contents": 290, "copies": 640, "bytes": 0}],
 "redundant": {"copies": 419, "bytes": 0},
 "staged": null,
 "keys": ["g:7", "e:31"]}
```
  `total` = 解析出的有效內容；`bytes` 每個內容只算一次；`by_remote` 依設定順序，每個遠端都列出（包含零）；`redundant` = 每個內容第一個之後的副本（所有雲端），位元組同理；`keys` 只在 `with_keys` 時出現（依鍵的自然順序）；`staged` 由服務填入（§E），此處為 `null`。
- 唯讀：測試斷言 `conn.total_changes` 不變。每一批鍵只用一次批次查詢，不做逐鍵查詢。
- `explore(..., draft_locate=None)`：一個可選的可呼叫物件 `(list[(remote_id, path)]) -> list[(remote_id, path)]`，由服務提供；設定時，每個回傳的副本多一個 `"staged": {"remote", "path"} | None`（後視圖位置等於 catalog 位置時為 `None`），每筆結果多一個 `"staged": n`（它搬動的副本數）。沒有它時輸出不變。

## C. 對選取內容去重（`builders.py`、`service.py`）

- `build_dedupe(..., groups: Sequence[str] | None = None)`：給定時，只檢查鍵在 `groups` 裡的精確群組；其他一切（集區、保留策略、見證檔、`remote`、`cross_remote`、`path_prefix`、`min_size`、`keep`、`skip_groups`、`limit_groups`、`group_window`）對該子集的行為一如既往。`groups` 必須是最多 `SELECT_LIMIT` 個 `g:<int>` 字串的清單，否則是 `BuildError` `BAD_PARAM {param: "groups"}`。沒有指到任何現有精確群組的鍵會被忽略，並計入 `stats["groups_missing"]`。位於 `groups` 之外的 `keep` 或 `skip_groups` 鍵是 `BAD_GROUP {group}`（它沒有指到這次建構的任何群組，與現況相同）。沒有 `groups` 時，輸出逐位元組不變。
- `Service._build("dedupe", params)`：當 `params` 有 `selection` 時，以 `explore.select(conn, selection, with_keys=True)` 解析它（其 `SelectionError` 向上傳遞），把它的 `g:` 鍵作為 `groups` 交給建構器，並回傳原樣保留 `selection` 加上 `groups`（解析出的 `g:` 鍵）的 params。`selection` 與 `groups` 同時出現 → `BuildError` `BAD_PARAM {param: "selection"}`。`estimate_plan` 與 `propose_plan` 都會加上 `stats["selection"] = {"contents", "grouped", "ungrouped", "gone"}`。
- `Service.plan_summaries`：`params.groups` 是清單的資料列，以 `params.groups_count`（整數）取代它；`GET /v1/plans/{id}`、`get_plan`（MCP）與 CLI 保留完整清單。

## D. 集中（`src/drivemerge/gather.py`，新增；`workspace.py`）

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

- `locate`：草稿有效步驟下目錄位置的後視圖位置（`_space_for(_Cat(conn), active acts)` 然後 `fwd`）；草稿必須存在（`NOT_FOUND`），任何狀態皆可。不寫入。
- `plan_gather`（唯讀）：`dst = {"remote", "path"}` 必須是後視圖的一個資料夾（`DST_NOT_DIR`），不在隔離資料夾或其底下（`QUARANTINE`），且位於已知的遠端（`NOT_FOUND`）；草稿必須是開啟的（`NOT_OPEN`）。對每個鍵，依鍵的自然順序：
  1. 副本（§A）；沒有 → 略過 `GONE`。透過 `locate` 取得每個副本的後視圖位置。
  2. 某個副本的後視圖父資料夾等於 `dst`（同一遠端；不分大小寫的遠端上用 NFC+casefold）→ 略過 `ALREADY_THERE {name}`。
  3. 候選 = `dst.remote` 上的副本；若沒有：沒有 `cross_cloud` 時 → 略過 `NOT_ON_TARGET {remote: dst.remote}`；有它時，取每個副本，扣掉原生文件（若因此變空 → `NATIVE_DOC`）。丟掉唯讀遠端上的副本（變空 → `READ_ONLY {remote}`，第一個的遠端）以及有歧義的 Drive 路徑上的副本（變空 → `AMBIGUOUS_PATH {remote, path}`）。
  4. 挑選 `builders.choose_keeper(candidates, target=dst.remote, prefer_remotes=prefer_remotes)`。
  5. 目標 = `dst.path/<挑中副本的後視圖名稱>`：後視圖中已被占用 → 略過 `NAME_TAKEN {name}`；等於（在不分大小寫的目的地上做 casefold 後）先前某個挑選的目標 → 略過 `NAME_CLASH {name, with: {remote, path}}`（先前那個挑選的來源）。
  
  結果：
```json
{"contents": 2, "files": 1, "bytes": 4100000, "cross_cloud": {"files": 0, "bytes": 0},
 "picked": [{"key": "g:9", "from": {"remote": "gdrive", "path": "Downloads/thesis-v9.pdf"},
             "to": {"remote": "gdrive", "path": "School/Thesis/thesis-v9.pdf"}, "size": 4100000, "cross_cloud": false}],
 "skipped": {"by_code": {"ALREADY_THERE": 1},
             "items": [{"key": "g:7", "name": "thesis_final.pdf", "code": "ALREADY_THERE", "params": {"name": "thesis_final.pdf"}}]},
 "items": [{"remote": "gdrive", "path": "Downloads/thesis-v9.pdf"}]}
```
  `from` 是後視圖位置（搬動動作暫存的內容）；`picked` 與 `skipped.items` 在回答中以 200 為上限（計數是精確的）；`items`（完整清單，內部使用）是 `gather` 暫存的內容，不會透過 HTTP 送出。挑出的檔案超過 `GATHER_LIMIT` → `GATHER_TOO_MANY {files, limit}`。
- `gather`：先 `plan_gather`，然後在沒有挑出任何東西時 `NOTHING_TO_GATHER {contents, skipped}`；否則在同一個鎖內 `workspace.add_action(conn, draft_id, {"kind": "move", "items": items, "dst": dst, "note": note or "explore:gather"}, created_by=created_by)`，並回傳計畫結果（不含 `items`）加上 `"action"`、`"draft_version"`、`"warnings"`。`add_action` 的 `WorkspaceError` 原封不動向上傳遞（`plan_gather` 之後不該發生；測試會在兩者之間讓 catalog 改變，並預期得到有代碼的拒絕，而不是部分完成的動作）。
- 效能：2,000 個鍵在一般筆電上規劃並暫存遠低於 2 秒（用產生出來的 catalog 測試，斷言 < 5 秒）。

## E. 服務、CLI、MCP

- `Service.select(selection, *, draft_id=None, with_keys=False)`：`explore.select`，加上當 `draft_id` 指到已開啟的草稿時的 `staged = {"copies": n}`（草稿搬動的所選副本數）。
- `Service.explore(q, ..., draft_id=None)`：給了 `draft_id` 時，傳入綁定 `workspace.locate` 的 `draft_locate`（未知的草稿 → `WorkspaceError NOT_FOUND`；未開啟的草稿 → 沒有標記）。
- `Service.gather(draft_id, selection, dst, *, cross_cloud=False, prefer_remotes=(), dry_run=False, note=None, created_by)`：以 `with_keys=True` 解析選取內容，然後 `gather.plan_gather`（試跑）或 `gather.gather`；結果多一個 `"selection": {"total", "gone"}`。
- CLI：`drivemerge plan dedupe --group KEY`（可重複；傳入 `groups`）。CLI 沒有其他變動。
- MCP：
  - `explore_contents(query="", in_remote=None, on=None, not_on=None, only_on=None, copies_min=None, copies_max=None, category=None, ext=None, min_size=None, max_size=None, modified_after=None, modified_before=None, path_prefix=None, sort="relevance", limit=25, offset=0)`：唯讀；`limit` ≤ 100；每筆結果的副本上限 10 個並附 `copies_truncated`；英文；不可信資料的提示與 `search_files` 相同。
  - `select_contents(selection)`：唯讀；以 `with_keys=False` 呼叫 `Service.select`。
  - `gather_into_draft(draft_id, selection, dst_remote, dst_path, cross_cloud=False, dry_run=False, note=None)`：`writes=True`，`created_by="mcp"`；文件字串說明它只做提案（如 `stage_moves`），列出略過代碼與警告，並說明由人類預覽與核准。
  - `propose_plan` 的文件字串說明 `dedupe` 的 `params.selection`。
  - 沒有核准、執行或清除的工具（不變）。`SelectionError` 在 MCP 的使用者錯誤之中。

## F. HTTP

| 方法與路徑 | 驗證 | 內容／查詢 | 回應 |
|---|---|---|---|
| `GET /v1/explore` | normal (shared router) | as today plus `draft` (int) | as today; with `draft`, copies carry `staged` and results `staged` (§B) |
| `POST /v1/explore/selection` | normal (shared router) | `{"selection", "draft"?: int, "with_keys"?: bool}` | §B result (`staged` filled with `draft`) |
| `POST /v1/plans/estimate`, `POST /v1/plans` | normal | as today; `params.selection` for `dedupe` | as today; `stats.selection`; persisted `params.selection` and `params.groups` |
| `POST /v1/drafts/{id}/gather` | normal (UI app, like the other draft routes) | `{"selection", "dst": {"remote", "path"}, "cross_cloud"?: false, "prefer_remotes"?: [names], "dry_run"?: false, "note"?: str}` | dry run: 200 §D plan result + `selection`; else 201 the same plus `action`, `draft_version`, `warnings`; `created_by` `ui` for a session, `api` for the bearer token |

- 有代碼的錯誤，兩個應用程式中皆為 `{"error", "code", "params"}`：`SelectionError` 經由 `CompareError` 處理器，它把 `NOT_FOUND` → 404，`BAD_QUERY`、`BAD_SELECTION`、`BAD_KEY`、`TOO_MANY_KEYS` → 400，其他選取代碼 → 409。集中的拒絕是 `WorkspaceError`（409，`NOT_FOUND` 404，`BAD_REQUEST` 400），代碼有 `NOT_FOUND`、`NOT_OPEN`、`DST_NOT_DIR`、`QUARANTINE`、`NOTHING_TO_GATHER`、`GATHER_TOO_MANY`。集中的順序：請求內容形狀（`BAD_REQUEST` 422/400）→ 草稿（`NOT_FOUND`、`NOT_OPEN`）→ 選取錯誤 → 目的地（`NOT_FOUND`、`QUARANTINE`、`DST_NOT_DIR`）→ `GATHER_TOO_MANY` → `NOTHING_TO_GATHER`。
- 這些回應都不帶 `approval_code`、摘要值或快照（擴充清除機密的測試）。本功能沒有任何路由位於人類通道上。

## G. 前端（`src/drivemerge/web/`）

規則與 WORKSPACE.zh-TW.md §F、EXPLORE.zh-TW.md §F 和 PLANS.zh-TW.md §E 相同（不需建置步驟、可離線、CSP、兩種語言、代碼由頁面措辭、測試會碰的東西加上 `data-testid`）。

- `selection.js`（新增）：選取模型，純粹且可測試：`{mode: 'keys'|'query', keys: Set, except: Set, query, seen}`；`toggle(key)`、`selectPage(keys)`、`selectAllMatching(params, total)`、`clear()`、`body()`（§A 的 JSON）、`toDedupeParams()` / `fromDedupeParams(urlParams)`（`selq`、`selx`、`seln`、`selk`）、每個查詢文字在這次造訪期間的記憶；強制 `PICK_LIMIT`（第 501 個挑選會以 `sel.limit` 拒絕）。
- `gather.js`（新增）：`GatherDialog`（可寫入雲端的目的地雲端下拉選單、帶後視圖自動完成的資料夾輸入框——重用 `compare.js` 的 `PathPicker` 並傳入 `draft`、透過 `ctx.stageMkdir` 的**新增資料夾…**、跨雲端核取方塊與其警告、防抖 300 毫秒並防範過期回應的試跑、最多 20 筆搬動的範例、依代碼列出的略過清單、暫存按鈕）。
- `explore.js`：核取方塊欄、表頭核取方塊、「選取全部 N 筆符合者」那一行、選取列（來自 `api.selection`，每次變更後防抖 250 毫秒）、暫存標記（`api.explore` 帶 `draft: ctx.draft.id`）、開啟 `GatherDialog`，以及**清理重複檔…** → `ctx.nav(plansHash('/new/dedupe', selection.toDedupeParams()))`。
- `plans.js`：去重表單讀取 `selq`/`selx`/`seln`/`selk`（透過 `selection.fromDedupeParams`），顯示選取橫幅，在估算與建立時送出 `params.selection`，以**改用目前的 N**處理 `SELECTION_CHANGED`；`planSummary` 把帶有 `params.selection` 的去重措辭為 `plans.sum.dedupeSelection {n}`（`n` = `groups_count` 或 `groups.length`）。
- `app.js`：`ctx.stageGather(body)` 與 `stageMove` 排進同一個暫存佇列（呼叫 `api.gather`、顯示警告、`syncDraft`、附**開啟工作區**的成功吐司）。沒有其他變動。
- `util.js`：`describeAction` 把備註為 `explore:gather` 的搬動措辭為 `act.gather {n, dst}`；`panels.js` 不把該備註當作自由文字顯示。
- `api.js`：`selection(body)`、`gather(draftId, body)`；`explore` 傳遞 `draft`。
- `styles.css`：欄位、選取列、標記與對話框。
- `data-testid`：`ex-select-<key>`、`ex-select-page`、`ex-select-all-matching`、`ex-selection-bar`、`ex-sel-count`、`ex-sel-copies`、`ex-sel-bytes`、`ex-sel-remote-<remote>`、`ex-sel-staged`、`ex-sel-cleared`、`ex-sel-clear`、`ex-sel-gather`、`ex-sel-dedupe`、`ex-sel-error`、`ex-staged`、`ex-staged-badge`、`gather-dialog`、`gather-dst-remote`、`gather-dst-path`、`gather-new-folder`、`gather-cross`、`gather-cross-warning`、`gather-check`、`gather-sample`、`gather-skipped`、`gather-skip-<CODE>`、`gather-stage`、`gather-error`、`dd-selection`、`dd-selection-back`、`dd-selection-remove`、`dd-selection-changed`、`dd-selection-update`。
- i18n 命名空間（兩種語言）：`sel.*`、`gather.*`、每個 `gather.SKIP_CODES` 代碼的 `gather.skip.<CODE>`、每個 `explore.SELECTION_CODES` 與 `gather.CODES` 代碼的 `err.<CODE>`、`act.gather`、`plans.sum.dedupeSelection`。每個句子只使用伺服器送出的參數。

## H. 測試

- `tests/test_selection.py`（新增）：兩種模式；每個驗證代碼與 §B 的順序；`except`；重新掃描新增一個符合的檔案後 `expect_total` → `SELECTION_CHANGED`；小的 `scan_cap` 造成 `SELECTION_TRUNCATED`；`SELECTION_TOO_LARGE`；已不存在的鍵；空檔案與未分組檔案的 `e:` 鍵；`by_remote` 與 `redundant`；隔離區副本絕不計入；`explore()` 的輸出在有無 `draft_locate` 時都不變；`total_changes` 不變。
- `tests/test_builders.py`（擴充）：`groups` 連同其他每個參數，恰好限縮到那些群組；格式錯誤的群組 → `BAD_PARAM`；`groups_missing`；`groups` 之外的 `keep` / `skip_groups` → `BAD_GROUP`；沒有 `groups` 時輸出不變。
- `tests/test_gather.py`（新增）：每個略過代碼在其步驟上；目的地上與帶 `prefer_remotes` 的保留者選擇；只有開啟選項才會挑跨雲端的副本；原生文件與唯讀雲端；後視圖來源（草稿已搬動的副本從草稿放置它的位置暫存；由 `mkdir` 暫存的資料夾作為目的地）；不分大小寫的 `ALREADY_THERE` 與 `NAME_CLASH`；`GATHER_TOO_MANY`；`NOTHING_TO_GATHER`；一個動作含全部項目，一次復原即移除；`locate`；2,000 個鍵的計時。
- `tests/test_service_workspace.py`、`tests/test_plans.py`（擴充）：`Service.gather` 試跑不持久化任何東西；帶 `selection` 的 `propose_plan` 記錄 `selection` 與 `groups`；`plan_summaries` 把 `groups` 換成 `groups_count`；`selection` + `groups` → `BAD_PARAM`。
- `tests/test_bulk_api.py`（新增）：§F 的每個路由，在有掛載的兩個應用程式中，bearer 與 session，帶狀態的有代碼錯誤與集中的拒絕順序，`created_by`，清除機密。
- `tests/test_mcp_server.py`、`tests/test_cli.py`（擴充）：三個工具（唯讀提示、上限、`created_by="mcp"`、僅英文），帶選取內容的 `propose_plan`，`plan dedupe --group`。
- `tests/test_i18n_contract.py`（擴充）：`explore.SELECTION_CODES`、`gather.CODES` 與 `gather.SKIP_CODES` 的每個代碼，在兩種語言中都有句子，且只使用其參數。
- `tests/test_ui_bulk.py`（新增，瀏覽器，模擬 API：`test_ui_frontend.py` 中 `MockApi` 的子類別，如同 `test_ui_quarantine.py`）：核取方塊、整頁選取與帶排除的「全部符合」，以及選取列顯示的伺服器數字；查詢改變時清除、按返回時還原；500 的限制；集中對話框（試跑的請求內容、防抖與過期回應、每個略過代碼的措辭、跨雲端警告、新增資料夾、暫存的請求內容與吐司）；暫存步驟在抽屜中的措辭；暫存標記；清理重複檔 → 表單的 URL、橫幅、估算與建立時的 `params.selection`、`SELECTION_CHANGED` 與**改用目前的 N**；計畫摘要的措辭；核准停用時以上全部仍可運作，且計畫審閱顯示指令；兩種語言。
- `tests/test_ui_bulk_e2e.py`（新增，真正的 rclone、以本機目錄建立的真實 catalog、真實伺服器、真實 Chromium；重用 `test_ui_e2e.py` 的 `make_world` 與 `Page`）。情境，額外檔案有 `gd: Downloads/survey.pdf`（內容與 `Research/Papers/survey.pdf` 的位元組相同）與 `gd: Desktop/paper-a.pdf`：
  1. 開啟 `#/explore?q=type:pdf`；選取全部符合者；選取列顯示 3 個內容（在 Drive 上有 2 個副本的 `survey.pdf`、`paper-a.pdf`，以及只在 Dropbox 上的 `hw1.pdf`）、4 個副本，以及它們的位元組數。
  2. 集中到 `gd:Research/Papers`：試跑顯示 `paper-a.pdf` 會搬動、`survey.pdf` 已經在那裡、`hw1.pdf` 以 `NOT_ON_TARGET` 略過；勾選跨雲端選項：`hw1.pdf` 從 Dropbox 搬動並附警告。暫存；吐司通往工作區，其暫存抽屜顯示一個步驟「Gather 2 files into gd:Research/Papers」；回到探索，兩個副本帶有暫存標記。
  3. 預覽、輸入代碼核准、執行、從橫幅重新掃描。磁碟上：`gd/Research/Papers/paper-a.pdf` 與 `gd/Research/Papers/hw1.pdf` 存在，`dbx/School/NTU/Courses/CS101/hw1.pdf` 在 Dropbox 的隔離區，其他東西都沒變。
  4. 探索 `type:pdf copies:>=2`；選取全部符合者（1 個內容）；**清理重複檔…**：表單顯示選取橫幅與 1 項移除的估算；建立計畫；審閱的摘要讀作「探索選取內容的去重（1 個內容）」；核准、執行。磁碟上 `gd/Downloads/survey.pdf` 在隔離區，`gd/Research/Papers/survey.pdf` 保留。計畫的 `params`（以第二個連線讀取）含有 `selection` 與 `groups == ["g:<survey's group>"]`。
  5. 在磁碟上新增一個檔案並從終端機重新掃描，使舊的「全部符合」去重 URL 回應 `SELECTION_CHANGED`，由頁面措辭；**改用目前的 N**即可恢復。
- 整套測試維持通過：`pytest`（含瀏覽器測試），但 `tests/test_docs_i18n.py` 在 `docs/BULK.zh-TW.md` 存在之前除外。

## I. 分工

兩位實作者並行作業；沒有檔案由兩人共同擁有。兩者之間的介面就是這份合約（代碼、形狀、路由）；前端對照模擬 API 與此處列出的代碼清單開發。

| 後端負責 | 前端負責 |
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

順序：兩邊同時開始。前端對 `test_i18n_contract.py` 的擴充會匯入 `explore.SELECTION_CODES`、`gather.CODES` 與 `gather.SKIP_CODES`，而 `test_ui_bulk_e2e.py` 需要真實的伺服器，所以這兩者要在後端完成後才跑到全部通過；其餘每一邊的部分，單獨就能通過。
