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
- iCloud Drive 不在歡迎畫面上：它的登入需要 Apple ID 密碼與終端機中的兩步驟驗證碼（`rclone config`），之後可以依名稱以連線的方式加入。
- Google Drive 需要你自己的 OAuth 用戶端（§2）。
- 登入需要瀏覽器與 drive-merge 位於同一台機器（本機回呼）。

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
