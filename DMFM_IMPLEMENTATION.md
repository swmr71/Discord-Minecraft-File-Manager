# DMFM 実装解説 - セキュリティと設計

このドキュメントでは、DMFM の実装で最も重要な部分を詳しく解説します。

---

## 目次

1. [パス安全性チェック](#パス安全性チェック)
2. [Diff 生成と承認フロー](#diff-生成と承認フロー)
3. [ファイル操作のライフサイクル](#ファイル操作のライフサイクル)
4. [セキュリティベストプラクティス](#セキュリティベストプラクティス)

---

## パス安全性チェック

### 最重要: ディレクトリトラバーサル対策

DMFM は **ユーザーが ROOT_DIR を超えてアクセスすることを絶対に許しません**。

### 仕組み

```python
def sanitize_path(user_path: str) -> Optional[Path]:
    """
    入力: user_path = "/config/../../../etc/passwd"
    
    【ステップ 1】相対パスを絶対パスに変換
    full_path = os.path.abspath(os.path.join(ROOT_DIR, user_path))
    結果: "/home/minecraft/server/config/../../../etc/passwd"
    
    【ステップ 2】パスを正規化（.. や . を解決）
    os.path.abspath() の時点で正規化されるため:
    "/etc/passwd" になってしまう
    
    【ステップ 3】ROOT_DIR も正規化
    root_abs = "/home/minecraft/server"
    
    【ステップ 4】commonpath で共通部分を確認
    common = os.path.commonpath(["/etc/passwd", "/home/minecraft/server"])
    結果: "/" （ルートディレクトリが共通部分）
    
    【ステップ 5】共通部分が ROOT_DIR か確認
    common != root_abs → True
    → None を返す（エラー）
    """
```

### 具体例

| 入力パス | 判定 | 理由 |
|---------|------|------|
| `config/server.properties` | ✅ OK | ROOT_DIR 配下 |
| `./config/server.properties` | ✅ OK | 相対パスも OK |
| `/home/minecraft/server/config` | ✅ OK | 絶対パス指定も OK |
| `../../../etc/passwd` | ❌ NG | ROOT_DIR 外へのトラバーサル |
| `/etc/passwd` | ❌ NG | ROOT_DIR 外へのアクセス |
| `../../sensitive.txt` | ❌ NG | 親ディレクトリ超過 |

### なぜこれで安全か

1. **相対パス記号 (`..`) を解決**
   - `os.path.abspath()` は `..` と `.` を展開してから返す
   - したがって「相対パスの罠」が成立しない

2. **commonpath で範囲外を検出**
   - `commonpath()` は複数パスの共通な接頭辞を返す
   - ROOT_DIR と入力パスの共通部分が ROOT_DIR でなければ NG

3. **例外処理で予期しないエラーも捕捉**
   - パスの解析に失敗した場合は None を返す
   - ボットはエラーメッセージを表示して処理を中止

---

## Diff 生成と承認フロー

### difflib の活用

DMFM は Python の `difflib` を使って **unified diff** (Git-like) を生成します。

```python
def generate_diff(original: str, modified: str, filename: str) -> str:
    """
    例）
    original = "name=MyServer\nmotd=Hello"
    modified = "name=MyServer\nmotd=Hello World\nport=25565"
    
    出力:
    --- a/server.properties
    +++ b/server.properties
    @@ -1,2 +1,3 @@
     name=MyServer
    -motd=Hello
    +motd=Hello World
    +port=25565
    """
    original_lines = original.splitlines(keepends=True)
    modified_lines = modified.splitlines(keepends=True)
    
    diff_lines = list(difflib.unified_diff(
        original_lines, modified_lines,
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
        lineterm=''
    ))
    
    return ''.join(diff_lines)
```

### なぜ Diff か

1. **変更内容が一目瞭然**
   - `+` が追加行、`-` が削除行
   - 管理者が「どこが変わったか」を即座に判断できる

2. **誤った変更を防止**
   - 新規作成の場合も「全文表示」で確認
   - サーバー設定の不正な変更を事前に防止

3. **監査ログとしても機能**
   - Discord のメッセージ履歴 = ファイル変更の履歴

### 承認フロー（時系列）

```
【1】ユーザーが /edit コマンド
    ↓
【2】Bot がファイル内容を表示
    「このメッセージに返信でファイルをアップロード」
    ↓
【3】ユーザーが返信にファイルをアップロード
    ↓
【4】Bot が on_message で検出
    → difflib で差分を生成
    ↓
【5】管理者チャンネルに「承認パネル」を表示
    - 操作者ユーザーID
    - 対象ファイルパス
    - Diff（コードブロック表示）
    - 「✅ 承認」「❌ 却下」ボタン
    ↓
【6a】「承認」クリック
    - ファイル → .bak にバックアップ
    - 新しいファイル内容を書き込み
    - 承認チャンネルに「成功」メッセージ
    - ユーザーに DM で通知
    ↓
【6b】「却下」クリック
    - ファイルは変更しない
    - 承認チャンネルに「却下」メッセージ
    - ユーザーに DM で通知
```

### コード内での実装

```python
# ファイル読み込み時
self.pending_edits[interaction.user.id] = PendingEdit(
    user_id=interaction.user.id,
    file_path=full_path,
    is_new=False,
    original_content=original_content  # 元のファイル内容を保存
)

# リプライ受け取り時
file_content = (await attachment.read()).decode('utf-8')

# 承認フェーズへ移行
await self._approval_phase(
    message.author.id,
    pending.file_path,
    pending.is_new,
    pending.original_content,  # 元の内容
    file_content,              # 新しい内容
    message
)
```

---

## ファイル操作のライフサイクル

### /edit コマンドの場合

```
【初期状態】
  server.properties  (元のファイル)

【ユーザーが edit を実行】
↓
【Bot がメモリに保存】
  pending_edits[user_id] = {
    file_path: /home/minecraft/server/server.properties
    original_content: "..."
    is_new: False
  }

【ユーザーがリプライでアップロード】
  修正済み server.properties

【Bot が on_message で受け取る】
  modified_content = "..."

【承認パネル表示】
  diff: - motd=Old
        + motd=New

【管理者が「承認」クリック】
↓
【バックアップ作成】
  server.properties → server.properties.bak
  （既存の .bak があれば上書き）

【ファイル上書き】
  server.properties = modified_content

【状態】
  server.properties (新しい内容)
  server.properties.bak (元の内容)
```

### /new コマンドの場合

```
【初期状態】
  config/custom.yml (存在しない)

【ユーザーが new を実行】
↓
【Bot がメモリに保存】
  pending_edits[user_id] = {
    file_path: /home/minecraft/server/config/custom.yml
    original_content: ""  (空文字列)
    is_new: True
  }

【ユーザーがリプライでアップロード】
  新規ファイル内容

【Bot が on_message で受け取る】
  modified_content = "..."

【承認パネル表示】
  「新規ファイル作成」
  内容全文表示

【管理者が「承認」クリック】
↓
【親ディレクトリ作成】
  /home/minecraft/server/config/ が存在しなければ mkdir -p

【ファイル作成】
  config/custom.yml = modified_content

【状態】
  config/custom.yml (新規作成)
  （.bak は不要、新規だから元がない）
```

---

## セキュリティベストプラクティス

### 1. 管理者権限の厳格な確認

```python
@discord.ui.button(label="承認して反映")
async def approve_button(self, interaction, button):
    if interaction.user.id != ADMIN_USER_ID:
        await interaction.response.send_message(
            "❌ 管理者のみ実行可能",
            ephemeral=True  # 自分にだけ見える
        )
        return
```

**ポイント**:
- 複数の管理者に対応する場合は `ADMIN_USER_IDS` をリストに変更
- `ephemeral=True` で、エラーメッセージは操作者にだけ表示

### 2. バックアップの自動作成

```python
def backup_file(path: Path) -> bool:
    if not path.exists():
        return True  # 既存ファイルなければ OK
    
    backup_path = Path(str(path) + ".bak")
    path.replace(backup_path)  # 元のファイルを .bak に移動
    return True
```

**利点**:
- 万が一の誤変更も 1 ステップで復旧可能
- ディスク容量を無駄に使わない（最新 1 世代のみ）

### 3. ファイルエンコーディングの安全性

```python
def read_file(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding='latin-1')
        except:
            return None
```

**理由**:
- YAML や JSON は UTF-8 が標準だが、古いファイルは latin-1 かもしれない
- 読み込み失敗したら None を返して、無理には進まない

### 4. メモリ上のペンディング管理

```python
self.pending_edits: Dict[int, PendingEdit] = {}
```

**注意点**:
- Bot が再起動されるとメモリがクリアされる
- 長時間の編集待機（数時間）には向かない
- 本番運用なら、タイムアウト機能を追加推奨

タイムアウト実装例：

```python
async def cleanup_stale_edits(self):
    """30分以上の古いペンディングを削除"""
    import asyncio
    while True:
        await asyncio.sleep(60)  # 1分ごとにチェック
        now = datetime.now()
        for user_id, pending in list(self.pending_edits.items()):
            if (now - pending.timestamp).total_seconds() > 1800:  # 30分
                del self.pending_edits[user_id]
```

### 5. 権限エラーの適切なハンドリング

```python
try:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
except PermissionError:
    # Bot の実行ユーザーがこのディレクトリに書き込めない
    return False
except Exception as e:
    # その他のエラー（ディスク満杯など）
    return False
```

---

## 運用のコツ

### チェックリスト：新規ファイル作成時

- [ ] ファイルパスは存在しないか確認
- [ ] 親ディレクトリは存在するか確認
- [ ] ファイル形式は妥当か確認（.yml, .json など）
- [ ] 内容は正しい YAML/JSON か検証

### チェックリスト：既存ファイル編集時

- [ ] Diff の変更内容に不可思議な点がないか
- [ ] Minecraft サーバーが起動状態か確認（ファイルロック回避）
- [ ] 変更後に `reload` が必要か判定

### 監査ログとしての活用

Discord のメッセージ履歴を保存することで、以下が記録される：

- **誰が** (ユーザーID)
- **何を** (ファイルパス)
- **どう変更したか** (Diff)
- **いつ** (タイムスタンプ)
- **誰が承認したか** (管理者ユーザーID)

バックアップとセットで、完全な変更履歴管理が実現できます。

---

## トラブルシューティング：実装の視点から

### 症状：ファイルが上書きされない

**考えられる原因**:

1. **権限不足**
   ```bash
   ls -l /home/minecraft/server/config/
   # Bot 実行ユーザーが owner or group に w 権限があるか
   ```

2. **ファイルがロック中**
   ```bash
   lsof /home/minecraft/server/config/server.properties
   # Minecraft プロセスが開いている場合
   ```

3. **write_file() が False を返している**
   ```python
   # ログを出力して確認
   if not write_file(self.file_path, self.modified_content):
       print(f"❌ Write failed for {self.file_path}")
       await interaction.followup.send("❌ ファイル書き込み失敗")
   ```

### 症状：承認ボタンが反応しない

**考えられる原因**:

1. **ADMIN_USER_ID が一致していない**
   ```python
   print(f"Admin ID: {ADMIN_USER_ID}")
   print(f"Clicked by: {interaction.user.id}")
   # ログで確認
   ```

2. **チャンネルが見つからない**
   ```python
   approval_channel = self.bot.get_channel(APPROVAL_CHANNEL_ID)
   if approval_channel is None:
       print(f"❌ Channel {APPROVAL_CHANNEL_ID} not found")
   ```

### 症状：日本語が文字化けする

**原因**: ファイルの文字コードが UTF-8 でない

**対策**:

```python
# latin-1 以外もサポート（オプション）
encodings = ['utf-8', 'shift-jis', 'cp932', 'latin-1']
for enc in encodings:
    try:
        return path.read_text(encoding=enc)
    except:
        continue
```

---

## まとめ

DMFM は以下を重視して設計されています：

✅ **セキュリティ**: ディレクトリトラバーサル対策を完全に実装
✅ **使いやすさ**: スラッシュコマンド + ボタン UI で直感的操作
✅ **監査性**: Discord 履歴で完全な変更管理
✅ **信頼性**: 承認フローで誤操作を防止

本番運用前に、上記のセキュリティチェックリストを一通り実施してくださいね！

Happy Coding! 🚀
