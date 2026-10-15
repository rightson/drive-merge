# 在瀏覽器裡完成第一次設定

[English](SETUP.md) | **繁體中文**

> 這份文件為視覺化工作區（[WORKSPACE.md](WORKSPACE.zh-TW.md)）新增一個 **設定（Setup）** 畫面：從 `pip install` 到建好已掃描的目錄，全程不必離開瀏覽器。使用者檢查 rclone，透過登入雲端服務本身的頁面連接 Google Drive、Dropbox、OneDrive 或 iCloud，加入自己已經設定好的 rclone remote，並執行第一次掃描。第一部分是設計；第二部分（實作合約）對實作者具有約束力。

## 1. 問題

今天的第一個小時都在終端機裡度過：`drivemerge init`，每個雲端各跑一次 `rclone config`（一份冗長的文字問卷），手動編輯 `config.toml` 裡的 `[[remotes]]`，讓名稱與 rclone 的一致，然後執行 `drivemerge scan --all` 與 `drivemerge identity rebuild`。做完這些之後，`drivemerge ui` 才有東西可以顯示。每一步都很容易出一點小錯（名稱對不上、用完整寫入權限的 Drive remote 來掃描、忘了重建 identity），而且從頁面上完全看不到這些。

## 2. 改版後的情境

```
pip install drive-merge            # and rclone ≥ 1.69
drivemerge ui                      # no config yet: it is created, and the page opens on Setup
```

1. **檢查**：頁面顯示找到的 rclone 版本（或安裝方式，以及 `[rclone] binary` 設定）。
2. **連接**：「連接雲端」→ 選 Google Drive / Dropbox / OneDrive / iCloud → 取個名稱（`gdrive`）→「登入」。新的瀏覽器分頁會開啟雲端服務自己的登入頁面（透過 rclone 在 `127.0.0.1:53682` 提供的登入輔助程式）；按下「允許」之後，頁面會自動繼續。有些服務在登入後還會提問（OneDrive：要用哪個磁碟機；iCloud：雙重驗證碼），這些問題會以小表單顯示。iCloud 沒有瀏覽器登入：它的表單一開始就要求輸入 Apple ID 與密碼，因為 rclone 需要它們才能開始。完成後，這個雲端會列為已連接、唯讀。
3. **加入**：這台機器上 rclone 已經有、但 drive-merge 還沒加入的 remote，會列出並附上「加入」；不需要重新登入。
4. **掃描**：「全部掃描」（或逐個雲端掃描）會把第一次掃描與 identity 重建當成一項工作執行，並即時顯示每個雲端的數量。
5. **完成**：「開啟總覽」。

導覽列多了第六個項目 **設定**，第一次之後依然有用：之後想新增雲端、查看每個雲端的狀態、重新掃描，都在這裡。

## 3. 原則

- **預設唯讀。** 在這裡連接的雲端，加入 drive-merge 時不帶 `write` remote，所以在使用者開放寫入之前，drive-merge 不能改動它。Google Drive 的 remote 會以 `scope = drive.readonly` 建立。開放寫入是另一個明確的步驟（§6），會建立或指定一個可寫入的 remote。
- **憑證仍由 rclone 保管。** drive-merge 從不接觸或儲存 OAuth token：rclone 把它們寫進自己的設定檔。drive-merge 只寫入自己 `config.toml` 的 `[[remotes]]` 項目（名稱、服務種類、rclone 路徑）。
- **只有坐在瀏覽器前的人能連接雲端。** 連接雲端等於授權讀取其中的一切，所以它走人類通道（session cookie、CSRF 標頭、同源，且伺服器從 AI 代理之外的互動式終端機啟動），與核准計畫完全一樣。透過 MCP 的 Claude 與 API token 不能在這裡新增雲端或啟動掃描。
- **通用優先於特例。** rclone 的非互動式設定會以 JSON 提出問題（名稱、說明、預設值、範例、是否為密碼）。頁面用同一個表單呈現任何這類問題，因此即使服務問了新的問題也能運作；少數由 drive-merge 自己做的選擇（Drive scope、唯讀）會在問題開始之前先設定好。
- **機密只經手，不留存。** 密碼（iCloud）或貼上的 token 會轉交給 rclone，由 rclone 在它的設定檔中加以混淆；drive-merge 不會記錄、回傳它，也不會在請求結束後保留。

## 4. 版面

```
┌ rail ┐┌──────────────────────────────────────────────────────────────────────────┐
│  ⚙   ││ Setup                                                                    │
│  ◎   ││ ① rclone        ✓ rclone v1.71.1                                         │
│  ⌕   ││ ② Clouds        gdrive   Google Drive  read-only  ✓ connected  [Test]    │
│  ⇆   ││                 dropbox  Dropbox       read-only  ✓ connected  [Test]    │
│  ☰   ││                 [ + Connect a cloud ]                                     │
│  ▦   ││                 Found in rclone: work-od (onedrive)            [ Add ]    │
│      ││ ③ First scan    gdrive ✓ 41,203 files · dropbox ⟳ 12,004 files…          │
│      ││                 [ Scan all ]                                              │
│      ││ ④ Ready         [ Open Overview ]   execution is off (that is fine for now)│
└──────┘└──────────────────────────────────────────────────────────────────────────┘
```

- 每個步驟都會顯示自己的狀態（完成、需要處理、尚未開始），而且從不鎖住：已經有掃描好目錄的人會看到四個綠色步驟。
- **測試** 會列出該 remote 的根目錄（`rclone lsjson --max-depth 1`），回報成功，或依代碼措辭說明 rclone 的錯誤（權杖過期、找不到、網路）。
- 當核准功能停用時（伺服器由代理程式啟動，或沒有終端機），這個畫面是唯讀的，並改為顯示指令：`rclone config`、可貼上的 `[[remotes]]` 區塊、`drivemerge scan --all`。

## 5. 連接流程

```
Connect a cloud
  Provider   (•) Google Drive  ( ) Dropbox  ( ) OneDrive  ( ) iCloud Drive
  Name       [ gdrive        ]   (letters, digits, - and _; must be new in rclone and drive-merge)
  ▸ Advanced (Drive): your own OAuth client ID and secret — recommended, rclone's shared ID is being retired
  [ Sign in ]
        ↓
  "A sign-in page opened in a new tab. Finish there; this page continues by itself."   [ Open it again ] [ Cancel ]
        ↓  (provider questions, if any, one at a time)
  OneDrive: Which drive?  (•) OneDrive (personal)  ( ) Documents (business)
        ↓
  ✓ gdrive connected (read-only).  [ Scan now ]
```

- 登入用的瀏覽器分頁由頁面開啟（是一個讓使用者自己點的連結，所以不會被彈出視窗封鎖器擋下），指向 rclone 印出的 `http://127.0.0.1:53682/auth?state=…` 網址。
- 取消（或 10 分鐘沒有回應）會停止 rclone 的輔助程式，並移除建了一半的 rclone remote。
- 如果 rclone 無法使用本機的瀏覽器（例如 `drivemerge ui` 跑在另一台機器上），「要在這台機器上用瀏覽器嗎？」這個問題會回答「否」，rclone 會改要求一個在別處用 `rclone authorize` 產生的 token；通用的問題表單會顯示 rclone 自己對此的說明。

## 6. 開放寫入（之後）

整併或去重需要可寫入的 remote。在雲端那一列，**開放變更…** 會說明這代表什麼，並且：
- 對 Drive，透過同一套登入流程建立第二個 rclone remote（`<name>-rw`，完整的 `drive` scope），並記為 `write` 路徑；
- 對其他雲端，把同一個 rclone remote 記為 `write` 路徑（它們的 token 沒有 scope 限制）。

執行本身仍然需要 `[safety] execution_enabled = true`；這個畫面會顯示它，但不會更改它：開啟總開關仍然是刻意去編輯設定檔。

## 7. 已知限制（v1）

- 一次只能有一個登入進行中（rclone 的輔助程式只聽一個固定的連接埠）。
- 從 drive-merge 移除雲端、重新命名，以及編輯 `extra_args`，仍然在設定檔裡做。
- 設定檔只會以附加或更新單一 `[[remotes]]` 區塊的方式改寫；其他地方的註解會保留，被更新的區塊內的註解則不會。
- 回答與 iCloud 密碼是以命令列引數的形式交給 rclone，就像手動輸入 `rclone config create` 一樣：該命令執行期間，共用電腦上的其他帳號可以從程序清單看到它們。請使用單人使用的機器，或改在終端機用 `rclone config` 設定這類雲端。

---

# 實作合約

以下全部對實作者具有約束力。請先讀：[WORKSPACE.md](WORKSPACE.zh-TW.md) §D/§F/§G、[EXPLORE.md](EXPLORE.zh-TW.md) §F、[PLANS.md](PLANS.zh-TW.md) §C/§E、`config.py`、`rclone.py`、`cli.py`（`cmd_init`、`cmd_ui`、`_write_private`）、`service.py`、`ui_api.py`（sessions、`require_human`、jobs、`rescan`、`run_ui`）、`providers.py`，以及 `src/drivemerge/web/` 裡的每個檔案。

## A. rclone 層（`rclone.py`）

在 `Rclone` 新增（全部遵循設定的 binary 與 `--config`）：
- `version_info() -> {"version": "v1.71.1", "ok": bool}`（`ok` = ≥ 1.69）；找不到 binary 時為 `None`。
- `list_remotes() -> list[{"name", "type"}]`，來自 `rclone listremotes --long --json`（較舊的版本退回解析 `--long` 文字輸出）。
- `config_create(name, rtype, options: dict[str, str]) -> dict` 執行 `rclone config create NAME TYPE k=v… --non-interactive` 並回傳 rclone 的 JSON（`State`、`Option`、`Error`、`Result`）；`config_continue(name, state, result) -> dict` 執行 `rclone config update NAME --continue --state S --result R --non-interactive`。兩者都是**串流**的：程序的 stdout 與 stderr 都接管，並有一個 callback 接收每一行 stderr，讓呼叫端在 rclone 等待瀏覽器時取得 `http://127.0.0.1:53682/auth?state=…`。回傳值：stdout 解析出的 JSON（`State` 為空代表設定已完成）。兩者都接受 `cancel: threading.Event`，用來終止程序。
- `config_delete(name)`、`test_remote(remote) -> None | RcloneError`（對根目錄執行 `lsjson --max-depth 1`，逾時 30 秒）。
- 絕不要把問題的 `IsPassword` 為 true 的選項值放進會被記錄的命令列；把值當成引數傳入（這個模組不會記錄 argv），並且不要讓它出現在任何例外訊息裡（`RcloneError` 只帶 stderr 的尾端；請確認 rclone 不會回顯它）。

## B. 寫入設定（`config.py`）

- `add_remote_block(path, remote: RemoteConfig) -> None`：重新讀取 TOML 檔後，附加一個 `[[remotes]]` 區塊（name、provider、read、有設定時的 write、來自下方服務預設值的 extra_args）；拒絕重複的名稱（`REMOTE_EXISTS`）；以原子方式寫入（同一目錄的暫存檔、fsync、rename）並保持權限 600；取代檔案之前先用 `parse_config` 重新解析結果，任何錯誤都讓檔案維持原樣。
- `set_remote_write(path, name, write: str | None)`：就地更新該 remote 區塊的 `write` 鍵（用區塊中的 `name = "…"` 那一行找到它；找不到時以 `REMOTE_NOT_FOUND` 拒絕，區塊無法明確定位時——例如 remote 寫成內嵌表格——以 `CONFIG_LAYOUT` 拒絕）。同樣是原子寫入並重新解析。
- 寫入新區塊的服務預設值：drive → `extra_args = ["--fast-list", "--drive-skip-shortcuts"]`；其他沒有。
- rclone 種類 → 服務鍵：`drive`→`drive`、`dropbox`→`dropbox`、`onedrive`→`onedrive`、`iclouddrive`→`iclouddrive`、`local`→`local`，其他一律 → `other`。
- `ensure_config(path) -> bool`：檔案不存在時，完全照 `drivemerge init` 的方式建立設定檔（共用的輔助函式；`cmd_init` 使用它），並回傳是否建立了新檔。

## C. 服務（`service.py`）

- `Service.reload_config(config)`：取代 `self.config`，把 remotes 重新同步進 catalog，並更新所有由設定衍生的東西（若設定改變，則包含 rclone 包裝器）。UI 伺服器在每次寫入設定之後、在請求鎖之內呼叫它，之後的工作使用新的設定物件。
- `Service.setup_state() -> dict`：`{"config_path", "rclone": {"found", "version", "ok", "binary"}, "remotes": [{"name", "provider", "read", "write", "writable", "last_scan": {...} | None, "files", "bytes"}], "rclone_remotes": [{"name", "type", "provider", "in_config": bool}], "scanned": bool（每個已設定的 remote 都有完整的掃描）, "identity_built": bool, "execution_enabled": bool}`。rclone remote 清單會快取 10 秒。

## D. HTTP（`ui_api.py`）

| 方法與路徑 | 通道 | 內容 / 查詢 | 回應 |
|---|---|---|---|
| `GET /v1/ui/setup` | 一般 | | `Service.setup_state()` 加上 `"needed": bool` = 沒有設定任何 remote，或 `scanned` 為 false |
| `POST /v1/ui/setup/connect` | 人類 | `{"provider": "drive"\|"dropbox"\|"onedrive"\|"iclouddrive", "name", "client_id"?, "client_secret"?（僅限 Drive）, "apple_id"?, "password"?（僅限 iCloud，兩者都必填）, "remote_auth"?: bool}` | 202 `{"job_id"}`；拒絕 `BAD_NAME`（正規表示式 `^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$`）、`MISSING_FIELD` `{field}`、`REMOTE_EXISTS`（在 rclone 或 drive-merge 裡）、`RCLONE_MISSING`、`JOB_BUSY`；為錯誤的服務送出欄位則是 `BAD_REQUEST` |
| `POST /v1/ui/setup/connect/{job_id}/answer` | 人類 | `{"result": str}` | `{"ok"}`；工作不在等待回答時為 409 `NOT_WAITING` |
| `POST /v1/ui/setup/connect/{job_id}/cancel` | 人類 | | `{"ok"}` |
| `POST /v1/ui/setup/adopt` | 人類 | `{"name"}`（尚未加入 drive-merge 的 rclone remote） | `{"remote": {...}}`；加入一個唯讀區塊 |
| `POST /v1/ui/setup/test` | 一般 | `{"name"}` | `{"ok": true}` 或 `{"ok": false, "code", "params"}`，代碼為 `AUTH_EXPIRED`、`NOT_FOUND`、`NETWORK`、`RCLONE_ERROR`（依 rclone 的 stderr 分類） |
| `POST /v1/ui/setup/enable-write` | 人類 | `{"name"}` | Drive：202 `{"job_id"}`（為 `<name>-rw` 建立完整 scope 的 connect 工作，完成時設定 `write`）；其他：`{"remote": {...}}` |
| `POST /v1/ui/rescan` | 人類 | 同現在 | 同現在；也必須能處理從未掃描過的 remote |

connect 工作（`kind: "connect"`）在自己的執行緒中驅動 rclone 的狀態機：
1. 用該服務預設的選項呼叫 `config_create`：Drive 為 `scope=drive.readonly`（`-rw` 則為 `drive`），有給時再加上 `client_id`/`client_secret`；其他服務沒有。
2. 對回傳的各個狀態迴圈處理。遇到 `config_is_local` 自動回答 `true`（除非請求要求遠端授權）。rclone 等待瀏覽器時，工作的 progress 是 `{"stage": "signin", "auth_url": "http://127.0.0.1:53682/auth?state=…"}`（只有位於 `127.0.0.1:53682` 的網址會被傳出；其他一律丟棄）。遇到其他任何問題，progress 變成 `{"stage": "question", "question": {"name", "help", "default", "examples": [{"value", "help"}], "password": bool, "required": bool, "exclusive": bool, "type"}}`，工作會等待（最多 10 分鐘）`/answer`；當 rclone 拒絕了前一個回答時，會加上 `"retry": true`（rclone 的錯誤文字不會轉送）。`remote_auth: true` 會對 `config_is_local` 回答 `false`，讓 rclone 把「用 `rclone authorize` 產生的 token」當成一般問題來問。每個 progress 都帶有 `{"stage", "name", "provider", "mode": "new" | "write"}`；階段有 `starting`、`working`、`signin`、`question`、`saving`、`done`、`failed`。
3. 當 `State` 為空：`add_remote_block`（唯讀，或對 `-rw` 工作用 `set_remote_write`）、`reload_config`，結果為 `{"remote": {...}}`。
4. 取消、逾時、rclone 錯誤或寫入設定的錯誤時：若此工作建立了它，就 `config_delete(name)`，工作以 `{"code", "params"}` 失敗（`SIGNIN_TIMEOUT`、`CANCELLED`、`RCLONE_ERROR`、`CONFIG_WRITE`）。

工作檢視中永遠不會包含使用者輸入的回答。每個拒絕都是 `{"error", "code", "params"}`，而且每個新代碼都需要兩種語言的 `err.<CODE>`。

`run_ui` / `cmd_ui`：設定檔不存在時，`drivemerge ui` 會用 `ensure_config` 建立它（並印出位置），而不是失敗，並把 `config_path` 傳給 `create_ui_app`，因為寫入需要它。`create_ui_app(..., config_path: Path | None)`：沒有路徑時，設定的寫入路由回應 409 `NO_CONFIG_PATH`。

## E. 前端（`src/drivemerge/web/`）

- `setup.js`：設定檢視（`#/setup`）、連接對話框（服務、名稱、Drive 的進階 client id/secret）、帶有「開啟登入頁面」連結（`target="_blank"`、`rel="noopener"`）與取消的等待登入狀態、通用的問題表單（互斥範例用單選群組、非互斥用選擇或輸入、`password` 用密碼輸入框、布林值用兩個按鈕）、加入列、測試按鈕、全部掃描／逐個雲端掃描並即時顯示數量（輪詢工作）、開放變更、含可複製指令與 `[[remotes]]` 區塊的唯讀備援。
- `shell.js`：設定是導覽列的第一個項目（`Alt+0`）；其他快捷鍵維持原來的編號。當 `GET /v1/ui/setup` 回報 `needed` 時，沒有路由的頁面會前往 `#/setup` 而不是工作區（有路由的頁面則去路由指定的地方）。沒有回應 `/v1/ui/setup`（或回應中沒有 `needed`）的模擬伺服器什麼都不會改變，所以每個既有的測試仍然載入工作區。
- 總覽：當沒有任何雲端掃描過時，它的空狀態會連結到設定。
- i18n：`setup.*`、`connect.*`、`q.rclone.*`（只用於通用表單周圍的措辭；rclone 自己的說明文字因為是英文且動態，會原樣顯示在標示為 rclone 文字的方框裡）、每個新代碼的 `err.<CODE>`。

## F. 測試

- `rclone.py` 對一個假的 rclone 腳本（像既有測試的假程式）進行測試，它要能實作帶狀態的 `config create/update --non-interactive`、在 stderr 印出登入網址並等待、`listremotes`、`config delete`、`lsjson` 錯誤：串流 callback、取消會終止程序、JSON 解析、版本解析。
- `config.py`：附加與更新區塊且保留其他地方的註解、原子寫入、權限 600、重複與不存在的名稱、拒絕內嵌表格的寫法、重新解析失敗時檔案維持原樣、`ensure_config`。
- HTTP：每個路由的通道（bearer 與由代理程式啟動的伺服器被人類路由拒絕）、對假 rclone 的完整 connect 工作（帶 `auth_url` 的登入階段、一個問題、一個回答、完成時寫入區塊，且 `reload_config` 讓該 remote 立刻可掃描）、取消與逾時會刪除建了一半的 rclone remote、回答永遠不會出現在工作檢視中、加入、測試分類、Drive 與 Dropbox 的開放寫入、從未掃描過的 remote 的重新掃描、沒有設定檔時 `drivemerge ui` 會建立一個。
- 瀏覽器（模擬 API）：只有在 `needed` 時才導向設定；四個步驟的狀態；連接流程（登入連結、每種輸入類型的問題表單、回答的內容、取消）；加入；依代碼措辭的測試結果；掃描進度；開放寫入；唯讀備援；兩種語言；既有測試不變。
- 整個測試套件維持綠燈：`pytest`（包含瀏覽器測試）。
