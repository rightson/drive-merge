# drive-merge

[English](README.md) | **繁體中文**

將 Google Drive、Dropbox、OneDrive 與 iCloud 的檔案合併到同一處，過程包含去重，並在搬動任何檔案前先產生模擬執行計畫（dry-run）。

drive-merge 在你的電腦上執行。它透過 [rclone](https://rclone.org) 將每個雲端的 **中繼資料**（絕不包含檔案內容）彙整進本機 SQLite 資料庫，告訴你每一份內容存放在哪裡，證明哪些檔案是重複的，提出整潔的資料夾架構建議，並且——僅透過經過模擬且經人工核准的計畫——將所有內容整合到單一雲端。

設計文件：[docs/DESIGN.zh-TW.md](docs/DESIGN.zh-TW.md) — 架構、安全模型，以及你可以調整的待決事項；[docs/WORKSPACE.zh-TW.md](docs/WORKSPACE.zh-TW.md) — 視覺化工作區；以及 [docs/EXPLORE.zh-TW.md](docs/EXPLORE.zh-TW.md) — 總覽、探索與比較三個檢視；[docs/BULK.zh-TW.md](docs/BULK.zh-TW.md) — 從探索進行批次操作；[docs/PLANS.zh-TW.md](docs/PLANS.zh-TW.md) — 計畫檢視；[docs/QUARANTINE.zh-TW.md](docs/QUARANTINE.zh-TW.md) — 隔離區檢視；以及 [docs/RULES.zh-TW.md](docs/RULES.zh-TW.md) — 規則檢視。繁體中文版本位於 `docs/*.zh-TW.md`。

## 設計理由

- **四個雲端沒有任何一種雜湊值是共通的**（Drive：md5/sha1/sha256；Dropbox：內容雜湊；OneDrive：QuickXorHash；iCloud：無），而且只有抗碰撞的雜湊值才能證明兩個檔案完全相同：sha256 與 Dropbox 內容雜湊。md5、sha1（已有實用的碰撞攻擊，而且每個回報 sha1 的雲端也都會回報 sha256）與 QuickXorHash 只能證明兩個檔案不同，永遠不能證明它們相同。drive-merge 會下載有歧義的檔案的*一份*副本，在本機計算全部五種雜湊類型，並利用這座「橋梁」，在不下載其他雲端副本的情況下，證明其他雲端中帶有強雜湊值的副本與之完全相同。沒有強雜湊值的 OneDrive 或 iCloud 檔案，只有透過以它自己的下載做出的橋梁才會成為 exact。
- **目錄是依每個 remote 分別建立，而非透過 rclone `union`。** Union 會依路徑合併並只顯示一份副本，這正好隱藏了去重工具所需的來源出處資訊。若你想要一個可瀏覽的單一檢視，請使用 `combine`（`drivemerge rclone-combine`）。
- **除非透過經核准的計畫，否則不會寫入任何雲端。** 計畫會先針對目錄進行模擬（不得使任何內容失去最後一份副本；每次刪除都必須有一份可證明完全相同、且會保留的見證副本；不覆寫；不得有大小寫衝突等）。核准與模擬摘要（digest）綁定，且需要互動式終端機。刪除的檔案會移至隔離資料夾，可以還原。
- **由 LLM 提議，由人類核准。** Claude Code（透過 MCP）可以搜尋、讀取資料夾概況、撰寫版面規則，以及建立與模擬計畫。它無法核准或執行這些計畫。

## 快速開始

需要 Python 3.11 以上版本，以及 [rclone](https://rclone.org/install/) ≥ 1.69（頁面可以代為安裝）。不需要設定檔，也不需要 `rclone config`。

```bash
pip install 'drive-merge[api]'
drivemerge ui                    # first run creates ~/.config/drive-merge/ and opens the page
```

1. **連接。** 頁面會先顯示歡迎畫面，每個雲端一張卡片。Dropbox 與 OneDrive 各按一個按鈕即可；Google Drive 需要你自己的 OAuth 用戶端，對話框會一步步帶你完成（約 5 分鐘，只需一次）。連線預設唯讀：卡片會顯示帳號、容量與「已連接，唯讀」。
2. **同步。** 一個按鈕就會平行把每個已連接雲端的檔案清單讀進本機目錄，並顯示各雲端的進度。之後的一切（總覽、探索、比較）都是本機查詢。
3. **釋放空間。** 在某個雲端按 **釋放空間…**：會顯示其中有多少內容*已證實*存在於你的其他雲端（位元組完全相同）、有多少只是*可能*（按一下 **驗證** 即可確定），以及有多少只存在於這裡。**允許變更**（第二次登入）讓 drive-merge 能建立釋放已證實部分的計畫；你檢視計畫、輸入核准碼、執行，再按 **立即清除** 就會把空間還給你。在此之前不會有任何變動。請參閱 [docs/ONBOARDING.zh-TW.md](docs/ONBOARDING.zh-TW.md)。

## 安裝

需要 Python 3.11 以上版本，以及 rclone ≥ 1.69（用於 iCloud）。

```bash
pip install -e '.[api,mcp]'      # or: uv pip install -e '.[api,mcp]'
drivemerge init                  # writes ~/.config/drive-merge/config.toml and an API token
```

需要腳本化，或頁面是唯讀時走終端機路線：`drivemerge connect dropbox|onedrive|drive|iclouddrive` 可在終端機登入，或自行為每個雲端設定一個 rclone remote（`rclone config`）。建議：用於掃描的唯讀 remote（例如 scope 為 `drive.readonly` 的 Google Drive），以及僅在你想讓 drive-merge 變更該雲端時，才另外設定可寫入的 remote。注意：rclone 共用的 Google Drive client_id 正於 2026 年淘汰——[請建立你自己的](https://rclone.org/drive/#making-your-own-client-id)。

編輯設定檔（請參閱 [examples/config.toml](examples/config.toml)）：

```toml
[[remotes]]
name = "gdrive"
provider = "drive"
read = "gdrive-ro:"
write = "gdrive:"
extra_args = ["--fast-list", "--drive-skip-shortcuts"]

[[remotes]]
name = "dropbox"
provider = "dropbox"
read = "dropbox:"
```

## 使用

第一步可以直接在瀏覽器完成：`drivemerge ui` 在設定檔不存在時會自動建立，並開啟**雲端**檢視，你可以在那裡連接雲端並同步（見下文）。也可以改在終端機做同樣的事：

```bash
drivemerge ui                              # first step, in the browser: Clouds connects clouds and syncs them
drivemerge scan --all                      # list every cloud into the catalog (read-only)
drivemerge identity rebuild                # group identical content
drivemerge stats
drivemerge search "報告 2023"               # substring search, works for CJK names
drivemerge duplicates --cross-remote
drivemerge bridge --max-bytes 5G           # download+hash ambiguous files once, then rebuild
drivemerge profiles --remote dropbox --depth 2
drivemerge rules preview --rules my-rules.toml --target-root Archive

drivemerge plan consolidate --target gdrive --root Archive   # copy-only by default
drivemerge plan show 1                     # simulation report: before/after per cloud
drivemerge plan approve 1                  # interactive: type the 8-char code
drivemerge plan execute 1                  # needs [safety] execution_enabled = true
drivemerge plan restore 1                  # undo deletes (moves them back from quarantine)
```

### 視覺化工作區

```bash
drivemerge ui                              # opens http://127.0.0.1:8765/ui/ (one-time token in the URL)
```

兩個樹狀窗格可並排顯示任意兩個位置，並可跨越不同雲端。拖曳檔案與資料夾到更適當的層級、重新命名、復原與重做、在「變更後」與「變更前」檢視之間切換，並取得相關或重複資料夾的建議。拖曳期間完全不會碰觸任何雲端：暫存的移動會透過與其他所有計畫相同的模擬器進行預覽（每個雲端的前後狀態、衝突、去重），只有輸入的 8 字元核准代碼才會開始執行——而且僅限於 `drivemerge ui` 在你自己的互動式終端機中執行、而非在 AI 代理程式之中時。頁面預設為英文；瀏覽器列出中文時則顯示繁體中文，`EN` / `中文` 按鈕可切換並會記住你的選擇。請參閱 [docs/WORKSPACE.zh-TW.md](docs/WORKSPACE.zh-TW.md)。

頁面左側的導覽列還能開啟三個唯讀的檢視，它們都會通往工作區：**總覽**（各雲端的內容重疊多少：用交集圖呈現哪些內容在哪些雲端，以及哪些只存在於單一雲端）、**探索**（用名稱或像 `type:pdf not:gdrive copies:1` 這樣的查詢找檔案；內容相同的副本會歸成一列，每個雲端一欄）與**比較**（對任意雲端上的任意兩個資料夾做對齊的差異檢視：相同、不同、搬動過、只在一邊，並有按鈕可把合併加入你的草稿）。它們不會寫入任何雲端，也不會更動 catalog。請參閱 [docs/EXPLORE.zh-TW.md](docs/EXPLORE.zh-TW.md)。

**探索**裡的結果可以被選取（手動挑選，或查詢符合的每一筆內容）並直接操作：把每個內容的一個副本**集中**到同一個資料夾（在你已開啟的草稿中暫存成一個一般的搬動，像任何一次拖放一樣預覽、核准；名稱絕不更改，名稱衝突的會被略過），或**清理它們的重複檔**（一份只限於這些內容的去重計畫）。探索也會標出你的草稿已經在搬動的副本。兩者都只是提案：在你核准之前，不會寫入任何雲端。請參閱 [docs/BULK.zh-TW.md](docs/BULK.zh-TW.md)。

**計畫**檢視讓你直接在頁面上建立並審閱整批計畫：去重計畫（有即時預估，並可逐組選擇要保留哪一份）、整併計畫（預設只複製；清理來源藏在有防護的區塊後面），以及審閱任何計畫（包含 Claude 提出的），使用同樣的模擬與同樣的輸入確認碼核准。總覽也能橋接沒有可比對雜湊的檔案。請參閱 [docs/PLANS.zh-TW.md](docs/PLANS.zh-TW.md)。

**隔離區**檢視（在計畫之後，`Alt+6`）列出每個已執行的計畫放進隔離資料夾的檔案：依雲端、依計畫顯示佔多少空間、從什麼時候開始。可以把整個計畫、一個資料夾或單一檔案放回原處（絕不覆蓋任何東西），並把已超過保留期限（`[safety] quarantine_retention_days`，預設 30 天）的內容永久清除；動手前會先依目錄做唯讀檢查，並要求輸入確認字（`restore` 或 `purge`），和 `drivemerge plan restore`、`drivemerge plan purge` 完全一樣。沒有核准功能時，頁面改為顯示終端機指令。請參閱 [docs/QUARANTINE.zh-TW.md](docs/QUARANTINE.zh-TW.md)。

**規則**檢視（在隔離區之後，`Alt+7`）用來編輯決定整併與重新整理計畫要把檔案放在哪裡的版面規則：附即時驗證的文字編輯器（每個錯誤都附行號，並以你的語言說明）、在真實目錄上的即時前後預覽（大型目錄以抽樣計算，並標示「≈」）、與使用中規則的差異，以及審閱 Claude 提出的規則變更；提案在你啟用之前絕不會生效（有核准功能時在頁面上啟用，否則在你自己的終端機執行 `drivemerge rules activate <id>`）。「用於計畫」會帶著編輯中的規則開啟整併與重新整理表單，而且每個計畫都會記錄它建立時所用的規則。請參閱 [docs/RULES.zh-TW.md](docs/RULES.zh-TW.md)。

**雲端**檢視（導覽列的第一個項目；開啟時如果還沒有東西可處理，頁面會直接停在這裡）帶你從 `pip install` 走到同步好的目錄。Dropbox、OneDrive 與 Google Drive 會在新分頁登入雲端服務，透過 rclone 在 `127.0.0.1:53682` 提供的登入輔助程式，所以 drive-merge 不會接觸到權杖；**iCloud Drive** 則輸入 Apple ID 與密碼，雙重驗證碼再由表單詢問。如果瀏覽器不在執行 drive-merge 的那台電腦上，**在另一台電腦上登入**會顯示要在那裡執行的 `rclone authorize` 指令，並接收你貼回來的權杖。**已經在使用 rclone？**會列出你自己 rclone 設定裡的 remote，並把其中一個匯入為唯讀連線，不會動到那個檔案；你在 `config.toml` 手動設定的雲端也會有一張卡片。測試結果會依原因說明（登入已過期、沒有網路、找不到）。只有從你自己的終端機啟動的 `drivemerge ui` 頁面才能連接與同步；否則這個檢視是唯讀的，只會顯示該執行的 `drivemerge connect` 指令。請參閱 [docs/ONBOARDING.zh-TW.md](docs/ONBOARDING.zh-TW.md)。

API 與 MCP：

```bash
drivemerge serve                           # REST API on 127.0.0.1:8765 (Bearer token)
claude mcp add drive-merge -- drivemerge mcp   # let Claude Code explore and propose plans
```

外部資料庫可透過 `GET /v1/changes?cursor=...` 進行增量同步。

## 開發

```bash
uv venv && uv pip install -e '.[dev]'
pytest                  # everything, including the browser tests (headless Chromium via Playwright)
pytest -m "not ui"      # without them
```

核心模組（`src/drivemerge`）只使用 Python 標準函式庫；FastAPI 與 MCP SDK 則是選用的額外套件。
