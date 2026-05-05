# Discord Minecraft File Manager (DMFM) - セットアップガイド

## 📋 目次

1. [要件](#要件)
2. [インストール](#インストール)
3. [設定](#設定)
4. [実行](#実行)
5. [使い方](#使い方)
6. [トラブルシューティング](#トラブルシューティング)

---

## 要件

- **Python**: 3.8 以上
- **discord.py**: 2.0 以上
- **OS**: Linux（Proxmox CT や Ubuntu 推奨）
- **Discord**: Bot アカウント（Discord Developer Portal で作成済み）

---

## インストール

### ステップ 1: Python パッケージのインストール

```bash
pip install discord.py
```

### ステップ 2: ファイルの配置

`dmfm_bot.py` を、実行用ディレクトリに配置します（例: `/home/minecraft/bot/`）。

```bash
mkdir -p /home/minecraft/bot
cp dmfm_bot.py /home/minecraft/bot/
cd /home/minecraft/bot
```

---

## 設定

### ステップ 1: Discord Bot Token の取得

1. [Discord Developer Portal](https://discord.com/developers/applications) にアクセス
2. 「New Application」をクリック
3. Bot タブから「Add Bot」
4. TOKEN をコピー（**他人に絶対に教えない！**）

### ステップ 2: Bot の権限設定

Developer Portal の「OAuth2」→「URL Generator」で以下権限をチェック：

```
Scopes:
- bot
- applications.commands

Permissions:
- Send Messages
- Embed Links
- Attach Files
- Read Message History
```

生成された URL から Bot をサーバーに招待します。

### ステップ 3: 環境変数の設定

`~/.bashrc` または `~/.zshrc` に以下を追加：

```bash
export DISCORD_TOKEN="your-bot-token-here"
export MINECRAFT_ROOT="/home/minecraft/server"
export ADMIN_USER_ID="1218055725352484904"
export APPROVAL_CHANNEL_ID="123456789012345678"  # 後で取得
```

**または** `.env` ファイルを使う場合（`python-dotenv` をインストール）：

```bash
pip install python-dotenv
```

ボットディレクトリ内に `.env` ファイルを作成：

```ini
DISCORD_TOKEN=your-bot-token-here
MINECRAFT_ROOT=/home/minecraft/server
ADMIN_USER_ID=0000000000000000000
APPROVAL_CHANNEL_ID=123456789012345678
```

コードを以下のように修正（先頭に追加）：

```python
from dotenv import load_dotenv
load_dotenv()
```

### ステップ 4: 承認チャンネル ID の取得

Discord サーバー上で、承認ボタンを表示するチャンネルを決定します。

1. Discord を開く
2. 該当チャンネルを右クリック → 「チャンネルをコピー」（URL コピー）
3. URL の最後の数字が チャンネル ID です（例: `https://discord.com/channels/.../<123456789012345678>` → `123456789012345678`）

または、チャンネルで `/dmfm_channel_id` のようなコマンドを打つと ID が表示される方式も実装可能です。

### ステップ 5: パス設定の確認

Minecraft サーバーディレクトリを確認：

```bash
ls -la /home/minecraft/server
# または設定した MINECRAFT_ROOT
```

---

## 実行

### 方法 1: フォアグラウンド実行（テスト用）

```bash
python dmfm_bot.py
```

画面に以下が表示されれば成功：

```
╔════════════════════════════════════════════════════════╗
║  Discord Minecraft File Manager (DMFM) Bot            ║
║  ファイルパス安全性チェック: ✅ 有効                  ║
║  バックアップ世代: 1 世代（最新のみ）                 ║
╚════════════════════════════════════════════════════════╝

📁 ROOT_DIR: /home/minecraft/server
👤 Admin ID: 1218055725352484904
📢 Approval Channel: 123456789012345678

⏳ Bot 起動中...
✅ Bot が起動しました。ユーザー: BotName#0000
✅ 4 個のスラッシュコマンドを同期しました
```

### 方法 2: systemd サービスで常時実行

ファイル `/etc/systemd/system/dmfm-bot.service` を作成：

```ini
[Unit]
Description=Discord Minecraft File Manager Bot
After=network.target

[Service]
Type=simple
User=minecraft
WorkingDirectory=/home/minecraft/bot
Environment="PATH=/usr/local/bin:/usr/bin:/bin"
EnvironmentFile=/home/minecraft/bot/.env
ExecStart=/usr/bin/python3 /home/minecraft/bot/dmfm_bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

起動：

```bash
sudo systemctl daemon-reload
sudo systemctl start dmfm-bot
sudo systemctl enable dmfm-bot  # 自動起動
```

ステータス確認：

```bash
sudo systemctl status dmfm-bot
```

ログ確認：

```bash
sudo journalctl -u dmfm-bot -f
```

### 方法 3: pm2 で管理（推奨）

```bash
npm install -g pm2
pm2 start dmfm_bot.py --name "dmfm-bot" --interpreter python3
pm2 save
pm2 startup
```

---

## 使い方

### /ls コマンド

**目的**: ディレクトリ内容を表示

```
/ls                  → カレントディレクトリを表示
/ls /config          → /config フォルダを表示
/ls /plugins         → /plugins フォルダを表示
```

**レスポンス例**:

```
📂 /plugins

📁 フォルダ:
  `Geyser-Spigot/`
  `ModelEngine/`
  `MyPlugin/`

📄 ファイル:
  `server.jar`
  `spigot.yml`

フォルダ: 3 個、ファイル: 2 個
```

### /cd コマンド

**目的**: カレントディレクトリを変更（ユーザーごと）

```
/cd /plugins         → /plugins に移動
/cd /config          → /config に移動
/cd /                → ルートに戻る
```

以降の `/ls` (パス指定なし) では、ここで設定したパスが基準になります。

### /edit コマンド

**目的**: ファイルを編集（承認フロー）

```
/edit /config/server.properties
```

**フロー**:

1. Bot がファイル内容を表示
2. ユーザーが Bot のメッセージに「リプライ」して、編集済みファイルをアップロード
3. 管理者に承認パネルが表示（`#承認チャンネル` など）
4. 管理者が「✅ 承認して反映」をクリック
5. ファイルが変更され、元の状態は `.bak` で保存

**編集済みファイルのアップロード方法**:

- Bot のメッセージを右クリック → 「返信」
- ファイルをアップロード（上矢印アイコン）
- メッセージ送信

### /new コマンド

**目的**: 新しいファイルを作成（承認フロー）

```
/new /plugins/MyPlugin/config.yml
```

**フロー**:

1. Bot がパスを確認し、新規作成ウィザードを表示
2. ユーザーが Bot のメッセージに「リプライ」して、ファイル内容をアップロード
3. 管理者に承認パネルが表示
4. 管理者が「✅ 承認して反映」をクリック
5. ファイルが作成される

---

## セキュリティ機能

### ✅ ディレクトリトラバーサル対策

**❌ 以下のようなアクセスは完全に遮断されます**:

```
/edit ../../../etc/passwd       → ❌ NG
/edit /etc/passwd               → ❌ NG
/edit ../../sensitive_file.txt  → ❌ NG
```

**仕組み**:

```python
def sanitize_path(user_path: str) -> Optional[Path]:
    """
    ユーザーパス → 絶対パス に変換
    ↓
    ROOT_DIR 配下であることを os.path.commonpath で確認
    ↓
    範囲外 → None を返す（エラー）
    """
```

### ✅ 管理者権限の確認

承認・却下ボタンは **管理者ユーザー ID のみ** が操作可能です。

```python
if interaction.user.id != ADMIN_USER_ID:
    # エラーを返す
```

### ✅ バックアップ管理

変更前のファイルは自動的に `.bak` として保存されます（最新1世代）。

```bash
ls -la /home/minecraft/server/config/server.properties*
# server.properties
# server.properties.bak  ← 前回の状態
```

復旧が必要な場合：

```bash
mv /home/minecraft/server/config/server.properties.bak \
   /home/minecraft/server/config/server.properties
```

---

## トラブルシューティング

### Bot が起動しない

**症状**: `❌ エラー: ROOT_DIR '...' が存在しません`

**解決**:

```bash
export MINECRAFT_ROOT="/home/minecraft/server"
# パスが存在するか確認
ls -la /home/minecraft/server
```

### コマンドが表示されない

**症状**: Discord でスラッシュコマンド `/ls` が出てこない

**解決**:

1. Bot に `applications.commands` 権限があるか確認
2. ボットを一度サーバーから削除して再招待
3. Discord クライアントをリロード（Ctrl+R）
4. ログを確認：

```bash
python dmfm_bot.py 2>&1 | grep -i "command\|error"
```

### リプライでファイルが認識されない

**症状**: ユーザーがファイルをアップロードしても反応がない

**解決**:

- **リプライ方式**: 必ず Bot のメッセージに「返信」する（右クリック → 返信）
- **ファイル形式**: UTF-8 でエンコードされていることを確認
- **ファイルサイズ**: Discord の添付サイズ制限（8 MB）を超えていないか確認

### 承認パネルが表示されない

**症状**: ファイルをアップロードしても、承認チャンネルに何も届かない

**解決**:

1. `APPROVAL_CHANNEL_ID` が正しく設定されているか確認：

```bash
echo $APPROVAL_CHANNEL_ID
```

2. Bot が該当チャンネルへのメッセージ送信権限があるか確認（Server Settings → Roles → Bot Role）

3. ログで詳細エラーを確認：

```bash
python dmfm_bot.py 2>&1 | grep -i "approval\|error"
```

### ファイルの編集が反映されない

**症状**: 承認ボタンをクリックしたが、ファイルが更新されていない

**解決**:

1. Bot の実行ユーザーがファイルへの書き込み権限があるか確認：

```bash
ls -l /home/minecraft/server/config/
# Bot ユーザーが所有者か、グループ・その他に書き込み権限があるか
```

2. Minecraft サーバーがファイルをロック中ではないか確認
3. ディスク容量を確認：

```bash
df -h /home/minecraft/server
```

---

## カスタマイズ例

### Minecraft サーバーコマンドの自動実行

承認時に `reload` コマンドを自動実行したい場合、以下を追加：

```python
# dmfm_bot.py の ApprovalView.approve_button() 内に追加

# ファイルを書き込み後
if not write_file(self.file_path, self.modified_content):
    ...

# RCON で reload コマンドを実行（mcrcon ライブラリ使用）
try:
    from mcrcon import MCRcon
    with MCRcon("localhost", "rcon_password", port=25575) as mcr:
        response = mcr.command("reload")
        print(f"✅ reload コマンド実行: {response}")
except Exception as e:
    print(f"⚠️ reload コマンド実行エラー: {e}")
```

### 特定ファイルのみ編集許可

例: `server.properties` と `config/*.yml` のみ許可

```python
def is_allowed_file(file_path: Path) -> bool:
    """編集許可対象か確認"""
    allowed = [
        "server.properties",
        "server-icon.png",
        "banned-players.json",
        "banned-ips.json",
    ]
    
    if file_path.name in allowed:
        return True
    
    if "config" in file_path.parts and file_path.suffix == ".yml":
        return True
    
    return False
```

コマンド内で呼び出し：

```python
@app_commands.command(name="edit")
async def edit_command(self, interaction, file_path: str):
    full_path = sanitize_path(file_path)
    
    if not is_allowed_file(full_path):
        await interaction.response.send_message("❌ このファイルは編集不可です")
        return
    
    # 以下、通常の処理...
```

---

## 本番運用のチェックリスト

- [ ] Bot Token を `.env` で安全に管理（`.gitignore` に追加）
- [ ] `ADMIN_USER_ID` が正しく設定されている
- [ ] `APPROVAL_CHANNEL_ID` が正しく設定されている
- [ ] Minecraft サーバーとの権限設定が完了
- [ ] systemd または pm2 で自動起動が設定済み
- [ ] ログローテーション設定（journalctl または pm2 ログ）
- [ ] 定期的にバックアップを確認
- [ ] テストで /edit と /new が正常に動作することを確認

---

## サポート

バグ報告や機能リクエストがあれば、コードのコメント部分を参考に実装してください！

Happy File Managing! 🚀
