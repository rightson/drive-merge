# 在瀏覽器中編輯版面規則

[English](RULES.md) | **繁體中文**

> 本文件為視覺化工作區（[WORKSPACE.md](WORKSPACE.zh-TW.md)、[EXPLORE.md](EXPLORE.zh-TW.md)、[PLANS.md](PLANS.zh-TW.md)、[QUARANTINE.md](QUARANTINE.zh-TW.md)）新增「**規則**」（Rules）頁面：編輯決定整併與重新整理計畫要把檔案放在哪裡的版面規則（[DESIGN.md](DESIGN.zh-TW.md) §6），提供即時驗證、在真實目錄上的即時前後預覽、與使用中規則的差異比對、審閱 Claude 提出的規則變更，以及從編輯中的規則直接進入計畫表單的路徑。第一部分是設計；第二部分（實作合約）對實作者具有約束力。

## 1. 問題

版面是整併中影響最大的輸入：一百萬個檔案各自落在哪裡，都由它決定。現在只能在頁面之外修改：

- DESIGN.md §6 與 PLANS.md §8 說規則由 CLI 或 Claude 編輯；整併表單寫著「請使用終端機或請 Claude 處理」（`cons.rulesHelp`）。
- `POST /v1/rules/validate` 與 `POST /v1/layout/preview` 已經存在，但頁面從不呼叫它們。CLI 有 `drivemerge rules check FILE` 與 `drivemerge rules preview`；要更改使用中的規則，就得用編輯器修改 `[rules] path` 指定的檔案，而這個檔案得先由人自己建立並登錄到 `config.toml`。
- 驗證只回覆一句英文（`RuleError`），而且只有第一個錯誤，沒有行號。
- `layout/preview` 每次呼叫都走過整份目錄（大型目錄要數秒），顯示每條規則的數量和樣本，但看不出相較於目前生效的規則*改變了什麼*，也看不出哪些檔案落到了後備規則。
- 規則變更沒有歷史。計畫只有在規則是以內嵌方式傳入時才會記下規則；用設定檔建立的計畫什麼都沒記，所以檔案一改，「這個計畫是用哪些規則做的？」就再也答不出來。
- Claude 可以把任意規則內嵌傳給 `propose_plan`。計畫仍會被模擬並核准，但沒有任何東西告訴人這個計畫用的不是他自己的規則。

這個頁面要回答的問題：

1. **用這些規則，我的檔案會去哪裡？** 每條規則：多少檔案與位元組、「之前 → 之後」的樣本檔案；哪些檔案沒有任何規則接手（它們走後備規則）；哪些單元整個一起搬。
2. **改了這條規則會有什麼變化？** 目的地和使用中規則不同的檔案：之前是哪條規則處理、現在是哪條。
3. **Claude 提了什麼？它生效了嗎？** 提案是等待審閱的版本，附有差異與預覽；在人啟用之前，它絕不會生效。
4. **能從這些規則直接去建立計畫嗎？** 「用於整併計畫」與「用於重新整理計畫」會開啟既有的表單並套用編輯中的規則；之後計畫的預估、建立、模擬與核准都和現在完全一樣。

## 2. 原則

- **規則不會更動任何雲端。** 規則只決定之後建立的計畫有哪些操作；每個這類計畫仍照現在的方式建立、模擬、以輸入的代碼核准並執行。計畫的操作在建立時就固定了，所以啟用新規則絕不會改變既有的計畫。
- **文字是唯一的事實來源。** 規則語言是一個小型、有順序的 TOML 檔，其中的註解很重要（預設檔有一半是註解）。頁面編輯的就是那份文字；每個結構化的輔助功能（調整順序、插入規則、移除規則）都是保留註解的文字轉換，並由解析器驗證（§4，R2）。
- **沒有任何東西會意外生效。** 編輯是草稿；Claude 的變更是提案。只有人，在人類通道上，看過與使用中規則的差異及預覽之後，才能啟用一個版本（R3）。在 drive-merge 之外修改規則檔會被偵測並顯示，絕不會被默默採用。
- **每個計畫都記錄自己的規則。** 整併與重新整理計畫會存下建立時所用規則的原文，以及它的來源（使用中的檔案、預設值、某個版本、內嵌），讓計畫可以重現，審閱時也會顯示計畫用的是不是使用中的規則。
- **快到可以邊打字邊用。** 每次打字停頓都會驗證，而且絕不讀取目錄。預覽是抽樣的、有快取、可取消，並且在目錄鎖之外執行（§7）。
- **由頁面負責所有措辭。** 驗證錯誤、警告和拒絕都是帶參數的代碼；句子由頁面用兩種語言寫，並指向對應的那一行。

## 3. 規則放在哪裡（決定）

**R1. 使用中的規則是一個檔案；它的歷史、草稿與提案存放在目錄中。**

- **使用中的規則**是一個檔案的文字：設定有指定 `[rules] path` 時就是它所指的檔案，否則是 `config.toml` 旁邊的 `rules.toml`（設定範本已經建議的路徑），drive-merge 對它的管理方式就像對 `connections.toml` 一樣。兩個檔案都不存在時，使用中的是內建的 `default_rules.toml`。`config.toml` 絕不會被編輯：`config.py` 的慣例維持不變，頁面也不必在 `config.toml` 加上任何 `[rules]` 項目就能運作。
- 目錄新增一張 `rule_versions` 資料表：每一份曾經生效的文字（附上是誰、何時讓它生效）、人的**草稿**，以及來自 Claude 或任何 API 用戶端的**提案**。版本一旦不再是草稿就不可變更。
- **啟用**一個版本（僅限人類通道）會把它的原文以原子方式寫入使用中的檔案，寫入前先確認該檔案仍然是人審閱時的那份文字（以它的 SHA-256 做比較再交換）。被取代的文字早已在歷史裡，所以**復原**就是「啟用前一個版本」。drive-merge 裡沒有其他東西會寫這個檔案。
- 這個檔案每次使用時都會重新讀取，和現在一樣，所以在文字編輯器或用 `drivemerge rules activate` 所做的修改會立刻生效。每當頁面載入規則狀態，或有計畫被提出時，歷史中尚未出現的檔案文字會以 `file`（「在 drive-merge 之外被修改」）記為一個版本，讓歷史完整，人也看得到外部的修改。

比較過的選項：

| 選項 | 手動編輯檔案 | 歷史與復原 | 計畫可重現性 | Claude 無法偷偷塞入變更 | 多個分頁 | 結論 |
|---|---|---|---|---|---|---|
| (a) 寫入 `[rules] path` 指定的檔案，並在 `config.toml` 建立檔案與 `[rules]` 項目 | 檔案仍是來源；但寫入 `config.toml` 違反「絕不編輯人的設定」的規則，而單純覆寫會丟失外部的修改 | 沒有：前一份文字就不見了 | 本身沒有 | 只有在寫入受到把關時才成立 | 最後寫入者獲勝 | 採用於*使用中*的文字，但不碰 `config.toml`，使用比較再交換，並加上 (b) 的歷史 |
| (b) 目錄中的具名規則集，加上「使用中」指標 | 第二個事實來源：檔案與指標互相不一致，CLI 的 `--rules FILE` 與人自己的編輯器都失去意義；目錄重建後規則就消失 | 完整 | 以版本編號 | 只要把關指標就夠了 | 版本計數器 | 只採用於歷史、草稿與提案 |
| (c) 規則只屬於各個計畫，以檔案儲存與載入 | 不受影響 | 沒有 | 完整（計畫帶著它的文字） | 內嵌規則在每個計畫上都看得到 | 沒有共用狀態 | 不作為模型（編輯隨分頁消失；瀏覽器不能寫檔；「我依賴的那份規則」仍只能在終端機處理）；它的優點，也就是每個計畫都帶著自己的文字，則被採用 |

人看到的是：規則頁會說明使用中的規則來自哪裡（「`~/.config/drive-merge/rules.toml` · 2 天前起使用，由你在瀏覽器中啟用」／「內建預設規則 · 尚未自訂」／「`~/rules/layout.toml` · 10 分鐘前在 drive-merge 之外被修改」），並有歷史清單、草稿，以及帶徽章的 Claude 提案。啟用對話框會指出它將寫入哪個檔案並顯示差異。

**R3. 規則不是雲端變更，但讓規則生效是人的行為。** 啟用會改變之後每個「使用中規則」計畫的行為，包括 CLI 建立的計畫，以及 Claude 在沒有傳入規則時提出的計畫；核准一個四萬個操作的計畫的人看的是總數，而不是每一個放置位置。所以啟用在人類通道上進行（由互動式終端機中的 `drivemerge ui` 開啟的瀏覽器工作階段，帶有 CSRF 與同源檢查），和連線一樣，以及在終端機（`drivemerge rules activate <version>`，受 TTY 關卡把關）。草稿、提案、預覽，以及使用內嵌規則的計畫都是提案，留在一般通道。

## 4. 編輯器（決定 R2）

**R2. 具有即時驗證的文字編輯器，加上一個動作都是文字編輯的大綱。** 結構化表單曾被評估，並被否決作為主要編輯器：頁面沒有 TOML 寫入器、經過 `tomllib` 來回轉換會丟掉每一則註解和人的排版、規則的順序是這個語言的主要語意（第一個符合的獲勝）而且用文字看最清楚，再加上 Claude 寫的就是 TOML。只靠欄位編輯還會藏起 `path_glob` 與 `name_regex`，而它們本來就是文字。大綱提供結構化的檢視，但不擁有那份文字：

- **大綱**：依序列出規則，單元規則有標記，每條附上比對摘要（類別、副檔名、萬用字元、大小、雲端）、目標範本、預覽的數量，以及該規則有錯誤時的紅色標記。點選一條規則會把游標移到它的 `[[rule]]` 那一行。**上移／下移**、**移除**與**插入規則…**會把文字送到伺服器，由伺服器把整個 `[[rule]]` 區塊連同它正上方的註解行一起移動或移除，或插入一個產生的區塊，而且只有在結果剛好解析成預期的規則時才回覆新文字（否則回覆 `RULES_CANNOT_EDIT`，由人改編輯文字）。
- **插入規則…**：一個小表單（名稱、單元或單獨的檔案、以核取方塊選擇的類別、副檔名、路徑萬用字元、檔名樣式、大小、雲端、目標與可點選的變數標籤，以及一個範例檔案的即時呈現），產生一個 TOML 區塊；這個區塊會插在所選規則之後。這個表單絕不編輯既有的規則。
- **文字**：等寬字型的 `<textarea>`，左側有行號欄。錯誤會在行號欄標出所在的行，並列在文字下方，句子由代碼與參數產生（「第 3 條規則（`screenshots`）：不明的類別 `foto`——你是指 `photo` 嗎？」），每則都有「前往該行」。警告（因為前面同類的規則什麼都符合而永遠不會符合的規則；沒有保留原本結構的後備規則；不是這份目錄中雲端的 `remote`）以琥珀色顯示。所有規則的所有錯誤都會列出，而不只是第一個。
- **起點**：「以使用中的規則新增」、「以預設規則新增」（含註解的內建檔）、「以版本…新增」，以及從計畫審閱頁面「開啟計畫的規則」。
- 草稿會自動儲存（停止打字 2 秒後）。每個分頁尚未儲存的文字也會保存在 `sessionStorage`，所以重新載入不會丟失。

## 5. 資訊架構

第八個側邊欄項目，**規則**（`#/rules`、`Alt+7`），位於隔離區之後。它的徽章顯示等待審閱的提案數量。

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

- **使用中**：來源（檔案路徑，或「內建預設規則」）、版本、規則數量、從何時起、由誰啟用；若自上次從頁面啟用之後，檔案在 drive-merge 之外被修改，會有琥珀色的提示；若使用中的檔案無法解析，則有紅色的提示（在修好之前，所有整併與重新整理計畫都會失敗；「在編輯器中開啟」會顯示錯誤）。
- **等待審閱**：提案（Claude、API），最新的在前，附上一行變更摘要（新增、移除、修改、調動的規則數），以及一旦算出來之後，有多少檔案的目的地會改變。點選一列會開啟審閱（§6）。
- **草稿**：人的草稿；**歷史**：每個曾經生效的版本，最新的在前，附有**還原**（再次啟用它）與**在編輯器中開啟**。

編輯器，`#/rules/edit/<version>`：

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

視窗較窄時，三個窗格會變成分頁（大綱 · 文字 · 預覽）。

## 6. 流程

**編輯與預覽**

1. 打字 → 200 ms 後 `POST /v1/rules/check`：錯誤、警告、大綱，以及與使用中規則的差異。完全不讀取目錄。
2. 文字沒有錯誤時 → 再過 400 ms（最後一次按鍵後 600 ms），以範圍（雲端、資料夾、目標根目錄）送出 `POST /v1/rules/preview`。對舊文字的回覆會被丟棄；較新的請求會中斷伺服器上正在執行的預覽。
3. 預覽顯示：範圍內的檔案，以及數字是抽樣（「≈」，附上抽樣比例）還是精確；與使用中規則相比有變動的檔案；後備（沒有規則符合）；已在原位（目標 = 目前路徑）；樣本中被多個檔案共用的目標；每條規則一列的表格，含使用中與編輯後的數量、位元組，以及單元規則的單元數（沒有任何符合的規則會被標示）；最主要的轉移（「documents → receipts 2,310」）；以及分頁的檔案清單，分頁為**有變動**、**後備**、**全部**，每條規則還各有一個；每一列顯示：雲端與目前路徑、使用中規則的目的地、編輯後規則的目的地、規則、一起搬移的單元。在抽樣範圍上，「計算全部檔案」會要求精確的數字。
4. **說明**（在任何檔案列上，或輸入 `cloud:path`）：依序列出每條規則是符合還是第一個不符合的鍵（「category 是 `pdf`，規則要的是 `photo`」）、該檔案所屬的單元，以及在編輯後與使用中規則下的目的地。

**啟用**

1. 「啟用…」（僅在核准功能啟用時）會開啟對話框：將被寫入的檔案、與使用中規則的文字差異、變更摘要，以及預覽中整份目錄的「與使用中規則相比有變動」數量（若上一次預覽是抽樣的，這裡會算出精確數字；對話框會說明正在計算）。
2. 「啟用」→ `POST /v1/ui/rules/versions/<id>/activate`，帶上對話框所顯示的使用中檔案 SHA-256。有錯誤的版本不能啟用（`RULES_INVALID`）。
3. 若檔案在這段時間被修改（另一個分頁、編輯器、CLI）：回覆 `RULES_ACTIVE_CHANGED`；對話框會重新載入並顯示新的差異。若檔案無法寫入：回覆帶有路徑的 `RULES_FILE_UNWRITABLE`，對話框提供「複製文字」與指令。
4. 啟用之後，該版本成為使用中，前一個被取代，規則頁、計畫表單與等待中的計畫都會看到新的狀態。已建立的計畫不變；頁面會這樣說明。

**Claude 的提案**

1. Claude 呼叫 MCP 工具 `propose_rules`（或 API 用戶端送出一個版本）。伺服器會驗證它（有錯誤的提案會連同代碼一起被拒絕，好讓 Claude 修正），並以 `mcp` 或 `api` 存為 `proposed` 版本。
2. 頁面在可見時每 10 秒輪詢一次，跳出「Claude 提出了規則變更」的提示，附有**審閱**；側邊欄徽章顯示提案數量。
3. 審閱（`#/rules/v/<id>`）：Claude 的標題與備註（不受信任的文字，只當資料顯示）、文字差異、變更摘要、相對於使用中規則的預覽。動作：**啟用…**（如上）、**編輯成草稿**（複製成以它為基礎的新草稿）、**用於計畫**（不啟用）、**忽略**。
4. 提案絕不會被就地編輯，也絕不會自己生效。Claude 也可以用它建立計畫（`rules_version`）；該計畫的審閱頁面會顯示「用提案規則 v15 建立 — 不是使用中的規則」。

**進入計畫**

- 「用於計畫 ▾」→ **整併**或**重新整理**。未儲存的草稿會先儲存；頁面接著開啟 `#/plans/new/consolidate?rules=<version>`（或 `reorganize`），並帶著預覽的目標根目錄與資料夾。
- 整併表單，以及新的重新整理表單（`#/plans/new/reorganize`：雲端、目標根目錄、資料夾、規則——建構器 `build_reorganize` 已存在；PLANS.md 之前沒有它的表單），多了**版面規則**欄位：「使用中的規則（來源）」或「版本 14（草稿）— 尚未啟用」，附有**編輯**（開啟編輯器並附上回到表單的連結）與**改用使用中的規則**。預估與「建立計畫」會送出 `rules_version`。`cons.rulesHelp` 那一句被取代。
- 計畫的預估、建立、模擬與核准都和現在完全一樣。它的審閱頁面會顯示**規則**方塊：來源、版本、SHA-256 前綴、不同時顯示「不是使用中的規則」、「檢視」（文字）與「在編輯器中開啟」。

**多個分頁**：每個分頁編輯自己的草稿；儲存一份被另一個分頁修改過的草稿會被拒絕（`RULES_DRAFT_CHANGED`），並提供「重新載入」或「另存為新草稿」。啟用受使用中檔案的 SHA-256 把關，所以兩個分頁不可能在沒被注意的情況下，對同一個基底都完成啟用。

**唯讀備援**（核准功能停用時）：除了啟用之外，所有功能都能用——草稿、檢查、預覽、說明、使用草稿規則的計畫（它們的核准則和每個唯讀頁面的計畫一樣，要在終端機完成）。「啟用…」與「還原」改成顯示指令 `drivemerge rules activate <version>`（先儲存草稿，這樣它才有編號），附有複製按鈕，並註明終端機會顯示相同的差異並要求確認。

## 7. 效能

- **檢查**是純計算（`tomllib` 加驗證，不碰目錄），文字上限 256 KB，防抖 200 ms。
- **預覽**是抽樣的：範圍內最多 100,000 個存活檔案時是精確的；超過時，它放置一份確定性雜湊抽樣的 20,000 個檔案（每次按鍵都是同樣的檔案，所以數字不會閃動，前後比較用的也是同一批檔案）並按比例放大數量，標示為「≈」。「計算全部檔案」依要求執行精確的一輪（每次請求上限 2,000,000 個檔案）。
- **單元有快取。**單元偵測會讀取某個遠端的每一筆存活資料，而且只取決於 `unit_markers` 與 `bundle_patterns`；它的結果依遠端快取，快取鍵是這兩份清單，加上目錄的快照與 `row_version`。編輯標記以外的規則，只需要付出樣本放置的成本。
- **使用中的基準有快取。**相同樣本與範圍在使用中規則下的放置結果，以（使用中的 SHA-256、範圍、目錄狀態）為鍵快取，所以每次按鍵只需要重新計算編輯後的那一側。
- **結果有快取並分頁。**預覽的每個檔案資料列保留在最多 8 份預覽的 LRU 中（以規則的 SHA-256、範圍與目錄狀態為鍵）；檔案分頁（100 列，最多 500）從中提供。被淘汰的預覽會回覆 `PREVIEW_EXPIRED`，頁面會重新計算。
- **在目錄鎖之外，可取消。**預覽在專用的唯讀連線（WAL）上執行，不在串行化其他路由的鎖之下，每個伺服器同一時間只有一個；較新的請求會中斷正在執行的那個（`Connection.interrupt()`，以及每 1,000 列檢查一次的取消旗標），被中斷的那個回覆 `PREVIEW_SUPERSEDED`，並被頁面忽略。

## 8. 交叉連結

- 計畫 → 整併與重新整理表單：版面規則欄位（編輯、改用使用中的規則）。
- 計畫審閱：規則方塊；「在編輯器中開啟」會用計畫的規則建立草稿。
- 規則頁：在使用中的規則與每個版本上都有「用於整併…」、「用於重新整理…」。
- 側邊欄徽章與提案的提示；計畫的提示維持不變。
- 探索：結果列上的「規則會把它放在哪裡？」會開啟編輯器的說明並帶入該檔案。

## 9. 已知限制（v1）

- 目錄只有一組使用中的規則。每個雲端各自的版面要用 `remote` 比對鍵表達，而不是用多組使用中的規則。
- 文字編輯器只是帶行號欄的純 `<textarea>`：沒有語法上色或自動完成。大綱與插入表單涵蓋了常見的編輯。
- 抽樣的數字是估計值；很大的目錄上要精確計數需要數秒（頁面會說明正在處理）。
- 預覽顯示的是放置位置，不是操作：只有建構器會處理的衝突（`LAYOUT_TAKEN`、目標上已有相同內容、唯讀雲端）出現在計畫預估裡，而不是預覽裡。預覽把樣本中被共用的目標算進去，當作提示。
- 目錄的新舊程度取決於每個雲端最近一次的掃描。
- 在 drive-merge 之外修改使用中的檔案，是在規則頁載入或有計畫被提出時才偵測到，而不是它發生的當下。
- 啟用規則不會改變任何既有計畫；用舊規則建立的計畫必須取消並重建，才會採用新規則。

---

# 實作合約

以下全部對實作者具有約束力。請先閱讀：[DESIGN.md](DESIGN.zh-TW.md) §6、§7.4、[PLANS.md](PLANS.zh-TW.md) §B、§C、§E、[WORKSPACE.md](WORKSPACE.zh-TW.md) §D、§F、[QUARANTINE.md](QUARANTINE.zh-TW.md) §E、§F、`classify.py`（`load_rules`、`_parse_rule`、`_check_template`、`RuleError`、`detect_units`、`plan_layout`、`render`）、`default_rules.toml`、`config.py`（`rules_path`、`write_private`、「絕不編輯 config.toml」的說明）、`service.py`（`ruleset`、`validate_rules`、`preview_layout`、`_build`、`propose_plan`、`estimate_plan`、`RESERVED_PARAMS`）、`db.py`（`migrate`、`SCHEMA_VERSION`、`journal`、`snapshot`）、`api.py`（`build_router`、`install_error_handlers`）、`ui_api.py`（`require_human`、`_Jobs`）、`mcp_server.py`、`cli.py`（`cmd_rules_check`、`cmd_rules_preview`、`plan approve` 的 TTY 關卡），以及 `src/drivemerge/web/` 裡的每個檔案。

## A. 定義

- **使用中的檔案** = `Config.rules_file`：有設定 `[rules] path` 時是 `Path(rules_path).expanduser()`；否則，設定是從檔案載入的（`config_path` 已設定）時是 `config_dir / "rules.toml"`；否則是 `None`（在記憶體中建立的設定，例如測試中的，絕不讀取人的家目錄裡的檔案）。**由 drive-merge 管理** = 沒有設定 `[rules] path`。
- **使用中的規則** = 使用中的檔案存在時的文字；否則是內建的 `default_rules.toml`（來源為 `default`）。設定了 `[rules] path` 但檔案不存在是錯誤（`RULES_FILE_MISSING`），不會默默退回預設值。
- **版本** = 一筆 `rule_versions` 資料。狀態：`draft`（由 `ui` 建立，可編輯）、`proposed`（由 `mcp`、`api` 建立；不可變更）、`active`（最多一筆；它的 `sha256` 等於使用中檔案的）、`retired`（曾經使用中）、`dismissed`。`created_by`：`ui`、`mcp`、`api`、`cli`、`file`（在使用中的檔案裡找到、而沒有任何版本有的文字）、`default`。
- **觀察** = 讀取使用中的檔案；若它的 SHA-256 與 `active` 資料不同，就把那筆資料設為取代，並把檔案的文字以 `file` 插入為新的 `active` 版本（或重用 SHA-256 相同的既有版本，把它設為 `active`）。觀察發生在 `Service.rules_state()`、啟用之前，以及整併與重新整理的 `propose_plan` 中；絕不發生在檢查、預覽或 `estimate_plan` 中（它們不寫入目錄）。
- 計畫的**規則參照** = `{"source": "file"|"default"|"version"|"inline", "version": int|null, "sha256": str, "path": str|null}`。

## B. `classify.py`

- `RuleError(code, message, **params)`：`code` 與 `params` 屬性；`str()` 仍是英文訊息（每則既有訊息都不變）。`params` 一定包含 `line`（從 1 起算，或 `null`）、`rule_index`（從 1 起算，或 `null`）、`rule`（名稱或 `null`）與 `key`（或 `null`），再加上該代碼自己的參數。`RULE_CODES` 列出每個代碼：`TOML_SYNTAX {column}`、`UNKNOWN_TOP_KEY {suggestion}`、`LAYOUT_NOT_TABLE`、`UNKNOWN_LAYOUT_KEY {suggestion}`、`MARKER_HAS_SLASH {value}`、`RULES_NOT_ARRAY`、`RULE_NOT_TABLE`、`UNKNOWN_RULE_KEY {suggestion}`、`NAME_REQUIRED`、`NAME_RESERVED`、`NAME_DUPLICATE {name}`、`TARGET_REQUIRED`、`BAD_UNIT_FLAG`、`TEMPLATE_EMPTY {where}`、`TEMPLATE_SYNTAX {where}`、`TEMPLATE_FORMAT_SPEC {where, variable}`、`UNKNOWN_VARIABLE {where, variable, suggestion}`、`TEMPLATE_DOT_SEGMENT {where}`、`UNIT_NEEDS_RELPATH`、`UNIT_VAR_NOT_UNIT`、`BAD_STRING_LIST`、`UNKNOWN_CATEGORY {value, suggestion}`、`BAD_EXT`、`BAD_GLOB`、`BAD_REGEX {value}`、`BAD_SIZE {value}`、`SIZE_RANGE_EMPTY {min, max}`、`RULES_TOO_LARGE {limit}`、`RULES_CANNOT_EDIT {reason}`（`reason` 為 `unparsed`、`not_found`、`mismatch` 其中之一）、`TARGET_ROOT_DOT`。`where` 是 `target` 或 `fallback`；`suggestion` 是字串或 `null`。回顯人所輸入文字的值（`value`、`name`）上限為 200 個字元。
- `TOML_SYNTAX`：`line` 與 `column` 在有 `TOMLDecodeError.lineno`/`colno` 時取自它們，否則從「(at line N, column M)」後綴解析，否則為 `null`。
- `rule_spans(text) -> list[Span]`，其中 `Span(kind: "layout"|"rule", index, comment_line, header_line, end_line)`：標題行為 `^\s*\[\[\s*rule\s*\]\]` 與 `^\s*\[\s*layout\s*\]`（允許尾端註解），`comment_line` 是標題正上方連續註解行的第一行，`end_line` 是下一個區段的 `comment_line` 的前一行（或最後一行）。只有在文字能解析、且規則標題的數量等於解析出的規則數量時，區段才可信；否則是 `[]`。規則錯誤的 `line` = 它的 `header_line`，錯誤帶有 `key` 時則是區段內 `key =` 所在的行。
- `check_rules(text) -> RulesCheck`（dataclass）：`ok`、`errors`（每條規則的第一個錯誤，加上每個 layout 錯誤，最多 50 個，依文字順序）、`warnings`、`outline`、`layout`、`ruleset`（不是 ok 時為 `None`）。規則各自獨立解析，所以一條壞規則不會藏起其他規則；重複的名稱記在後面那條規則上。`load_rules(text)` 維持它的合約（拋出第一個錯誤；相同的英文訊息）。
- 警告（`RULE_WARN_CODES`）：`RULE_SHADOWED {rule, by}`（前面同類（單元或單獨檔案）的規則完全沒有比對鍵，所以這條規則永遠不會符合）、`FALLBACK_LOSES_STRUCTURE`（後備規則既沒用 `{path}` 也沒用 `{relpath}`，所以沒人處理的單元檔案會丟失結構），以及 `RULE_REMOTE_UNKNOWN {rule, value}`（不是目錄中雲端的 `remote` 項目；由知道目錄的 `Service.check_rules` 加入），每個都帶有 `line`。
- `outline` 項目：`{"index", "name", "unit", "line", "end_line", "match": {"category", "ext", "path_glob", "name_regex", "min_size", "max_size", "remote"}, "target"}`；`layout`：`{"unit_markers", "bundle_patterns", "fallback", "defaulted": [沒有給的鍵]}`。
- `transform_rules(text, op) -> str`：`op` 是 `{"op": "move", "index", "to"}`、`{"op": "remove", "index"}` 或 `{"op": "insert", "after": index|0, "rule": {name, target, unit?, category?, ext?, path_glob?, name_regex?, min_size?, max_size?, remote?}}`。移動與移除作用於整個區段（包含註解行）；插入會產生一個區塊（TOML 基本字串並做跳脫；`name_regex` 與 `path_glob` 在不含 `'` 時使用字面字串），後面接一個空行。結果必須解析成預期的規則（相同的規則，被移動；少一條；新規則在要求的位置），否則拋出 `RULES_CANNOT_EDIT`。插入的規則會像任何規則一樣被驗證。
- `plan_layout(..., sample_threshold: int | None = None, units: Mapping[int, list[Unit]] | None = None, cancel: Callable[[], bool] | None = None)`：`sample_threshold` 只保留 `((id * 2654435761) % 4294967296) < sample_threshold` 的資料列（在 SQL 中）；給定 `units` 時，它取代每個遠端 id 的 `detect_units`；`cancel` 每 1,000 列呼叫一次，回傳 true 就拋出 `LayoutCancelled`。沒有這些參數時，輸出和現在逐位元組相同。
- `explain(conn, ruleset, remote_id, path, *, target_root="", units=None) -> dict`：`{"remote", "path", "unit": path|null, "unit_rule": name|null, "steps": [{"rule", "unit", "result": "match"|"skip"|"not_tried", "failed_key": key|null, "wanted": value|null, "got": value|null}], "rule", "target", "in_place"}`；不是存活檔案的路徑拋出 `NOT_FOUND`（`KeyError`）。

## C. `rulestore.py`（新增）與目錄

- 結構描述（`schema.sql`），`SCHEMA_VERSION = 3`（沒有東西需要遷移：資料表由 `CREATE TABLE IF NOT EXISTS` 建立；`_upgrade_after` 只記錄版本）：

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

- `RulesError(status, code, message, **params)` 與 `CODES`：`RULES_VERSION_NOT_FOUND {version}`（404）、`RULES_NOT_DRAFT {version, version_status}`（409）、`RULES_DRAFT_CHANGED {version, revision}`（409）、`RULES_INVALID {errors}`（400；`errors` = 前 10 個 `check_rules` 錯誤，格式為 `{code, params}`）、`RULES_ACTIVE_CHANGED {active_sha256, active_version}`（409）、`RULES_FILE_UNWRITABLE {path}`（409）、`RULES_FILE_MISSING {path}`（409）、`RULES_FILE_UNREADABLE {path}`（409）、`RULES_NO_FILE`（409：沒有設定檔，所以無處可寫）、`RULES_NOT_ACTIVATABLE {version, version_status}`（409：已忽略）、`RULES_BOTH`（400：同時給了 `rules` 與 `rules_version`）、`DRAFT_UI_ONLY`（403：只有瀏覽器工作階段能編輯或刪除草稿）、`PREVIEW_EXPIRED {preview_id}`（410）、`PREVIEW_SUPERSEDED`（409）、`PREVIEW_TOO_LARGE {files, limit}`（409）。
- 函式（全部接收 `conn`；文字上限 256 KB → `RULES_TOO_LARGE`）：`active(conn, config) -> ActiveRules`（`text, sha256, source, path, owned, version_id, error: {code, params}|null`；只讀取，不寫入）、`observe(conn, config) -> ActiveRules`、`create_version(conn, text, *, created_by, title=None, note=None, base_id=None) -> dict`（`ui` 為狀態 `draft`，其他為 `proposed`；提案必須通過 `check_rules`，否則 `RULES_INVALID`；草稿可以有錯誤）、`update_draft(conn, version_id, text, *, revision, title=None, note=None)`、`get_version(conn, version_id)`、`list_versions(conn, *, status=None, limit=50)`、`discard(conn, version_id, *, by)`（草稿被刪除；提案變成 `dismissed`）、`activate(conn, config, version_id, *, base_sha256, activated_by) -> ActiveRules`、`diff(old_text, new_text) -> {"hunks": [{"old_start", "new_start", "lines": [{"op": " "|"+"|"-", "text"}]}], "added", "removed"}`（3 行上下文，`difflib`）、`rule_changes(old: RuleSet, new: RuleSet) -> [{"name", "change": "added"|"removed"|"changed"|"moved"}]`（以名稱比對）。
- `activate`：先觀察；拒絕不是 `draft`/`proposed`/`retired`/`active` 或不是 ok 的版本；比較 `base_sha256` 與使用中的 SHA-256（`RULES_ACTIVE_CHANGED`）；把原文以原子方式寫在目標旁邊（暫存檔、fsync、改名；符號連結會寫穿；沿用既有檔案的權限，新檔案是 0600；由 drive-merge 管理的檔案會建立上層目錄）；然後在同一個交易中，把使用中的那筆資料設為取代、把這筆設為 `active` 並寫入 `activated_at`/`activated_by`，以及 `db.journal(conn, "rules_activate", detail={"version", "sha256", "path", "by"})`。啟用內建預設值，就是啟用一個由它們的文字做成的版本（由 `default` 建立）。寫入絕不碰 `config.toml`。

## D. 服務層

- `Service.ruleset(rules_text=None, *, rules_version=None)`：給了版本就用它的文字（任何狀態），否則用 `rules_text`，否則用使用中的規則（`rulestore.active`）；兩者都給時是 `RULES_BOTH`。使用中的檔案無效時，會拋出它的 `RuleError`，和現在一樣。
- `_build` 取出 `rules` 與 `rules_version`；對整併與重新整理，它解析規則並回傳規則參照。`propose_plan` 先觀察，然後存下 `params["rules"]`（這兩種類型一律存實際使用的文字）與 `params["rules_ref"]`。`rules_ref` 加入 `RESERVED_PARAMS`（呼叫者不能宣稱它）。`estimate_plan` 接受 `rules_version`，並在回覆中加上 `"rules_ref"`；它仍然不寫入任何東西（既有的 `total_changes` 測試不變）。其他類型不變。
- 新方法（讀取／提案區段）：`rules_state()` → `{"active": ActiveRules 轉成 dict + "check": 檢查摘要, "proposals": [...], "drafts": [...], "history": [...]}`（摘要：`{id, status, title, created_by, created_at, updated_at, activated_at, activated_by, sha256, ok, base_id, revision, rules, changes}`，其中 `changes` 是相對於使用中規則的 `rule_changes`）、`rules_default()` → `{"text", "sha256"}`、`rule_versions(status=None, limit=50)`、`rule_version(id)` → 摘要 + `text` + `check` + 相對於使用中規則的 `diff`、`save_rule_version(text, *, created_by, title, note, base_id)`、`update_rule_draft(id, text, *, revision, title, note)`、`discard_rule_version(id, *, by)`、`check_rules(text, against=None)` → `{"ok", "errors", "warnings", "outline", "layout", "diff", "changes", "sha256"}`（`against`：版本 id，預設是使用中的規則）、`transform_rules(text, op)` → `{"text", "check"}`、`explain_layout(...)`。
- `validate_rules(text)` 維持 `{"ok", "rules"}` / `{"ok": false, "error"}`，並加上 `"code"`、`"params"`、`"errors"`（全部）與 `"warnings"`。
- 預覽（`rulepreview.py`，新增；服務層的方法很薄）：`preview_rules(*, text=None, version_id=None, remotes=None, target_root="", path_prefix=None, mode="auto"|"exact", limit=100, filter="changed", conn=None)` 與 `preview_page(preview_id, *, filter, q=None, offset=0, limit=100)`。回覆：

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

  規則以名稱跨版本對應（使用中的規則沒有的規則，`active_files` 為 `null`；使用中那一側沒有對應規則時，`from` 為 `null`）。`filter` 是 `all`、`changed`、`fallback`、`in_place` 或 `rule:<name>`；`q` 是 `remote:path` 中不分大小寫的子字串。沒有抽樣時，`estimate_*` 等於實際數量。`mode: "exact"` 會拒絕超過 2,000,000 個檔案的範圍（`PREVIEW_TOO_LARGE`）。範圍內 100,000 個檔案以內是精確的，否則是 20,000 個檔案的抽樣（`sample_threshold` 依範圍的檔案數選定）。快取如 §7；每個快取鍵都包含 `db.snapshot` 與 `MAX(row_version)`。
- `preview_layout` 與 `POST /v1/layout/preview` 維持原樣（腳本與 MCP 使用它們）。
- 僅限人類的區段：`activate_rules(version_id, *, base_sha256, activated_by)`。

## E. HTTP

位於共用的路由器（`api.py`，兩個應用程式都有，一般驗證），再加上 `ui_api.py` 裡的一條人類通道路由。`RuleError` 與 `RulesError` 在兩個應用程式中都以 `{"error", "code", "params"}` 回覆（一律帶代碼）：`RuleError` 為 400，`RulesError` 為它自己的狀態。請求本文禁止多餘的鍵。`created_by` 對瀏覽器工作階段是 `ui`，對 bearer token 是 `api`（既有的 `created_by(request)` 掛鉤）。

| 方法與路徑 | 驗證 | 本文／查詢 | 回覆 |
|---|---|---|---|
| `GET /v1/rules` | 一般 | | `rules_state()`（會觀察使用中的檔案） |
| `GET /v1/rules/default` | 一般 | | `{"text", "sha256"}` |
| `POST /v1/rules/check` | 一般 | `{"text", "against"?: int}` | §D 的 `check_rules` |
| `POST /v1/rules/transform` | 一般 | `{"text", "op"}` | `{"text", "check"}`；400 `RULES_CANNOT_EDIT` |
| `GET /v1/rules/versions` | 一般 | `status`、`limit`（≤ 200） | `{"versions": [摘要]}` |
| `POST /v1/rules/versions` | 一般 | `{"text", "title"?, "note"?, "base_id"?}`（title ≤ 200、note ≤ 2000 字元） | 201 該版本（工作階段為草稿，bearer token 為提案） |
| `GET /v1/rules/versions/{id}` | 一般 | | `rule_version(id)` |
| `PUT /v1/rules/versions/{id}` | 一般，僅限工作階段 | `{"text", "revision", "title"?, "note"?}` | 該草稿；403 `DRAFT_UI_ONLY`、409 `RULES_NOT_DRAFT`、409 `RULES_DRAFT_CHANGED` |
| `DELETE /v1/rules/versions/{id}` | 一般 | | `{"ok"}`；草稿（僅限工作階段，否則 403 `DRAFT_UI_ONLY`）會被刪除，提案則被忽略（工作階段，或 bearer token 對 `api` 建立的提案） |
| `POST /v1/rules/preview` | 一般 | `{"text"?, "version_id"?, "remotes"?, "target_root"?, "path_prefix"?, "mode"?: "auto"\|"exact", "filter"?, "limit"? (≤ 500)}` | §D 的預覽；400 `RULES_BOTH`（同時給了文字與版本），文字無效時回覆它的第一個 `RuleError` |
| `GET /v1/rules/previews/{preview_id}/files` | 一般 | `filter`、`q`、`offset`、`limit`（≤ 500） | `page` 物件；410 `PREVIEW_EXPIRED` |
| `POST /v1/rules/explain` | 一般 | `{"text"?, "version_id"?, "remote", "path", "target_root"?}` | `{"edited": explain, "active": explain}` |
| `POST /v1/ui/rules/versions/{id}/activate` | 人類通道 | `{"base_sha256"}` | `{"active": ActiveRules}`；拒絕依此順序：人類通道自己的（`UNAUTHENTICATED`、`HUMAN_ONLY`、`APPROVALS_DISABLED`、`BAD_CSRF`、`BAD_ORIGIN`）、`RULES_VERSION_NOT_FOUND`、`RULES_NOT_ACTIVATABLE`、`RULES_INVALID`、`RULES_NO_FILE`、`RULES_ACTIVE_CHANGED`、`RULES_FILE_UNWRITABLE` |

- 預覽路由在應用程式專用的讀取連線上執行（在 `config.db_path` 上延遲開啟，帶 `PRAGMA query_only = ON`），不在目錄鎖之內，一次一個；較新的預覽會中斷正在執行的那個，被中斷的那個回覆 409 `PREVIEW_SUPERSEDED`。使用記憶體中的目錄時（`create_app` 的測試），它們在服務連線上、鎖之內執行。
- `POST /v1/plans`、`POST /v1/plans/estimate`：整併與重新整理接受 `params.rules_version`；`rules_ref` 和 `draft_id` 一樣被拒絕。
- `GET /v1/ui/config` 新增 `"rules": {"source", "path", "owned", "active_version", "proposals": n}`。
- 這裡沒有任何東西會回傳 `approval_code`、digest 或快照（把清除秘密的測試擴及新路由）。

## F. MCP 與 CLI

- `validate_rules` 除了 `error` 之外，也回傳 §D 的新欄位（`code`、`params`、`errors`、`warnings`）。
- 新工具 `get_rules()`（讀取）：使用中規則的來源、路徑、版本、SHA-256 與文字，以及等待中的提案（id、title、created_by、created_at）。
- 新工具 `propose_rules(rules_toml, title, note=None)`（`writes=True`）：驗證（錯誤以帶代碼的一般結果回傳，不存任何東西）、以 `mcp` 存為提案、回傳 `{version_id, rule_changes, warnings}`，並附上提醒：「Nothing is active. The human reviews proposal v<id> in the browser's Rules view and activates it there or with `drivemerge rules activate <id>` in their own terminal. Plans use the active rules unless you pass rules_version.」title ≤ 200、note ≤ 2,000 個字元。
- `preview_layout` 與 `propose_plan` 接受 `rules_version`；`propose_plan` 的 docstring 會說明它，並說明用提案建立的計畫會在審閱時顯示這點。
- 沒有任何工具能啟用、編輯或忽略版本。INSTRUCTIONS 新增：規則變更用 `propose_rules` 提出，絕不寫入規則檔；絕不聲稱提案的規則已經生效。
- CLI：`drivemerge rules show [--version N]`（來源、路徑、文字）、`drivemerge rules history [--json]`、`drivemerge rules activate N`（TTY 關卡同 `plan approve`：印出與使用中規則的差異並要求輸入 `activate` 這個字；拒絕情形與路由相同，以英文措辭）。`rules check` 會印出每個錯誤與它的行號。`.claude/settings.json` 在 `deny` 加入 `Bash(drivemerge rules activate:*)` 與 `Bash(*drivemerge rules activate*)`，並對 `~/.config/drive-merge/rules.toml` 加上 `Edit`/`Write` 的拒絕。
- DESIGN.md §6、§9 與 PLANS.md §5、§8 改為指向本文件，而不是說「只能用 CLI 或 Claude」（連同它們的 zh-TW 對照版）。

## G. 前端（`src/drivemerge/web/`）

與 WORKSPACE.md §F、EXPLORE.md §F、PLANS.md §E 和 QUARANTINE.md §F 的規則相同（沒有建置步驟、離線、CSP、來自 `vendor/` 的 Preact + htm、兩種語言、代碼由頁面措辭、測試會碰的元素帶 `data-testid`）。

- `rules.js`（新增）：規則頁（`#/rules`）、編輯器（`#/rules/edit/<id>?back=<encoded hash>`）、版本審閱（`#/rules/v/<id>`）、啟用對話框、唯讀的指令方塊、插入規則表單、預覽窗格（摘要卡片、每條規則的表格、轉移、含篩選分頁與「載入更多」的檔案清單）、說明。匯出給計畫表單用的 `RulesField({ value, onChange, backHash })`，以及給計畫審閱用的 `PlanRulesBox({ plan })`。
- `ruletext.js`（新增）：文字編輯器元件（`<textarea>` + 行號欄、每行的錯誤與警告標記、前往該行、大綱移動游標時保留捲動位置）與純函式輔助（行 ↔ 位移）。沒有第三方編輯器。
- `shell.js`：路由 `#/rules`、`#/rules/edit/<id>`、`#/rules/v/<id>`；`quarantine` 之後的側邊欄項目（`rules` 圖示），`VIEWS` 新增 `rules`（`Alt+7`），徽章 = 提案數量。
- `app.js`：與計畫輪詢一起輪詢 `GET /v1/rules/versions?status=proposed`（可見時每 10 秒一次）；`mcp` 提出的新提案會跳出附有「審閱」的「Claude 提出的」提示。
- `api.js`：`rules()`、`rulesDefault()`、`checkRules(text, against)`、`transformRules(text, op)`、`ruleVersions(query)`、`ruleVersion(id)`、`createRuleVersion(body)`、`updateRuleDraft(id, body)`、`discardRuleVersion(id)`、`previewRules(body)`、`previewFiles(id, query)`、`explainRules(body)`、`activateRules(id, body)`（人類通道標頭）。
- `plans.js`：整併表單上的 `RulesField`（取代 `cons.rulesHelp`）、新的重新整理表單（`#/plans/new/reorganize?remote=&root=&path=&rules=`；只列可寫入的雲端；預估與建立同整併）、兩個表單 URL 狀態中的 `rules`、它們參數中的 `rules_version`、整併與重新整理計畫審閱頁上的 `PlanRulesBox`，以及清單上的「新增：重新整理」按鈕。既有的 test id 不變。
- `explore.js`：只有「規則會把它放在哪裡？」這個資料列動作。
- `styles.css`、`icons.js`（`rules` 圖示）。
- 防抖：檢查 200 ms，預覽在最後一次按鍵後 600 ms，草稿自動儲存 2 秒；過期的回覆以序號丟棄；`PREVIEW_SUPERSEDED` 被忽略；`PREVIEW_EXPIRED` 會重新計算。
- `data-testid`：`view-rules`、`rules-active`、`rules-active-source`、`rules-outside-change`、`rules-active-invalid`、`rules-proposal-row-<id>`、`rules-draft-row-<id>`、`rules-history-row-<id>`、`rules-restore-<id>`、`rules-new-active`、`rules-new-default`、`view-rules-edit`、`rules-text`、`rules-gutter`、`rules-error-<n>`、`rules-warning-<n>`、`rules-goto-<line>`、`rules-outline`、`rules-outline-item-<index>`、`rules-move-up`、`rules-move-down`、`rules-remove`、`rules-insert`、`rules-insert-form`、`rules-save-state`、`rules-diff`、`rules-use-plan`、`rules-use-consolidate`、`rules-use-reorganize`、`rules-activate`、`rules-activate-dialog`、`rules-activate-confirm`、`rules-activate-error`、`rules-cli`、`rules-draft-conflict`、`rules-preview`、`rules-preview-scope`、`rules-preview-sampled`、`rules-preview-exact`、`rules-preview-changed`、`rules-preview-fallback`、`rules-preview-inplace`、`rules-rule-row-<name>`、`rules-transition`、`rules-files-tab-<filter>`、`rules-file-row`、`rules-files-more`、`rules-explain`、`view-rules-version`、`rules-dismiss`、`rules-edit-as-draft`、`rules-field`、`rules-field-edit`、`rules-field-active`、`plan-rules-box`、`plans-new-reorganize`、帶有 `data-kind="reorganize"` 的 `view-plan-new`、`reorg-remote`、`reorg-root`、`reorg-path`、`rail-rules`、`rail-rules-badge`。
- i18n 命名空間（兩種語言）：`nav.rules`、`nav.rulesTip`、`rules.*`、對應每個 `RULE_CODES` 項目的 `rulecheck.<CODE>`、對應每個 `RULE_WARN_CODES` 項目的 `rulewarn.<CODE>`、對應每個 `rulestore.CODES` 項目的 `err.<CODE>`、`rsrc.<source>`（file、default、version、inline）、`rstatus.<status>`、`rby.<created_by>`、`rchange.<change>`、`reorg.*`，以及取代 `cons.rulesHelp` 的句子。每個句子只使用伺服器會送出的參數（`line`、`rule_index`、`rule`、`key` 與該代碼自己的參數）。

## H. 測試

- `tests/test_classify.py`（擴充）：每個 `RULE_CODES` 代碼在它的拋出點都帶有 `line`、`rule_index`、`rule`、`key`；`TOML_SYNTAX` 的行與欄；一次 `check_rules` 中有多條壞規則的所有錯誤；不變的英文訊息與 `load_rules` 行為；`rule_spans`（標題上方的註解、尾端註解、多行字串裡的 `[[rule]]`、行內陣列規則 → `[]`）；`transform_rules` 的移動／移除／插入，其餘每一則註解與空行逐位元組保留，並以每個 `reason` 拒絕；警告；帶有 `sample_threshold`、`units` 與 `cancel` 的 `plan_layout`，以及沒有它們時逐位元組不變的輸出；單獨檔案、單元規則與後備規則的 `explain` 步驟。
- `tests/test_rulestore.py`（新增）：使用中檔案的解析（設定指定的、由 drive-merge 管理的、沒有的、設定了但檔案不存在的、記憶體中的設定絕不讀取家目錄）、觀察（外部修改只記錄一次、重用 SHA-256）、版本生命週期與唯一的 active 索引、草稿版本衝突、無效時被拒絕的提案、啟用（比較再交換、保留權限與符號連結的原子寫入、新的由 drive-merge 管理的檔案為 0600、journal 資料、`config.toml` 逐位元組不變、無法寫入的檔案、沒有設定檔）、重新啟用已被取代的版本來復原、`diff` 與 `rule_changes`。
- `tests/test_rules_preview.py`（新增）：精確對抽樣、確定性的抽樣、數量與估計值、以手工建構的目錄驗證 `changed`／`fallback`／`in_place`／`shared_targets`／`transitions`、篩選與分頁、`PREVIEW_EXPIRED`、快取命中（只有目標改變時不重算單元；標記或目錄改變時重算）、取消，以及預覽不寫入任何東西（`total_changes` 不變）。
- `tests/test_rules_api.py`（新增）：§E 的每個路由，在有掛載的兩個應用程式上；驗證（bearer → 提案、工作階段 → 草稿、`DRAFT_UI_ONLY`、啟用路由的拒絕順序，包括 `APPROVALS_DISABLED` 與 `HUMAN_ONLY`）；帶代碼的 `RuleError` 與 `RulesError`；預估與建立中的 `rules_version` 以及被拒絕的 `rules_ref`；整併與重新整理所存的 `params.rules` 與 `rules_ref`；兩個同時進行的預覽下的 `PREVIEW_SUPERSEDED`；清除秘密；`/v1/ui/config` 的 `rules`。
- `tests/test_service*.py`、`tests/test_plans.py`（調整）：釘住整併與重新整理 `params` 的斷言加上 `rules` 與 `rules_ref`；`estimate_plan` 仍然不寫入任何東西。
- `tests/test_mcp_server.py`（擴充）：`get_rules`、`propose_rules`（無效文字不存任何東西；提醒文字）、`rules_version` 傳遞、MCP 可呼叫的服務方法（沒有 `activate_rules`、`update_rule_draft`、`discard_rule_version`）、工具清單。
- `tests/test_cli.py`（擴充）：`rules show`、`rules history`、`rules activate` 的 TTY 關卡與拒絕、`rules check` 的行。`tests/test_config.py`：`rules_file`。`tests/test_migrations.py`：版本 2 的目錄升級到 3 並帶有該資料表。
- `tests/test_i18n_contract.py`（擴充）：每個 `RULE_CODES`、`RULE_WARN_CODES` 與 `rulestore.CODES` 代碼，在兩種語言中都有只使用它的參數的句子。
- `tests/test_ui_rules.py`（新增，瀏覽器，模擬 API，從 `test_ui_frontend.py` 匯入 `MockApi`/`Ui`）：側邊欄項目、徽章與 `Alt+7`；規則頁的各區塊與來源措辭（檔案、由 drive-merge 管理、預設、外部修改、無效的使用中檔案）；編輯器打字 → 檢查防抖 → 行號欄與清單中的錯誤與前往該行；大綱移動／移除／插入的請求本文與新文字；自動儲存與 `RULES_DRAFT_CHANGED`；預覽防抖、過期與被取代的回覆、抽樣標記、篩選分頁、分頁、說明；啟用對話框（差異、帶 `base_sha256` 的請求本文、`RULES_ACTIVE_CHANGED` 重新載入、`RULES_FILE_UNWRITABLE`）；唯讀的指令方塊；Claude 提案的提示、審閱、忽略、編輯成草稿；「用於計畫」→ 整併與重新整理表單，URL 帶 `rules`，預估與建立的本文帶 `rules_version`；`PlanRulesBox` 的「不是使用中的規則」；兩種語言。既有的瀏覽器測試維持通過（只更新釘住 `cons.rulesHelp` 的斷言）。
- 整套測試維持通過：`pytest`（含瀏覽器測試），但 `tests/test_docs_i18n.py` 在 `docs/RULES.zh-TW.md` 存在之前除外。

## I. 工作分配（兩位實作者並行）

§E 的 HTTP 形狀，以及 §B 與 §C 的代碼，就是介面；在後端完成之前，前端以 `MockApi` 運作。沒有任何檔案同時屬於兩方。

| 實作者 | 擁有（建立或編輯） |
|---|---|
| 後端 | `src/drivemerge/classify.py`、`rulestore.py`（新增）、`rulepreview.py`（新增）、`service.py`、`config.py`、`schema.sql`、`db.py`、`api.py`、`ui_api.py`、`mcp_server.py`、`cli.py`、`.claude/settings.json`；`tests/test_classify.py`、`test_rulestore.py`、`test_rules_preview.py`、`test_rules_api.py`、`test_api.py`、`test_service*.py`、`test_plans.py`、`test_mcp_server.py`、`test_cli.py`、`test_config.py`、`test_migrations.py`；對 `docs/DESIGN.md` 與 `docs/PLANS.md`（及其 zh-TW 對照版）的修改 |
| 前端 | `src/drivemerge/web/rules.js`（新增）、`ruletext.js`（新增）、`shell.js`、`app.js`、`api.js`、`plans.js`、`explore.js`、`i18n.js`、`styles.css`、`icons.js`；`tests/test_ui_rules.py`（新增）、`test_ui_frontend.py`（§E 的 `MockApi` 路由，以及 `cons.rulesHelp` 的斷言）、`test_plans_view.py`、`test_i18n_contract.py`；`docs/RULES.zh-TW.md` |

`test_i18n_contract.py` 會匯入代碼 tuple（`classify.RULE_CODES`、`classify.RULE_WARN_CODES`、`rulestore.CODES`）；前端實作者依 §B 與 §C 的清單撰寫它，兩邊合併之後它就會變綠。
