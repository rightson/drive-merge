# 連線、同步、釋出空間

[English](ONBOARDING.md) | **繁體中文**

> 本文件讓 drive-merge 開箱即用：在頁面上連線 Google Drive、OneDrive 與 Dropbox，按一下按鈕就能把每個雲端的檔案清單同步到本機目錄，並以實際證據告訴你某個雲端最多能釋出多少空間。第一部分是設計；第二部分（實作合約）對實作者具有約束力。

## 1. 目標體驗

1. `pip install drive-merge` 然後 `drivemerge ui`。不需要設定檔、不需要 `rclone config`、不需要編輯 TOML：頁面會開在 **歡迎** 畫面，每個雲端各有一張卡片。
2. **連線**：點選卡片會在新分頁開啟服務商的登入流程；完成後卡片會顯示帳號、配額（已用／總量），以及綠色的「已連線，唯讀」徽章。
3. **同步**：標題列的一個按鈕會透過 API 並行列出每個已連線的雲端，寫入本機 SQLite 目錄，並即時顯示每個雲端的進度。之後的一切（總覽、探索、比較、計畫、工作區、釋出空間報告）都只是本機查詢。
4. **釋出空間**：選一個雲端，就會得到一個答案：「這個雲端有 *N GB* 已被證明存在於你的其他雲端」，並分成已證明、只是可能（按一下即可驗證），以及只存在這裡。再按一下就能建立釋出已證明部分的計畫；之後它會經過和其他計畫相同的審閱、輸入確認與執行。

## 2. 塑造設計的事實

| 事實 | 影響 |
|---|---|
| rclone 共用的 Google 用戶端 ID 在 2026 年停止運作，而 Google 只允許未經驗證的應用程式為其擁有者自己使用而請求 Drive 權限範圍。 | Google Drive 需要 **你自己的 OAuth 用戶端**（一次性、約 5 分鐘的 Google Cloud Console 設定）。連線對話框會逐步引導並檢查結果。 |
| OAuth 用戶端的同意畫面若處於 **測試中（Testing）**，取得的更新權杖會在 **7 天後過期**。 | 精靈會告訴你把發布狀態設為 **正式版（In production）**（應用程式仍未經驗證；登入時你會看到一個警告畫面）。否則 Drive 每週都會悄悄中斷連線。 |
| rclone 內建的 OneDrive 與 Dropbox 應用程式無需任何設定即可使用。OneDrive 接受較窄的權限範圍清單（`access_scopes`）；rclone 的 Dropbox 應用程式則一律要求完整存取權。 | OneDrive 以 **唯讀** 方式連線（`Files.Read Files.Read.All Sites.Read.All offline_access`）。透過 rclone 應用程式取得的 Dropbox 權杖 *可以* 寫入；在你允許變更之前，drive-merge 不會用它寫入，對話框也會說明這一點。若要取得真正無法寫入的權杖，對話框提供「使用我自己的 Dropbox 應用程式」（權限範圍 `files.metadata.read files.content.read account_info.read`）。 |
| rclone 的登入使用 `127.0.0.1:53682` 上的本機回呼。 | 登入只有在瀏覽器與 drive-merge 執行於同一台機器時才能運作，這是支援的設定；同一時間只能進行一次登入。 |
| 刪除到 drive-merge 的隔離區，會在 **同一個** 雲端上移動檔案，而 Google Drive 垃圾桶與 OneDrive 的資源回收筒都會計入配額。 | 只有在隔離區被 **清除** 後才會釋出空間。釋出空間檢視會以白話說明，顯示清除前後的已用／總量，並允許你在頁面上清除（需輸入確認）。 |
| 大型雲端的完整列表需要數分鐘；服務商也提供變更串流（Drive `changes.list`、Dropbox `list_folder/continue`、OneDrive `delta`）。 | v1 以完整的並行列表同步（rclone `lsjson`，Drive 上使用 `--fast-list`）。依變更串流的增量同步是第二階段（§7）：它會讓第二次及之後的同步從數分鐘縮短到數秒。 |

## 3. 連線的安全模型

- **預設唯讀。** 一次連線會建立一個 rclone 遠端 `<name>-ro`，權限範圍為服務商允許的最窄範圍（Drive `drive.readonly`、OneDrive `Files.Read…`）。掃描、比較、釋出空間報告與橋接都只使用它。
- **變更需要第二次明確同意。** 「允許 drive-merge 變更此雲端」會再執行一次具寫入權限的登入，並建立 `<name>-rw`，也就是執行器使用的 `write` 遠端。沒有它，涉及該雲端的計畫會被模擬器拒絕（`READ_ONLY_REMOTE`），和現在一樣。
- **權杖只留在本機。** drive-merge 保存自己的 rclone 設定檔 `~/.config/drive-merge/rclone.conf`（權限 600），與任何個人的 `rclone.conf` 分開。權杖從不傳給頁面、API 或 MCP；頁面只看得到帳號名稱與配額。
- **只有人可以連線。** 開始登入、啟用寫入、中斷連線與安裝 rclone，都是人類通道的路由（從終端機中 `drivemerge ui` 印出的網址開啟的瀏覽器工作階段），和核准計畫一樣。API 權杖與 MCP 不能新增或變更連線。同步同樣屬於人類通道（它會用到你的權杖與頻寬）；讀取結果則不是。
- **中斷連線**會移除 rclone 遠端與連線項目；目錄會保留最後一次看到的檔案（標記為來自已中斷連線的雲端）。

## 4. 歡迎與連線

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Welcome to drive-merge                                       rclone 1.75 ✓   │
│ Connect the clouds you want to compare. Nothing is changed until you approve │
│ a plan; connections are read-only.                                           │
│ ┌ Google Drive ──────────┐ ┌ OneDrive ──────────────┐ ┌ Dropbox ───────────┐ │
│ │ needs your own Google  │ │ ✓ connected, read-only │ │ [ Connect ]        │ │
│ │ client · 5 min, once   │ │ rightson@outlook.com   │ │                    │ │
│ │ [ Set up and connect ] │ │ 412 GB / 1 TB          │ │                    │ │
│ └────────────────────────┘ └────────────────────────┘ └────────────────────┘ │
│                                   [ Sync all and continue → ]                │
└──────────────────────────────────────────────────────────────────────────────┘
```

- 沒有任何雲端連線時會顯示歡迎畫面，並可從 **雲端** 側邊欄項目再次開啟。每張卡片包含：狀態（未連線／登入中／已連線唯讀／已連線且允許變更／需要重新登入／錯誤）、帳號、配額、最後同步時間，以及動作（連線、測試、允許變更、中斷連線）。
- **Google Drive 對話框**：附連結的編號步驟（建立專案、啟用 Drive API、把同意畫面設為 External 並 **正式版（In production）**、建立 *桌面應用程式* 類型的 OAuth 用戶端），兩個欄位分別填用戶端 ID 與密鑰，以及「連線」。錯誤會有說明（用戶端類型錯誤、同意畫面仍在測試中、API 未啟用）。
- **OneDrive／Dropbox 對話框**：一個按鈕；Dropbox 另有「使用我自己的應用程式」的說明區塊。
- 連線名稱預設為 `gdrive`、`onedrive`、`dropbox`（第二個帳號為 `-2`、`-3`）。
- **未安裝 rclone 或版本太舊**（< 1.69）：歡迎畫面的標題列會提供「安裝 rclone」：它會從 `downloads.rclone.org` 下載此平台的官方版本，依發布的 `SHA256SUMS` 驗證，並安裝到 `~/.local/share/drive-merge/bin/`。你也可以自行安裝，頁面會重新檢查。

## 5. 同步

- 頁面標題列的 **同步** 按鈕（以及歡迎畫面上的「同步全部並繼續 →」）。它會並行列出所有已連線的雲端，每個雲端各自使用自己的目錄連線（寫入由 SQLite 序列化），然後一次重建身分與資料夾統計。
- 標題列會顯示精簡的進度膠囊（`Syncing 2/3 · 184,203 files`）；彈出面板會列出每個雲端的檔案數、位元組、速率與狀態，並列出各雲端的失敗（一個雲端失敗不會停止其他雲端；它的舊資料會保留，並標記為過時）。
- 每個雲端在重要的地方都會顯示其 **資料年齡**（總覽卡片、釋出空間檢視）：「3 分鐘前同步」，超過 24 小時則以警示色調顯示。
- DESIGN.md 的可疑掃描防護仍然適用：一次讓雲端大部分內容消失的列表會被拒絕並回報。

## 6. 釋出空間

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Free space on  [ dropbox ▾ ]   folder [ (whole cloud)      ]   synced 3 min ago│
│ Used 96.0 GB of 2 TB ███████░░░░░░░░░░░░░░░░░  after purge: 41.3 GB           │
├──────────────────────────────────────────────────────────────────────────────┤
│ ✓ Proven elsewhere      54.7 GB  18,204 files   copies on gdrive 52.1 · od 2.6 │
│ ? Probably elsewhere     3.2 GB     411 files   [ Verify (downloads 3.2 GB) ]  │
│ ✕ Only on dropbox       37.9 GB   9,120 files   [ Copy to … first ]            │
│ – Cannot be judged       0.2 GB      35 files   (Paper docs, unknown size)     │
├──────────────────────────────────────────────────────────────────────────────┤
│ Keep copies on: gdrive ⠿ onedrive ⠿                                            │
│                         [ Create plan to free 54.7 GB → ]                      │
└──────────────────────────────────────────────────────────────────────────────┘
```

- **已證明存在於其他處**：所選雲端（或資料夾）中，其內容群組在其他雲端上有存活副本、且不在隔離區的每個存活檔案。證明來自目錄：強雜湊或橋接（DESIGN.md §5）。此列會依副本所在位置拆分位元組。
- **可能存在於其他處**：沒有證明，但在其他雲端上有候選檔案（大小與名稱相同，或弱雜湊相等：MD5、SHA-1、QuickXorHash）。「驗證」只會對這些檔案執行橋接（下載一次、在本機雜湊，之後它們會移到「已證明」或「只存在這裡」）。
- **只存在於此雲端**：其他地方沒有副本。釋出它們會造成遺失，因此計畫永遠不會碰它們；「先複製到 …」會開啟新增整併，並以此雲端為來源。
- **無法判斷**：原生文件與沒有大小的物件。
- **所選雲端內部的**重複檔案不會被重複計算：報告以檔案為單位，檔案只有在其他雲端上有副本時才算「已證明存在於其他處」。（在同一雲端內精簡重複檔案是去重流程的工作。）
- **新鮮度閘門**：當所選雲端或任何持有見證的雲端超過 24 小時未同步、或從未同步時，檢視會顯示「先同步」，而建立按鈕會在建構前先同步。執行器無論如何都會即時重新檢查每個來源與見證。
- **建立計畫**會建構一個 `release` 計畫：每個已證明的檔案各一筆隔離區刪除，並各自附上另一個雲端上的見證，見證依「保留副本於」的順序（再依保留策略）選出。計畫會在計畫審閱中開啟；在輸入核准確認之前，什麼都不會執行。
- **執行後**：計畫頁面與此檢視會顯示「dropbox 上有 N GB 在隔離區；清除後空間才會回來」，並提供「立即清除」（輸入確認 `purge`；清除前會先重新證明每個見證）或「30 天後」（預設保留期限）。

## 7. 第二階段：增量同步（不在本版本中）

完整列表後，為每個雲端儲存一個變更游標，並在下次同步時只套用變更：Drive 以起始頁面權杖執行 `changes.list`，Dropbox 以游標執行 `files/list_folder/continue`，OneDrive 則執行 `/drive/root/delta`。游標過期或出錯時會退回完整列表；每週仍會執行一次完整列表，以捕捉變更串流遺漏的任何內容。這會讓大型雲端的重複同步從數分鐘縮短為數秒。它需要使用 rclone 保存的 OAuth 權杖，直接透過 HTTPS 使用，並以錄製的 API 回應進行測試。

## 8. 已知限制（v1）

- 每次同步都是完整列表（§7）。
- iCloud Drive 用 Apple ID 與密碼加上雙重驗證碼連接（§G）；rclone 在那一次呼叫中會以命令列引數收到密碼，所以在共用電腦上，其他帳號在它執行期間可以從程序清單讀到。
- Google Drive 需要你自己的 OAuth 用戶端（§2）。
- 頁面上的登入需要瀏覽器與 drive-merge 位於同一台機器（迴路回呼）；否則要在另一台電腦上用 `rclone authorize` 產生權杖再貼上（§G）。

---

# 實作合約

以下全部具有約束力。請先閱讀：[DESIGN.zh-TW.md](DESIGN.zh-TW.md)、[WORKSPACE.zh-TW.md](WORKSPACE.zh-TW.md) §D、[EXPLORE.zh-TW.md](EXPLORE.zh-TW.md)、[PLANS.zh-TW.md](PLANS.zh-TW.md)、`config.py`、`rclone.py`、`scanner.py`、`service.py`、`ui_api.py`、`builders.py`、`identity.py`（`STRONG_TYPES`、`WEAK_TYPES`、`content_key`）、`coverage.py`、`executor.py`（`purge_quarantine`），以及 `src/drivemerge/web/`。

## A. 連線（`src/drivemerge/connections.py`、`config.py`）

檔案皆為權限 600，位於主設定檔旁（預設為 `~/.config/drive-merge/`）：
- `rclone.conf`：drive-merge 自己的 rclone 設定檔。當主設定沒有設定 `[rclone] config` 時，`Config.rclone_config` 預設使用它。
- `connections.toml`：由 drive-merge 寫入的 `[[remotes]]` 項目（與主設定的 `[[remotes]]` 結構相同，另外加上 `connection = true`、`account`、`created_at`）。`load_config` 會把它們接在主設定的遠端之後合併；名稱重複是錯誤，錯誤訊息會同時指出兩個檔案。寫入是原子的（暫存檔加上重新命名）。

```python
PROVIDER_SPECS: dict[str, ProviderSpec]   # 'drive', 'onedrive', 'dropbox'
@dataclass(frozen=True)
class ProviderSpec:
    key: str; rclone_type: str; label: str
    needs_client: bool                       # drive: True
    read_options: dict[str, str]             # drive {'scope': 'drive.readonly'}; onedrive {'access_scopes': 'Files.Read Files.Read.All Sites.Read.All offline_access'}; dropbox {}
    write_options: dict[str, str]            # drive {'scope': 'drive'}; onedrive default scopes; dropbox {}
    extra_args: tuple[str, ...]              # drive ('--fast-list', '--drive-skip-shortcuts')

class ConnectError(ValueError): code: str   # NO_RCLONE, RCLONE_TOO_OLD, FLOW_BUSY, FLOW_FAILED, FLOW_TIMEOUT, NAME_TAKEN, BAD_NAME, NEEDS_CLIENT, TEST_FAILED, NOT_FOUND

def rclone_status(config) -> dict            # {'found', 'path', 'version', 'ok' (>= 1.69), 'managed' (installed by drive-merge)}
def install_rclone(dest_dir: Path, *, progress=None) -> Path   # official zip for this OS/arch + SHA256SUMS verification; raises on mismatch
def list_connections(config) -> list[dict]   # name, provider, account, read, write, writable, created_at (never tokens)

class AuthFlow:  # one rclone `authorize` subprocess
    id: str; provider: str; purpose: str ('read'|'write'); status: str ('waiting'|'succeeded'|'failed'|'cancelled'|'timeout')
    auth_url: str | None; error: str | None
def start_flow(config, provider, *, purpose='read', client_id=None, client_secret=None, timeout=600) -> AuthFlow
def get_flow(flow_id) -> AuthFlow
def cancel_flow(flow_id) -> None
def finish_connect(config, flow_id, *, name: str | None = None) -> dict      # creates '<name>-ro', tests it, writes connections.toml; returns the connection
def finish_enable_writes(config, flow_id, name: str) -> dict                  # creates '<name>-rw', sets write in connections.toml
def test_connection(config, name) -> dict    # lsjson --max-depth 1 + about: {'ok', 'account', 'quota': {'used','total','free'} | None, 'error'}
def disconnect(config, name) -> None         # deletes '<name>-ro'/'<name>-rw' from drive-merge's rclone.conf and the entry
```

- `start_flow` 執行 `rclone authorize <type> [client_id client_secret] --auth-no-open-browser`（若 rclone 接受，再加上該用途的權限範圍選項，以 `--<type>-<option>` 旗標傳入），使用 drive-merge 的 rclone 設定，從其輸出讀取登入網址（≤ 15 秒），並保留該程序；權杖在程序以 0 結束時從 stdout 讀取，且只保存在記憶體中。同一時間只能有一個流程（`FLOW_BUSY`）；`timeout` 會終止它。
- `finish_connect` 以 `rclone config create <name>-ro <type> token=<json> <read_options…> [client_id=… client_secret=…] --non-interactive` 建立遠端，若 rclone 提出問題則遵循其非互動狀態機（OneDrive 的雲端硬碟選擇：帳號的預設雲端硬碟）。接著執行 `test_connection`；若失敗，遠端會被再次移除，並拋出附上 rclone 訊息的 `TEST_FAILED`。
- 名稱：`^[a-z][a-z0-9-]{0,30}$`，且尚未設定為遠端（`NAME_TAKEN`）。
- `account` 透過 rclone 從服務商取得（`rclone about`，或在可用時取 `backend` 資訊）；盡力而為，可能為 None。
- 此模組中沒有任何東西會記錄或回傳權杖。測試使用假的 `rclone` 可執行檔（放在 PATH 上的腳本），實作 `version`、`authorize`（印出網址，然後在觸發檔出現後印出權杖）、`config create/delete`、`lsjson`、`about`。

CLI：`drivemerge connect drive|onedrive|dropbox [--name N] [--client-id … --client-secret …]` 在終端機中執行相同的流程（印出網址並等待）、`drivemerge connect --enable-writes NAME`、`drivemerge disconnect NAME`、`drivemerge connections`。`drivemerge init` 不再寫入範例遠端；沒有設定檔時，`drivemerge ui` 會先執行 `init`。

## B. 同步（`src/drivemerge/syncall.py`）

```python
def sync_all(config, *, remotes: list[str] | None = None, progress=None, max_parallel: int = 4) -> dict
```

以並行執行緒列出給定的遠端（預設為全部）；每個執行緒各自開啟 `db.connect(config.db_path)` 並呼叫 `scanner.scan_remote`。每批呼叫一次 `progress(name, state, files, dirs, bytes)`（`state`：queued、listing、done、failed）。之後在新連線上執行一次 `identity.rebuild_groups` 與 `dirstats.refresh`。回傳 `{'remotes': {name: {'status', 'files', 'bytes', 'seconds', 'error'}}, 'seconds', 'identity': …}`。單一遠端失敗不會停止其他遠端。`Service.sync(remotes=None, progress=None)` 包裝了它。遠端的資料年齡是其最後一次完整掃描的 `finished_at`。

## C. 釋出空間（`src/drivemerge/release.py`）

```python
def release_report(conn, remote: str, *, path_prefix: str | None = None, keep_on: list[str] | None = None,
                   stale_after_hours: int = 24) -> dict
def build_release(conn, *, remote: str, path_prefix: str | None = None, keep_on: list[str] | None = None,
                  quarantine_dir: str | None = None) -> builders.BuildResult
```

- `remote` 在 `path_prefix` 之下的存活檔案（隔離區之外）。分類桶：`proven`（內容群組在另一個遠端上有存活副本，若有給 `keep_on` 則限定於其中，且不在隔離區）、`probable`（沒有證明；另一個遠端上有大小與 name_norm 相同的存活檔案，或弱雜湊相等）、`only_here`、`unjudged`（原生文件、大小未知）。每個桶有 `files`、`bytes`；`proven` 還有 `by_remote: {name: bytes}`（其所選見證所在的遠端）；`probable` 還有 `entry_ids`（上限 5000，附 `entry_ids_total`）以供驗證。
- `freshness`：`{name: {'synced_at', 'stale'}}`，涵蓋 `remote` 以及所有持有見證或候選的遠端；超過 `stale_after_hours` 或從未同步時 `stale` 為真。
- `quota`：此處不計算（需要網路）；HTTP 層會加上快取的 `test_connection` 配額。
- `build_release`：每個 `proven` 檔案一個 `delete` 操作（`builders.op_from_entry`，`reason='release <remote>: copy on <w_remote>:<w_path>'`），見證為另一個遠端上的存活副本，依 `keep_on` 順序，再依 `choose_keeper`。絕不包含原生文件、有歧義的 Drive 路徑或隔離區。若 `remote` 沒有寫入遠端，則拒絕（BuildError，代碼 `READ_ONLY`）。`stats`：報告的各個桶。計畫種類為 `release`；`Service.propose_plan('release', {...})` 會分派到此處。
- 限定於給定項目的橋接：`Service.bridge(entry_ids=[…])`（擴充既有方法）只下載這些檔案的位元組（或在另一端較便宜的候選即可決定同一個問題時使用該候選），然後重建群組。

## D. HTTP（`ui_api.py`、`api.py`）與 MCP

| 路由 | 通道 | 請求內容／查詢 → 回應 |
|---|---|---|
| `GET /v1/setup` | 讀取 | `{'first_run', 'config_path', 'rclone': rclone_status, 'connections': [...], 'providers': [{key,label,needs_client}]}` |
| `POST /v1/ui/connect/start` | 人類 | `{provider, purpose:'read', client_id?, client_secret?}` → `{flow_id, auth_url}` |
| `GET /v1/ui/connect/flows/{id}` | 人類 | 流程狀態（不含權杖） |
| `POST /v1/ui/connect/flows/{id}/finish` | 人類 | `{name?}` → 連線；用途為 `write` 時為 `{name}` |
| `POST /v1/ui/connect/flows/{id}/cancel` | 人類 | `{ok}` |
| `POST /v1/ui/connections/{name}/test` | 人類 | 測試結果（配額快取 10 分鐘） |
| `DELETE /v1/ui/connections/{name}` | 人類 | `{ok}` |
| `POST /v1/ui/rclone/install` | 人類 | 工作 |
| `POST /v1/ui/sync` | 人類 | `{remotes?}` → 工作（種類 `sync`，每個遠端各有進度）；取代舊重新掃描路由的請求內容（保留 `/v1/ui/rescan` 作為別名） |
| `GET /v1/release/{remote}` | 讀取 | `path`、`keep_on`（逗號分隔的清單）→ 報告 + `quota` |
| `POST /v1/ui/plans/{id}/purge` | 人類 | `{confirm:'purge', older_than_days?: 0}` → 工作 |

錯誤一律附帶 `code` 與 `params`（與其他地方相同）。連線在伺服器執行期間建立，伺服器不需重新啟動即可看見（服務會重新載入合併後的設定）。MCP：一個唯讀的 `free_space_report` 工具；`propose_plan` 接受種類 `release`。MCP 中沒有連線或同步工具。

## E. 前端（`src/drivemerge/web/`）

- 側邊欄：**雲端** 項目排在最前（沒有連線時為歡迎畫面，否則為卡片）。載入時若 `first_run` 或沒有任何連線，頁面會開在此處。
- 連線對話框如 §4；頁面會在新分頁開啟 `auth_url`，並每秒輪詢流程；成功後更新卡片；錯誤依代碼翻譯。
- 標題列：同步按鈕、進度膠囊與彈出面板（§5）；總覽卡片上的資料年齡標籤。
- **釋出空間** 檢視（`#/free/<remote>?path=…&keep=…`）：§6 的版面；從每張總覽雲端卡片（「釋出空間…」）與計畫（「新增：釋出空間」）連結過來。「驗證」會啟動限定範圍的橋接工作；「建立計畫」會提出 `release` 並開啟其審閱；執行後，計畫頁面會提供「清除」。
- 所有文字都透過 `i18n.js`，以英文與繁體中文呈現；每個測試需要的新元素都有 `data-testid`。

## F. 測試

- 每條連線路徑的假 rclone 測試，包括緩慢的登入、失敗的登入、取消、逾時、忙碌的流程、名稱衝突、權杖從未出現在任何 HTTP 回應或日誌行中、600 的檔案權限，以及合併後的設定。
- `sync_all` 以三個本機遠端並行執行，其中一個失敗。
- `release_report`／`build_release` 涵蓋已證明、可能（名稱加大小，以及弱雜湊）、只在此處、無法判斷、keep_on、隔離區、唯讀與過時的情況；每個建構的計畫都以 `contents_lost == 0` 模擬通過。
- Playwright 端對端執行：無設定啟動 → 歡迎 → 連線假的 Dropbox 與假的 Drive（假 rclone 由本機資料夾支援）→ 同步 → 對其中一個釋出空間 → 驗證 → 建立計畫 → 核准 → 執行 → 立即清除 → 位元組已從該資料夾消失，並仍存在於另一個雲端。

## G. 與設定畫面（Setup）整併

PR #5（`claude/setup-screen`，文件 `SETUP.md`）在這套流程旁邊另外做了一個新手引導：導覽列上的 **Setup** 項目、`setup.js`、`/v1/ui/setup/*`、`rclone.py` 裡串流式的 `rclone config` 狀態機，以及寫進使用者自己 `config.toml` 的 `[[remotes]]` 區塊。合併之後，新使用者會看到兩種連接雲端的方式。本節具有約束力，並且取代 PR #5 設計中的任何內容：只保留**一個**新手引導，也就是本文件的**雲端**檢視及其儲存模型（`connections.toml` + drive-merge 自己的 `rclone.conf`，`<name>-ro`/`<name>-rw`）。PR #5 中本文件缺少的功能併入這裡；PR #5 新手引導的其餘部分一律刪除。本節與 §1–§8 或 A–F 衝突時，以本節為準。

### G1. 為什麼以雲端模型為基礎

| 問題 | 雲端（本文件） | Setup（PR #5） | 保留 |
|---|---|---|---|
| 連線寫在哪裡 | `connections.toml`，由 drive-merge 擁有並整份重寫的檔案；權杖放在它自己的 `rclone.conf` | 對人工編輯的 `config.toml` 做逐行修改（`add_remote_block`、`set_remote_write`、重新解析、拒絕內嵌表格）；權杖放在正在使用的任何 rclone 設定裡 | 雲端：不編輯人工維護的檔案，沒有 `CONFIG_LAYOUT` 這類失敗，可以中斷連線 |
| 預設唯讀 | 每個服務使用最窄的權限範圍（Drive `drive.readonly`、OneDrive `Files.Read…`）；開放寫入需要第二次登入，建立 `<name>-rw` | Drive `drive.readonly`；其他沒有限縮範圍，「Enable changes」只是把 `read` 複製成 `write` | 雲端（OneDrive 只有這裡才真的唯讀） |
| 登入機制 | `rclone authorize` → 權杖留在記憶體 → `config create … token=…`；登入成功之前不會寫入 rclone 任何東西 | 不帶權杖的 `config create`，在 rclone 等待瀏覽器時串流進行；每次失敗都必須刪除做到一半的遠端 | OAuth 用雲端的做法；PR #5 的問題迴圈只保留在 `authorize` 無法使用的地方（iCloud，以及只有使用者能回答的問題） |
| 同步／第一次掃描 | 平行的 `sync_all`、標頭進度藥丸與彈出視窗，身分只重建一次 | 由設定頁輪詢的序列式重新掃描工作 | 雲端 |
| 缺少 rclone | 經過驗證的安裝，安裝到 `~/.local/share/drive-merge/bin` | 連到 rclone.org 的連結 | 雲端 |
| 中斷連線、容量、帳號、Google 指南、Dropbox 自有應用程式 | 有 | 沒有 | 雲端 |
| iCloud、通用問題表單、為另一台電腦貼上權杖、採用既有的 rclone 遠端、唯讀備援、依代碼說明的測試失敗、`needed` 轉址、`lsjson` 測試逾時、僅限本機迴路的登入網址、代碼的 i18n 契約 | 沒有 | 有 | 從 PR #5 移植（G3–G6） |

### G2. 功能對照表（兩邊各有什麼；合併後的檢視做什麼）

| 功能 | Main（PR #6） | PR #5 | 合併後 |
|---|---|---|---|
| rclone 偵測／安裝 | `connections.rclone_status`、`install_rclone`（SHA256SUMS）、`POST /v1/ui/rclone/install`、`RcloneBanner` | `Rclone.version_info`，只有安裝連結 | main |
| `127.0.0.1:53682` 上的 OAuth 登入 | `start_flow`（`rclone authorize`）、輪詢的流程、`_URL_RE` 接受任何主機 | 串流式 `config create`，`AUTH_URL` 只接受 `127.0.0.1:53682` | main，但採用 PR #5 只限迴路位址的網址樣式 |
| 通用 rclone 問題表單 | 無：每個問題都自動回答（`_create_remote`） | `_question`、`QuestionForm`（單選、選擇或輸入、密碼、是／否） | 已知問題自動回答（main 的對照表）；其他任何問題，以及 rclone 提供多個雲端硬碟時 OneDrive 的硬碟選擇，交給 PR #5 的表單 |
| iCloud（Apple ID、密碼、2FA） | 不提供（只能用終端機） | 先給 `apple_id`/`password`，2FA 透過表單 | 已移植；規格 `iclouddrive`，種類 `config` |
| 來自另一台電腦的權杖 | 不提供 | `remote_auth` 回答 `config_is_local=false`，權杖以問題的方式貼上 | `POST /v1/ui/connect/paste`，並顯示確切的 `rclone authorize` 指令 |
| 採用既有的 rclone 遠端 | 無 | 列出正在使用的 rclone 設定中的遠端並寫入一個區塊 | **匯入**：把使用者自己 rclone 設定中的遠端複製到 drive-merge 的 `rclone.conf`，名為 `<name>-ro`（G4.4） |
| 唯讀 +「Enable changes」 | 所有服務都用第二次登入 → `<name>-rw` | Drive 第二次登入；其他服務重用 `read` | main；沒有限縮範圍登入的服務（iCloud、匯入的）在確認後重用 `read` |
| 測試連線 | `POST /v1/ui/connections/{name}/test`（human），原始錯誤文字，無逾時 | `POST /v1/ui/setup/test`（normal），依代碼分類 `AUTH_EXPIRED`/`NETWORK`/`NOT_FOUND`/`RCLONE_ERROR`，30 秒 | main 的路由，加上 PR #5 的代碼與 30 秒逾時；`config.toml` 裡的遠端也適用 |
| 第一次掃描／同步，含進度與身分重建 | `sync_all`、`/v1/ui/sync`、藥丸 + 彈出視窗 | 設定的第 3 步輪詢 `/v1/ui/rescan`，額外的 `remote_index`/`scans` 欄位 | main；PR #5 額外的進度欄位刪除 |
| 清除／移除 | 清除路由、`disconnect` | 無（移除「留在設定檔裡」） | main |
| 設定寫在哪裡 | `connections.toml` + 自己的 `rclone.conf` | `config.toml [[remotes]]` | main |
| 人工通道 | 連接／完成／取消／測試／刪除／安裝／同步／清除都用 `require_human` | 它的路由用同樣的機制 | main 的清單加上新路由（G4.5） |
| 機密遮蔽 | 權杖／用戶端密鑰保存在 `AuthFlow`、`_scrub`、假 rclone 日誌遮蔽 | 回答從不出現在工作檢視、`_config_call` 遮蔽 argv 與 stderr | main 的做法，擴充到回答、Apple 密碼與貼上的權杖（G4.2） |
| 唯讀備援（核准關閉時） | 無：卡片甚至會自動執行測試並顯示 `APPROVALS_DISABLED` | 附指令的唯讀面板 | 移植到雲端檢視，使用 `drivemerge connect` 指令 |
| 首次執行轉址 | 沒有路由的頁面 → `first_run` 或沒有任何連線時 `#/clouds` | 沒有路由 → `needed` 時 `#/setup` | 單一規則：沒有路由 → `needed` 時 `#/clouds`（G4.5） |
| i18n | `conn.*`、`welcome.*`、`rclone.*`、`err.<ConnectError>`（沒有契約測試） | `setup.*`、`connect.*`、`err.<code>`，有契約測試 | 只用 `conn.*`；PR #5 的契約測試改以 `ConnectError` 為目標 |

### G3. 合併後的使用體驗

導覽列是 `Clouds, Overview, Explore, Compare, Plans, Workspace`（與 main 相同；`Alt+1…5` 不變，沒有 `Alt+0`）。沒有指定路由而開啟的頁面，在 `needed`（沒有設定任何雲端，或有設定的雲端但沒有完整掃描）時會前往 `#/clouds`；舊書籤的 `#/setup` 會被替換成 `#/clouds`。雲端檢視顯示：rclone 橫幅或標籤；每個連線各一張卡片，**以及**在 `config.toml` 手動設定的每個遠端各一張；每個服務各一個方塊（Google Drive、OneDrive、Dropbox、**iCloud Drive**）；當使用者自己的 rclone 設定中有可匯入的遠端時，顯示「已經在使用 rclone？」清單；「同步全部並繼續」。每一條連接路徑都會落在同一張卡片，且一律唯讀。

### G4. 後端合約

#### G4.1 `connections.py`

```python
@dataclass(frozen=True)
class ProviderSpec:
    ...                                   # existing fields unchanged
    kind: str = "authorize"               # 'authorize': rclone authorize + token; 'config': rclone config asks (iCloud)
    fields: tuple[str, ...] = ()          # what `start_flow` needs up front for kind 'config'
    scoped_write: bool = True             # False: the read sign-in can already write; Enable changes reuses `read`

PROVIDER_SPECS["iclouddrive"] = ProviderSpec("iclouddrive", "iclouddrive", "iCloud Drive", False, {}, {}, (),
                                             "icloud", kind="config", fields=("apple_id", "password"),
                                             scoped_write=False)

class ConnectError(ValueError):
    def __init__(self, code: str, message: str, **params: str)   # params added (MISSING_FIELD {field}, ...)
# codes: NO_RCLONE, RCLONE_TOO_OLD, FLOW_BUSY, FLOW_FAILED, FLOW_TIMEOUT, NAME_TAKEN, BAD_NAME, NEEDS_CLIENT,
#        TEST_FAILED, NOT_FOUND, plus MISSING_FIELD {field}, NOT_WAITING, TOKEN_UNREADABLE, CONFIG_WRITE,
#        IMPORT_UNSUPPORTED {type}, IMPORT_ENCRYPTED, BAD_REQUEST

class AuthFlow:
    status: str      # waiting | question | working | succeeded | done | failed | cancelled | timeout
    name: str | None                 # reserved connection name once known
    question: dict | None            # question_view(...) while status == 'question'
    retry: bool                      # rclone refused the previous answer (its text is never forwarded)
    connection: dict | None          # the public entry once status == 'done'
    code: str | None; params: dict   # coded failure
    # private: _token, _client_id, _client_secret, _secrets (every value to scrub), _state (rclone State),
    #          _remote (the rclone remote this flow created and must remove on failure), _timer
    def public(self) -> dict         # id, provider, purpose, status, auth_url, error, code, params, name,
                                     # question, retry, connection; never a token, password, answer or secret

def question_view(opt: dict) -> dict  # moved from ui_api._question, same shape:
                                      # {name, help, default, examples:[{value, help}], password, required, exclusive, type}
def start_flow(config, provider, *, purpose='read', name=None, client_id=None, client_secret=None,
               apple_id=None, password=None, timeout=600, url_wait=15) -> AuthFlow
def paste_token(config, provider, token: str, *, purpose='read', name=None,
                client_id=None, client_secret=None) -> AuthFlow
def authorize_command(provider: str, purpose: str, client_id: str | None = None) -> list[str]
def finish_connect(config, flow_id, *, name=None) -> dict          # connection | question result
def finish_enable_writes(config, flow_id, name) -> dict            # connection | question result
def answer_flow(config, flow_id, result: str) -> dict              # connection | question result
def importable(config) -> dict
def import_remote(config, source: str, *, name: str | None = None) -> dict   # connection | question result
def test_connection(config, name) -> dict
```

一個**問題結果**是 `{"flow_id", "status": "question", "question", "retry"}`；連線永遠沒有 `flow_id` 這個鍵，呼叫端就是靠這點來分辨兩者。

- `_URL_RE` 改成 `http://127\.0\.0\.1:53682/auth\?state=[A-Za-z0-9_.~%-]+`（PR #5 的 `AUTH_URL`）。rclone 印出的其他任何網址都不會存進流程。
- **一個問題迴圈**，`_advance(config, flow)`，取代 `_create_remote` 內嵌的迴圈，並供每一條執行 `rclone config create … --non-interactive` 的路徑使用。自動回答：`config_refresh_token` → `false`、`config_change_team_drive` → `false`、`config_type` → `onedrive`、`config_drive_ok` → `true`、`config_driveid` → rclone 提供的範例至多一個時取其預設值。其他每一個問題（有多個硬碟的 OneDrive、iCloud 的 `config_2fa`、rclone 日後新增的任何問題）都會把 `status = 'question'`，儲存 `question_view(Option)` 與 rclone 的 `State`，並回傳問題結果。對自動回答的問題出現帶有 `Option` 的 `Error` 是 `FLOW_FAILED`；對使用者回答的問題則以 `retry = True` 重新詢問。最多 30 輪（`FLOW_FAILED`）。
- `answer_flow`：除非 `status == 'question'`，否則回傳 `NOT_WAITING`；`result` 最多 65 536 個字元（`BAD_REQUEST`）。它執行 `config create <remote> <type> --non-interactive --continue --state S --result R`（逾時 120 秒）並繼續 `_advance`。回答在 rclone 執行之前先加進 `_secrets`，從每一段 stderr 尾端與每則訊息中遮蔽，並在呼叫返回時丟棄。
- 問題最多等待 600 秒（`threading.Timer`）：之後 `status = 'timeout'`、`code = 'FLOW_TIMEOUT'`，刪除遠端並釋出名稱。`cancel_flow` 在 `waiting`、`question` 與 `succeeded` 都有效，也會刪除 `_remote`。
- 任何流程處於 `waiting`、`question` 或 `working` 時，回傳 `FLOW_BUSY`。
- **名稱**：`NAME_RE` 維持 `^[a-z][a-z0-9-]{0,30}$`。`_taken` 也會計入由進行中流程保留的名稱（`_RESERVED`，在 `_FLOWS_LOCK` 之下），因此使用者在回答問題時，名稱不會被第二次登入搶走。在 `done`、`failed`、`cancelled`、`timeout` 時釋出。
- **回復**：`_remote` 存在之後的每一次失敗都會刪除它（`_delete_remote`），包括失敗的 `_write_entries`（新代碼 `CONFIG_WRITE`；main 目前在那裡會留下孤立的 `<name>-ro`）。`TEST_FAILED` 與現在相同。
- **種類 `config`（iCloud）**：`start_flow` 需要 `apple_id` 與 `password`（`MISSING_FIELD {field}`），對任何其他服務拒絕它們，並對 drive/dropbox 以外的服務拒絕 `client_id`/`client_secret`（`BAD_REQUEST`）。它挑選並保留名稱（`name`，或 `icloud`、`icloud-2`、…），以同步方式執行 `config create <name>-ro iclouddrive apple_id=… password=… --non-interactive`（逾時 120 秒；密碼進入 `_secrets`，之後丟棄），然後執行 `_advance`；回傳的流程處於 `question`（2FA 驗證碼）或 `done`。它不使用 `finish_connect`；最後一個回答會執行測試並寫入項目。
- **paste_token**：只適用於種類 `authorize`。接受 rclone 的整段貼上區塊（`--->` … `<---End paste`）或單純的 JSON（`_parse_token`）；最多 16 384 個字元；其他一律是 `TOKEN_UNREADABLE`（訊息絕不引用輸入）。Drive 仍然需要用戶端 ID 與密鑰（`NEEDS_CLIENT`），因為權杖綁定在製造它的那個用戶端。回傳一個已經是 `succeeded` 且沒有程序的流程；接著頁面像瀏覽器登入之後那樣呼叫 finish。`authorize_command` 回傳對話框顯示的 argv：`["rclone", "authorize", <type>, <client_id>, "CLIENT_SECRET", <該用途的權限範圍旗標>]`（用戶端這一對只在有提供時才加入；密鑰是佔位符，由使用者自己輸入）。貼上的權杖依 drive-merge 的政策視為唯讀：它的權限範圍無法檢查，對話框會說明指令必須完全照所示執行。
- 對於其服務有 `scoped_write = False`、或是匯入（設有 `imported_from`）的項目，**開放變更**：`start_flow(purpose='write', name=…)` 回傳一個已經是 `succeeded` 的流程；`finish_enable_writes` 把 `write` 設為該項目的 `read`（不建立第二個遠端）。Drive、OneDrive、Dropbox 連線仍然使用第二次登入。
- **項目**在 `connections.toml` 中多了兩個選用的鍵：`scoped = false`（iCloud、匯入的）與 `imported_from = "<使用者 rclone 設定中的遠端名稱>"`。`list_connections` 把它們回傳為 `scoped`（預設 true）與 `imported_from`（預設 null）。
- **disconnect** 只刪除項目的 `read` 與 `write` 所指、且是 drive-merge 的 `rclone.conf` 中區段的 rclone 遠端（而不是猜測 `-ro`/`-rw`）。它絕不會開啟使用者自己的 rclone 設定。

#### G4.2 機密（合併後的保證）

下列任何東西都絕不會出現在 HTTP 回應、流程檢視、工作檢視、日誌行、例外訊息或 `FAKE_RCLONE_LOG` 中：OAuth 權杖、貼上的權杖、Drive/Dropbox 的用戶端密鑰、Apple ID 密碼、`password` 為 true 的問題的任何回答，以及流程檢視中的任何回答。它們全都會用 `_scrub(text, *flow._secrets)` 從每一段 rclone stderr 尾端遮蔽，之後才成為 `error` 或訊息。rclone 會在一次呼叫中以命令列引數的方式收到權杖、密碼與回答（§8 的限制）。

#### G4.3 `test_connection`

`test_connection(config, name)` 也接受來自 `config.toml` 的遠端（非連線），使用它的 `read`。它透過 `_run` 以 30 秒逾時（PR #5 的 `TEST_TIMEOUT_S`）執行 `lsjson --max-depth 1`，然後如現在一樣執行 `about` 與帳號。結果：`{'ok', 'account', 'quota', 'error', 'code', 'params'}`；失敗時 `code = rclone.classify_failure(returncode, stderr)`（`AUTH_EXPIRED`、`NETWORK`、`NOT_FOUND`、帶有 `params.detail` 的 `RCLONE_ERROR`），`error` 如現在一樣是遮蔽後的尾端。`classify_failure` 及其正規表示式留在 `rclone.py`。

#### G4.4 匯入（取代 PR #5 的 adopt）

- 使用者自己的 rclone 設定是 `RCLONE_CONFIG`（若有設定），否則是**不帶** `--config` 執行 `rclone config file` 所印出的最後一行。若它解析到 drive-merge 的 `rclone.conf`，則忽略。
- `importable(config) -> {'config_path': str | None, 'encrypted': bool, 'remotes': [{'name', 'type', 'provider', 'supported', 'imported'}]}`：區段名稱與 `type` 直接從該檔案讀取（絕不透過會印出權杖的子程序）。第一個非空白行是 `# Encrypted rclone configuration File` 的檔案會得到 `encrypted: true` 且沒有遠端。`provider` 是 `config.provider_of(type)`。對包裝其他遠端或輪替一次性權杖的遠端，`supported` 為 false：`alias`、`crypt`、`union`、`combine`、`chunker`、`hasher`、`compress`、`cache`、`box`。當某個項目已有 `imported_from == name` 時，`imported` 為 true。
- `import_remote(config, source, *, name=None)`：`IMPORT_ENCRYPTED`、`NOT_FOUND`、`IMPORT_UNSUPPORTED {type}`；名稱 = `name`，或是把來源名稱轉成小寫並符合 `NAME_RE`（否則 `BAD_NAME`/`NAME_TAKEN`）。它在 drive-merge 的 `rclone.conf` 中以 `config create <name>-ro <type> <來源區段中除了 type 以外的每個 key=value> --no-obscure --non-interactive` 建立 `<name>-ro`（值照儲存的樣子複製，所以已混淆的密碼只會保持混淆一次），執行 `_advance`，測試，並寫入項目，帶有 `scoped = false`、`imported_from = source`，`write` 不設定。區段中的每一個值都進入 `_secrets`。使用者的檔案絕不會被寫入。

#### G4.5 HTTP

新增與變更的路由（§D 的其餘部分不變）：

| 路由 | 通道 | 本文／查詢 → 回應 |
|---|---|---|
| `GET /v1/setup` | read | 如 §D，另加 `needed: bool`（沒有設定任何遠端，或有設定的遠端但沒有完整掃描）、`manual_remotes: [{name, provider, writable}]`（`config.toml` 的遠端）、每個連線的 `scoped` 與 `imported_from`，以及每個服務的 `kind`、`fields` 與 `authorize: {read: [...], write: [...]}`（不帶用戶端 ID 的 `authorize_command`）。回答之前，若 `connections.toml` 的 mtime 自上次載入後改變了，服務會重新載入合併後的設定（伺服器執行期間用 `drivemerge connect` 建立的連線）。 |
| `POST /v1/ui/connect/start` | human | `{provider, purpose, name?, client_id?, client_secret?, apple_id?, password?}` → `{flow_id, auth_url, status}`；iCloud 的回應是 `auth_url: null` 的問題結果（HTTP 202），或是連線（200） |
| `GET /v1/ui/connect/flows/{id}` | human | `AuthFlow.public()` |
| `POST /v1/ui/connect/flows/{id}/finish` | human | `{name?}` → 200 連線，或 202 問題結果 |
| `POST /v1/ui/connect/flows/{id}/answer` | human | `{result}` → 200 連線，或 202 問題結果；409 `NOT_WAITING` |
| `POST /v1/ui/connect/flows/{id}/cancel` | human | `{ok}` |
| `POST /v1/ui/connect/paste` | human | `{provider, purpose, name?, client_id?, client_secret?, token}` → `{flow_id, status: "succeeded"}` |
| `GET /v1/ui/connect/importable` | normal | `importable(config)` |
| `POST /v1/ui/connect/import` | human | `{source, name?}` → 200 連線，或 202 問題結果 |
| `POST /v1/ui/connections/{name}/test` | human | G4.3 的結果；`manual_remotes` 也適用 |
| `POST /v1/ui/sync`、`/v1/ui/rescan` | human | 如 §D；PR #5 新增的進度鍵 `remote`、`remote_index` 與結果鍵 `scans` 移除 |

每一次成功的 finish、answer 或 import，都會在請求鎖之下呼叫 `quotas.drop(name)` 與 `service.reload_config()`，就像現在的 `connect_finish` 一樣。`ConnectError` 繼續透過 `api.install_error_handlers` 帶著 `code` 與 `params` 呈現；`_CONNECT_STATUS` 新增 `MISSING_FIELD: 400`、`BAD_REQUEST: 400`、`TOKEN_UNREADABLE: 400`、`NOT_WAITING: 409`、`IMPORT_UNSUPPORTED: 400`。失敗流程的 `public()` 帶有 `code`/`params`。

**從 `ui_api.py` 刪除：** `GET /v1/ui/setup`、`POST /v1/ui/setup/test`、`/v1/ui/setup/connect`、`/v1/ui/setup/connect/{job_id}/answer`、`/v1/ui/setup/connect/{job_id}/cancel`、`/v1/ui/setup/adopt`、`/v1/ui/setup/enable-write`；`ConnectBody`、`AnswerBody`（為新路由重新建立成 `{result}`）、`NameBody`、`EnableWriteBody`；`_Connect`、`_question`（已搬移）、`_connect_failure`、`_remote_view`；`need_path`、`swap_config`、`remote_view`、`check_new_name`、`start_connect`、`connect_job`、`connects` 字典；`SIGNIN_TIMEOUT_S`、`ANSWER_TIMEOUT_S`、`CREATE_TIMEOUT_S`、`NEW_REMOTE_NAME`、`ADOPT_NAME`、`MAX_ANSWER`（搬到 `connections.py`）、`CONNECT_TYPES`；`ConfigError` 處理器；`create_ui_app(config_path=…)` 與 `NO_CONFIG_PATH`（`run_ui` 仍把 `config_path` 交給 `Service`）。

#### G4.6 其他模組

- `config.py`：刪除 `ConfigError`、`PROVIDER_EXTRA_ARGS`、`_write_lock`、`_HEADER`、`_NAME_LINE`、`_toml_str`、`_toml_list`、`_read_text`、`_commit`、`add_remote_block`、`set_remote_write`。保留 PR #5 的 `write_private`（fsync，可透過符號連結寫入）、`provider_of`/`RCLONE_PROVIDERS`（匯入）、`write_new_config`、`ensure_config`（`cmd_init` 與 `cmd_ui` 使用它們）。
- `rclone.py`：刪除 `RcloneCancelled`、`RcloneTimeout`、`_REMOTE_NAME`、`MIN_RCLONE`、`TEST_TIMEOUT_S`（搬到 `connections.py`）、`AUTH_URL`、`_scrub`、`version_info`、`list_remotes`、`_check_name`、`config_create`、`config_continue`、`config_delete`、`test_remote`、`_config_call`、`_config_result`。保留 `classify_failure` 及其樣式。
- `service.py`：刪除 `SETUP_CACHE_S`、`_setup_cache`、`_setup_lock`、`forget_rclone`、`rclone_facts`、`setup_state`、`reload_config` 的 `config` 參數，以及 `sync` 與 `rebuild_identity` 中的 `identity_built_at` meta 寫入。`setup_status` 新增 `needed` 與 `manual_remotes`（G4.5）。
- `cli.py`：`cmd_ui` 在建立設定時印出一行（`Created <path> … connect your clouds on the page that opens.`）；合併帶來的重複訊息移除。`drivemerge connect iclouddrive --apple-id ID` 用 `getpass` 詢問密碼，並透過 `answer_flow` 在終端機詢問每個問題（`input`，若 `password` 則用 `getpass`）。`drivemerge connect --paste-token PROVIDER` 從 stdin 讀取權杖。`drivemerge import SOURCE [--name N]` 包裝 `import_remote`。全部放在 `_connect_gate` 之後。

### G5. 前端合約

- **刪除** `web/setup.js`；`shell.js` 中 `RAIL`、`VIEW_ICON`、`ROUTE`、`shortcutOf` 與 `Alt+0` 的 `setup` 項目（`#/setup` 雜湊會被替換成 `#/clouds`）；`setup` 圖示；`api.setupState`、`api.connect`、`api.answer`、`api.cancelConnect`、`api.adopt`、`api.testRemote`、`api.enableWrite`；`app.js` 中第二段轉址區塊與 `ctx.reloadConfig`（`refreshSetup` 本來就會重新取得 `/v1/ui/config`）；沒有被搬移的表單用到的 `.su-*` 樣式；`util.wordParams` 中的 `connect.field.*` 特例（Apple ID 欄位改由 `conn.field.*` 說明）。
- **轉址**（`app.js`）：沒有路由的頁面，在 `GET /v1/setup` 回答 `needed: true` 時前往 `#/clouds`；沒有回應、或回答中沒有 `needed` 的伺服器，則不改變任何東西。
- 把 `QuestionForm` 與 `RcloneText` 從 `setup.js` **搬到**新的 `web/question.js`，行為不變（獨占範例用單選群組、選擇或輸入、送出請求前會自行清空的密碼輸入、以兩個按鈕呈現的布林值、`retry` 提示、放在標示為 rclone 原文的方框裡的 rclone 說明），並保留它們的 `data-testid`（`connect-question`、`connect-q-name`、`connect-q-options`、`connect-q-option-<i>`、`connect-q-yes`、`connect-q-no`、`connect-q-select`、`connect-q-input`、`connect-q-continue`、`connect-q-retry`、`connect-q-help`）。
- **`ConnectDialog`**（`onboarding.js`）新增階段 `question`：finish、start、import 或 answer 回應 202 時顯示 `QuestionForm`；送出時呼叫 `api.connectAnswer(flowId, value)`。此階段的取消會呼叫 `connectCancel`。頁面只會開啟符合 `^http://127\.0\.0\.1:53682/auth\?` 的 `auth_url`。
- **iCloud 方塊與對話框**（`provider-iclouddrive`、`connect-iclouddrive`）：Apple ID（`connect-apple-id`）、密碼（`connect-apple-password`，送出請求時清空）、2FA 與程序清單的說明，然後進入問題階段。
- 種類 `authorize` 的**「在另一台電腦上登入」**展開區（`conn-paste-toggle`）：來自 `providers[].authorize[purpose]`、並填入用戶端 ID 的指令（`conn-paste-command`，附複製按鈕）、一個文字區（`conn-paste-token`）與 `conn-paste-submit` → `api.connectPaste` → 像瀏覽器登入之後一樣 finish。權杖在請求之前就離開元件狀態，絕不會進入網址、儲存空間或提示訊息。
- **匯入**區段：當 `GET /v1/ui/connect/importable` 列出遠端時，顯示在方塊下方：`import-list`，每個遠端一列（`import-<name>`，按鈕 `import-add-<name>`，不支援時停用並顯示 `conn.import.unsupported`，已匯入時隱藏），以及 `import-encrypted` 提示。
- **卡片**：`manual_remotes` 也有卡片（`conn-card-<name>`、`data-source="config"`）：狀態、來自測試的容量、上次同步，只有測試與同步，以及提示 `conn.manual`。`scoped: false` 的連線顯示 `conn.unscoped`；它們的「開放變更」改為詢問確認（`conn.allowSameTitle`/`conn.allowSameText`），而不是登入。測試失敗且 `code: AUTH_EXPIRED` 時，卡片進入 `relogin` 狀態。
- **唯讀備援**（`config.approvals_enabled === false`）：載入時不執行任何測試，每個連接／匯入／開放變更／中斷連線／同步按鈕都停用，並由面板（`clouds-readonly`）顯示要在終端機執行的指令（`drivemerge connect drive|onedrive|dropbox|iclouddrive`、`drivemerge scan --all`、`drivemerge identity rebuild`），附複製按鈕；之後重新載入頁面就會看到新的連線（G4.5 的 mtime 重新載入）。
- **總覽**的空白狀態：`ov-setup-link` 變成 `ov-clouds-link` → `#/clouds`。

### G6. i18n

- **刪除**每一個 `setup.*` 與 `nav.setup*` 鍵、`ov.setupLink`、下面沒有列出的 `connect.*` 鍵，以及 `err.REMOTE_EXISTS`、`err.REMOTE_NOT_FOUND`、`err.CONFIG_LAYOUT`、`err.RCLONE_MISSING`、`err.NOT_WAITING`（下面會用新文字重新加入）、`err.NO_CONFIG_PATH`、`err.SIGNIN_TIMEOUT`、`err.CANCELLED`。`err.BAD_NAME` 回到 main 的雲端名稱規則（小寫英文字母、數字與 `-`，以字母開頭，最多 31 個字元）。
- **重新命名** PR #5 的問題表單鍵 `connect.q.*` → `conn.q.*`（文字不變）。
- 兩種語言都要**新增**：`conn.q.*`（來自 PR #5）、`conn.field.apple_id`、`conn.field.password`、`conn.tile.iclouddrive`、`conn.icloud.intro`、`conn.icloud.appleId`、`conn.icloud.password`、`conn.icloud.twoFactor`、`conn.icloud.processList`、`conn.paste.toggle`、`conn.paste.text`、`conn.paste.command`、`conn.paste.token`、`conn.paste.submit`、`conn.paste.scope`、`conn.import.heading`、`conn.import.text`、`conn.import.add`、`conn.import.unsupported`、`conn.import.encrypted`、`conn.import.done`、`conn.manual`、`conn.unscoped`、`conn.allowSameTitle`、`conn.allowSameText`、`conn.ro.title`、`conn.ro.text`、`conn.ro.connect`、`conn.ro.scan`、`ov.cloudsLink`，以及 `err.MISSING_FIELD {field}`、`err.NOT_WAITING`、`err.TOKEN_UNREADABLE`、`err.CONFIG_WRITE`、`err.IMPORT_UNSUPPORTED {type}`、`err.IMPORT_ENCRYPTED`、`err.AUTH_EXPIRED`（「…請中斷連線後重新連接」）、`err.NETWORK`、`err.RCLONE_ERROR`。

### G7. 測試

每一項保留下來的功能都維持測試覆蓋；只有已刪除機制的測試會被移除。

**刪除**（功能已不存在，或由 main 的模型取代，且 main 已涵蓋該功能）：
- `tests/test_setup_api.py` 整個檔案，在把下列清單中的測試移植之後。不移植的有：setup_state 的形狀、rclone 清單快取與 `reload_config(config)`（已不存在）；`NO_CONFIG_PATH`；不合法／合法的名稱與名稱衝突（main 的 `test_default_names_count_up_and_explicit_names_are_checked`、`test_name_clash_with_a_remote_of_the_main_config`）；沒有 rclone 的連接、一次只能一個、Dropbox 端對端、Drive 的權限範圍與密鑰、沒有用戶端 ID 的 Drive（main 要求提供）、等待瀏覽器時取消、登入逾時、被拒絕的存取（main 的 `test_busy_slow_cancel_and_failure`、`test_flow_times_out`、`test_connect_dropbox_end_to_end`、`test_drive_needs_client_and_uses_the_read_scope`、`test_tokens_never_leave_and_files_are_private`）；內嵌表格的版面；Dropbox 的 enable-write 重用 `read` 與 Drive 的 enable-write（main 的第二次登入，`test_enable_writes_and_disconnect`）；enable-write 的拒絕與 `-rw` 長度檢查；完整的首次執行（main 的 `test_ui_onboarding_e2e.py`）。
- `tests/fake_rclone.py`，在它的 iCloud 2FA、多個硬碟與回答回音狀態都進入 `tests/fakes/rclone` 之後。
- `tests/test_config.py`：每一個 `add_remote_block`/`set_remote_write` 測試（附加、服務預設值、引號、空檔案、重複、內嵌陣列、不是 TOML、缺少檔案、設定／插入／移除 write、其他區塊、未知遠端、內嵌表格、拒絕更動更多內容、CRLF、重新解析失敗）。
- `tests/test_rclone.py`：`version_info`、`list_remotes`、`config_create`/`config_continue` 的 argv、名稱／類型旗標的拒絕、`config_delete` 的 argv、`test_remote` 回傳錯誤、所有針對 PR #5 假檔的串流／取消／逾時測試、`test_listing_the_fake_remote_reports_what_rclone_reports`。
- `tests/test_ui_frontend.py`：PR #5 的 Setup 測試與 `MockApi` 的 Setup 路由（Setup 領頭的導覽列、四個步驟、Setup 掃描進度與失敗、Setup 截圖、連接被拒絕的原因（main 已涵蓋））。
- `tests/test_i18n_contract.py`：`test_the_setup_job_failures_send_the_name_so_the_page_can_say_which_cloud`。

**移植**（相同的斷言，新的位置）：
- 到 `tests/test_connections.py`（假的 `tests/fakes/rclone`）：有多個硬碟的 OneDrive 會詢問要用哪一個並使用回答，只有一個硬碟則不詢問；回答驗證（長度、`NOT_WAITING`）；iCloud 要先給 Apple ID 與密碼（`MISSING_FIELD`），然後是 2FA 問題，錯誤的驗證碼會帶著 `retry` 重新詢問，且絕不回音；權杖貼上（區塊與單純 JSON、不引用輸入的 `TOKEN_UNREADABLE`、Drive 的 `NEEDS_CLIENT`）絕不把權杖顯示回來；問題等待時取消會刪除遠端；取消是冪等的；時間內沒有回答就逾時並清理；rclone 在 create 與 continue 失敗時移除它寫入的東西；會回音回答的 rclone 無法洩漏它（訊息、流程檢視、日誌、`capsys`、`caplog`）；`connections.toml` 寫入錯誤會刪除新遠端（`CONFIG_WRITE`）；中途被占用的名稱在最後被拒絕，而被等待中的問題保留的名稱會擋住第二個流程；匯入加入唯讀的 `scoped = false` 項目，並讓使用者的設定逐位元組不變；匯入的拒絕（已加密、未知、不支援的類型、已匯入）；未知類型成為服務 `other`；測試的分類（`AUTH_EXPIRED`、`NETWORK`、`NOT_FOUND`、帶 `detail` 的 `RCLONE_ERROR`）、手動遠端的測試、30 秒的測試逾時；對 iCloud 與匯入的遠端開放變更會重用 `read`；被取消的寫入流程不改變任何東西；`_URL_RE` 只接受輔助程式的網址（PR #5 的 `test_auth_url_pattern_*` 參數化）；`classify_failure` 的參數化留在 `test_rclone.py`；PR #5 的 `test_real_rclone_setup_calls` 變成對 `local` 遠端執行 `importable`/`import_remote`、並對不存在的遠端執行 `classify_failure` 的真 rclone 測試（沒有 rclone 時略過）。
- 到 `tests/test_onboarding_api.py`：新路由（`answer`、`paste`、`import`）加入人工通道、CSRF／來源與核准關閉的參數化測試，`importable` 可由 bearer 與工作階段讀取但匿名不行；`GET /v1/setup` 的 `needed` 在沒有設定任何東西時與有從未掃描的雲端時為 true，所有雲端都掃描過後為 false（PR #5 的 `test_setup_route_adds_needed`、`test_needed_is_false_once_every_cloud_is_scanned`）；對從未掃描的連線進行同步（PR #5 的重新掃描測試）；伺服器執行期間用 `drivemerge connect` 建立的連線，在下一次 `GET /v1/setup` 就看得到；iCloud 開始 → 202 問題 → 回答 → 200 連線；有多個硬碟的 OneDrive 的 finish → 202；本文驗證（非 iCloud 服務的 `apple_id`、iCloud 的用戶端欄位 → `BAD_REQUEST`）。
- 到 `tests/test_cli.py`：`drivemerge ui` 會建立缺少的設定並把它的路徑交給伺服器；其他指令在設定缺少時仍然提示 `init`；`run_ui` 把設定路徑傳給服務；透過假檔執行 `drivemerge connect iclouddrive` 與 `drivemerge import`。
- 到 `tests/test_config.py`：`write_private` 的模式 600、透過符號連結、原子性取代、暫存檔位於同一個目錄並已 fsync（由區塊測試改寫）；`ensure_config` 與 `write_new_config`；`provider_of` 的參數化。
- 到 `tests/test_ui_onboarding.py`（模擬的 API）：沒有路由時只在 `needed` 才轉址到 `#/clouds`，有路由時絕不轉址，沒有 `needed` 就不改變任何東西；`#/setup` 會落在雲端檢視；導覽列以 `rail-clouds` 開頭且沒有 `rail-setup`（還原合併時的修改）；`Alt+0` 什麼都不做；每一種輸入類型的問題表單；不是 rclone 輔助程式的登入連結絕不開啟也不顯示；問題進行中取消；依新代碼說明的連接失敗；iCloud 欄位，且密碼與 2FA 驗證碼不會進入網址、儲存空間或提示訊息；貼上展開區（含權限範圍旗標的指令、權杖只在送出時傳送且絕不儲存）；匯入的列、不支援與已加密；依代碼說明的測試結果，且 `AUTH_EXPIRED` → relogin；對 `scoped: false` 以確認方式開放變更；手動遠端的卡片；有指令且不自動測試的唯讀面板；總覽的空白狀態連到雲端；以繁體中文顯示的問題表單與 iCloud 對話框；`FLOW_BUSY` 與 `MISSING_FIELD` 的說明；新元素用到的每個鍵在兩種語言中都存在。
- 到 `tests/test_i18n_contract.py`：PR #5 的 `test_every_setup_code_has_a_sentence…` 變成 `test_every_connect_code_has_a_sentence_in_both_languages_that_uses_only_what_every_site_sends`，收集 `connections.py`、`ui_api.py` 與 `cli.py` 中的 `ConnectError(code, …)` 位置，加上 `classify_failure` 的代碼。

**新增**：§F 的 Playwright 端對端執行也會連接一個假的 iCloud（Apple ID、密碼，2FA 在問題表單中），並從假的個人 rclone 設定匯入一個遠端，然後同步全部三個雲端。`tests/fakes/rclone` 新增 `config create … iclouddrive`（不連任何外部；詢問 `config_2fa`，接受 `123456`，否則以 `Error` 重新詢問）、有多個硬碟的 OneDrive 帳號（以環境變數切換）、`--no-obscure` 與 `config file`。

### G8. 文件

- 刪除 `docs/SETUP.md` 與 `docs/SETUP.zh-TW.md`。
- `README.md` 與 `README.zh-TW.md`：移除設計文件那句話中的 SETUP 連結，並把 Setup 段落改寫成雲端段落（iCloud、在另一台電腦上登入、從你的 rclone 設定匯入、唯讀備援），連到 `docs/ONBOARDING.md` / `docs/ONBOARDING.zh-TW.md`。
- `docs/ONBOARDING.zh-TW.md` 以相同的標題、表格、程式碼區塊與代碼，對應本節與 §8 的修改（`tests/test_docs_i18n.py`）。

### G9. 風險

- **匯入的權杖是副本。** drive-merge 的副本與使用者自己的遠端各自獨立更新。Google 與 Dropbox 的更新權杖不會輪替；Microsoft 會發出新的更新權杖但舊的仍然有效，所以兩份副本都能繼續使用；使用一次性更新權杖的服務（Box）會被拒絕。中斷連線只刪除 drive-merge 的副本。
- **貼上的權杖無法檢查權限範圍。** 沒有帶上所示 `--drive-scope=drive.readonly` 製作的權杖可能可以寫入；drive-merge 在沒有 `write` 遠端時仍然絕不寫入，但「唯讀」標示會比權杖實際的權限寬鬆。對話框會在指令旁邊說明這一點。
- **rclone 重寫 `rclone.conf`。** 同步在建立連線的同時更新權杖，會從另一個程序寫入同一個檔案。請在固定的 rclone 版本上驗證，它的設定儲存在存檔前會重新載入磁碟上已改變的檔案；否則連接／匯入只能在沒有同步工作時進行（`FLOW_BUSY` 式的拒絕）。
- **iCloud 的信任會過期**（約 30 天，Apple 的規則）：卡片接著透過 `AUTH_EXPIRED` 顯示 `relogin`；重新連接的方法是先中斷連線再連接。
- **既有的 PR #5 使用者**可能在 `config.toml` 裡已有 Setup 寫入的 `[[remotes]]` 區塊；它們會繼續作為手動遠端運作（有測試與同步的卡片），不會被遷移。
