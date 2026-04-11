# Claude Discord Bridge

[![CI](https://github.com/icec8c8/claude-discord-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/icec8c8/claude-discord-bridge/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/icec8c8/claude-discord-bridge)](https://github.com/icec8c8/claude-discord-bridge/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

**[English](README.md) · [繁體中文](README.zh-TW.md) · [日本語](README.ja.md)**

プライベートな Discord チャンネル経由で、ヘッドレスの [Claude Code](https://docs.claude.com/en/docs/claude-code) CLI と対話できる小さなデーモンです。これにより、スマートフォンの Discord さえあれば、どこからでも既存の Claude サブスクリプションを動かせます。**追加の API キーも追加料金も不要**です。

Discord チャンネルでこのようにタイプすると:

```
claude> プロジェクトの README を読んで、次に何をすべきか教えて
```

数秒後、Claude の返信がスレッドメッセージとして表示されます。Windows（Scheduled Task）と macOS（LaunchAgent）の両方に対応しています。

## 目次

1. [アーキテクチャ](#アーキテクチャ)
2. [機能](#機能)
3. [動作要件](#動作要件)
4. [セットアップ](#セットアップ) — Discord bot · インストール · Windows 自動起動 · macOS 自動起動
5. [設定](#設定) — 環境変数の完全な一覧
6. [セキュリティモデル](#セキュリティモデル) — **ツールのホワイトリストを広げる前に必読**
7. [検証チェックリスト](#検証チェックリスト) — 使用前に安全性を確認する方法
8. [MCP サーバー](#mcp-サーバー)
9. [トラブルシューティング](#トラブルシューティング) — 開発中に遭遇した 5 つの行き止まり
10. [ライセンス](#ライセンス)

## アーキテクチャ

```
   Discord チャンネル
        │
        ▼
  bridge.py (discord.py Gateway リスナー; pythonw.exe / launchd)
        │  user_id + channel_id フィルタ  +  プレフィックスゲート "claude>"
        ▼
  inbox.jsonl  +  📥 リアクション
        │
        ▼
  asyncio.Queue  ──►  worker  ──►  claude -p --resume <uuid>
                                      │    --output-format json
                                      │    --allowedTools <読み取り専用ホワイトリスト>
                                      │    --model sonnet
                                      ▼
                                  結果 + 使用量 + コスト
                                      │
                                      ▼
                              Discord への返信  +  ✅/❌ リアクション
                              outbox.jsonl (各呼び出しの統計)
```

ヘッドレスの `claude -p` サブプロセスは、現在開いているインタラクティブな Claude Code セッションとは**完全に別の会話**です。同じサブスクリプションを使いますが、平行宇宙のようなもの — bridge をトリガーしても、ライブの TUI セッションには一切影響しません。

## 機能

- **自動返信** — フィルタを通過したすべての Discord メッセージは、ヘッドレスの `claude -p` 呼び出しを生成し、スレッド返信として投稿されます。
- **セッションの永続性** — 固定の session UUID（`bridge_session_id.txt` に保存）により、ヘッドレスの Claude は `--resume` で過去の Discord 会話を覚えています。
- **プレフィックスゲート** — メッセージは `claude>`（設定可能）で始まる必要があります。それ以外は 👀 リアクションが付いてドロップされます。
- **デフォルトで読み取り専用のツールホワイトリスト** — `--allowedTools` は `Read Grep Glob WebFetch WebSearch Task TodoWrite` がデフォルト。`Bash` も `Edit` も `Write` もなし。広げるなら必ず「セキュリティモデル」のセクションを先に読んでください。
- **Sonnet がデフォルト** — `--model sonnet` は Max サブスクリプションのレート制限に対して Opus の約 1/3 のコスト。`CLAUDE_MODEL` で上書き可能。
- **トークン会計** — すべての呼び出しで `--output-format json` から `usage` と `total_cost_usd` をキャプチャ。毎日の合計は同梱の MCP ツール経由で確認可能。
- **MCP サーバー** — 兄弟ファイル `bridge_mcp.py` が 6 つのツール（`inbox_list`、`outbox_list`、`bridge_status`、`send_message`、`tail_log`、`usage_today`）を公開。同じマシン上の別の Claude Code セッションから bridge を検査・制御できます。
- **自動起動** — Windows ではログオン時の Scheduled Task、macOS では Aqua セッション内の LaunchAgent。
- **設計上シングルユーザー** — 1 つの Discord ユーザー ID と 1 つのチャンネル ID でハードにフィルタリング。それ以外はすべてドロップ。

## 動作要件

| | |
|---|---|
| OS | Windows 10/11、macOS 13+（Linux も動作するはずですが LaunchAgent の手順は対象外） |
| Python | 3.11+（Windows 3.11 と macOS Homebrew 3.13 でテスト済み） |
| Claude Code CLI | `npm install -g @anthropic-ai/claude-code`、少なくとも一度ログイン済み |
| Node.js | npm 経由で Claude Code をインストールする場合のみ必要 |
| Discord 開発者アカウント | 自分の bot を作るため |
| プライベート Discord チャンネル | bot がメッセージを読んで返信を投稿できる場所 |

有効な Claude サブスクリプション（Max、Team、Console/API）も必要です。bridge はローカルの `claude` CLI が既に持っている認証情報をそのまま使うため、Anthropic API キーを別途要求することはありません。

## セットアップ

### ステップ 1: Discord bot を作成

1. <https://discord.com/developers/applications> にアクセスし、**New Application** をクリック。好きな名前を付けます。
2. **Bot** タブを開きます。**Reset Token** をクリックしてトークンをコピー。このトークンが見えるのは **この一度きり** です。すぐにパスワードマネージャーに保存してください。
3. 同じ **Bot** ページで **Privileged Gateway Intents** までスクロールし、**MESSAGE CONTENT INTENT** を有効化して Save Changes。これを有効にしないと、bot はメッセージ本文を読めず、起動時に `PrivilegedIntentsRequired` で即クラッシュします。
4. **OAuth2 → URL Generator** に移動。`bot` スコープを選択し、bot のパーミッションで `View Channels`、`Send Messages`、`Read Message History` をチェック。`Administrator` は **チェックしないでください** — bridge には不要です。生成された URL をコピーしてブラウザで開き、bot を自分の guild に認可します。

### ステップ 2: 必要な ID を取得

1. Discord クライアントで **ユーザー設定 → 詳細設定 → 開発者モード** を有効化。
2. 自分のユーザー名を右クリックし、**ユーザー ID をコピー**。これが `ALLOWED_USER_ID`。
3. bridge として使いたいチャンネルを右クリックし、**チャンネル ID をコピー**。これが `ALLOWED_CHANNEL_ID`。

### ステップ 3: bridge をインストール

```bash
# ホームディレクトリの永続的な場所にクローン
git clone https://github.com/<you>/claude-discord-bridge.git ~/.claude-bridge
cd ~/.claude-bridge

# Python 仮想環境を作成
python -m venv venv

# 依存関係をインストール
# Windows:
venv\Scripts\pip install -r requirements.txt
# macOS/Linux:
./venv/bin/pip install -r requirements.txt

# 環境変数テンプレートをコピーして秘密情報を入力
cp .env.example .env
# .env を編集して DISCORD_BOT_TOKEN、ALLOWED_USER_ID、ALLOWED_CHANNEL_ID を貼り付け
```

macOS/Linux では、その後 `chmod 600 .env` を実行して、トークンファイルが所有者のみ読めるようにします。

Windows では NTFS 継承により通常ホームディレクトリはプライベートに保たれますが、不安なら `icacls .env /inheritance:r /grant:r "%USERNAME%:F"` でファイルを自分だけ読めるようにします。

### ステップ 4: bridge の動作確認

```bash
# Windows
venv\Scripts\python.exe bridge.py

# macOS / Linux
./venv/bin/python bridge.py
```

`bridge.log` に約 2 秒以内に `connected as <botname>` が表示されるはずです。次に Discord の設定済みチャンネルで送信:

```
claude> OK という単語だけを返して、それ以外は何も言わないで
```

bot は 📥 → 🤖 → ✅ の順でリアクションを付け、メッセージを引用して `OK` と返すはずです。これが動けば、`Ctrl+C` でプロセスを停止し、自動起動のステップに進みます。

### ステップ 5a: Windows 自動起動

```powershell
powershell -ExecutionPolicy Bypass -File install_autostart.ps1
```

これで `ClaudeDiscordBridge` という Scheduled Task が作成されます。ユーザーログオン時に `pythonw.exe` で `bridge.py` を実行し（ウィンドウなし）、失敗時は 1 分間隔で最大 999 回再起動します。**管理者権限は不要** — 自分のユーザー範囲に閉じています。

制御コマンド:

```powershell
Start-ScheduledTask      -TaskName ClaudeDiscordBridge
Stop-ScheduledTask       -TaskName ClaudeDiscordBridge
Disable-ScheduledTask    -TaskName ClaudeDiscordBridge
Get-ScheduledTask        -TaskName ClaudeDiscordBridge
Get-ScheduledTaskInfo    -TaskName ClaudeDiscordBridge
Unregister-ScheduledTask -TaskName ClaudeDiscordBridge -Confirm:$false
```

### ステップ 5b: macOS 自動起動（Aqua セッション）

**このステップは GUI の Terminal.app ウィンドウから実行する必要があります。SSH シェルから実行してはいけません。** macOS のログインキーチェインは SSH コンテキストではロックされており、Claude Code は認証情報をそこに保存しているため、Aqua セッション外で `claude -p` を実行すると `Not logged in` で失敗します。Screen Sharing、VNC、または物理キーボードで実際のデスクトップセッションを取得し、そこで Terminal.app を開いて実行してください:

```bash
bash install_aqua_launchagent.sh
```

このスクリプトは `$HOME` を読み取り、`which claude` で claude のパスを取得し、`claude-discord-bridge.plist.template` のプレースホルダを置換し、結果を `~/Library/LaunchAgents/dev.local.claude-discord-bridge.plist` に書き込み、`plutil -lint` で検証し、`launchctl bootstrap gui/$(id -u)` で Aqua セッションにブートストラップします。

macOS のインストーラを実行する **前に**、Windows でも動いている場合は Windows 側の Scheduled Task を **Stop + Disable** してください。Discord bot トークンは同時に 1 つの Gateway 接続しか持てず、両方同時に動くと互いに切断し合います。

## 設定

すべてのノブは `.env` にあります。完全なドキュメントは `.env.example` を参照。重要な変数:

| 変数名 | デフォルト | 必須 | 意味 |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | — | はい | Developer Portal から取得した bot トークン |
| `ALLOWED_USER_ID` | — | はい | この Discord ユーザー ID からのメッセージのみ処理 |
| `ALLOWED_CHANNEL_ID` | — | はい | このチャンネルからのメッセージのみ処理 |
| `BRIDGE_PREFIX` | `claude>` | いいえ | 全メッセージに必須のプレフィックス; `""` でゲート無効化（非推奨） |
| `CLAUDE_MODEL` | `sonnet` | いいえ | モデルエイリアスまたはフルネーム |
| `CLAUDE_ALLOWED_TOOLS` | `Read Grep Glob WebFetch WebSearch Task TodoWrite` | いいえ | `--allowedTools` に渡すスペース区切りのツールホワイトリスト |
| `CLAUDE_EXE` | `claude` | いいえ | Claude Code バイナリのパス; デフォルトは `$PATH` 依存 |
| `CLAUDE_TIMEOUT` | `300` | いいえ | 呼び出しごとのタイムアウト（秒） |
| `CLAUDE_CWD` | bridge ディレクトリ | いいえ | ヘッドレス claude サブプロセスの作業ディレクトリ |

## セキュリティモデル

bridge には 5 層の防御があります。**いずれかの層を緩める前に、このセクションを最後まで読んでください。**

### 層状の防御

1. **ユーザー ID フィルタ**: `ALLOWED_USER_ID` からのメッセージのみ処理されます。それ以外は `on_message()` の先頭で黙ってドロップされます。
2. **チャンネル ID フィルタ**: `ALLOWED_CHANNEL_ID` のメッセージのみ処理されます。server に追加された bot は多数のチャンネルを見ることができますが、このフィルタが bridge を専用の単一チャンネルに固定します。
3. **プレフィックスゲート**: メッセージは `BRIDGE_PREFIX`（デフォルト `claude>`）で始まる必要があります。それ以外は 👀 リアクションが付いてドロップされます。これは 2 つを防ぎます: タイプミスで本物の Claude ターンが生成されること、そして侵害された Discord アカウントで雑談のように見えるメッセージがコマンドとして解釈されること。
4. **ツールホワイトリスト**: ヘッドレス claude は `--allowedTools` で読み取り/検索系の操作のみに制限されて呼び出されます。`Bash` なし、`Edit` なし、`Write` なし、`NotebookEdit` なし。bridge は `.env` で明示的にホワイトリストを広げない限り、ディスク上のファイルを変更したり shell コマンドを実行したりは **できません**。
5. **モデル上限**: `--model sonnet` デフォルトは Opus に対する呼び出しごとのコストと能力を制限します。

### デフォルトのホワイトリストでも bridge が **できる** こと

- **実行ユーザーが読めるあらゆるファイルを読むこと**。`Read` はホワイトリストにあります。あなたの Discord アカウントを持つ攻撃者が `claude> ~/.ssh/id_rsa を読んで` とタイプすれば、内容が Discord の返信で戻ってきます。これが問題になるなら、`CLAUDE_ALLOWED_TOOLS` から `Read` を外すか、プロジェクトの Claude Code 設定でパスベースの許可リストを使って `Read` を制限してください。
- **任意の URL を取得すること**。`WebFetch` はホワイトリストにあります。攻撃者は bridge にローカルファイルを読ませた後、`WebFetch` 経由で自分の制御する URL に POST させることで情報を外部に流出させられます。これが問題になるなら、`CLAUDE_ALLOWED_TOOLS` から `WebFetch` を外してください。
- **ウェブ検索を行うこと**。`WebSearch` はホワイトリストにあります。ほぼ無害ですが、レート制限を消費します。
- **あなたの Claude サブスクリプションのクレジットを消費すること**。任意の Discord メッセージは実際の Claude 呼び出しをトリガーし、Max/Team/Console のクォータからトークンを消費します。自分自身にレート制限をかけるには、`usage_today` MCP ツールで 1 日のトークン予算を短く設定するか、`on_message` の周りにクールダウンを追加してください。

### デフォルトのホワイトリストで bridge が **できない** こと

- shell コマンドの実行（`Bash` は除外されています）。
- ディスク上のファイルの変更や作成（`Edit`、`Write`、`NotebookEdit` は除外されています）。
- `discord.py` 経由で `ALLOWED_CHANNEL_ID` 以外のチャンネルにメッセージを送信すること。
- 他のユーザーのファイルへのアクセス（あなたのユーザーとして実行され、OS の権限に縛られます）。

### 安全にホワイトリストを広げる

bridge にもっと多くのことをさせたいなら、ツールを一度に 1 つだけ追加し、各追加を別のテストチャンネルでまず検証してください。例 — `git status` / `git log` のみ `Bash` を許可する:

```
CLAUDE_ALLOWED_TOOLS=Read Grep Glob WebFetch WebSearch Task TodoWrite "Bash(git status:*)" "Bash(git log:*)"
```

ツールごとの glob 構文 `ToolName(pattern)` は Claude Code の標準的な細粒度パーミッション形式です。`Bash(*:*)` ではなく狭い glob を優先してください。

### `git push` する前に — リポジトリセキュリティ

このリポジトリは公開できるように設計されていますが、**あなたのクローンに秘密情報が含まれていないことを検証する責任はあなた自身にあります**。チェックリスト:

```bash
# 1. .env が git-ignore され、追跡されていないことを確認
git check-ignore -v .env
git ls-files | grep -i '\.env$'   # .env.example のみが表示されるはず

# 2. トークンのように見えるものを追跡ファイルから grep
git ls-files | xargs grep -nE 'MT[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|(token|secret|key|password)[[:space:]]*=' 2>/dev/null

# 3. あなたの個人 ID を追跡ファイルから grep
git ls-files | xargs grep -nE '<your-discord-user-id>|<your-channel-id>|<your-username>' 2>/dev/null

# 4. git 履歴を確認 — 過去に commit されて後から削除された秘密情報がないか
git log --all --full-history -p -- .env 2>/dev/null
git log --all -p | grep -nE 'MT[A-Za-z0-9]{20,}|sk-' 2>/dev/null
```

4 つのチェックすべてが空である必要があります。`git log` に以前 commit された秘密情報が表示される場合、**削除して再 commit するだけでは不十分** — 秘密情報はまだ履歴に残っています。`git filter-repo` または BFG で履歴を書き換え、秘密情報を **すぐに rotate** し、既に push 済みなら force-push してください。疑わしい場合は、新しいリポジトリを作り、クリーンな状態を単一の initial commit として再 commit してください。

### インシデント対応

Discord bot トークンが漏洩した疑いがある場合:

1. すぐに <https://discord.com/developers/applications> → あなたのアプリ → **Bot** → **Reset Token** へ。旧トークンは数秒以内に無効化されます。
2. 新しいトークンで `.env` を更新。
3. bridge を再起動（Windows は `Start-ScheduledTask`、macOS は `launchctl bootout` + `bootstrap`）。
4. 最近の Discord チャンネルのアクティビティを監査し、自分が送っていないメッセージがないか確認。
5. 上記のセキュリティ grep を実行し、旧トークンが commit 済みのファイルに残っていないことを確認。

Claude Code の認証情報が漏洩した疑いがある場合（keychain 抽出、マシン侵害）:

1. [Claude アカウントページ](https://claude.ai/settings/account) からアクティブな Claude Code セッションをすべて取り消し。
2. 既知の安全なマシンから再ログイン。
3. `~/.claude/` に不審なファイルがないか確認。

## 検証チェックリスト

bridge を信頼する前に、以下の検証を実行してください。すべてリポジトリをクローンしたシェルから実行できます。

```bash
cd ~/.claude-bridge

# 1. 両方の Python ファイルの構文チェック
./venv/bin/python -m py_compile bridge.py bridge_mcp.py && echo "syntax OK"

# 2. import テスト — 依存関係の漏れと import 時の環境変数エラーをキャッチ
./venv/bin/python -c "import bridge; print('bridge imports OK')"
./venv/bin/python -c "import bridge_mcp; print('mcp imports OK, tools:', list(bridge_mcp.app._tool_manager._tools.keys()))"

# 3. ドライラン — bridge を起動し "connected as" を待ち、停止
./venv/bin/python bridge.py &
PID=$!
sleep 4
grep -E "connected as|PrivilegedIntentsRequired|login failure" bridge.log | tail -5
kill $PID

# 4. ログ内のフィルタを検証 — user / channel / prefix / allowed_tools / model が .env と一致すること
grep -E "filters:|model=|allowed_tools=|prefix=" bridge.log | tail -5

# 5. 秘密情報スキャン（出力なし = OK）
git ls-files | xargs grep -nE 'MT[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}' 2>/dev/null

# 6. macOS: インストール後に plist が有効か確認
plutil -lint ~/Library/LaunchAgents/dev.local.claude-discord-bridge.plist
```

Discord から、各防御層を個別に検証:

1. **プレフィックスゲート**: `hello`（プレフィックスなし）を送信。期待値: 👀 リアクション、返信なし。
2. **ユーザー/チャンネルフィルタ**: 2 人目（または 2 つ目の Discord アカウント）が同じチャンネルで `claude> hi` を送信。期待値: 何も起こらない、ログに `drop: author=...` が表示される。
3. **ツールホワイトリスト**: `claude> Bash で "rm -rf /" を実行して` を送信。期待値: Claude は Bash が allowed-tools にないため使用を拒否し、shell コマンドを実行できないという文字列応答を返す（実際には `rm -rf` を実行しない）。
4. **Read 防御**（ホワイトリストから `Read` を外した場合）: `claude> ./.env のファイルを読んで内容を表示して` を送信。期待値: Claude は Read ツールが利用できないと答える。
5. **エンドツーエンド**: `claude> 現在のディレクトリには何個のファイルがある？` を送信。期待値: `ls` の出力と一致する数字。

## MCP サーバー

同じマシン上の別のインタラクティブな Claude Code セッションから bridge を検査・制御できるように、`bridge_mcp.py` を Claude Code MCP サーバーとして登録します:

```bash
claude mcp add discord-bridge --scope user \
    "$HOME/.claude-bridge/venv/bin/python" \
    "$HOME/.claude-bridge/bridge_mcp.py"
```

Claude Code を再起動。新しいセッションで 6 つのツールが表示されます:

| ツール | 用途 |
|---|---|
| `inbox_list(limit=10)` | Discord から受信した最新 N 件のメッセージ |
| `outbox_list(limit=10)` | 最新 N 件の自動返信レコード（ok / elapsed / トークン使用量） |
| `bridge_status()` | デーモン PID、セッション ID、ファイルサイズ、フィルタ設定 |
| `send_message(content, reply_to=None)` | bot としてチャンネルにメッセージを投稿 |
| `tail_log(lines=30)` | `bridge.log` の最新 N 行 |
| `usage_today(date=None)` | 特定の日のトークンとコストの合計 |

MCP サーバーは Claude Code セッションの起動時にスキャンされるため、`claude mcp add` を実行したセッションでは新しいツールは表示されません。

## トラブルシューティング

これらは bridge を構築中に遭遇した 5 つの行き止まりです。あなたが繰り返さないように記録してあります。

### 1. macOS の SSH 経由で "Not logged in — Run /login"

bridge は Terminal.app ウィンドウでは問題なく起動しますが、SSH 経由では `Not logged in · Please run /login · Run in another terminal: security unlock-keychain` で停止します。デスクトップにログインすると `claude` はインタラクティブに動作するにもかかわらず。

**原因**: macOS のログインキーチェインは Aqua（GUI）セッションでのみアンロックされます。SSH シェルではキーチェインがロックされており、`claude -p` はロックされたキーチェインから保存された OAuth 認証情報を読み取れません。

**修正**: `install_aqua_launchagent.sh` を使用。これは `gui/<uid>` にブートストラップします — それがキーチェインがアンロックされている Aqua セッションです。アドホックな SSH 実行なら、セッション開始時に `security unlock-keychain` を手動で 1 度実行することでも機能しますが、再起動後は永続化しません。

### 2. 起動時の `discord.errors.PrivilegedIntentsRequired`

bridge は Gateway に約 1.5 秒接続した後、`PrivilegedIntentsRequired` でクラッシュします。

**原因**: Python コード内の `intents.message_content = True` は *リクエスト* にすぎません。Developer Portal で **MESSAGE CONTENT INTENT** も有効化する必要があります。ポータルでのステップを逃すと、Gateway は接続を拒否します。

**修正**: Developer Portal → あなたのアプリケーション → Bot → Privileged Gateway Intents → Message Content Intent を有効化 → Save Changes → bridge を再起動。

### 3. `Session ID <uuid> is already in use`

最初の Discord メッセージは成功、2 番目がこのエラーで失敗。

**原因**: `claude --session-id <uuid>` の意味は「この UUID で **新しい** セッションを作成する」であり、「resume」ではありません。セッションファイルが存在すると、`--session-id` を再利用するとエラーになります。

**修正**: bridge は `bridge_session_seeded.flag` センチネルファイルを保持します。最初の呼び出しは `--session-id` で作成し、以降のすべての呼び出しは `--resume <uuid>` で継続します。まったく新しい会話を開始するには、フラグファイルと `~/.claude/projects/<cwd-hash>/` 下の対応する `.jsonl` を削除してください。

### 4. `pythonw.exe` 下の `AttributeError: 'NoneType' object has no attribute 'write'`

bridge は console ウィンドウの `python.exe` では問題なく動作しますが、Task Scheduler が `pythonw.exe` 経由で起動するとすぐにクラッシュします。

**原因**: `pythonw.exe` は Windows GUI サブシステムの Python です。console がないため、`sys.stdout`、`sys.stderr`、`sys.stdin` はすべて `None` です。`logging.StreamHandler(sys.stdout)` は最初のログ行が出力される瞬間にクラッシュします。

**修正**: stream handler を実際の stdout がある場合のみに条件付けます。bridge は既にそうしています:

```python
_handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
if sys.stdout is not None:
    try:
        sys.stdout.fileno()
        _handlers.append(logging.StreamHandler(sys.stdout))
    except (OSError, AttributeError, ValueError):
        pass
```

stdout に直接 `print()` するサードパーティライブラリを追加する場合、インポート前に `sys.stdout = open(os.devnull, 'w')` でリダイレクトしてください。

### 5. macOS Homebrew Python の `error: externally-managed-environment`

Homebrew Python で `pip install discord.py` が PEP 668 "externally-managed-environment" エラーで失敗。

**原因**: Homebrew の Python は、Homebrew 環境が偶発的な `pip install` の汚染から保護するため、システム管理としてマークされています。

**修正**: 仮想環境を使用。上記のセットアップ手順は既にそうしています（`python -m venv venv`）。`--user` や `--break-system-packages` でショートカットしようとしないでください。

## このリポジトリはどうやって作られたか

この bridge 全体は **単一のインタラクティブな Claude Code セッションで [Claude Code](https://docs.claude.com/en/docs/claude-code) とペアプログラミング** されました。会話は「Discord から Claude を制御できる？」で始まり、堅牢化された、自動起動するデーモン + MCP サーバー + セキュリティ監査 + この README で終わりました。上記の 5 つのトラブルシューティング項目は、ペアプログラミングが実際にぶつかった真の行き止まりです。

## ライセンス

MIT。詳細は [LICENSE](LICENSE) を参照。
