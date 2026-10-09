# Claude Discord Bridge

[![CI](https://github.com/icec8c8/claude-discord-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/icec8c8/claude-discord-bridge/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/icec8c8/claude-discord-bridge)](https://github.com/icec8c8/claude-discord-bridge/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

**[English](README.md) · [繁體中文](README.zh-TW.md) · [日本語](README.ja.md)**

一個小小的常駐程式，讓你透過一個私人的 Discord 頻道與 [Claude Code](https://docs.claude.com/en/docs/claude-code) CLI 對話。這樣不論你人在哪裡，只要手機上有 Discord，就能驅動你現有的 Claude 訂閱。**不需要額外的 API key，也不會產生額外費用**。

在你的 Discord 頻道打這樣的訊息：

```
claude> 幫我讀一下專案 README，告訴我接下來該做什麼
```

幾秒後，Claude 的回覆就會以引用訊息的方式出現在你的頻道。支援 Windows（Scheduled Task）與 macOS（LaunchAgent）。

## 目錄

1. [架構](#架構)
2. [功能特色](#功能特色)
3. [系統需求](#系統需求)
4. [安裝步驟](#安裝步驟) — Discord bot · 安裝 · Windows 開機自啟 · macOS 開機自啟
5. [Remote Control](#remote-control) — 用 Claude App 操作這台電腦
6. [設定](#設定) — 完整環境變數列表
7. [安全模型](#安全模型) — **在放寬工具白名單之前請務必讀完**
8. [驗證清單](#驗證清單) — 使用前如何驗證安全性
9. [MCP server](#mcp-server)
10. [疑難排解](#疑難排解) — 開發時踩過的 5 個坑
11. [授權條款](#授權條款)

## 架構

```
   Discord 頻道
        │
        ▼
  bridge.py (discord.py Gateway 監聽器; pythonw.exe / launchd)
        │  過濾 user_id + channel_id  +  前綴 gate "claude>"
        ▼
  inbox.jsonl  +  📥 reaction
        │
        ▼
  asyncio.Queue  ──►  worker  ──►  claude -p --resume <uuid>
                                      │    --output-format json
                                      │    --allowedTools <唯讀白名單>
                                      │    --model sonnet
                                      ▼
                                  result + usage + cost
                                      │
                                      ▼
                              Discord 回覆  +  ✅/❌ reaction
                              outbox.jsonl (每次呼叫統計)
```

headless `claude -p` 子行程是**跟你正在開啟的任何 Claude Code TUI session 完全分開的對話**。使用同一個訂閱，但是兩個平行宇宙 — 所以觸發 bridge 不會打擾到你正在互動的 session。

## 功能特色

- **自動回覆** — 每一條通過過濾的 Discord 訊息都會觸發一次 headless `claude -p`，回應以引用訊息的方式送回 Discord。
- **session 延續** — 透過固定的 session UUID（存在 `bridge_session_id.txt`），headless Claude 會記得先前的 Discord 對話（靠 `--resume`）。
- **前綴 gate** — 訊息必須以 `claude>` 開頭（可設定）。沒帶前綴的訊息會被標上 👀 reaction 然後丟棄。
- **預設唯讀工具白名單** — `--allowedTools` 預設為 `Read Grep Glob WebFetch WebSearch Task TodoWrite`。**沒有** `Bash`、**沒有** `Edit`、**沒有** `Write`。要放寬請先讀完「安全模型」那節。
- **預設使用 Sonnet** — `--model sonnet`，成本約為 Opus 的 1/3，對 Max 訂閱的 rate limit 壓力小很多。透過 `CLAUDE_MODEL` 可以覆蓋。
- **Token 用量追蹤** — 每次呼叫都用 `--output-format json` 取回 `usage` 和 `total_cost_usd`，寫進 `outbox.jsonl`。每日總計可以透過內建 MCP tool 查詢。
- **MCP server** — 附帶的 `bridge_mcp.py` 暴露 6 個 tools（`inbox_list`、`outbox_list`、`bridge_status`、`send_message`、`tail_log`、`usage_today`），讓同一台機器上的另一個 Claude Code session 能夠檢查與控制 bridge。
- **開機自啟** — Windows 走 Scheduled Task；macOS 走 LaunchAgent 綁 Aqua session。
- **Remote Control 常駐** — 另附安裝腳本，登入時就讓 `claude remote-control` 常駐，手機 Claude App 隨時能在這台電腦開完整的 Claude Code session。見 [Remote Control](#remote-control)。
- **設計上就是單使用者** — 硬綁定一個 Discord user ID 和一個 channel ID，其餘一律 drop。

## 系統需求

| | |
|---|---|
| 作業系統 | Windows 10/11、macOS 13+（Linux 應該可行，只是沒 LaunchAgent 那條） |
| Python | 3.11+（實測過 Windows 3.11 + macOS Homebrew 3.13） |
| Claude Code CLI | `npm install -g @anthropic-ai/claude-code`，至少成功登入過一次 |
| Node.js | 只有透過 npm 裝 Claude Code 時才需要 |
| Discord 開發者帳號 | 用來開自己的 bot |
| 一個私人 Discord 頻道 | bot 要能讀訊息、能發訊息的地方 |

你也需要一個有效的 Claude 訂閱（Max、Team、Console/API 都可以）。bridge 直接用你本機 `claude` CLI 既有的認證，**不會要求你提供 Anthropic API key**。

## 安裝步驟

### 步驟 1：建立一個 Discord bot

1. 到 <https://discord.com/developers/applications> 點 **New Application**，隨便取個名字。
2. 打開 **Bot** 分頁，點 **Reset Token** 複製 token。這是**唯一一次**你能看到這個 token 的機會，立刻存進密碼管理器。
3. 同一個 **Bot** 頁面往下滾到 **Privileged Gateway Intents**，把 **MESSAGE CONTENT INTENT** 打開，Save Changes。沒開這個 bot 讀不到訊息內容，bridge 啟動時會立刻炸 `PrivilegedIntentsRequired`。
4. 到 **OAuth2 → URL Generator**，scope 勾 `bot`，bot permissions 勾 `View Channels`、`Send Messages`、`Read Message History`。**不要**勾 `Administrator` — bridge 不需要。複製產生的 URL，貼進瀏覽器，把 bot 授權進你的 guild。

### 步驟 2：找出需要的 ID

1. Discord 客戶端 → **使用者設定 → 進階 → 開發者模式** 打開。
2. 右鍵你自己的名字（任何地方都可以）→ **複製使用者 ID**。這是 `ALLOWED_USER_ID`。
3. 右鍵你要當 bridge 頻道的那個頻道 → **複製頻道 ID**。這是 `ALLOWED_CHANNEL_ID`。

### 步驟 3：安裝 bridge

```bash
# Clone 到家目錄底下一個持久位置
git clone https://github.com/<you>/claude-discord-bridge.git ~/.claude-bridge
cd ~/.claude-bridge

# 建立 Python 虛擬環境
python -m venv venv

# 裝相依套件
# Windows:
venv\Scripts\pip install -r requirements.txt
# macOS/Linux:
./venv/bin/pip install -r requirements.txt

# 複製環境變數範例檔並填入你的 secrets
cp .env.example .env
# 編輯 .env，貼上 DISCORD_BOT_TOKEN、ALLOWED_USER_ID、ALLOWED_CHANNEL_ID
```

macOS/Linux 裝完後執行 `chmod 600 .env`，讓 token 檔只有擁有者能讀。

Windows 上 NTFS 繼承通常已經把你的家目錄設為私有；不放心的話，執行 `icacls .env /inheritance:r /grant:r "%USERNAME%:F"` 把這個檔案設為只有你能讀。

### 步驟 4：手動測試 bridge

```bash
# Windows
venv\Scripts\python.exe bridge.py

# macOS / Linux
./venv/bin/python bridge.py
```

你應該在約 2 秒內看到 `bridge.log` 出現 `connected as <botname>`。接著到 Discord 的設定頻道發：

```
claude> 只回 OK 兩個字，其他什麼都不要說
```

Bot 應該會依序加 📥 → 🤖 → ✅ reaction，然後引用你的訊息回 `OK`。若這個能跑，按 `Ctrl+C` 停掉進程，進入下一步「開機自啟」。

### 步驟 5a：Windows 開機自啟

```powershell
powershell -ExecutionPolicy Bypass -File install_autostart.ps1
```

這會建立一個名為 `ClaudeDiscordBridge` 的 Scheduled Task，在使用者登入時以 `pythonw.exe` 執行 `bridge.py`（無視窗），失敗時最多重啟 999 次、每次間隔 1 分鐘。**不需要系統管理員權限** — 它只對你這個使用者有效。

控制指令：

```powershell
Start-ScheduledTask      -TaskName ClaudeDiscordBridge
Stop-ScheduledTask       -TaskName ClaudeDiscordBridge
Disable-ScheduledTask    -TaskName ClaudeDiscordBridge
Get-ScheduledTask        -TaskName ClaudeDiscordBridge
Get-ScheduledTaskInfo    -TaskName ClaudeDiscordBridge
Unregister-ScheduledTask -TaskName ClaudeDiscordBridge -Confirm:$false
```

### 步驟 5b：macOS 開機自啟（Aqua session）

**這一步必須從 GUI Terminal.app 視窗執行，不能從 SSH shell 執行。** macOS login keychain 在 SSH context 是鎖住的，而 Claude Code 把憑證存在那裡 — 在非 Aqua session 裡跑 `claude -p` 會直接得到 `Not logged in` 錯誤。請透過 Screen Sharing / VNC / 實體鍵鼠登入桌面，在那邊開 Terminal.app，然後：

```bash
bash install_aqua_launchagent.sh
```

腳本會讀取你的 `$HOME`、用 `which claude` 找到 claude 可執行檔、把 `claude-discord-bridge.plist.template` 裡的 placeholder 做字串替換、輸出到 `~/Library/LaunchAgents/dev.local.claude-discord-bridge.plist`、用 `plutil -lint` 驗證、再透過 `launchctl bootstrap gui/$(id -u)` 載入。

執行 macOS 這邊的 installer **之前**，若你同時也跑 Windows 版，**請先 Stop + Disable Windows 那邊的 Scheduled Task**。一個 Discord bot token 同一時間只能有一個 Gateway 連線，兩邊同時跑會互踢。

## Remote Control

Discord bridge 適合「傳一句話、拿一個答案」。如果想用手機上的 **Claude App** 在這台電腦開一個完整的 Claude Code session（改檔、跑指令，每一步都能在手機上核准），請用 Claude Code 內建的 [Remote Control](https://code.claude.com/docs/en/remote-control)。這個 repo 附了讓它登入後自動常駐的安裝腳本。

| | Discord bridge | Remote Control |
|---|---|---|
| 從哪裡操作 | Discord | Claude App 的 **Code** 分頁，或 claude.ai/code |
| 工具 | 預設唯讀白名單 | 完整的 Claude Code，需要權限的動作會在手機上問你 |
| 對話 | 單一固定 session | 想開幾個 session 都可以 |

兩者可以同時跑，共用同一個訂閱的用量額度。

**需求：** Pro、Max、Team 或 Enterprise 方案，並用 claude.ai 帳號登入（`claude auth login`）。不支援 API key。Team / Enterprise 要先請 Owner 在 claude.ai 管理設定裡開啟 Remote Control。

### 步驟 1：一次性首次設定（在一般終端機手動做）

第一次啟動會問兩個問題，背景服務沒辦法代答，所以要先手動跑一次：

```bash
# Windows (PowerShell)
mkdir $HOME\claude-remote; cd $HOME\claude-remote
claude remote-control

# macOS
mkdir -p ~/claude-remote && cd ~/claude-remote
claude remote-control
```

`Enable Remote Control? (y/n)` 回 `y`，`Trust <資料夾>?` 也回 `y`。看到 session 網址後，可以先打開手機 Claude App 的 **Code** 分頁，確認看得到這台電腦，然後按 `Ctrl+C` 結束。

`claude-remote` 是 session 的起始資料夾，換成任何專案資料夾都可以，但**不能是家目錄本身**：Claude Code 不會記住家目錄的信任設定，背景服務在那裡會啟動失敗。

### 步驟 2a：Windows 開機自啟

```powershell
powershell -ExecutionPolicy Bypass -File install_remote_control.ps1
# 自訂資料夾 / 名稱 / 權限模式：
powershell -ExecutionPolicy Bypass -File install_remote_control.ps1 -WorkDir D:\projects -Name "Home PC" -ExtraArgs "--permission-mode acceptEdits"
```

這會建立名為 `ClaudeRemoteControl` 的 Scheduled Task。登入時以隱藏視窗執行 `remote_control_loop.ps1`，由它執行 `claude remote-control`，程式結束時（例如斷網約 10 分鐘後）會自動重啟。輸出寫在 `remote-control.log`。安裝完會等 20 秒，並印出 log 的最後幾行。

移除（同時停止伺服器）：

```powershell
powershell -ExecutionPolicy Bypass -File install_remote_control.ps1 -Uninstall
```

### 步驟 2b：macOS 開機自啟（Aqua session）

跟 bridge 一樣，**要在 GUI Terminal.app 視窗執行，不能用 SSH**：

```bash
bash install_remote_control_launchagent.sh
# 或：bash install_remote_control_launchagent.sh --workdir ~/projects --name "Mac mini"
```

這會安裝 `~/Library/LaunchAgents/dev.local.claude-remote-control.plist`，由 `remote_control_loop.sh` 負責在需要時重啟。session 會沿用你執行安裝時那個終端機的 `$PATH`。移除：`bash install_remote_control_launchagent.sh --uninstall`。

### 使用方式

打開 Claude App，點 **Code** 分頁，有電腦圖示加綠點的就是這台電腦。點進現有的 session 或開新的 session，直接打你要做的事。

### 注意事項

- 電腦要保持開機、不能睡眠。筆電睡眠時會離線，醒來後會自動恢復。
- App 裡不能選 bypass permissions。需要權限的動作在手機上核准；想自動接受改檔，可以用 `-ExtraArgs` / `--extra-args` 帶 `--permission-mode acceptEdits`。
- 對話紀錄會經過 Anthropic 的伺服器，實際執行都在你的電腦上。
- 服務在跑的時候，不要在同一個資料夾另外手動跑 `claude remote-control`。
- 手機上一直看不到這台電腦時，看 `remote-control.log`：出現 `Workspace not trusted` 或卡在 `Enable Remote Control? (y/n)`，代表那個資料夾還沒做步驟 1；出現登入錯誤就執行 `claude auth login`，macOS 還要確認是在 Aqua session 安裝的。

## 設定

所有旋鈕都在 `.env`。`.env.example` 有完整的註解。重點變數：

| 變數名 | 預設值 | 必填 | 用途 |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | — | 是 | 從 Developer Portal 拿到的 bot token |
| `ALLOWED_USER_ID` | — | 是 | 只處理這個 Discord user ID 的訊息 |
| `ALLOWED_CHANNEL_ID` | — | 是 | 只處理這個頻道的訊息 |
| `BRIDGE_PREFIX` | `claude>` | 否 | 每條訊息必須以此開頭；設成 `""` 關掉 gate（不建議） |
| `CLAUDE_MODEL` | `sonnet` | 否 | 別名或完整 model 名 |
| `CLAUDE_ALLOWED_TOOLS` | `Read Grep Glob WebFetch WebSearch Task TodoWrite` | 否 | 空白分隔的 tool 白名單，傳給 `--allowedTools` |
| `CLAUDE_EXE` | `claude` | 否 | claude 可執行檔路徑；預設走 `$PATH` |
| `CLAUDE_TIMEOUT` | `300` | 否 | 每次呼叫的 timeout（秒） |
| `CLAUDE_CWD` | bridge 目錄 | 否 | headless claude 子行程的工作目錄 |

## 安全模型

Bridge 有 5 層防線。**在放寬任何一層之前，請完整讀完這一節。**

### 分層防禦

1. **User ID 過濾**：只有 `ALLOWED_USER_ID` 送的訊息會被處理。`on_message()` 最上面就 drop 其他所有人。
2. **Channel ID 過濾**：只有 `ALLOWED_CHANNEL_ID` 的訊息會被處理。一個 bot 加進 server 後看得到很多頻道，這層把 bridge 綁死在單一專用頻道。
3. **前綴 gate**：訊息必須以 `BRIDGE_PREFIX`（預設 `claude>`）開頭。其他的加 👀 reaction 然後 drop。這層防兩件事：不小心打字變成真的 Claude turn；以及 Discord 帳號被盜後，攻擊者打的「看起來像聊天」的訊息本來會被當成指令執行。
4. **工具白名單**：headless claude 用 `--allowedTools` 限制只能用讀取/搜尋類工具。**沒有** `Bash`、`Edit`、`Write`、`NotebookEdit`。bridge **不能**改檔、**不能**跑 shell 指令，除非你在 `.env` 裡明確放寬白名單。
5. **Model 上限**：`--model sonnet` 預設限制每次呼叫的成本與能力（相對 Opus）。

### 預設白名單之下，bridge **仍然能做**的事

- **讀取執行使用者能讀的任何檔案**。`Read` 在白名單裡。若攻擊者控制了你的 Discord 帳號，打 `claude> 讀 ~/.ssh/id_rsa`，內容會被回到 Discord。若這很重要，自訂 `CLAUDE_ALLOWED_TOOLS` 拿掉 `Read`，或在專案 Claude Code settings 用路徑白名單限制 `Read`。
- **抓任意 URL**。`WebFetch` 在白名單裡。攻擊者能叫 bridge 先讀本地檔案，再透過 `WebFetch` POST 到他們的 URL（資料外洩）。若這很重要，把 `WebFetch` 從 `CLAUDE_ALLOWED_TOOLS` 拿掉。
- **跑 web search**。`WebSearch` 在白名單裡。基本無害但會燒 rate limit。
- **花掉你的 Claude 訂閱額度**。任何一條 Discord 訊息都會觸發真實的 Claude 呼叫，消耗你 Max/Team/Console 的額度。請用 `usage_today` MCP tool 設置每日預算，或在 `on_message` 加速率限制。

### 預設白名單之下，bridge **不能做**的事

- 跑 shell 指令（`Bash` 被排除）。
- 改或建立本地檔案（`Edit`、`Write`、`NotebookEdit` 都被排除）。
- 透過 `discord.py` 送訊息到 `ALLOWED_CHANNEL_ID` 以外的頻道。
- 存取其他使用者的檔案（它以你的身分執行，受 OS 權限約束）。

### 安全地放寬白名單

要 bridge 做更多事，一次只加一個工具，並且先在測試頻道驗證。例如 — 只讓 Bash 跑 `git status` / `git log`：

```
CLAUDE_ALLOWED_TOOLS=Read Grep Glob WebFetch WebSearch Task TodoWrite "Bash(git status:*)" "Bash(git log:*)"
```

`ToolName(pattern)` 這種 per-tool glob 語法是 Claude Code 標準的細粒度權限格式。優先用窄 glob，不要用 `Bash(*:*)`。

### `git push` 之前 — repository 安全

這個 repo 設計為可以公開，但**是否真的沒洩漏 secret，責任在你身上**，push 到任何地方之前必須自己驗。Checklist：

```bash
# 1. 確認 .env 被 git-ignore 且沒被 tracked
git check-ignore -v .env
git ls-files | grep -i '\.env$'   # 應該只看到 .env.example

# 2. Grep 所有 tracked 檔案裡是否有看起來像 token 的東西
git ls-files | xargs grep -nE 'MT[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|(token|secret|key|password)[[:space:]]*=' 2>/dev/null

# 3. Grep 所有 tracked 檔案裡是否有你的個人 ID
git ls-files | xargs grep -nE '<your-discord-user-id>|<your-channel-id>|<your-username>' 2>/dev/null

# 4. 檢查 git 歷史，以防之前 commit 過 secret 又被刪掉
git log --all --full-history -p -- .env 2>/dev/null
git log --all -p | grep -nE 'MT[A-Za-z0-9]{20,}|sk-' 2>/dev/null
```

四項都要是空結果。若 `git log` 顯示之前 commit 過 secret，**不要只是刪掉再 commit 一次** — secret 還在歷史裡。用 `git filter-repo` 或 BFG 改寫歷史，並且**立刻 rotate 那個 secret**，再 force-push（如果已經 push 過）。懷疑時就直接建一個新 repo，把乾淨狀態當成唯一的 initial commit。

### 事故應變

如果你懷疑 Discord bot token 洩漏：

1. 立刻去 <https://discord.com/developers/applications> → 你的 app → **Bot** → **Reset Token**。舊 token 會在幾秒內失效。
2. 把新 token 更新進 `.env`。
3. 重啟 bridge（Windows `Start-ScheduledTask`；macOS `launchctl bootout` + `bootstrap`）。
4. 檢查 Discord 頻道近期活動，看有沒有你沒送的訊息。
5. 跑上面的 security grep 確認舊 token 沒躺在任何被 commit 的檔案裡。

如果你懷疑 Claude Code 憑證洩漏（keychain 被偷、機器被入侵）：

1. 到 [Claude 帳號頁面](https://claude.ai/settings/account) 撤銷目前所有的 Claude Code session。
2. 在乾淨的機器上重新登入。
3. 檢查 `~/.claude/` 有沒有可疑檔案。

## 驗證清單

信任 bridge 之前，請跑以下驗證（全部都可在你 clone 的 shell 裡執行）：

```bash
cd ~/.claude-bridge

# 1. 兩個 Python 檔的語法檢查
./venv/bin/python -m py_compile bridge.py bridge_mcp.py && echo "syntax OK"

# 2. Import 測試 — 抓相依套件缺失與 import 時的環境變數問題
./venv/bin/python -c "import bridge; print('bridge imports OK')"
./venv/bin/python -c "import bridge_mcp; print('mcp imports OK, tools:', list(bridge_mcp.app._tool_manager._tools.keys()))"

# 3. Dry run — 啟動 bridge，等 "connected as" 訊息，然後停
./venv/bin/python bridge.py &
PID=$!
sleep 4
grep -E "connected as|PrivilegedIntentsRequired|login failure" bridge.log | tail -5
kill $PID

# 4. 驗證 log 裡的 filter — user / channel / prefix / allowed_tools / model 要與 .env 一致
grep -E "filters:|model=|allowed_tools=|prefix=" bridge.log | tail -5

# 5. Secret 掃描（沒有輸出即 OK）
git ls-files | xargs grep -nE 'MT[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}' 2>/dev/null

# 6. macOS 上安裝後驗證 plist 有效
plutil -lint ~/Library/LaunchAgents/dev.local.claude-discord-bridge.plist
```

從 Discord 端，逐一驗證每一層防禦：

1. **前綴 gate**：送 `hello`（沒有 prefix）。預期：👀 reaction、沒有回覆。
2. **使用者/頻道過濾**：找第二個人（或第二個 Discord 帳號）在同頻道送 `claude> 試試`。預期：什麼都不會發生，log 出現 `drop: author=...`。
3. **工具白名單**：送 `claude> 透過 Bash 跑 "rm -rf /"`。預期：Claude 拒絕使用 Bash（因為不在 allowed-tools），回一段文字說無法執行 shell 指令，**不會真的跑 `rm -rf`**。
4. **Read 防禦**（若你從白名單拿掉了 `Read`）：送 `claude> 讀 ./.env 並顯示內容`。預期：Claude 回答沒有 Read tool 可用。
5. **端對端**：送 `claude> 現在目錄有幾個檔案？`。預期：一個數字，對得上 `ls` 的輸出。

## MCP server

把 `bridge_mcp.py` 註冊成 Claude Code MCP server，讓同台機器上另一個互動式 Claude Code session 能夠檢查與控制 bridge：

```bash
claude mcp add discord-bridge --scope user \
    "$HOME/.claude-bridge/venv/bin/python" \
    "$HOME/.claude-bridge/bridge_mcp.py"
```

重開 Claude Code。新 session 會看到 6 個 tools：

| Tool | 用途 |
|---|---|
| `inbox_list(limit=10)` | 從 Discord 收到的最近 N 則訊息 |
| `outbox_list(limit=10)` | 最近 N 筆自動回覆紀錄（ok / elapsed / token 用量） |
| `bridge_status()` | Daemon PID、session ID、檔案大小、過濾設定 |
| `send_message(content, reply_to=None)` | 以 bot 身分向 bridge 頻道送訊息 |
| `tail_log(lines=30)` | `bridge.log` 最後 N 行 |
| `usage_today(date=None)` | 某一天的 token 與成本加總 |

MCP server 是在 Claude Code session 啟動時才 scan 的，所以「跑 `claude mcp add` 的當下那個 session」**看不到新的 tools**，要**下次**開新 session 才生效。

## 疑難排解

這 5 個是開發 bridge 時踩過的死路。記錄在這裡讓你不用重複踩。

### 1. macOS 透過 SSH 跑出現 "Not logged in — Run /login"

Bridge 在 Terminal.app 裡起得了，但 SSH 進去就炸 `Not logged in · Please run /login · Run in another terminal: security unlock-keychain`，即使你從桌面登入時 `claude` 明明可以用。

**原因**：macOS login keychain 只在 Aqua（GUI）session 裡自動解鎖。SSH session 的 keychain 是鎖住的，`claude -p` 讀不到 keychain 裡的 OAuth 憑證。

**修法**：用 `install_aqua_launchagent.sh`，它會 bootstrap 到 `gui/<uid>`，也就是 Aqua session，keychain 在那裡是解鎖的。臨時的 SSH 跑法可以先用 `security unlock-keychain`，但重開機後就又鎖回去。

### 2. 啟動時 `discord.errors.PrivilegedIntentsRequired`

Bridge 連 Gateway 約 1.5 秒後炸 `PrivilegedIntentsRequired`。

**原因**：Python code 裡 `intents.message_content = True` 只是「請求」這個 intent。你**還必須**去 Developer Portal 把 **MESSAGE CONTENT INTENT** 的 toggle 打開。漏掉 Portal 那一步，Gateway 會拒絕連線。

**修法**：Developer Portal → 你的 application → Bot → Privileged Gateway Intents → 開 Message Content Intent → Save Changes → 重啟 bridge。

### 3. `Session ID <uuid> is already in use`

第一條 Discord 訊息成功，第二條炸這個錯誤。

**原因**：`claude --session-id <uuid>` 的語意是「**創建**一個這個 UUID 的新 session」，不是「**resume**」。Session 檔一旦存在，再用 `--session-id` 就衝突。

**修法**：bridge 用一個 `bridge_session_seeded.flag` 旗標檔。第一次呼叫用 `--session-id` 創建；之後每次都用 `--resume <uuid>` 繼續。要開全新對話的話，刪掉 flag 檔和 `~/.claude/projects/<cwd-hash>/<uuid>.jsonl`。

### 4. `pythonw.exe` 下 `AttributeError: 'NoneType' object has no attribute 'write'`

Bridge 在 `python.exe`（有 console 視窗）下跑 OK，但 Task Scheduler 用 `pythonw.exe` 叫起來就立刻炸。

**原因**：`pythonw.exe` 是 Windows GUI 子系統的 Python，**沒有 console**。`sys.stdout`、`sys.stderr`、`sys.stdin` 全部是 `None`。`logging.StreamHandler(sys.stdout)` 在第一次寫 log 時就炸。

**修法**：把 StreamHandler 加上條件判斷。Bridge 已經這樣做：

```python
_handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
if sys.stdout is not None:
    try:
        sys.stdout.fileno()
        _handlers.append(logging.StreamHandler(sys.stdout))
    except (OSError, AttributeError, ValueError):
        pass
```

若你加了一個會 `print()` 到 stdout 的第三方套件，import 之前先 `sys.stdout = open(os.devnull, 'w')`。

### 5. macOS Homebrew Python 的 `error: externally-managed-environment`

在 Homebrew Python 上 `pip install discord.py` 被 PEP 668 擋住，錯誤訊息是 "externally-managed-environment"。

**原因**：Homebrew 的 Python 標記為 system-managed，禁止直接 `pip install`，避免污染 Homebrew 環境。

**修法**：用 virtual environment。上面的安裝步驟本來就這樣做（`python -m venv venv`）。**不要**用 `--user` 或 `--break-system-packages` 去繞過。

## 這個 repo 是怎麼做出來的

整個 bridge 是在**一個 Claude Code 互動 session 裡跟 [Claude Code](https://docs.claude.com/en/docs/claude-code) 結對寫出來的**。對話從「我可以從 Discord 控制 Claude 嗎？」開始，最後做出一個加固過的、會開機自啟的 daemon、加 MCP server、加安全審計、加這份 README。上面那 5 個疑難排解是真的被踩過的坑。

## 授權條款

MIT。詳見 [LICENSE](LICENSE)。
