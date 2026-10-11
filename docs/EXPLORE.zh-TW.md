# 探索與比較（Explore and Compare）

[English](EXPLORE.md) | **繁體中文**

> 本文件為視覺化工作區（[WORKSPACE.zh-TW.md](WORKSPACE.zh-TW.md)）新增三個唯讀檢視：**總覽（Overview）**、**探索（Explore）**與**比較（Compare）**。前半是設計；後半（實作合約）對實作者具有約束力。

## 1. 問題

四個雲端各自長大，裡面放的是*相似*的樹：Drive 上的 `School`、Dropbox 上的 `School (1)`，OneDrive 的 `Backup/2021` 底下又有一半。雙窗格工作區擅長在你知道要搬什麼之後去*搬*，但它回答不了更前面的問題：

1. **它在哪裡？**「我論文的所有副本，不管叫什麼名字，都在哪裡？」
2. **我的雲端怎麼重疊？**「Dropbox 有多少已經在 Drive 上了？哪些只存在於單一雲端、而且會隨它一起消失？」
3. **這兩個資料夾差在哪裡？**「`gdrive:School` 與 `dropbox:School (1)` 看起來很像：哪些檔案相同、哪些不同、哪些只存在於一邊、哪些只是改了名字或搬了位置？」

每個答案都必須遵守本專案的證據原則：只有取得強雜湊證明（內容群組）才能宣稱*相同*；其他一律是「不同」（以大小或不相等的雜湊證明）或「未證明」。

## 2. 資訊架構

畫面左側有一條窄的**導覽列（rail）**，在四個檢視之間切換。工作區維持原樣；新增的三個檢視都是唯讀的，並且會把你帶進工作區。

| 檢視 | 要回答的問題 | 主要元素 |
|---|---|---|
| 總覽 | 我的雲端怎麼重疊？ | 雲端卡片、交集圖（UpSet 風格）、相似資料夾清單 |
| 探索 | 它在哪裡？ | 附篩選標籤的查詢列；結果依*內容*分組，每個雲端一欄存在狀態 |
| 比較 | 這兩個資料夾差在哪裡？ | 任意雲端上兩個資料夾的一棵對齊差異樹，附狀態篩選與詳細資料面板 |
| 工作區 | 該怎麼整理？ | 既有的雙窗格、草稿、預覽與核准 |

為什麼用交集圖而不是文氏圖：四個集合的文氏圖有 15 個區域，用大小相等的圓畫不出來，也讀不懂。UpSet 圖把每一種確切的雲端組合畫成一根長條（依位元組數排序），下方配一個圓點矩陣，可擴充到任意數量的雲端，而且每根長條都是通往其中檔案的連結。

為什麼結果依內容分組：使用者的問題是關於*內容*，不是關於各個項目。一份 PDF 的十個副本是一筆結果、十個位置，存在狀態欄一眼就能看出它存在於哪些雲端。不符合搜尋字詞的副本（改過名字的）仍會列在它的內容底下，並標示為不符合。

為什麼比較先依相對路徑對齊、再依內容對齊：兩個同步過後又各自分歧的資料夾，大多仍保有相同的相對路徑，所以路徑對齊就能解釋大部分的列；內容對齊接著找出資料夾內被改名或搬動的檔案，單純的路徑差異會把它顯示成一次刪除加一次新增。

### 交叉連結（這些檢視是同一個工具，不是四個）

- 總覽 → 探索：交集圖的一根長條會開啟探索，並篩選成恰好那一組雲端（`only:dropbox,onedrive`）。雲端卡片上的「僅存在這裡」數字會開啟 `only:<cloud>`。
- 總覽 → 比較：每一列相似資料夾（來自建議引擎）都有一個比較按鈕。
- 探索 → 比較：在兩個不同資料夾裡有副本的結果，會提供「比較它們所在的資料夾」（兩個父資料夾）。
- 探索 / 比較 → 工作區：「顯示位置（Reveal）」會開啟工作區，並讓該項目在左窗格取得焦點。
- 工作區 → 比較：資料夾的列選單有「與…比較」，它會開啟比較，左側放這個資料夾，並讓焦點停在右側的選擇器。
- 工作區的建議抽屜：每張卡片在「加入」旁邊多了一個比較按鈕。
- 比較 → 草稿：「左側併入右側」（以及反向）會暫存一個移動動作；只存在於一邊的列提供「搬到另一邊」。暫存走的是與拖放相同的 `POST /v1/drafts/{id}/actions`，警告與拒絕也相同，提示訊息會提供「開啟工作區」。

## 3. 總覽

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

- 雲端卡片：檔案數、位元組數、最近一次完成的掃描、唯讀標章、「僅存在這裡」（在其他雲端沒有副本的內容：風險數字），以及「無可比對的雜湊」（沒有內容群組的檔案：橋接之前的 iCloud）。最後這個數字不是零時，卡片會說明這個雲端的重疊被*低估*了，並顯示 `drivemerge bridge` 指令。
- 圖表：計量方式可在內容數與位元組數之間切換；分類篩選會重新計算（只看照片、只看文件）。長條依計量方式排序，空組合永遠不出現，最多畫 24 根長條，另加一根「其他組合」。游標移上去與鍵盤聚焦時顯示確切數字；按 Enter 或點擊會開啟探索。
- 相似資料夾：兩種類型（`/v1/suggestions`）的前 10 個建議，措辭與抽屜相同，每一個都有比較（前兩個資料夾；超過兩個時是一個小選單）。

## 4. 探索

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

**查詢語言**（在同一個輸入框中輸入，由頁面解析成篩選標籤；一般字詞仍是字詞）：

| 寫法 | 意思 | 請求參數 |
|---|---|---|
| `word`, `"two words"` | 名稱或路徑包含它（支援 CJK，與搜尋相同） | `q` |
| `in:gdrive` | *符合的那份副本*在該雲端 | `in` |
| `on:gdrive` | 該內容在該雲端有一份副本（可重複：每一個都要有） | `on` |
| `not:gdrive` | 該內容在該雲端沒有副本（可重複） | `not_on` |
| `only:gdrive,dropbox` | 該內容恰好只在這些雲端 | `only_on` |
| `copies:1`, `copies:>2`, `copies:2..4` | 現存副本數 | `copies_min`, `copies_max` |
| `type:pdf` | 副檔名 | `ext` |
| `cat:image` | 分類（`categories.py`） | `category` |
| `size:>10MB`, `size:<1k`, `size:1M..1G` | 大小（B、K/KB、M/MB、G/GB、T/TB；以 1024 為進位） | `min_size`, `max_size` |
| `modified:2023`, `modified:2023-06`, `modified:>2024-01-01`, `modified:2021..2022` | 修改時間 | `modified_after`, `modified_before` |
| `path:School/Thesis` | 位於此路徑底下（搭配單一雲端的 `in:` 時限定該雲端，否則任何雲端） | `path_prefix` |

不認得的條件（`foo:bar`）或格式錯誤的值會以紅色標籤保持可見、附上提示文字，而且不會送出。`?` 按鈕會開啟這張表。分面側欄會切換相同的篩選標籤，所以輸入框永遠顯示完整的查詢。查詢存在網址裡（`#/explore?q=…`），因此「上一頁」可用，查詢也能加入書籤。

**結果**：每個內容一列。存在狀態欄依設定順序每個雲端一欄，各用該雲端的顏色：雲端有副本時畫實心圓點（有多份時附上數量），沒有時畫空心圓環；每個格子都有可供輔助技術讀取的標籤（「on dropbox, 2 copies」）。沒有內容群組（未經雜湊辨識）的內容會畫成虛線外框，並附上提示「identity not proven: copies elsewhere may exist under a different hash」。可依相關度（有字詞時為預設）、大小、修改時間、名稱或副本數排序。一列可展開成它所有的副本，每個副本都有顯示位置（Reveal）；當兩個副本位於不同資料夾時，還有「比較它們所在的資料夾」。

## 5. 比較

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

- **選擇器**：兩邊各有一個雲端下拉選單與一個路徑輸入框（資料夾自動完成來自 `GET /v1/tree`）；⇄ 會左右對調。兩邊都設好時比較就會執行，這一對會放進網址（`#/compare?l=gdrive:School&r=dropbox:School%20(1)`）。
- **一列的狀態**（§B）：`same`（已證明相同）、`unverified`（顯示為「probably same — not proven」：路徑相同，沒有證據顯示不同，也沒有證據顯示相同）、`differs`（已證明不同）、`moved`（相同內容在另一邊的另一個相對路徑）、`left_only`、`right_only`。只存在於一邊的列還會說明它的內容在別處是否有副本（兩個比較資料夾之外，⧉ 加上數量），或根本沒有（`unique`，警告色調：失去這一邊就失去這份內容）。
- **樹**：列依相對路徑分組成資料夾。資料夾列帶有一條代表其子項目狀態的小型堆疊長條，以及不相同檔案的數量；檔案全部都是 `same` 的資料夾會收合，而「顯示相同的」關閉（預設）時會被隱藏。篩選標籤切換各個狀態；標頭中的數字永遠是整個比較的數字。
- **詳細資料面板**：兩邊的路徑、大小、修改時間、雜湊證據（哪一種雜湊相符或不同，或「no comparable hash」）、該內容在別處的副本，以及顯示位置按鈕。
- **暫存**：標頭按鈕「左側併入右側」與「右側併入左側」會暫存一個移動整個資料夾的動作（目的地：另一個資料夾的父資料夾，`new_name`：另一個資料夾的名字，這使它成為合併；相同的檔案變成有見證的去重，同名但不同的檔案照常在預覽中成為衝突）。只存在於一邊的列提供「搬到另一邊」（目的地：另一邊相同的相對父資料夾，它必須存在；否則按鈕會說明那個資料夾在那一邊不存在）。比較讀的是 catalog，不是草稿：當開啟中的草稿有生效的步驟時，會有一則說明指出比較顯示的是雲端上次掃描時的樣子。

## 6. 這些檢視絕不做的事

它們絕不寫入雲端、絕不更動 catalog，也絕不建立計畫。從比較暫存，是把一個動作加進開啟中的草稿，與拖放完全一樣；其餘路徑（預覽、模擬器、輸入確認碼的核准）不變。比較寧可拒絕也不截斷：靜靜漏掉檔案的差異會宣稱那些檔案不存在。

## 7. 已知限制（v1）

- 比較與探索讀的是 catalog（雲端上次掃描時的樣子），不是草稿的變更後視圖。
- 空資料夾不會被比較（catalog 的資料夾不帶內容）。
- 身分只認精確的群組；「可能」重複（同名同大小）不會合併成一筆探索結果，比較也只在它們位於相同相對路徑時才把它們回報為 `unverified`。
- 探索每次查詢最多整理 20,000 個符合的檔案；超過時回答會說明已被截斷，並請你縮小查詢範圍。

---

# 實作合約

以下全部具有約束力。請先讀：[WORKSPACE.zh-TW.md](WORKSPACE.zh-TW.md)（尤其是 §D、§F 與 §G）、`src/drivemerge/search.py`、`identity.py`（`content_key`，證明規則）、`db.py`（`quarantine_dir`、`outside_quarantine`）、`dirstats.py`、`ui_api.py`、`api.py`、`service.py`，以及 `src/drivemerge/web/` 底下的每個檔案。

## A. 共通規則

- 唯讀：這個功能的任何模組都不寫入 catalog。
- 「live 檔案」= `present = 1 AND is_dir = 0`，且位於隔離資料夾之外（`db.outside_quarantine` 或其 SQL 等價寫法）。隔離區中的檔案不會出現在這些檢視的任何一處。
- 內容鍵 = `identity.content_key(row)`（`g:<group>` 或 `e:<entry id>`）。只有兩個檔案共用同一個非空的 `content_group`，才算**已證明相同**。當兩者的大小都已知卻不相等，或 `search.HASH_KEYS` 中某一種雜湊在兩者都非空卻不相等時，算**已證明不同**。除此之外，兩個方向都**未證明**。
- 空檔案（大小為 0）不算內容，不列入涵蓋範圍與副本數的計算（它們是佔位檔，`dup_files` 本來就這樣看待），但在比較中它們和其他檔案一樣依路徑比較（兩個位於相同路徑的空檔案，分到群組時為 `same`，否則為 `unverified`）。
- 遠端的順序是設定順序（`remotes.id`）。跨越 HTTP 邊界的是遠端的名稱，不是 id。
- 錯誤遵循 WORKSPACE.zh-TW.md 的 §D：英文的 `error`、一個 `code`，以及給頁面用的 `params`。新的 code：`NOT_A_FOLDER`（409：該路徑不是那個遠端的 live 資料夾；`''` 永遠是資料夾；params `{remote, path}`）、`COMPARE_NESTED`（409：其中一個資料夾就是另一個，或在同一個遠端上位於另一個底下；params `{left, right}`，各為 `{remote, path}`）、`COMPARE_TOO_LARGE`（409：某一邊的 live 檔案超過 `max_files`；params `{side: "left"|"right", files, limit}`）、`QUARANTINE`（409：被比較的資料夾就是隔離資料夾，或位於其底下）、`BAD_QUERY`（400：探索或涵蓋範圍的某個參數無效；params `{param}`）。不認得的遠端名稱是 404 `NOT_FOUND`。

## B. 比較（`src/drivemerge/compare.py`）

```python
class CompareError(ValueError): code: str; params: dict
def compare_folders(conn, left: tuple[str, str], right: tuple[str, str], *, max_files: int = 20000) -> dict
```

輸入是 `(remote_name, path)`，路徑採用 catalog 的慣例（去掉 `/`）。檢查順序：不認得的遠端（`NOT_FOUND`）、`QUARANTINE`、`NOT_A_FOLDER`、`COMPARE_NESTED`、`COMPARE_TOO_LARGE`（可用時以 `dir_stats.files` 在載入之前就拒絕；否則由列重新計數）。

演算法：
1. 載入每個根底下的 live 檔案。`rel` = 相對於根的路徑。比對鍵 `k(rel)` = `rel` 的 NFC，當**任一**遠端不分大小寫時再做 casefold。
2. **路徑階段。** 對兩邊都有的每個鍵，把檔案配成對。Drive 可以在同一個路徑上放好幾個檔案：先配已證明相同的，再依 entry id 順序配其餘的；剩下的檔案進入步驟 3。一對的狀態：已證明相同則為 `same`；已證明不同則為 `differs`；否則為 `unverified`。
3. **內容階段。** 在尚未配對的檔案中，把共用同一個非空 `content_group` 的左、右檔案配成一對 → `moved`（同一群組內依 `rel` 順序，一對一配對）。
4. 其餘的是 `left_only` / `right_only`。對其中每一個，當它的 `content_group` 非空時，數出該群組在兩個被比較的根之外（任何遠端）的 live 副本數：`elsewhere = {"count": n, "examples": [up to 3 {"remote","path"}]}`；`unique = (n == 0)`，而且另一個根裡也沒有副本。沒有群組的單邊檔案是 `elsewhere = None`、`unique = None`（未知）。
5. 一對的 `evidence`：`same` 為 `{"kind": "group"}`；`differs` 為 `{"kind": "size"}` 或 `{"kind": "hash", "hash": "<type>"}`；`unverified` 為 `{"kind": "none"}`。

結果：
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
- `rows` 依 `rel` 的自然、不分大小寫順序排序（對 `moved` 與 `right_only` 的列，`rel` 在有左側時是左側的 rel，否則是右側的 rel；`rel_right` 只在 `moved` 設定）。
- `summary` 的位元組數，成對與僅左側的列計左側檔案，`right_only` 計右側檔案。
- `similarity` = `(same + moved) / (same + moved + unverified + differs + left_only + right_only)`，以成對與單獨者的檔案數計算（一對只算一次）；兩個空資料夾為 `1.0`。
- 效能：兩次索引範圍查詢（`entries_path`），對單邊群組做一次 `elsewhere` 的群組查詢；不對每一列各查一次。每邊 20,000 個檔案，在筆電上必須遠低於一秒內回答。

## C. 涵蓋範圍（`src/drivemerge/coverage.py`）

```python
def coverage(conn, *, category: str | None = None, max_bars: int = 24) -> dict
```
針對 `size > 0` 的 live 檔案（可選擇只取一種分類），依每個內容鍵蒐集持有副本的遠端集合。
```json
{
  "remotes": [{"name", "provider", "writable", "files", "bytes", "only_here": {"contents", "bytes"},
               "unverified_files", "last_scan": {"id", "status", "finished_at"} | null}],
  "totals": {"contents", "bytes", "stored_bytes", "redundant_bytes", "copies"},
  "intersections": [{"remotes": [names], "contents", "bytes", "copies", "stored_bytes"}],
  "other": {"combinations", "contents", "bytes", "copies", "stored_bytes"} | null
}
```
- 一個內容的 `bytes` = 它的大小只算一次；`stored_bytes` = 所有副本的總和。`redundant_bytes = stored_bytes − bytes`。
- `intersections` 依 `bytes` 由大到小、再依 `contents` 由大到小、再依名稱排序；列出前 `max_bars` 種組合，其餘加總成 `other`。
- `unverified_files`：該遠端上 live、size > 0 且沒有內容群組的檔案（各自算一個內容：頁面會警告重疊被低估）。
- 在 SQL 中用內容鍵做 `GROUP BY`（一個群組的遠端集合用 `GROUP_CONCAT(DISTINCT remote_id)`），再於 Python 中彙總這些集合。除了這一趟之外，不得對副本逐一做 Python 迴圈。

## D. 探索（`src/drivemerge/explore.py`）

```python
def explore(conn, q: str | None = None, *, in_remote=None, category=None, ext=None, min_size=None, max_size=None,
            modified_after=None, modified_before=None, path_prefix=None, on: list[str] = (), not_on: list[str] = (),
            only_on: list[str] | None = None, copies_min=None, copies_max=None, sort="relevance",
            limit=50, offset=0, scan_cap=20000) -> dict
```
- 命中：符合 `q` 與屬性篩選的 live 檔案，規則與 `search.search` **完全相同**（重構 `search.py`，讓 WHERE 建構器共用；`search.search` 必須維持它的行為與測試不變）。`in_remote` 把命中限制在單一遠端。
- 依內容鍵把命中分組；對每個群組鍵載入它**所有**的 live 副本（包含改過名字的副本，每個都帶 `matched: bool`）。存在狀態篩選與 `copies_*` 作用在完整的副本集合上：`on` = 列出的每個遠端都持有副本；`not_on` = 沒有任何一個持有；`only_on` = 遠端的集合恰好等於它；副本數計入每一個 live 副本。
- 最多讀取 `scan_cap` 筆命中（依命中順序）；超過時 `truncated: true`。
- 排序：`relevance`（查詢含 FTS 字詞時為符合副本中最佳的 bm25 排名，否則依名稱）、`size` 由大到小、`mtime` 由新到舊（最新的副本）、`name`、`copies` 由多到少。同分時依名稱，再依鍵。
- 分面（在分頁之前，針對篩選後的群組）：`{"remotes": [{"name", "contents"}], "copies": {"1": n, "2": n, "3+": n}, "categories": [{"category", "contents"}], "exts": [top 12 {"ext", "contents"}]}`。
```json
{"total": 213, "truncated": false, "facets": {…},
 "results": [{"key": "g:7", "name": "thesis_final.pdf", "size": 4212331, "category": "document", "ext": "pdf",
              "mtime": "…", "verified": true, "remotes": ["gdrive", "dropbox"], "copies_count": 3,
              "copies": [{"entry_id", "remote", "path", "name", "mtime", "matched"}]}]}
```
`limit` ≤ 200。任何參數中不認得的遠端名稱是 404 `NOT_FOUND`；互相矛盾的存在狀態篩選（`on` ∩ `not_on`）只會回傳空結果。

## E. 服務、HTTP 與 MCP

- `Service.compare(left, right, max_files=…)`、`Service.coverage(category=None)`、`Service.explore(**params)`。
- 路由放在 `api.py` 的共用 router 上（因此 `drivemerge serve` 與 `drivemerge ui` 都有，認證方式與 `/v1/search` 相同）：

| 方法與路徑 | 查詢參數 | 回應 |
|---|---|---|
| `GET /v1/compare` | `left_remote, left_path, right_remote, right_path, max_files`（≤ 100000） | §B |
| `GET /v1/coverage` | `category`, `max_bars`（≤ 64） | §C |
| `GET /v1/explore` | §D 的參數；清單參數以逗號分隔（`on=gdrive,dropbox`） | §D |

  `CompareError` 對應到它自己的狀態碼（`NOT_FOUND` 為 404、`BAD_QUERY` 為 400，其餘為 409），在 UI app（有 code 的處理器）與 REST app 中都回傳 `{"error","code","params"}`。
- MCP：唯讀工具 `compare_folders(left_remote, left_path, right_remote, right_path)`（答案中的列最多 300 筆，另附完整的 summary 與一個 `rows_truncated` 旗標）與 `coverage(category?)`。只用英文訊息，與每一個 MCP 工具相同。

## F. 前端（`src/drivemerge/web/`）

規則與 WORKSPACE.zh-TW.md 的 §F 相同：沒有建置步驟、離線可用、對 CSP 安全（沒有需要 `unsafe-eval` 的行內 script 或 style 屬性；CSP 允許 `style=` 屬性）、Preact + htm 取自 `vendor/`、每一個使用者看得到的字串都經過 `t()` 且鍵同時存在於**兩種**語言、伺服器訊息由 `code` + `params` 組成語句、測試會碰到的元素都有 `data-testid`。

檔案：
- `shell.js`：導覽列與路由器。路由放在 `location.hash`：`#/overview`、`#/explore?<params>`、`#/compare?l=<remote>:<path>&r=<remote>:<path>`、`#/workspace`。沒有路由（或權杖交換的 `#token=`）就是工作區，所以每一個既有的測試仍然載入工作區。顯示其他檢視時，工作區仍保持掛載（以 `hidden` 屬性隱藏），所以它的草稿、輪詢與窗格狀態在切換之間得以保留。`Alt+1…4` 切換檢視；焦點不在文字欄位時，`/` 會把焦點移到探索的輸入框（並切換到探索）。導覽列是一個 `nav`，目前這一項帶有 `aria-current="page"`。
- `clouds.js`：每個遠端一個穩定的顏色，取自固定的 8 色調色盤，各有淺色與深色變體（CSS 自訂屬性 `--cloud-0` … `--cloud-7`，在兩種配色中對表面的對比都選在 ≥ 3:1），依設定順序指派；元件 `CloudDot`、`PresenceCells`。
- `query.js`：`parseQuery(text, remoteNames) -> {params, chips, errors}` 與 `formatQuery(params) -> text`，皆為純函式（§4 的表）。來回轉換：`parseQuery(formatQuery(p)).params` 等於 `p`。
- `overview.js`、`explore.js`、`compare.js`：§3–§5 的檢視。圖表是元件自己畫的行內 SVG（不使用圖表函式庫）；長條可聚焦（`role="button"`，標籤含數字）。
- `api.js`：`compare`、`coverage`、`explore`。
- `app.js`、`tree.js`、`panels.js`：只放 §2 的掛鉤（導覽列的宿主、資料夾列選單中的「與…比較」、建議卡片上的比較、其他檢視會呼叫的 `reveal` 入口）。這些修改要小；不要重構工作區。

比較樹的繪製：在頁面上依 `rel` 建出資料夾節點，只繪製展開的節點；最多顯示 2,000 列，其後是「顯示更多」列。鍵盤：上／下移動，右／左展開／收合，Enter 開啟詳細資料；樹是一個 `role="tree"`。

探索：輸入防抖 250 ms，取消過時的回應（忽略請求不是最新一筆的回應），以「載入更多」分頁（一次 50 筆）。

新的 i18n 命名空間：`nav.*`、`ov.*`、`ex.*`、`q.*`、`cmp.*`、`cloud.*`，外加 `err.NOT_A_FOLDER`、`err.COMPARE_NESTED`、`err.COMPARE_TOO_LARGE`、`err.BAD_QUERY`（`err.QUARANTINE` 已經存在）。狀態標籤：`cmp.status.<status>`；證據：`cmp.evidence.<kind>`。

## G. 測試

- `tests/test_compare.py`：用手工建立的 catalog 涵蓋每一種狀態（路徑階段、Drive 同路徑重複檔案、搬動檔案的內容階段、不分大小寫比對、NFD 對 NFC 的名稱、`elsewhere` / `unique`、排除隔離區、每一個拒絕 code 與它們的順序、相似度的邊界情況、`max_files`）。
- `tests/test_coverage.py`、`tests/test_explore.py`：交集與 `other` 的分組、`only_here`、`unverified_files`、分類篩選；探索的分組與改名副本（`matched`）、每一種存在狀態篩選、副本數篩選、各種排序、分面、`truncated`，以及 `search.search` 的結果不變。
- 三個路由透過兩個 app（bearer 與 session）的 API 測試，包含有 code 的錯誤。
- 兩個工具的 MCP 測試。
- `tests/test_ui_frontend.py`（模擬 API）：導覽列切換與 `aria-current`、由 hash 決定的路由與「上一頁」、切換之間工作區狀態保留、`parseQuery`/`formatQuery` 的來回轉換與紅色標籤、探索結果與存在狀態格子、分面切換編輯查詢文字、總覽長條以 `only:` 開啟探索、比較樹的狀態與篩選、詳細資料面板、合併暫存的請求本文、單邊「搬到另一邊」的本文、由 `code` 組成的拒絕訊息、兩種語言。
- i18n 契約測試也必須涵蓋新的錯誤 code（擴充 `test_i18n_contract.py`，使 `compare.py` 與路由可能拋出的每一個 code 在兩種語言都有 `err.<CODE>`）。
- 整套測試維持通過：`pytest`（含瀏覽器測試）。
