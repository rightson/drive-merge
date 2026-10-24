# 視覺化工作區（Visual Workspace）

[English](WORKSPACE.md) | **繁體中文**

> 本文件是 [DESIGN.zh-TW.md](DESIGN.zh-TW.md) §15 的展開：前半是設計，後半（實作合約）是給實作者的精確合約。

## 1. 使用情境

「我有一些學校跟研究的東西分散在不同目錄（甚至不同雲）。我想開左右兩個窗格，看不同路徑底下的東西在哪個階層，把檔案或資料夾拖到合適的階層，來回拖到滿意，然後預覽結果；確認無誤、核准之後才真的去改雲端。」

對應的操作模型：

1. **兩個窗格**，每個都是可展開的樹狀檢視；根節點可以是「所有雲端」（每朵雲一個頂層節點，等同 rclone `combine` 的視圖）或某一朵雲。
2. **拖放 = 暫存（stage）一個動作**，不碰雲端。可在同一窗格不同階層之間、或左右窗格（含跨雲）之間拖放；可重新命名、新增資料夾。
3. **來回拖** 不會累積成一串操作：最後只計算「淨效果」——拖回原處的東西等於沒動。
4. 每次拖放是一個 **復原步驟**（Ctrl+Z / Ctrl+Shift+Z）。
5. 窗格預設顯示「**變更後**」的虛擬樹（暫存的效果立即可見），可切換「變更前」；可開「顯示原位置」看到每個東西被搬去哪。
6. **預覽** = 把暫存的淨效果轉成 Plan → 模擬器檢查 → 顯示 before/after、跨雲傳輸量、衝突與問題清單。
7. **核准並執行** 需要人在瀏覽器輸入 8 碼確認碼（見 §4）；執行中顯示進度；完成後可一鍵重掃受影響的雲端。
8. **建議**：服務會主動找出「散落各處但相關」的資料夾（例如名稱/內容相近的學校、研究資料夾）與「內容幾乎相同的重複資料夾」，一鍵把建議加入暫存，仍可再調整。
9. **Claude 也能提案**：透過 MCP 把搬移加入同一份草稿（標示「由 Claude 提出」），畫面即時更新；但 Claude 不能核准或執行。

## 2. 為什麼這樣設計

- **草稿是動作序列，不是目標樹**：使用者看到的是「變更後」視圖並在其上繼續拖放，因此每個動作的來源路徑是「當時的虛擬路徑」。依序套用（函式合成）就能自然支援「先把 A 拖進 B，再把 B 拖進 C」與來回拖放；淨效果由「每個檔案的原始位置 → 最終位置」計算，與中間過程無關。
- **資料夾同名 = 合併**：整理分散的資料最常見的需求就是把兩個同名/同主題的資料夾併在一起。合併時內容完全相同的檔案自動變成「保留一份、另一份移到隔離區」（有見證的去重）；內容不同的同名檔案列為衝突，由使用者改名解決——永不覆寫。
- **跨雲搬移 = 複製 + 驗證 + 來源移入隔離區**（依賴關係保證先驗證才刪）。
- **預覽一定經過模擬器**：視覺化介面只是另一種產生 Plan 的方式，安全不變式完全相同（[DESIGN.zh-TW.md](DESIGN.zh-TW.md) §7）。
- **草稿版本綁定預覽**：草稿只要有任何變動，之前的預覽就失效，必須重新預覽才能核准。
- **重掃後失效的步驟**：重新掃描可能讓草稿裡某一步的來源消失（被搬走或刪除）。這種步驟不會產生任何操作，所以草稿與預覽都會把它標成「失效」並說明原因，而不是默默略過；其餘步驟照常預覽。
- **隔離區不能拖放**：隔離資料夾裡是已刪除的檔案，請在「隔離區」頁面（[QUARANTINE.zh-TW.md](QUARANTINE.zh-TW.md)）或用 CLI 還原（`drivemerge plan restore`）或清除（`purge`）；畫面上它有獨立的樣式，不能拖出、也不能拖進，暫存這種搬移會被拒絕（`QUARANTINE`）。

## 3. 建議引擎

| 類型 | 判定 | 建議 |
|---|---|---|
| 相關資料夾（`related`） | 資料夾名稱詞彙（英文詞、CJK 雙字）+ 內部檔名詞彙的 TF-IDF 相似度，加上共享內容（重複檔案）的證據；排除已在同一父目錄或互為祖孫者 | 以檔案最多（或位於目標雲）的資料夾為錨點，其他資料夾搬入錨點成為子資料夾 |
| 重複資料夾（`duplicates`） | 兩資料夾的內容群組 Jaccard ≥ 0.9（只算已證明相同的內容），父層也重複時只報父層 | 把一個合併進另一個 → 相同檔案自動變成有見證的去重 |

語意相近但字面不同（「論文」vs「thesis」）是規則引擎做不到的，這正是讓 Claude 透過 MCP 讀 folder profiles 後提出搬移的地方。

## 4. 核准管道（人類專用）

威脅模型：防止 LLM（或被檔名 prompt injection 的 LLM）在無人確認下改動雲端；不防禦以使用者權限執行的惡意程式。

- `drivemerge ui` 在終端機印出一次性網址 `http://127.0.0.1:8765/ui/#token=…`（token 只存在記憶體，不寫入檔案）。瀏覽器以它換取 HttpOnly、SameSite=Strict 的 session cookie。
- **只有** UI session（cookie + CSRF header + Origin 檢查）可以呼叫核准/執行；REST API 的 Bearer token 與 MCP 永遠不行。
- 只有在 `drivemerge ui` 由互動式終端機啟動、且環境中沒有 `CLAUDECODE` / `AI_AGENT` 時才開啟核准能力；否則 UI 為「只能提案」模式，並顯示需在終端機執行的指令。
- 核准仍需輸入模擬 digest 的 8 碼；執行仍需 `[safety] execution_enabled = true`。

## 5. 已知限制（v1）

- 執行以檔案為單位（同雲搬移資料夾時會逐檔 move；Drive 上大量檔案較慢）。未來可加「整個資料夾一次伺服器端搬移」的 op。
- 搬走後留下的空資料夾不會自動刪除（預覽會列出數量）。新增但沒有放東西的資料夾不會被建立。
- 多個瀏覽器分頁同時編輯同一份草稿時，以伺服器版本為準（頁面會偵測版本變化並重新整理）。
- 底下沒有任何檔案的資料夾無法被搬動：執行以檔案為單位，沒有東西會在新位置建立它。預覽會把這種資料夾列出來（`empty_dirs_skipped`），而不是默默丟掉，也不把它們算成變更；一份草稿若只做了這種事，就沒有任何東西可執行。
- 在不分大小寫的雲端上，只改變名稱大小寫的更名（`Docs` 改成 `docs`）無法逐檔執行：雲端會保留已存在資料夾的拼法，而且 executor 拒絕把檔案當成它自己的重複檔。暫存時允許，但會附上 `CASE_ONLY_RENAME` 警告，預覽則把它回報為衝突，所以永遠不會被提供核准。請改用不同的名稱（先執行那一步，再改名一次）。
- executor 絕不會動 catalog，所以執行之後，樹狀檢視描述的仍是雲端原本的樣子，直到掃描完成。頁面會用一條橫幅說明這件事：關掉對話框或重新載入頁面都不會讓它消失（伺服器會回報自某個雲端上次完成掃描後，執行動過它的雲端），而且在重掃之前，不提供新預覽的核准；無法重掃的頁面會改為顯示 `drivemerge scan` 指令。從頁面重掃從不使用強制模式：如果掃描防護因為某個雲端真的被清空而擋下它，請在終端機執行 `drivemerge scan --force`。

---

# 實作合約（Implementation contract）

以下全部對實作者具有約束力。請先讀 `src/drivemerge/schema.sql`（資料表 `drafts`、`draft_actions`、`dir_stats`）、`models.py`、`plans.py`、`builders.py`（`op_from_entry`、`best_hash`）、`simulate.py`、`service.py`。

## A. 命名空間語意（`src/drivemerge/workspace.py`）

位置（location）以 `(remote_id, path)` 表示；路徑沿用 catalog 的慣例（'' = remote 根目錄，以 '/' 分隔，開頭與結尾都不帶 '/'）。

**動作**（actions；`draft_actions` 中 `state='active'` 的列，依 `seq` 排序）：
- `move`：項目 `[{"remote_id", "path"}]`（暫存當下的虛擬路徑）→ 移入目錄 `(dst_remote_id, dst_path)`；恰好只有一個項目時，`new_name` 用來重新命名。每個項目展開成一個*子搬移（sub-move）* `src=(rs, ps) → dst=(rd, join(dst_path, new_name or basename(ps)))`。同一個動作中，巢狀在另一個項目底下的項目會被丟棄（由祖先項目帶著走）。
- `mkdir`：建立虛擬目錄 `(dst_remote_id, join(dst_path, new_name))`。

**正向映射（forward map）** `fwd((r, p))`：依序套用每個 active 動作的每個子搬移：若 `r == rs and (p == ps or p.startswith(ps + "/"))`，則 `(r, p) = (rd, pd + p[len(ps):])`。比對必須以完整的路徑片段為單位（`a/b` 不會匹配 `a/bc`）。

**原點集合（origin set）**：live 的 catalog 項目（`present=1`，包含檔案與目錄）加上每個 mkdir 目錄（其原點即它的建立位置，標記為 virtual）。變更後視圖的命名空間是 `{fwd(o) : o in origins}`。

**原像（preimage）** `pre((r, D))` = 內容可能落在 `D` 的原點位置集合：從 `{(r, D)}` 開始；依「反向」順序走訪各動作；對每個子搬移與每個候選 `c`：若 `c` 位於 `dst` 本身或其底下，加入 `src + c[len(dst):]`（remote 為 `rs`）；保留 `c` 本身，除非 `c` 在同一個 remote 內位於 `src` 本身或其底下（該區域已被搬走）。mkdir 目錄本身就是原點。由於資料夾合併會有多個原像，所以這是一個集合。

**列出 `(r, D)` 在變更後視圖中的子項目**：對 `pre((r, D))` 中每個候選原點目錄，列出它在 catalog 中的 live 子項目；對每個子搬移的根與每個 mkdir 目錄，取其正向映像；保留「經正向映射後的*父層*」等於 `(r, D)` 的項目；依最終路徑分組。含有多個目錄的群組為 `merged`；含有多個檔案、且內容鍵（content key）不同的群組為 `conflict`（鍵相同 → `dedupe`，只顯示一次並加上標章）。變更前視圖 = 單純的 catalog 列表。

**已搬動集合 M（moved set）**（供統計與計畫使用）：位於任一子搬移之 `src` 的原像（取該動作當時的原像）底下的每個 live catalog 項目，經所有動作正向映射後，保留最終位置與原點不同者。最佳化方式：每個不同的父目錄只映射一次；只有當檔案本身是搬移來源時，才個別映射該檔案。

**變更後視圖的目錄統計**（可加總、精確）：`after(r, F) = before(r, F) − Σ{size of f in M : origin(f) under F and final(f) not under F} + Σ{size of f in M : final(f) under F and origin(f) not under F}`；`before` 取自 `dir_stats`（不存在時為 0）。檔案數與 dup_files 亦同。有一項細修讓樹狀檢視與預覽講的是同一個故事：被搬動的檔案若其最終位置上已有相同內容（即*被去重的檔案，dedupe victim*：計畫會刪除它而不是複製它），它會離開原本的資料夾，卻不會抵達任何資料夾，所以在第二個加總中會被略過（而且即使有些資料夾同時也放著它內容相同的另一份，仍算它離開了）。當這使某個內容只剩一份 live 副本時，這份倖存的副本就不再計入 `dup_files`。如此一來，這些數字就等於模擬器每朵雲的 `files_after` / `bytes_after`。

**暫存驗證**（`add_action`），對照目前的變更後視圖檢查：
- `SRC_NOT_FOUND`：某個項目在變更後視圖中不存在。
- `DST_NOT_DIR`：目的地在變更後視圖中不是目錄（remote 根目錄 `''` 一律視為目錄）。
- `INTO_ITSELF`：目的地等於被搬動的目錄項目，或位於其底下。
- `NAME_EXISTS`：*檔案*項目的目標名稱已被檔案佔用（或目錄對上檔案／檔案對上目錄）。目錄搬到已存在的同名目錄上是允許的，並成為合併（以警告 `MERGE` 回傳）。
- `NOOP`：每個項目都會落在它原本所在的位置（拒絕；沒有東西可暫存）。
- `BAD_NAME`：new_name 為空、含有 '/'、為 '.' 或 '..'，或含有 NUL。
- `QUARANTINE`：某個項目位於隔離資料夾（`db.quarantine_dir(conn)`，預設 `.drive-merge-quarantine`，每個 remote 上都有；在不分大小寫的 remote 上以 NFC+casefold 比較）本身或其底下；某個項目是包含該隔離資料夾的資料夾（只有 `quarantine_dir` 為巢狀路徑時才可能發生）；或某個項目的目標路徑（目的地加上其名稱或 `new_name`）、某個 `mkdir` 的目標路徑，位於隔離資料夾本身或其底下。已刪除的檔案只能由「隔離區」頁面或命令列還原或清除（`drivemerge plan restore` / `purge`），絕不能透過草稿。此項最先檢查，早於 `SRC_NOT_FOUND`：它只取決於路徑。
- 警告（非致命）：`MERGE`、`READ_ONLY`（來源或目的地 remote 沒有 write_remote）、`CROSS_REMOTE`（位元組會經由這台機器複製）、`CASE_COLLISION`（不分大小寫的目的地 remote 上已有大小寫不同的同名項目）、`CASE_ONLY_RENAME`（在兩者都不分的 remote 上，只改變名稱的大小寫或 Unicode 形式的更名：暫存時允許，預覽則把它當成衝突拒絕，見 §G）。

暫存時各項檢查的順序：`BAD_NAME`（new_name）、`BAD_REQUEST`（沒有項目）、`QUARANTINE`、`SRC_NOT_FOUND`、`DST_NOT_DIR`、`INTO_ITSELF`、`NAME_EXISTS`、`NOOP`。

新增動作時，會刪除該草稿所有 `undone` 的動作（截斷重做堆疊），並讓 `drafts.version` 加一。

**復原／重做／移除**：`undo` 把 seq 最大的 active 動作標為 `undone`；`redo` 重新啟用 seq 最小的 undone 動作；`remove_action(id)` 先把該動作標為 undone，再依序重新驗證之後每個 active 動作，把不再通過驗證的也標為 undone（回傳它們的 id）。每一項都會讓 `version` 加一。被移除的步驟保留它原本的 `seq`，所以移除某個中間的步驟、再復原較後面的一步之後，`redo` 會先把被移除的步驟帶回來：Ctrl+Z 一律取走最後一個 active 的步驟，Ctrl+Shift+Z 則取回編號最小的 undone 步驟。

## B. 草稿 → 計畫（`workspace.build_ops`、`workspace.preview`）

對 M 中的每個檔案（目錄不產生 op；mkdir 的目錄也不產生）：
- 最終位置已被 live 且未搬動的 catalog 檔案（或被先前已處理的搬動檔案）佔用：內容鍵（`identity.content_key`）相同 → 以該佔用者為見證，`delete` 被搬動的檔案（合併時去重）；內容鍵不同 → 衝突（仍然產生 move/copy，讓模擬器回報 `DST_EXISTS`，並列在 `conflicts` 之下）。
- 同一個 remote → `move`（`builders.op_from_entry(row, "move", dst_remote_id, dst_path, reason=...)`）。
- 不同 remote → 先 `copy` 到最終位置，再 `delete` 來源，以該 copy 為見證，且 `depends_on` = 該 copy 的 seq（在清單中的 index+1）。

順序：先排所有 move 與 copy（依來源 remote、path 排序），再排 delete。Reason 字串會註明草稿與動作的 seq。

`preview(conn, draft_id, *, policy=None) -> dict`：建立 ops；若沒有任何 op，在取消該草稿先前的預覽計畫（已經執行過的除外）並清除 `drafts.preview_plan_id` / `preview_version` 之後，回傳 `{"plan_id": None, "message": "nothing to do", "stale_actions": [...], "empty_dirs_skipped": [...]}`，如此草稿已不再持有的那些搬移所對應的計畫，就不會留在可核准的狀態；否則呼叫 `plans.create_plan(conn, f"Workspace: {title}", "workspace", ops, created_by="workspace", params={"draft_id", "draft_version"})` 與 `simulate.simulate_plan`，把結果存入 `drafts.preview_plan_id/preview_version`，並回傳 `{"plan_id", "draft_version", "ok", "simulation": asdict(report), "summary": {...}, "changes": [...], "conflicts": [...], "empty_dirs_left": n, "empty_dirs_skipped": [...], "stale_actions": [...]}`（`ok` = 模擬 ok **且**沒有衝突；唯有如此，UI 才可提供核准）。再次預覽會取消該草稿先前的預覽計畫（已經執行過的除外）；`changes` 每個被搬動的根項目列一列 `{"from": {"remote", "path"}, "to": {...}, "files", "bytes", "kind": "move" | "copy+delete" | "merge" | "dedupe"}`。`empty_dirs_skipped` 對每個底下沒有任何檔案的被搬動資料夾（巢狀者只列最上層）列出 `{"from": {"remote", "path"}, "to": {...}}`：計畫以檔案為單位，所以沒有東西會建立它們；`summary.empty_dirs_skipped` 是它們的數量。

`changes` 與 `conflicts` 的說明見 §G。`stale_actions` 列出不再符合 catalog 的 active 草稿動作（見 §G「失效的步驟」）；它一律存在，沒有時為 `[]`，在 `{"plan_id": None, "message": "nothing to do"}` 的回應中也一樣（`empty_dirs_skipped` 亦然）。

若計畫的 `params.draft_version` 與草稿目前的版本不同，該計畫即已失效（stale）：透過 UI 核准時必須拒絕它。

## C. Python API（`workspace.py`）

```python
class WorkspaceError(ValueError): code: str          # one of the staging codes above
def create_draft(conn, title: str) -> dict
def list_drafts(conn, *, status: str | None = "open") -> list[dict]
def get_draft(conn, draft_id: int) -> dict           # {"id","title","status","version","preview_plan_id","preview_version","actions":[...],"undo_available","redo_available","summary":{"moved_files","moved_bytes","cross_remote_bytes","remotes_touched":[names],"stale_actions":n}}
def discard_draft(conn, draft_id: int) -> None
def add_action(conn, draft_id: int, action: dict, *, created_by: str) -> dict   # action as in HTTP body below (remote *names*); returns {"action", "draft_version", "warnings":[{"code","message","params"}]}
def undo(conn, draft_id: int) -> dict
def redo(conn, draft_id: int) -> dict
def remove_action(conn, draft_id: int, action_id: int) -> dict                  # {"draft_version", "removed": [ids]}
def list_dir(conn, remote: str, path: str, *, draft_id: int | None = None, view: str = "after", ghosts: bool = False, limit: int = 500, offset: int = 0) -> dict
def roots(conn, *, draft_id: int | None = None) -> list[dict]                   # one node per remote, with stats
def build_ops(conn, draft_id: int) -> tuple[list[Op], dict]                     # ops, info {"changes","conflicts","empty_dirs_left","dedupes"}
def preview(conn, draft_id: int, *, policy=None) -> dict
def mark_applied(conn, draft_id: int) -> None
```
清單項目（list item）的結構：`{"name", "path", "remote", "is_dir", "size", "mtime", "category", "files", "dirs", "bytes", "dup_files", "entry_id"（僅檔案有，virtual 者為 None）, "origins": [{"remote","path"}], "moved_here": bool, "merged": bool, "conflict": bool, "dedupe": bool, "virtual": bool, "quarantine": bool（位於隔離資料夾本身或其底下：鎖定，見 `QUARANTINE`）, "quarantine_root": bool（隔離資料夾本身）}`；排序為先目錄，再依自然排序、不分大小寫的名稱順序。Ghost（原位置的影子）的結構：`{"name", "path", "is_dir", "moved_to": {"remote","path"}}`。

`summary.cross_remote_bytes`（`get_draft` 與 `preview` 中皆然）不包含合併時被刪除而不是被複製的檔案，所以它等於模擬器的 `bytes_cross_remote`。`discard_draft` 也會取消該草稿尚未執行的預覽計畫。

## D. HTTP 合約（`src/drivemerge/ui_api.py`，全為 JSON）

驗證：每個路由都接受 API Bearer token **或** UI session cookie，唯獨人類通道（`/v1/ui/plans/...`、`/v1/ui/rescan`）例外：它要求 UI session cookie，**且**標頭 `X-CSRF-Token` 與該 session 相符，**且**（若有帶）`Origin` 等於伺服器自己的 origin，**且**此伺服器行程已啟用核准。Bearer token 永遠到不了人類通道。

| 方法與路徑 | 內容／查詢參數 | 回應 |
|---|---|---|
| `POST /v1/ui/session` | `{"token"}`（`drivemerge ui` 印出的那一個） | 設定 cookie `dm_session`（HttpOnly、SameSite=Strict、Path=/）；`{"csrf", "approvals_enabled"}` |
| `GET /v1/ui/config` | | `{"approvals_enabled", "execution_enabled", "remotes": [{"id","name","provider","writable","case_insensitive"}], "version", "csrf", "outdated": [names]}`（`csrf`：對 cookie 呼叫者是該 session 的 token，讓重新載入的頁面能取回；對 Bearer 則為 `null`。`outdated`：在其上次完成掃描之後，有計畫操作執行過（或執行到一半失敗）的雲端，讓重新載入的頁面仍能說明 catalog 落後了） |
| `GET /v1/tree/roots` | `draft` | `{"roots": [...]}` |
| `GET /v1/tree` | `remote, path, draft, view=after\|before, ghosts=0\|1, limit, offset` | `{"remote","path","view","draft_version","items":[...],"ghosts":[...],"total"}` |
| `GET /v1/drafts` / `POST /v1/drafts` | `?status=open\|applied\|discarded\|all`（預設 open） / `{"title"}` | `{"drafts": [...]}` / 草稿（201） |
| `GET /v1/drafts/{id}` / `DELETE /v1/drafts/{id}` | | 草稿 / `{"ok"}` |
| `POST /v1/drafts/{id}/actions` | `{"kind":"move","items":[{"remote","path"}],"dst":{"remote","path"},"new_name"?, "note"?}` 或 `{"kind":"mkdir","dst":{"remote","path"},"new_name"}` | 201 `{"action","draft_version","warnings"}`；409 `{"error","code","params"?}` |
| `POST /v1/drafts/{id}/undo` / `redo` | | 草稿 |
| `DELETE /v1/drafts/{id}/actions/{action_id}` | | `{"draft_version","removed"}` |
| `POST /v1/drafts/{id}/preview` | | preview 字典，但不含 `simulation.digest`（其前 8 個字元*就是*核准碼），另外只在「UI session 且已啟用核准、同時 `ok` 為真」時附上 `"approval_code"` |
| `GET /v1/suggestions` | `kind=related\|duplicates`（省略時兩者皆回傳）, `remote, limit` | `{"suggestions":[...]}` |
| `POST /v1/ui/plans/{id}/approve` | `{"code"}` | `{"ok"}`；拒絕已失效的草稿預覽（409） |
| `POST /v1/ui/plans/{id}/execute` | | 202 `{"job_id"}`；任一工作執行中時回 409 `JOB_BUSY`（執行與重掃都會寫入 catalog），409 `NOT_APPROVED` / `STALE_PREVIEW`，403 `EXECUTION_DISABLED`。workspace 計畫執行完畢時也會一併結束它的草稿（`result.draft_applied`） |
| `POST /v1/ui/rescan` | `{"remotes":[names]}` | 202 `{"job_id"}`（先掃描每個 remote，再重建身分；兩者都會更新 `dir_stats`） |
| `GET /v1/jobs/{job_id}` | | `{"id","kind","status":"running\|done\|failed","progress":{...},"result","error"}` |
| `GET /v1/plans/{id}/progress` | | `{"status","ops_total","done","failed","skipped","current","recent_journal":[...]}`（journal 的詳細內容不含 digest、確認碼或快照） |
| `GET /ui/` 與 `/ui/static/*` | | 來自 `src/drivemerge/web/` 的單頁應用程式 |

此伺服器也一併掛載的 REST API 計畫路由（`GET /v1/plans`、`GET /v1/plans/{id}`、`POST /v1/plans`、`POST /v1/plans/{id}/simulate`），無論對 Bearer token 或瀏覽器 session，回應中任何層級都不含 `digest`、`approval_code` 與 `snapshot`：確認碼只會經由「可核准之 session 的工作區預覽」或 CLI 到達真人手中。`POST /v1/plans`（與 MCP 工具 `propose_plan`、CLI 一樣）對每一種計畫類型都拒絕參數 `draft_id` 與 `draft_version`：只有 `workspace.preview` 能把計畫綁到草稿。

工作（job）在背景執行緒中執行，各自使用**自己的** SQLite 連線（`db.connect(config.db_path)`，因此 catalog 必須是檔案）；處理請求的連線是共用的，並以鎖保護。此應用程式的錯誤格式為 `{"error", "code"}`，當拒絕帶有頁面可據以措辭的事實時另有 `"params"`（`error` 是英文句子）：`WorkspaceError` 回 409 並帶其 code，但 `NOT_FOUND`（404）與 `BAD_REQUEST`（400）除外；無效的請求內容回 422 並帶 `BAD_REQUEST`。此應用程式的每個錯誤都帶有 `code`，連 REST API 共用處理器產生的錯誤也一樣（`BAD_REQUEST`、`NOT_FOUND`、`UNAUTHENTICATED`），因此頁面永遠不必顯示英文句子。

## E. 建議（`src/drivemerge/suggest.py`）

```python
def related_folders(conn, *, remote: str | None = None, min_files: int = 3, limit: int = 20) -> list[dict]
def duplicate_folders(conn, *, remote: str | None = None, min_files: int = 3, threshold: float = 0.9, limit: int = 20) -> list[dict]
```
建議（suggestion）的結構：`{"id": "<kind>:<stable hash>", "kind", "score", "title", "reason", "code", "params", "folders": [{"remote","path","files","bytes"}], "terms": [...], "actions": [<HTTP action bodies>]}` — `actions` 可直接 POST 到 `/v1/drafts/{id}/actions`。`title` 與 `reason` 是英文句子，CLI 與 MCP 原樣顯示。`code`（`RELATED` 或 `DUPLICATES`）與 `params` 是同樣的句子，只是以資料表示，讓用戶端能以自己的語言重新表達：`RELATED` 為 `{"count", "names", "anchor", "name_terms", "content_terms", "same"}`（資料夾數量、不重複的名稱、建議整理到哪裡、共同的資料夾名稱詞、共同的檔名詞、內容完全相同的數量），`DUPLICATES` 為 `{"count", "names", "kept", "similarity", "shared", "saved_bytes"}`。頁面一律由 `code` 與 `params` 自行組出卡片文字（`sug.<CODE>.title` 與 `sug.<CODE>.reason`），只有遇到不認得的 `code` 時，才顯示英文的 `title` 與 `reason`。

## F. 前端（`src/drivemerge/web/`）

無建置步驟：`index.html`、`app.js`（ES module，使用 `./vendor/htm-preact-standalone.mjs`）、`styles.css`、`i18n.js`（預設為英文；瀏覽器列出中文時使用 zh-TW；缺少任何鍵時以英文為備援）。必須能離線運作（不使用 CDN）。版面與行為如 §1；HTML5 拖放；鍵盤復原／重做；每 3 秒輪詢目前草稿的版本，讓 Claude 暫存的變更能顯示出來；預覽對話框把違規（violation）以白話翻譯；核准並執行的流程附進度顯示；核准停用時顯示唯讀橫幅。e2e 測試需要的每個元素都帶有 `data-testid`。

值得保留的行為（每項都有瀏覽器測試）：
- **語言。** 頁面只在載入時決定一次語言：有 `?lang=` 查詢參數（`en` 或 `zh-TW`）就用它，否則用使用者先前的選擇（`EN` / `中文` 按鈕，保存在 `localStorage` 的 `dm-lang`；儲存空間會丟出例外時就忽略，此時的選擇只在這次瀏覽中有效），否則依瀏覽器自己的偏好順序：從 `navigator.languages` 的最前面往下走，第一個頁面有對應語言的項目就決定結果（`en`、`en-US`、`en_GB` 用英文；`zh`、`zh-TW`、`zh-CN`、`zh-Hant-HK`、`zh_TW` 用 `zh-TW`；其他項目一律略過），都沒有就用英文，所以 `en-US, zh-TW` 是英文，而 `ja, zh-TW, en` 是 `zh-TW`。按鈕會立即切換，不需要重新載入。某個語言缺少的鍵一律以英文補上，而 i18n 完整性測試要求每個鍵在兩種語言中都存在。
- **伺服器的訊息。** 伺服器不替頁面寫任何句子。使用者可能讀到的內容，都是供 CLI、API 與 MCP 使用的英文 `message`（衝突為 `reason`，建議為 `title` 與 `reason`），外加由頁面以自己的語言措辭的 `code` 與 `params`：建議（`sug.<CODE>.*`，§E）、暫存警告（`warn.<CODE>`）、拒絕（`err.<CODE>`，以錯誤內容中的 `params` 措辭）、失效原因（`stale.<CODE>`，由 `items` 措辭）、衝突（`conflict.<CODE>.title`、`.text` 與 `.detail`），以及模擬器的違規與警告。違規的 `params` 含有 `variant`（代碼有好幾個句子時，指出是哪一個）、以 `{"remote","path"}` 表示的位置（`at` 是「顯示位置」連結要前往的那一個）、數量與大小，以及自帶句子的簡短種類 `why`、`problem`、`what` 與 `role`。頁面由 `viol.<CODE>.detail`（或 `viol.<CODE>.detail.<variant>`）組出細節那一行，並把代碼以小字的等寬字型放在旁邊；頁面沒有對應句子的訊息（較新的伺服器，或 `params` 出現之前存下來的報告），就顯示伺服器的英文 `message`。違規與衝突代碼另有翻譯後的標題與說明（`viol.<CODE>.title` 與 `.text`、`conflict.<CODE>.*`）。新增代碼、變體或警告時，每種語言都要補上它們的字串：測試會從 `simulate.py`、`workspace.py`、`suggest.py` 與 `ui_api.py` 讀取這些項目，檢查每個句子在兩種語言中都存在，且只使用伺服器真的會送出的參數。唯一仍原樣顯示的文字，是執行失敗的細節（rclone 的輸出與執行器的錯誤），並附有一行說明。MCP 用戶端取得的是不含 `params` 的英文訊息。
- **失效的步驟。** 輪詢也會偵測到草稿版本不變、但 `summary.stale_actions` 改變的情況（重掃造成），並重新整理樹狀檢視、發出警告。暫存清單把失效的步驟加上刪除線並附上原因（`stale.<CODE>`），頂端列顯示警告數量，預覽對話框會列出 `stale_actions`（當每個步驟都已失效時，會明說這一點，而不是顯示「沒有任何變更」）。
- **隔離區。** 帶有 `quarantine: true` 的項目呈現為鎖定狀態（不可拖曳、不是放置目標、不能重新命名、不能剪下、不能在其中新增資料夾或貼上，麵包屑也一樣）；`quarantine_root` 那一列標示為「隔離區」/「Quarantine」，並附工具提示說明如何用 CLI 還原與清除。
- **提示訊息（toast）。** 位於右下角（抽屜開啟時顯示在抽屜旁，不蓋住抽屜的按鈕）。錯誤訊息會一直留到被關閉；資訊、警告、成功與「由 Claude 提出」的訊息約 6 秒後消失（帶按鈕者 12 秒），且在滑鼠指標或鍵盤焦點停在其上時會暫停倒數。同一則訊息再次出現時，會取代先前的那一則。訊息堆疊把自己的高度發布為 `--toast-reserve`，對話框的背景遮罩會在對話框下方留出這段空間，所以提示訊息絕不會蓋住對話框的頁尾。
- **合併中的同名衝突。** 標有 `conflict` 的那一列代表好幾個檔案，重新命名它會連同原本就在那裡的檔案一起改名。所以 F2 與雙擊它都不會開啟編輯框：對話框會說明原因，並提供「取回把進來的那個檔案帶到那裡的步驟」，之後提示訊息會帶你到該檔案原本的位置。預覽的 `DIFFERENT_CONTENT` 卡片也有相同的一鍵解法。一個衝突就是一個問題：頁面會去掉模擬器附帶的回音（衝突已經說明了的位置上的 `DST_EXISTS`、同一朵雲上 `READ_ONLY_REMOTE` 底下的 `NO_WITNESS`，以及重複的句子）。
- **執行後 catalog 落後。** 執行結束（完成或失敗）、或草稿在終端機被套用時，橫幅會點名這些雲端並提供重掃（沒有核准能力的頁面則顯示 `drivemerge scan <remote>`）。關掉對話框後橫幅仍在；重新載入頁面時，只要 `/v1/ui/config` 回報 `outdated`，橫幅就會回來；也可以手動關閉。期間做出的預覽會顯示一則附重掃按鈕的提示，且在 catalog 更新之前不提供核准。
- **窄窗格。** 窗格是一個 CSS 容器：寬度 480px 以下時隱藏檔案數欄位（輔助科技仍會讀出），380px 以下時標章縮成圖示或圓點，並保留工具提示。麵包屑固定為單行高度（麵包屑片段縮短並加上刪節號；雲端名稱與焦點項目維持完整），所以聚焦某一列絕不會讓指標底下的樹狀檢視位移。
- **很長的資料夾。** 每次暫存動作後的重新整理，都會重新載入與先前展開數量相同的子項目（以分頁方式，上限為伺服器的上限），所以用「載入更多」開啟的分頁不會遺失。
- **重新命名。** 輸入 IME 組字期間（`isComposing`、按鍵碼 229），輸入框會忽略 Enter 與 Escape；離開輸入框會提交已輸入的名稱（整個視窗失去焦點時除外）；按下 Enter 或 Escape 之後，鍵盤仍留在樹狀檢視中，改名後的那一列會被選取並取得焦點。
- **建議。** 建議卡片由草稿推導（note 為該建議 id 的 active 動作），所以暫存後又被復原的建議可以再次暫存；有 `detail` 時，卡片文字用它來組成。
- **沒有檔案的資料夾。** 預覽會在失效步驟旁列出 `empty_dirs_skipped`，不論是在審閱畫面還是「沒有可執行的內容」的回應中。

## G. 實作比 A–F 更精確之處

於整合期間決定；程式碼與測試依照這些內容，前面各節是較簡短的合約。

**命名空間與計畫（`workspace.py`）**
- 以 `mkdir` 建立的目錄，只會被*在它之後*暫存的子搬移映射（它的原點是它的建立位置）；較早的子搬移不適用於它。
- 變更後視圖的目錄計數以不重複的位置為單位（合併後的資料夾算一個資料夾，新建的 `mkdir` 資料夾也算），而且每個計數都以 0 為下限。缺少 `dir_stats` 列時視為全為零，因此在掃描與身分重建之後必須執行 `dirstats.refresh`（`Service.scan`、`Service.rebuild_identity` 與 `Service.bridge` 會執行它）。
- 空檔案絕不算重複檔：它們共用同一個 hash，只是佔位用的檔案（`__init__.py`、`.gitkeep`），所以 `dup_files`（檔案列與資料夾計數皆然）會忽略大小為零的檔案，就像搜尋、`dir_stats` 與重複清單一樣。
- 在不分大小寫的 remote 上，`build_ops` 以完整路徑的 NFC+casefold 找出最終路徑的佔用者；暫存時則維持以精確名稱比對，另加 `CASE_COLLISION` 警告。
- `info["conflicts"]`（以及 `preview()["conflicts"]`）中的衝突帶有 `code`、相關的位置（`from`、`to`、`with`）與英文的 `reason`：`DIFFERENT_CONTENT`、`DIR_IN_WAY`（catalog 中的某個資料夾實際上仍在那裡：逐檔執行永遠不會移除資料夾）、`FILE_IN_WAY`、`OCCUPIED`（該位置屬於一個要到刪除階段才會離開的檔案）、`CYCLE`（彼此等待的搬移，例如互換，或必須搬進與自己同名資料夾的檔案）與 `CASE_ONLY_RENAME`（檔案或資料夾的最終位置，就是它自己換了大小寫或 Unicode 形式的另一種拼法，且發生在兩者都不分的 remote 上：每個被更名的根項目回報一個衝突，而不是裡面每個檔案各一個）。當內容相同時，`OCCUPIED`、`CYCLE` 與 `CASE_ONLY_RENAME` 可能通過模擬器，這就是 `preview()["ok"]` 也要求沒有衝突的原因。
- move 與 copy 以拓撲排序，確保檔案只有在其目標位置上的東西都離開之後才會落地；同序者依來源 remote id 與 path 決定先後。對於有衝突的跨 remote copy，不會產生與之成對的 delete。
- **失效的步驟。** 重掃可能在草稿仍開啟時，改變其底下的 catalog。當一個 active 動作在它之前各動作的變更後視圖上重播時，已沒有任何東西可作用，它就是*失效的（stale）*：某個 move 項目已不存在（`SRC_NOT_FOUND`；每個消失的項目都會列出），或某個目的地（move 或 `mkdir`）已不再是資料夾（`DST_NOT_DIR`）。建立在失效動作之上的動作也同樣失效。名稱衝突不算失效：預覽會把它們回報為衝突。`get_draft` 的每個動作都帶有 `"stale": bool` 與 `"stale_reason": {"code", "message", "items": [{"remote","path"}]} | None`（`items` 是消失的來源，或不存在的目的地，也是頁面用來措辭該 code 的資料）；`summary.stale_actions` 負責計數。`preview()` 會回傳 `"stale_actions": [<action dicts as in get_draft, stale_reason set>]` 與 `summary.stale_actions`。失效的步驟不產生 op，也不改變 `ok`（它們不會造成傷害），所以頁面在列出暫存步驟的每個地方以及預覽中，都必須顯示它們。已復原（undone）的動作絕不會失效。沒有被動過的 catalog，絕不會讓一份乾淨暫存的草稿產生失效的步驟。
- **工作區中的隔離區。** `list_dir` 會把位於隔離資料夾本身或其底下的每個項目標上 `quarantine: true`，並把該資料夾本身標上 `quarantine_root: true`（變更前視圖中也一樣）；頁面會以不同的樣式繪製根項目（標籤、指向 CLI 的工具提示），並讓整個子樹既不能拖曳、也不是放置目標。針對它的暫存會以 `QUARANTINE` 被拒絕（§A），`redo` 與 `remove_action` 在重新驗證較舊的步驟時也會套用這項檢查。
- 對已經是 undone 的動作執行 `remove_action`，只會讓版本加一。`WorkspaceError` 另外還使用 `NOT_FOUND`、`NOT_OPEN`、`BAD_REQUEST`、`NOTHING_TO_UNDO`、`NOTHING_TO_REDO`。
- `list_drafts` 的列是精簡的：`id, title, status, version, preview_plan_id, preview_version, created_at, updated_at, action_count`。

**建議（`suggest.py`）**
- 種類（kind）有 `"related"` 與 `"duplicates"`；id 為 `related:<12 hex>` / `duplicates:<12 hex>`；actions 帶的 `note` 就是該建議的 id。related 的門檻是 0.25；必須要有共同的資料夾名稱詞彙或共同的檔案，光靠檔名詞彙絕不會讓兩個資料夾被視為相關。
- 互為鏡像的重複資料夾會被分組（一個目標、多個 move 動作），而互為鏡像的一對只在其最上層回報。同一叢集內巢狀的資料夾，會收斂為邊（edge）較強的那一個。

**隔離區不算 live 內容**
- 放在隔離資料夾（`[safety] quarantine_dir`）裡的檔案會和其他檔案一樣被掃描，但它們是已刪除的檔案：`identity` 把它們排除在所有群組、重複清單與 bridging 候選之外，`suggest` 也會略過它們，因此被清理掉的副本絕不會成為取代它的那個檔案的「重複檔」，工作區也絕不會提議把資料夾從隔離區合併出去。它們仍計入檔案數與位元組數（它們佔用雲端空間），也會出現在樹狀檢視中。
- 資料夾名稱由 `config.sync_remotes`（每次 `Service` 啟動時）記錄在 catalog 的 `meta` 資料表，因此每個只拿到連線的模組（`db.quarantine_dir`、`db.outside_quarantine`）都與 executor 一致。

**伺服器（`ui_api.py`）**
- `GET /v1/suggestions` 未帶 `kind` 時，先回傳 related 再回傳 duplicates；未知的 kind 回 400。在一般路由上，以 cookie 驗證、且帶有外來 `Origin` 的不安全（unsafe）請求，回 403 `BAD_ORIGIN`。
- `api_token` 可以為空：此時只有瀏覽器 session 能用。靜態 HTML 帶有 CSP（`script-src 'self'`，沒有內嵌腳本，沒有其他來源）；因此 `index.html` 使用絕對路徑的 `/ui/static/` URL。

**Service 門面（`service.py`）**
- CLI、REST 與 MCP 都透過 `Service` 存取工作區：讀取用 `tree_roots`、`list_dir`、`get_draft`、`list_drafts`、`suggestions`；維護用 `refresh_dir_stats`（`scan`、`rebuild_identity` 與 `bridge` 也會執行它）；提案用 `create_draft`、`discard_draft`、`stage_action(..., created_by)`、`undo_action`、`redo_action`、`remove_action`、`preview_draft`。`preview_draft` 會回傳模擬 digest，所以呼叫者若不是在瀏覽器前的真人，就必須把它丟掉。核准與執行仍留在僅限人類的區段。`policy()` 是唯一依設定建立模擬器 `SafetyPolicy` 的地方。
- MCP 的草稿工具（`list_drafts`、`get_draft`、`create_draft`、`browse_tree`、`suggest_reorganization`、`stage_moves`、`undo_last`、`remove_staged`、`preview_draft`）以 `created_by="mcp"` 暫存（頁面只有在此情況才會顯示「由 Claude 提出」的標章與提示訊息），而且絕不暴露 `approval_code` 或 digest。

**執行**
- `Service.execute` 是唯一會結束草稿的地方：對「就是某份草稿自己的預覽」的計畫（類型為 `workspace`、建立者為 `workspace`；`Service.propose_plan` 拒絕 `draft_id` / `draft_version` 參數，而只是帶著它們的計畫不屬於任何草稿，這裡與預覽失效的檢查都一樣）完成一次 `done` 的執行後，若草稿的版本仍等於 `params.draft_version`，就把草稿標為已套用；否則草稿維持開啟，並在結果中說明原因（`draft_applied: false`、`draft_error`）。因此 CLI、瀏覽器與重掃三者的結論一致。
