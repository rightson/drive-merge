# 在瀏覽器中管理隔離區

[English](QUARANTINE.md) | **繁體中文**

> 本文件為視覺化工作區（[WORKSPACE.md](WORKSPACE.zh-TW.md)、[EXPLORE.md](EXPLORE.zh-TW.md)、[PLANS.md](PLANS.zh-TW.md)、[ONBOARDING.md](ONBOARDING.zh-TW.md)）新增「**隔離區**」（Quarantine）頁面：每個已執行的計畫把哪些檔案放進了隔離資料夾、在哪個雲端、佔多少空間、從什麼時候開始；把它們還原（整個計畫、一個資料夾或單一檔案）；以及在不再需要時永久清除。第一部分是設計；第二部分（實作合約）對實作者具有約束力。

## 1. 問題

drive-merge 從不刪除：刪除是把檔案移到同一個雲端上的 `.drive-merge-quarantine/<plan id>/<original path>`（[DESIGN.md](DESIGN.zh-TW.md) §7.3），大膽整理才因此安全。頁面現在已經能執行所有會讓檔案進入隔離區的計畫：去重、含來源清理的整併、從工作區發起的跨雲端搬移，以及釋放空間的 `release` 計畫。但讓整件事收尾的兩個步驟，頁面上還缺：

- **還原只能在終端機做。** `drivemerge plan restore <id>` 沒有對應的路由；每一則失敗橫幅、樹狀圖中隔離資料夾的提示，以及 `QUARANTINE` 的拒絕訊息，都要人去開終端機。在頁面上做了去重、事後反悔的人，得離開頁面。
- **清除只在 `release` 計畫之後提供**，而且只針對那個計畫、只在它剛跑完的時候。去重、整併或工作區搬移留下的隔離檔案，只能從終端機清除，頁面上也完全沒有說明哪裡佔著空間、在哪個雲端、從何時開始。
- **沒有任何地方列出隔離區。** 樹狀圖只把那個資料夾鎖起來；「裡面有什麼、為什麼在那裡、可以安全清空嗎？」沒有答案。
- **還原不會把目錄標成落後。**「目錄已落後」橫幅（WORKSPACE.md §5）是由已執行的操作驅動的；還原會把它的操作標為 `skipped`，不被計入，所以還原之後，樹狀圖仍默默顯示檔案還在隔離區。

這個頁面要回答的問題：

1. **隔離區裡有什麼？** 每個雲端：檔案數、位元組數、有多少已超過保留期限；每個計畫：誰提出的、何時執行、移除了什麼、為什麼（該操作的原因及其見證檔，也就是替它背書的那份副本）。
2. **能把它拿回來嗎？** 把一個檔案、一個資料夾或整個計畫移除的檔案放回原處，絕不覆蓋任何東西。
3. **現在能釋放那些空間嗎？** 清除已過保留期限的檔案（或刻意清除全部），並且每個見證檔都先重新證實一次。

## 2. 原則

- **只處理計畫放進去的東西。** 這個頁面只作用於已執行計畫的隔離操作，其他一概不碰：每個項目都是一筆 `plan_ops` 紀錄，執行器曾把它的來源移進 `<quarantine_dir>/<plan id>/`。沒有任意路徑、沒有「還原這個雲端的某個資料夾」；隔離資料夾裡不是這份目錄的任何計畫放進去的檔案，會以*來源不明*的身分唯讀列出（§6）。
- **不增加新的改動雲端的方式。** 還原和清除是 DESIGN.md §7.3 已經為核准過的計畫定義的兩個收尾步驟，由與 `drivemerge plan restore`、`drivemerge plan purge` 相同的執行器函式執行，並且受相同的關卡約束：人類通道（用啟用核准的 `drivemerge ui` 開啟的瀏覽器工作階段）、`[safety] execution_enabled`，以及要輸入的確認字。還原只會把檔案從該計畫自己的隔離資料夾，移回它原本的確切路徑、同一個雲端，遇到被佔用的路徑就拒絕；清除只會刪除該計畫隔離資料夾底下的東西，而且是在即時重新證明見證檔仍然存在且內容相同之後。兩者都不能建立新內容、不能覆蓋、也碰不到隔離區以外。
- **先檢查再動手。** 每個動作都先依目錄做一次唯讀檢查（現在有多少原路徑被佔用、有多少隔離副本是上次掃描沒看到的、有多少見證檔已不在或未證實、有多少太新還不能清除），讓人在輸入確認字之前就知道什麼會被保留。執行器會即時再檢查一遍所有東西；目錄檢查只是參考，絕不取代它。
- **保留期限看得見，但絕不自動執行。** 每個項目都顯示它何時進入隔離區、何時到期（`quarantined_at` + `[safety] quarantine_retention_days`，預設 30）。沒有任何東西會被定時清除。
- **頁面負責所有的措辭。** 伺服器只送代碼和參數；句子由頁面用兩種語言寫。
- **唯讀備援。** 沒有核准功能時，這個頁面是同樣的清單，而每個動作改成顯示對應的終端機指令原文。

## 3. 資訊架構

第七個側欄項目「**隔離區**」（`#/quarantine`、`Alt+6`），排在計畫之後。它的徽章顯示有項目已過保留期限的計畫數。

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

- **雲端卡片**：已保留的檔案數與位元組數、其中已過保留期限的部分、該雲端的容量（已用 / 總量，以及「清除後」，當該雲端是容量已知的連線時）、以及一個把計畫清單篩選到該雲端的連結（`#/quarantine?remote=dropbox`）。
- **計畫清單**：每個仍有檔案留在隔離區的計畫一列，最舊的在前：編號、種類與計畫清單所用的一行摘要、「由 Claude 提出」徽章、雲端、保留的檔案數與位元組數、執行時間、到期時間。已沒有東西留下的計畫（所有項目都已還原或清除）收進「**已處理完畢**」。點一列即開啟該計畫的隔離區（§4）。
- **清除所有已過保留期限的檔案…**：在同一個工作中，一個計畫接著一個計畫，清除每個到期日已過的項目（也可以只清除篩選中的那個雲端）。它會先開啟清除檢查（§5）。

## 4. 一個計畫的隔離區（`#/quarantine/<id>`）

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

- 項目依它們的**原始**路徑（也就是它們要回去的地方）分組成資料夾。每一列：名稱、大小、狀態、到期日，以及當目錄檢查發現問題（原路徑被佔用、隔離副本上次掃描沒看到、見證檔不見了）時的警告記號。資料夾列顯示數量與位元組數，並有三態核取方塊。
- **狀態篩選**：`held`（預設）、`restored`、`purged`、`all`。已還原與已清除的項目是歷史紀錄：沒有核取方塊、列為淡色，並顯示發生的時間。
- **細節面板**：原始路徑、隔離路徑、該操作的原因、見證檔（替它背書的那份副本：刪除操作的見證檔，或是搬移到內容相同之處的目的地）及它在目錄中的狀態（還在且內容相同／還在但無法證明相同／不在目錄中）、進入隔離區與到期的日期。
- **動作**：還原選取的項目、全部還原、清除（選單：選取的項目 · 已過保留期限 · 立刻清除全部）。每個動作都先開啟它的檢查對話框（§5）。
- 交叉連結：「開啟計畫審閱」（`#/plans/<id>`）、在工作區顯示原本的資料夾、顯示見證檔。

## 5. 流程

**還原**

1. 選取（或「全部」）→ 以 `action: "restore"` 呼叫 `POST /v1/quarantine/plans/<id>/check`。
2. 對話框顯示：要還原的檔案數與位元組數；目前在目錄中有多少原路徑已被**佔用**（那些會留在隔離區：drive-merge 絕不覆蓋）；有多少隔離副本是上次掃描**沒看到**的（它們可能已經不在了）；以及，對於跨雲端搬移和整併，提醒另一個雲端上的那份副本會保留，所以之後同樣的內容會存在兩份（去重流程之後可以把它整理掉）。
3. 輸入 `restore` 才能啟用按鈕 → `POST /v1/ui/plans/<id>/restore` → 一個帶有進度（`restored`、`failed`、`total`）的工作。
4. 結果依各自的代碼（`ORIGINAL_TAKEN`、`QUARANTINE_GONE`……）列出每個沒有還原成功的項目。「目錄已落後」橫幅會針對受影響的雲端出現，並附上重新掃描按鈕，和任何一次執行之後一樣。

**清除**

1. 選取、「已過保留期限」或「立刻清除全部」→ 以 `action: "purge"` 和 `older_than_days` 做檢查（「已過保留期限」用保留天數，其他用 `0`）。
2. 對話框顯示：會被永久刪除的檔案數與位元組數；有多少**太新**而會留下（只有「已過保留期限」才有）；有多少見證檔依目錄是**已經不在**的（這些項目會保留：隔離區裡那份可能是最後一份）；有多少見證檔還在但目錄**無法證明內容相同**（執行器會即時證實它們，這最多可能下載所顯示的位元組數）；以及已知時，清除後的容量。若有任何要清除的項目是不到 7 天前才進入隔離區的，則以警告色顯示。
3. 輸入 `purge` → `POST /v1/ui/plans/<id>/purge`（或從總覽用 `POST /v1/ui/quarantine/purge`）→ 工作 → 結果，包含被保留的項目及原因。

**唯讀備援**（核准功能關閉時）：檢查仍會執行（它們是唯讀的），而對話框最後顯示的是指令，不是按鈕：`drivemerge plan restore <id>`（每個選取的項目加一個 `--op <op id>`，最多 20 個；超過就改用涵蓋整個計畫的指令並加上說明）、`drivemerge plan purge <id> --older-than-days <n>`，或總覽動作用的 `drivemerge quarantine purge-due`。核准功能開啟但執行功能關閉時，按鈕停用並顯示 `[safety] execution_enabled = true` 的說明，和計畫審閱頁一樣。

## 6. 來源不明的項目

隔離資料夾底下、這份目錄中沒有任何保留中的操作能說明來由的現存檔案：這份目錄不認得的計畫編號、計畫從未隔離過的路徑、有人手動搬進去的檔案、還原後又放回去的副本。它們依雲端列出（數量、位元組數、三個例子），僅此而已：沒有任何紀錄說明它們從哪裡來、由什麼背書，所以 drive-merge 既不提供還原也不提供清除，而模擬器仍會拒絕任何碰到它們的計畫（`QUARANTINE`）。頁面會說明可以用該雲端自己的 App 來移動它們。

## 7. 交叉連結

- 側欄徽章：有項目已過保留期限的計畫。
- 一個仍有項目留在隔離區、狀態為 `done` 或 `failed` 的計畫，它的計畫審閱頁會出現一個方塊：「*這個計畫有 N 個檔案（X）在隔離區*」，附有**還原…**和**在隔離區開啟**。release 計畫保留它原有的清除方塊，並加上同樣的連結。
- 執行後的失敗說明（`prog.failedHelp`）、樹狀圖中隔離資料夾的提示，以及 `err.QUARANTINE`：核准功能開啟時，改為指向這個頁面（計畫已知時是 `#/quarantine/<id>`，否則是 `#/quarantine?remote=<cloud>`），不再叫人去終端機；終端機的措辭只保留給唯讀頁面。
- 樹狀圖：隔離根目錄那一列多一個「開啟隔離區」；`<quarantine_dir>/<id>` 資料夾那一列則開啟該計畫。
- 總覽的雲端卡片與釋放空間頁面：「這個雲端的隔離區有 X（N 個已到期）」，連結到 `#/quarantine?remote=<cloud>`；釋放空間頁面中「有計畫已完成」的說明，連結到該計畫的隔離區。

## 8. 已知限制（v1）

- 還原絕不覆蓋：原路徑已被佔用的項目會留在隔離區。不提供還原成另一個名稱或另一個資料夾。
- 還原只撤銷隔離。計畫的其他影響仍然存在：它複製到另一個雲端的副本、它在同一個雲端內移動的檔案。要撤銷整個計畫，得另外產生一個反向的計畫（經過模擬與核准），這是未來的工作。
- 目錄檢查的新舊程度，和各雲端最後一次掃描一樣；最終由執行器即時判定。
- 清除會即時證實每個見證檔；沒有強雜湊的雲端（OneDrive、iCloud）上的見證檔，可能每個項目都要下載一次，而除了所顯示的項目數之外沒有其他預算限制。
- 一次只能有一個工作（`JOB_BUSY`），和這個頁面的所有工作一樣：清除好幾個計畫是一個依序走過它們的總覽工作，不是平行的工作。
- 來源不明的項目（§6）只能列出。

---

# 實作合約

以下全部具有約束力。請先閱讀：[DESIGN.md](DESIGN.zh-TW.md) §7.3、[WORKSPACE.md](WORKSPACE.zh-TW.md) §D、§F、[PLANS.md](PLANS.zh-TW.md) §B、§C、§E、[ONBOARDING.md](ONBOARDING.zh-TW.md) §D、`executor.py`（`restore_plan`、`purge_quarantine`、`_quarantined_ops`、`quarantine_path`、`_inside_quarantine`、`_Run.restore`、`_Run.purge`）、`plans.py`、`db.py`（`outside_quarantine`、`quarantine_dir`）、`service.py`、`api.py`（`build_router`、`QuotaCache`）、`ui_api.py`（`_Jobs`、`_outdated_remotes`、清除路由、`require_human`）、`mcp_server.py`、`cli.py`（`cmd_plan_restore`、`cmd_plan_purge`），以及 `src/drivemerge/web/` 裡的每個檔案。

## A. 定義

- **隔離操作** = 對狀態為 `done` 或 `failed` 的計畫，由 `executor._quarantined_ops` 傳回的操作：一個 `done` 的刪除，或一個帶有 `quarantine` 日誌紀錄的 `done` 搬移（它遇到了內容相同的檔案）。它的**項目**是位於 `op.src_remote_id` 上 `quarantine_path(config, plan_id, op.src_path)` 的那個檔案。
- 項目的**狀態**：當操作的狀態是 `skipped`，且該操作有一筆 `detail.status == "restored"` 的 `restore` 日誌紀錄時為 `restored`；當該操作有一筆 `detail.status == "purged"` 的 `purge` 日誌紀錄時為 `purged`；其他情況為 `held`。`restored_at` / `purged_at` 是那些日誌紀錄的 `ts`。
- **見證檔** = 刪除操作的 `(witness_remote_id, witness_path)`，搬移操作的 `(dst_remote_id, dst_path)`。
- `quarantined_at` = 該操作的 `executed_at`；`due_at` = `quarantined_at + retention_days` 天；**到期**（due）= `due_at <= now`。
- 與目錄的路徑比對：NFC；當雲端不分大小寫時再做 casefold（和 `compare.py` 一樣）。
- `retention_days` = `config.safety.quarantine_retention_days`（新增，§D）。

## B. `src/drivemerge/quarantine.py`（唯讀）

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

這個模組中沒有任何函式會寫入目錄（有一個測試斷言 `conn.total_changes` 不會變動）。

`quarantine_report` 的結果：
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
- `remotes` 依設定檔的順序列出每個目錄中的雲端（包含全為零的）。`plans` 列出每個至少有一個隔離操作的計畫，先依保留中項目的 `oldest_at` 排序，再來是沒有任何保留中項目（`held.files == 0`）的計畫，依 `finished_at` 由新到舊。位元組數是 `expect_size`（為 null 時算 0）。
- `unknown`：每個雲端上位於 `<quarantine_dir>/` 或其底下、且路徑不是任何**保留中**項目的項目路徑的現存檔案（`present = 1 AND is_dir = 0`）；沒有這類檔案的雲端省略。
- 針對 `plan_ops` 與日誌 join 後做 SQL 聚合（一次掃過就得到所有狀態），不是在 Python 裡對資料庫的每個操作各跑一次迴圈。

`plan_items` 的結果：
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
- `state` ∈ `held|restored|purged|all`（否則 `BAD_QUERY {param: "state"}`）；`remote` 和 `path_prefix` 針對項目的雲端與**原始**路徑篩選（以整段路徑為單位的前綴）；`limit` ≤ 500；項目先依雲端順序，再依不分大小寫的自然排序原始路徑排列。
- `catalog`：`seen` = 隔離路徑上有一個現存的目錄檔案；`original_taken` = 原始路徑上有一個現存的目錄檔案；`witness_live` = 見證檔路徑上有一個現存檔案，當操作沒有指定見證檔時為 `null`；`witness_same` = 它與隔離檔案共用一個非 null 的 `content_group`（或者，當隔離檔案不在目錄中時，與該操作的 `expect_hash` 有相同的強雜湊），未知時為 `null`。每一頁、每個欄位用一次批次查詢計算，不是每個項目查一次。

`check(action="restore")` 傳回 `{"action", "plan_id", "files", "bytes", "original_taken": {"files", "bytes"}, "not_seen": {"files", "bytes"}, "cross_remote_copies": n, "remotes": [names], "op_ids": [the resolved held op ids]}`。`cross_remote_copies` = 其計畫也把相同內容複製到另一個雲端的項目數（還原之後該內容會存在兩份）。

`check(action="purge")` 傳回 `{"action", "plan_id", "files", "bytes", "too_recent": {"files", "bytes"}, "witness_gone": {"files", "bytes"}, "witness_unproven": {"files", "bytes", "download_bytes"}, "recent": {"files", "bytes"} (attempted items quarantined less than 7 days ago), "by_remote": [{"remote", "files", "bytes"}], "op_ids": [...]}`；`files`/`bytes` 是會被嘗試清除的（比截止日更舊的），`download_bytes` 估計即時重新證實時可能下載的量（見證檔沒有共同強雜湊者，其隔離大小的總和）。

錯誤：未知的計畫 `NOT_FOUND {plan}`；沒有任何保留中項目的計畫 `NOTHING_QUARANTINED {plan}`；`op_ids` 中點名了不是這個計畫保留中項目的東西 `BAD_OPS {plan, op_ids: [first 20 offending]}`；`action` 不是 `restore`/`purge`，或 `older_than_days` 為負數 `BAD_QUERY {param}`。`op_ids = None` 表示每個保留中的項目。

## C. 執行器（`executor.py`）

- `restore_plan(conn, rclone, config, plan_id, *, op_ids=None, progress=None)` 和 `purge_quarantine(..., older_than_days=30, op_ids=None, progress=None, tmpdir=None)`：`op_ids` 把這次執行限制在那些隔離操作上（呼叫端已經驗證過；這裡對未知的編號直接忽略，絕不據以動作）；`progress(done, failed, total)` 在每個項目之後被呼叫。沒有新參數時，行為與輸出不變（既有測試不變）。
- `_OpFailed` 新增 `code`（預設 `OP_FAILED`）和 `params`。還原與清除的拋出處設定：`ORIGINAL_TAKEN {remote, path}`、`QUARANTINE_GONE {remote, path}`、`READ_ONLY {remote}`、`BAD_PATH {path}`、`VERIFY_FAILED {remote, path}`（搬移之後不見了或內容不同）、`NO_WITNESS_RECORD {remote, path}`、`WITNESS_UNPROVEN {remote, path, witness}`（即時重新檢查發現它不在了，或內容不相同）。英文訊息維持原樣。
- `ExecResult` 新增 `failures: list[dict]`，最多 200 筆 `{op_id, seq, remote, path, code, params, error}`，只由還原與清除填入。
- 狀態為 `executing` 的計畫仍然拋出 `ExecutionError`；給它 `code = "PLAN_EXECUTING"` 和 `params = {plan}`。

## D. 設定、服務、CLI、MCP

- `SafetyConfig.quarantine_retention_days: int = 30`，來自 `[safety] quarantine_retention_days`（≥ 1；否則是點名該鍵的設定錯誤）。`drivemerge plan purge --older-than-days` 預設用它。
- `Service.quarantine_report()`、`Service.quarantine_items(plan_id, **kw)`、`Service.quarantine_check(plan_id, action, **kw)`（從設定檔傳入 `quarantine_dir` 和 `retention_days`）；`Service.restore(plan_id, *, op_ids=None, progress=None)` 和 `Service.purge(plan_id, *, older_than_days=30, op_ids=None, progress=None)`；`Service.purge_due(remote=None, progress=None) -> dict` 走過報告中有到期項目的計畫（給定 `remote` 時，只限該雲端上的），對每個計畫以 `older_than_days = retention_days` 清除，傳回 `{"plans": [{plan_id, status, done, failed, skipped, failures}], "done", "failed", "skipped"}`；某個計畫失敗後它會繼續處理下一個。
- CLI：`drivemerge quarantine` 印出報告（每個雲端、每個計畫一張表；`--json`）；`drivemerge quarantine show <plan id> [--state …] [--remote …] [--prefix …]`；`drivemerge quarantine purge-due [--remote …]`（和 `plan purge` 一樣有終端機關卡，確認字是 `purge`）；`plan restore` 和 `plan purge` 新增 `--op ID`（可重複）。`.claude/settings.json` 在 `deny` 中加入 `Bash(drivemerge quarantine purge-due:*)` 和 `Bash(*drivemerge quarantine purge-due*)`。
- MCP：唯讀工具 `quarantine_report()` 和 `quarantine_items(plan_id, state="held", remote=None, path_prefix=None, limit=100)`（英文，和 `get_plan` 同樣的去識別處理）。沒有還原或清除工具。

## E. HTTP

| 方法與路徑 | 認證 | 本文 / 查詢 | 回應 |
|---|---|---|---|
| `GET /v1/quarantine` | 一般（共用路由器：兩個應用程式都有） | | §B 的報告；每個 `remotes[]` 列多一個 `"quota": QuotaCache.get(...)`（已用 / 總量 / 剩餘，或 `null`） |
| `GET /v1/quarantine/plans/{id}` | 一般 | `state`、`remote`、`path`、`offset`、`limit` | §B 的 `plan_items` |
| `POST /v1/quarantine/plans/{id}/check` | 一般 | `{"action": "restore"\|"purge", "op_ids"?: [int] (≤ 5000), "older_than_days"?: int}` | §B 的 `check` |
| `POST /v1/ui/plans/{id}/restore` | 人類通道 | `{"confirm": "restore", "op_ids"?: [int]}` | 202 `{"job_id"}`；工作種類 `restore`，進度 `{restored, failed, total}`，結果為帶有 `failures` 的 `ExecResult`；任何項目失敗時工作即失敗（狀態 `failed`，並設有 `error`） |
| `POST /v1/ui/plans/{id}/purge` | 人類通道 | 同現狀，另加 `"op_ids"?: [int]` | 同現狀；進度 `{purged, failed, total}`；結果多一個 `failures` |
| `POST /v1/ui/quarantine/purge` | 人類通道 | `{"confirm": "purge", "remote"?: name}` | 202 `{"job_id"}`；工作種類 `purge`，進度 `{plans_total, plans_done, purged, failed}`，結果為 `Service.purge_due` |

- 啟動還原或清除工作之前，路由會先用 `quarantine.check` 驗證（所以 `NOTHING_QUARANTINED`、`BAD_OPS`、`NOT_FOUND` 是同步回答，不是以失敗的工作呈現）。拒絕的順序：`UNAUTHENTICATED` / `HUMAN_ONLY` / `APPROVALS_DISABLED` / `BAD_CSRF` / `BAD_ORIGIN`（人類通道，不變）、`BAD_REQUEST`（確認字不對：400，和現在的清除一樣）、`EXECUTION_DISABLED`（403）、`NOT_FOUND`（404）、計畫尚未執行時的 `CONFLICT`（409，和現在的清除一樣，參數 `{plan, plan_status}`）、`PLAN_EXECUTING`（409）、`NOTHING_QUARANTINED`（409）、`BAD_OPS`（400）、`JOB_BUSY`（409）。
- 沒有 `op_ids` 時，清除路由和現在完全一樣地呼叫 `service.purge(plan_id, older_than_days=…)`，所以既有的清除測試維持不變。
- 兩個應用程式都把 `QuarantineError` 回答為 `{"error", "code", "params"}`：`NOT_FOUND` 用 404，`BAD_OPS` / `BAD_QUERY` 用 400，其他用 409。
- `_outdated_remotes` 也會針對該操作的來源雲端，計入每一筆比該雲端最後一次完整掃描更新、且 `detail.status == "restored"` 的 `restore` 日誌紀錄，以及 `detail.status == "purged"` 的 `purge` 紀錄，所以不論是從頁面還是終端機還原或清除之後，都會出現「目錄已落後」橫幅。
- `approval_code`、摘要與快照不會出現在任何這些回應中（擴充去識別處理的測試）。

## F. 前端（`src/drivemerge/web/`）

與 WORKSPACE.md §F、EXPLORE.md §F、PLANS.md §E 的規則相同（沒有建置步驟、離線可用、CSP、兩種語言、代碼由頁面措辭、測試會碰到的東西都加 `data-testid`）。

- `quarantine.js`（新）：總覽（`#/quarantine?remote=<name>`）與計畫頁面（`#/quarantine/<id>?state=&remote=&path=`；篩選狀態存在網址裡）、檢查並確認的對話框、工作進度（沿用 `report.js` 中 `ProgressBox` 風格的呈現，但不修改它）、唯讀的指令方塊（沿用 `CliBox` 的做法），以及給計畫審閱頁用的匯出 `PlanQuarantineBox({ planId })`。項目樹由頁面依原始路徑建立；只繪製展開的節點；「載入更多」每次 200 個。
- `shell.js`：路由 `quarantine`（`/^\/quarantine(?:\/(\d+))?(?:\?(.*))?$/`）、排在 `plans` 之後的側欄項目（圖示 `lock`）、`VIEWS` 新增 `quarantine`（`Alt+6`）、徽章 = `due.files > 0` 的計畫數，在啟動時，以及任何 `execute`、`restore` 或 `purge` 工作結束之後，從 `GET /v1/quarantine` 讀取（沒有定時輪詢）。
- `api.js`：`quarantine()`、`quarantineItems(id, query)`、`quarantineCheck(id, body)`、`restore(id, body)`、`purgeDue(body)`；`purge` 新增 `op_ids` 直接傳遞。
- `plans.js`：在種類不是 `release` 的 `done`/`failed` 計畫的審閱頁中繪製 `PlanQuarantineBox`；在 `PurgeBox` 加上「在隔離區開啟」連結（它既有的行為與測試 id 不變）。
- `report.js`、`tree.js`、`overview.js`、`free.js`：只加 §7 的連結（核准功能開啟時使用新的鍵 `prog.failedHelpPage`、`quarantine.tipPage`、`quarantine.insidePage`、`err.QUARANTINE_PAGE`；既有的終端機句子保留給唯讀頁面）。這些修改要小。
- `styles.css`：這個頁面的樣式；`icons.js` 只有在 `lock` 需要變體時才改。
- `data-testid`：`view-quarantine`、`qr-cloud-<remote>`、`qr-plan-row-<id>`、`qr-settled`、`qr-unknown`、`qr-purge-due`、`view-quarantine-plan`、`qr-filter-state`、`qr-filter-remote`、`qr-filter-path`、`qr-item-<op_id>`、`qr-folder-<path>`、`qr-check-<op_id>`、`qr-detail`、`qr-restore-selected`、`qr-restore-all`、`qr-purge-menu`、`qr-purge-selected`、`qr-purge-due-plan`、`qr-purge-now`、`qr-dialog`、`qr-dialog-check`、`qr-confirm-input`、`qr-confirm`、`qr-progress`、`qr-result`、`qr-failure-<op_id>`、`qr-cli`、`qr-exec-disabled`、`plan-quarantine-box`、`rail-quarantine`、`rail-quarantine-badge`。
- i18n 命名空間（兩種語言）：`nav.quarantine`、`nav.quarantineTip`、`qr.*`、`qr.state.<held|restored|purged|all>`、§C 每個 `_OpFailed` 代碼對應的 `qr.fail.<CODE>`、`err.NOTHING_QUARANTINED`、`err.BAD_OPS`、`err.PLAN_EXECUTING`，以及上面四個 `*Page` 鍵。每個句子只使用伺服器會送的參數。

## G. 測試

- `tests/test_quarantine.py`（新）：用手工建立的計畫、操作與日誌紀錄測試各種狀態（保留中、已還原、已清除、搬移到相同內容之處、有部分項目的 `failed` 計畫）；保留期限與 `due`；報告的排序與總計；`unknown`（未知的計畫編號、沒有被說明的路徑、還原後又重新隔離的檔案）；`plan_items` 的篩選、分頁、排序、每個 `catalog` 旗標（包含不分大小寫與 NFD 路徑）；兩種 `check` 動作；每個錯誤代碼；`total_changes` 不變。
- `tests/test_executor.py`（擴充）：還原與清除的 `op_ids`、`progress` 呼叫、每個新 `_OpFailed` 代碼在它的拋出處、`ExecResult` 中的 `failures`、沒有新參數時輸出不變。
- `tests/test_quarantine_api.py`（新）：每個路由，在有掛載的兩個應用程式中都測；認證（bearer、工作階段、人類通道）、§E 的拒絕順序、工作（進度、失敗讓工作失敗、`JOB_BUSY`）、`purge_due` 跨多個計畫且其中一個失敗、容量的傳遞、還原之後與清除之後的 `_outdated_remotes`、去識別處理。
- `tests/test_cli.py`、`tests/test_mcp_server.py`（擴充）：`quarantine`、`quarantine show`、`quarantine purge-due` 的終端機關卡、`--op`；那兩個 MCP 工具，以及沒有任何 MCP 工具能還原或清除。
- `tests/test_config.py`（擴充）：`quarantine_retention_days` 的預設值、設定值、無效值。
- `tests/test_i18n_contract.py`（擴充）：`quarantine.CODES` 中的每個代碼、§C 的每個 `_OpFailed` 代碼，以及 `PLAN_EXECUTING`，在兩種語言中都有句子，且只使用它的參數。
- `tests/test_ui_quarantine.py`（新，瀏覽器、模擬 API，從 `test_ui_frontend.py` 匯入 `MockApi`/`Ui`）：側欄項目、徽章與 `Alt+6`；雲端卡片與篩選；含「已處理完畢」與「來源不明」的計畫清單；計畫頁面的樹、選取與三態資料夾、網址中的篩選與「上一頁」；細節面板的旗標；還原與清除的檢查對話框（帶有 `op_ids` 的請求本文、輸入確認字的閘門、進度、依失敗代碼措辭的結果）；從總覽清除所有到期項目；帶有 `--op` 行的唯讀指令方塊，以及超過 20 個時退回涵蓋整個計畫的做法；執行功能關閉的說明；已完成的去重計畫審閱頁中的 `PlanQuarantineBox`；從失敗說明、樹狀圖和釋放空間頁面來的連結；兩種語言。既有的瀏覽器測試維持通過（核准功能開啟時，只更新那些釘住被取代的終端機句子的斷言）。
- 整個測試套件維持通過：`pytest`（包含瀏覽器測試），但 `tests/test_docs_i18n.py` 在 `docs/QUARANTINE.zh-TW.md` 存在之前除外。
