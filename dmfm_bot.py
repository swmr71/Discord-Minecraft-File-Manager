"""
Discord Minecraft File Manager (DMFM)
マインクラフトサーバーのファイルをDiscord経由で管理・編集・新規作成するBot。
GitHubのPRフロー的な「提案→確認→承認→反映」をDiscordで完結。
"""

import os
import difflib
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Tuple

import discord
from discord import app_commands
from discord.ext import commands

# ================== 設定セクション ==================
# 環境変数から読み込む（本番環境では .env ファイル利用推奨）
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "your-token-here")
ROOT_DIR = os.getenv("MINECRAFT_ROOT", "/home/minecraft/server")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "1218055725352484904"))  # りゅうのユーザーID
APPROVAL_CHANNEL_ID = int(os.getenv("APPROVAL_CHANNEL_ID", "0"))  # 後で教えてもらうID

# バックアップファイルのサフィックス
BACKUP_SUFFIX = ".bak"

# ================== パス安全性チェック関数 ==================
def sanitize_path(user_path: str) -> Optional[Path]:
    """
    ユーザーが指定したパスの安全性をチェックし、安全であれば Path オブジェクトを返す。
    
    【セキュリティの詳細】
    1. 相対パス（"../config" など）の場合も、ROOT_DIR を基準に絶対パスに変換
    2. os.path.abspath で正規化（シンボリックリンク追跡）
    3. os.path.commonpath で ROOT_DIR 配下であることを確認
       → ROOT_DIR より上のディレクトリへのアクセスを完全にブロック
    4. 問題があれば None を返す
    
    【例】
    - user_path = "config/server.properties" → OK （ROOT_DIR/config/server.properties）
    - user_path = "../../../etc/passwd"      → NG （ROOT_DIR 外）
    - user_path = "/etc/passwd"              → NG （絶対パス外部アクセス）
    """
    try:
        # ROOT_DIR を基準に、相対パスを絶対パスに変換
        full_path = os.path.abspath(os.path.join(ROOT_DIR, user_path))
        
        # ROOT_DIR も絶対パスに正規化
        root_abs = os.path.abspath(ROOT_DIR)
        
        # commonpath でパスの共通部分を確認
        # 万が一シンボリックリンクで逃げようとしても、commonpath は追跡済みパスで判定
        common = os.path.commonpath([full_path, root_abs])
        
        if common != root_abs:
            # 共通部分が ROOT_DIR でない = ROOT_DIR 外へのアクセス試行
            return None
        
        return Path(full_path)
    
    except (ValueError, OSError):
        # パスが無効、または ROOT_DIR と共通部分がない
        return None


# ================== ファイル操作ヘルパー関数 ==================
def read_file(path: Path) -> Optional[str]:
    """ファイルを読み込む（文字化けに強い UTF-8 優先）"""
    try:
        return path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        # UTF-8 で失敗した場合は latin-1 を試す
        try:
            return path.read_text(encoding='latin-1')
        except Exception:
            return None
    except Exception:
        return None


def write_file(path: Path, content: str) -> bool:
    """ファイルを書き込む"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return True
    except Exception:
        return False


def backup_file(path: Path) -> bool:
    """既存ファイルを .bak にバックアップ（最新1世代のみ）"""
    if not path.exists():
        return True  # 既存ファイルがなければバックアップは不要
    
    try:
        backup_path = Path(str(path) + BACKUP_SUFFIX)
        # 既存の .bak があれば上書き
        path.replace(backup_path)
        return True
    except Exception:
        return False


def generate_diff(original: str, modified: str, filename: str) -> str:
    """
    difflib を使って diff を生成。
    GitHub の diff のような見た目で、コードブロック用に整形。
    """
    original_lines = original.splitlines(keepends=True)
    modified_lines = modified.splitlines(keepends=True)
    
    diff_lines = list(difflib.unified_diff(
        original_lines, modified_lines,
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
        lineterm=''
    ))
    
    if not diff_lines:
        return "（変更なし）"
    
    return ''.join(diff_lines)


# ================== Pending Edit 管理 ==================
class PendingEdit:
    """編集待機状態を管理するクラス"""
    def __init__(self, user_id: int, file_path: Path, is_new: bool, original_content: str = ""):
        self.user_id = user_id
        self.file_path = file_path
        self.is_new = is_new
        self.original_content = original_content
        self.timestamp = datetime.now()


# ================== Discord Bot ==================
class DMFMBot(commands.Cog):
    """DMFM のメインロジック"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.pending_edits: Dict[int, PendingEdit] = {}  # user_id -> PendingEdit
        self.user_cwd: Dict[int, str] = {}  # user_id -> current working directory
    
    def get_cwd(self, user_id: int) -> str:
        """ユーザーのカレントディレクトリを取得（デフォルトは "/"）"""
        return self.user_cwd.get(user_id, "/")
    
    def set_cwd(self, user_id: int, path: str) -> None:
        """ユーザーのカレントディレクトリを設定"""
        self.user_cwd[user_id] = path
    
    # ===================== /ls コマンド =====================
    @app_commands.command(name="ls", description="ディレクトリ内のファイル・フォルダ一覧を表示")
    @app_commands.describe(path="表示したいパス（指定なしでカレントディレクトリ）")
    async def ls_command(self, interaction: discord.Interaction, path: Optional[str] = None):
        """
        /ls [path]
        
        例）
        /ls              → カレントディレクトリを表示
        /ls /config      → /config フォルダを表示
        /ls plugins      → 相対パス plugins/ を表示
        """
        await interaction.response.defer(thinking=True)
        
        # パスを決定
        if path is None:
            path = self.get_cwd(interaction.user.id)
        
        # セキュリティチェック
        full_path = sanitize_path(path)
        if full_path is None:
            await interaction.followup.send("❌ **エラー**: パスが無効です（ROOT_DIR 外へのアクセスは不可）")
            return
        
        if not full_path.exists():
            await interaction.followup.send(f"❌ **エラー**: パス `{path}` が存在しません")
            return
        
        if not full_path.is_dir():
            await interaction.followup.send(f"❌ **エラー**: `{path}` はファイルです")
            return
        
        # ディレクトリ内容を列挙
        try:
            items = sorted(full_path.iterdir())
        except PermissionError:
            await interaction.followup.send("❌ **エラー**: このディレクトリにアクセスする権限がありません")
            return
        
        if not items:
            embed = discord.Embed(
                title=f"📂 {path}",
                description="（空のディレクトリ）",
                color=discord.Color.blue()
            )
        else:
            # ファイル・フォルダを分類
            folders = [item for item in items if item.is_dir()]
            files = [item for item in items if item.is_file()]
            
            description_parts = []
            
            if folders:
                description_parts.append("**📁 フォルダ:**")
                for folder in folders[:20]:  # 最大20個表示
                    description_parts.append(f"  `{folder.name}/`")
            
            if files:
                description_parts.append("\n**📄 ファイル:**")
                for file in files[:20]:  # 最大20個表示
                    description_parts.append(f"  `{file.name}`")
            
            if len(folders) + len(files) > 40:
                description_parts.append(f"\n... 他 {len(folders) + len(files) - 40} 件")
            
            embed = discord.Embed(
                title=f"📂 {path}",
                description="\n".join(description_parts),
                color=discord.Color.blue()
            )
            embed.set_footer(text=f"フォルダ: {len(folders)} 個、ファイル: {len(files)} 個")
        
        await interaction.followup.send(embed=embed)
    
    # ===================== /cd コマンド =====================
    @app_commands.command(name="cd", description="カレントディレクトリを変更")
    @app_commands.describe(path="移動先パス")
    async def cd_command(self, interaction: discord.Interaction, path: str):
        """
        /cd <path>
        
        ユーザーごとのカレントディレクトリをメモリに保持。
        例）
        /cd /plugins     → /plugins に移動
        /cd ..           → 親ディレクトリに移動
        """
        await interaction.response.defer(thinking=True)
        
        # パスを確認
        full_path = sanitize_path(path)
        if full_path is None:
            await interaction.followup.send("❌ **エラー**: パスが無効です")
            return
        
        if not full_path.exists():
            await interaction.followup.send(f"❌ **エラー**: `{path}` が存在しません")
            return
        
        if not full_path.is_dir():
            await interaction.followup.send(f"❌ **エラー**: `{path}` はファイルです")
            return
        
        # カレントディレクトリを設定
        # ROOT_DIR からの相対パスを保存
        relative_path = os.path.relpath(full_path, ROOT_DIR)
        if relative_path == ".":
            relative_path = "/"
        else:
            relative_path = "/" + relative_path.replace("\\", "/")
        
        self.set_cwd(interaction.user.id, relative_path)
        
        await interaction.followup.send(
            f"✅ カレントディレクトリを `{relative_path}` に変更しました"
        )
    
    # ===================== /edit コマンド =====================
    @app_commands.command(name="edit", description="既存ファイルの編集")
    @app_commands.describe(file_path="編集するファイルパス")
    async def edit_command(self, interaction: discord.Interaction, file_path: str):
        """
        /edit <file_path>
        
        フロー：
        1. ファイルを読み込んで Discord に添付送信
        2. ユーザーを「編集待機状態」に設定
        3. ユーザーがリプライでファイルをアップロードするまで待つ
        4. 受信後、承認フェーズへ
        """
        await interaction.response.defer(thinking=True)
        
        # セキュリティチェック
        full_path = sanitize_path(file_path)
        if full_path is None:
            await interaction.followup.send("❌ **エラー**: パスが無効です")
            return
        
        if not full_path.exists():
            await interaction.followup.send(f"❌ **エラー**: ファイル `{file_path}` が存在しません")
            return
        
        if not full_path.is_file():
            await interaction.followup.send(f"❌ **エラー**: `{file_path}` はフォルダです")
            return
        
        # ファイルを読み込み
        original_content = read_file(full_path)
        if original_content is None:
            await interaction.followup.send("❌ **エラー**: ファイルを読み込めません")
            return
        
        # 編集待機状態を記録
        self.pending_edits[interaction.user.id] = PendingEdit(
            user_id=interaction.user.id,
            file_path=full_path,
            is_new=False,
            original_content=original_content
        )
        
        # ファイル内容を送信（デカいファイルの場合は圧縮）
        if len(original_content) > 1900:
            # テキスト添付として送信
            file_obj = discord.File(
                fp=__import__('io').StringIO(original_content),
                filename=full_path.name
            )
            await interaction.followup.send(
                f"📝 **編集対象**: `{file_path}`\n\n"
                f"下のファイルをダウンロードして編集し、このメッセージにリプライで新しいファイルをアップロードしてください。",
                file=file_obj
            )
        else:
            # コードブロックで送信
            embed = discord.Embed(
                title=f"📝 編集対象: {file_path}",
                description=f"```\n{original_content[:1900]}\n```" if original_content else "（空ファイル）",
                color=discord.Color.orange()
            )
            await interaction.followup.send(
                "下のファイルを編集して、**このメッセージにリプライ**で新しいファイルをアップロードしてください。",
                embed=embed
            )
    
    # ===================== /new コマンド =====================
    @app_commands.command(name="new", description="新規ファイルを作成")
    @app_commands.describe(file_path="作成するファイルパス")
    async def new_command(self, interaction: discord.Interaction, file_path: str):
        """
        /new <file_path>
        
        フロー：
        1. 指定パスが既存でないか確認
        2. 親ディレクトリが存在しなければ作成準備
        3. ユーザーにファイルアップロードを促す
        4. 受信後、承認フェーズへ
        """
        await interaction.response.defer(thinking=True)
        
        # セキュリティチェック
        full_path = sanitize_path(file_path)
        if full_path is None:
            await interaction.followup.send("❌ **エラー**: パスが無効です")
            return
        
        if full_path.exists():
            await interaction.followup.send(f"❌ **エラー**: ファイル `{file_path}` は既に存在します")
            return
        
        # 親ディレクトリが存在するか確認（存在しなければ後で作成）
        if not full_path.parent.exists():
            try:
                # 実際には mkdir しない（承認後に行う）
                pass
            except PermissionError:
                await interaction.followup.send("❌ **エラー**: 親ディレクトリへのアクセス権がありません")
                return
        
        # 新規作成待機状態を記録
        self.pending_edits[interaction.user.id] = PendingEdit(
            user_id=interaction.user.id,
            file_path=full_path,
            is_new=True,
            original_content=""
        )
        
        embed = discord.Embed(
            title=f"✨ 新規ファイル作成",
            description=f"**パス**: `{file_path}`\n\n"
                        f"ファイルの内容をアップロードしてください。\n"
                        f"このメッセージにリプライで新しいファイルをアップロードしてください。",
            color=discord.Color.green()
        )
        await interaction.followup.send(embed=embed)
    
    # ===================== メッセージリプライの監視 =====================
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """
        ユーザーからのリプライ（ファイルアップロード）を監視。
        /edit または /new の後に、このメッセージがリプライとして届く。
        """
        if message.author.bot:
            return
        
        # このユーザーが編集待機状態か確認
        pending = self.pending_edits.get(message.author.id)
        if pending is None:
            return
        
        # メッセージが返信か、かつ待機状態のコマンドメッセージへのリプライか確認
        if message.reference is None:
            return
        
        # ファイルがアップロードされているか確認
        if not message.attachments:
            await message.reply("❌ ファイルがアップロードされていません。ファイルをアップロードしてください。")
            return
        
        # 最初のファイルを取得
        attachment = message.attachments[0]
        
        try:
            # ファイル内容をダウンロード
            file_content = (await attachment.read()).decode('utf-8')
        except UnicodeDecodeError:
            await message.reply("❌ ファイルがUTF-8でエンコードされていません。UTF-8で保存してください。")
            return
        except Exception as e:
            await message.reply(f"❌ ファイルの読み込みに失敗: {e}")
            return
        
        # 承認フェーズへ移行
        await self._approval_phase(
            message.author.id,
            pending.file_path,
            pending.is_new,
            pending.original_content,
            file_content,
            message
        )
        
        # 編集待機状態を削除
        del self.pending_edits[message.author.id]
    
    # ===================== 承認フェーズ =====================
    async def _approval_phase(
        self,
        user_id: int,
        file_path: Path,
        is_new: bool,
        original_content: str,
        modified_content: str,
        source_message: discord.Message
    ):
        """
        承認パネルを表示する。
        管理者のみが承認・却下ボタンを操作可能。
        """
        # 承認チャンネルを取得
        approval_channel = self.bot.get_channel(APPROVAL_CHANNEL_ID)
        if approval_channel is None:
            await source_message.reply(
                "❌ **エラー**: 承認チャンネルが見つかりません。"
                "管理者に APPROVAL_CHANNEL_ID の設定を確認させてください。"
            )
            return
        
        # diff を生成
        relative_path = os.path.relpath(file_path, ROOT_DIR)
        if relative_path == ".":
            relative_path = "/"
        else:
            relative_path = "/" + relative_path.replace("\\", "/")
        
        if is_new:
            diff_text = f"【新規ファイル作成】\n\n{modified_content[:1900]}"
        else:
            diff_text = generate_diff(original_content, modified_content, relative_path)
        
        # Embed を作成
        embed = discord.Embed(
            title="🔔 ファイル変更の承認が必要です",
            color=discord.Color.red()
        )
        embed.add_field(name="👤 操作者", value=f"<@{user_id}>", inline=False)
        embed.add_field(name="📁 ファイルパス", value=f"`{relative_path}`", inline=False)
        embed.add_field(
            name="📋 差分",
            value=f"```diff\n{diff_text[:800]}\n```" if len(diff_text) < 1900 else f"```diff\n{diff_text[:800]}\n...\n```",
            inline=False
        )
        embed.set_footer(text=f"リクエストID: {user_id}")
        
        # ボタンを作成
        view = ApprovalView(
            bot=self.bot,
            user_id=user_id,
            file_path=file_path,
            is_new=is_new,
            modified_content=modified_content,
            original_content=original_content,
            source_message=source_message
        )
        
        # 承認パネルを送信
        await approval_channel.send(embed=embed, view=view)
        
        await source_message.reply(
            "✅ 変更提案が送信されました。管理者の承認を待ってください。"
        )


# ================== 承認ボタン ==================
class ApprovalView(discord.ui.View):
    """承認・却下ボタン"""
    
    def __init__(
        self,
        bot: commands.Bot,
        user_id: int,
        file_path: Path,
        is_new: bool,
        modified_content: str,
        original_content: str,
        source_message: discord.Message
    ):
        super().__init__(timeout=None)  # タイムアウトなし
        self.bot = bot
        self.user_id = user_id
        self.file_path = file_path
        self.is_new = is_new
        self.modified_content = modified_content
        self.original_content = original_content
        self.source_message = source_message
    
    @discord.ui.button(label="承認して反映", style=discord.ButtonStyle.green)
    async def approve_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """承認ボタン（緑）"""
        # 管理者か確認
        if interaction.user.id != ADMIN_USER_ID:
            await interaction.response.send_message(
                "❌ **エラー**: この操作は管理者のみ実行できます。",
                ephemeral=True
            )
            return
        
        await interaction.response.defer(thinking=True)
        
        try:
            # バックアップ（最新1世代のみ）
            backup_file(self.file_path)
            
            # 新しいファイルを書き込み
            if not write_file(self.file_path, self.modified_content):
                await interaction.followup.send("❌ **エラー**: ファイルの書き込みに失敗しました。")
                return
            
            # 成功メッセージ
            relative_path = os.path.relpath(self.file_path, ROOT_DIR)
            if relative_path == ".":
                relative_path = "/"
            else:
                relative_path = "/" + relative_path.replace("\\", "/")
            
            embed = discord.Embed(
                title="✅ ファイルが反映されました",
                description=f"📁 `{relative_path}`",
                color=discord.Color.green()
            )
            embed.add_field(name="👤 承認者", value=f"<@{interaction.user.id}>", inline=False)
            embed.add_field(name="⏰ 時刻", value=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), inline=False)
            
            await interaction.followup.send(embed=embed)
            
            # 操作者に DM で通知
            try:
                user = await self.bot.fetch_user(self.user_id)
                await user.send(f"✅ あなたの変更が承認され、`{relative_path}` に反映されました。")
            except:
                pass
        
        except Exception as e:
            await interaction.followup.send(f"❌ **エラー**: {e}")
    
    @discord.ui.button(label="却下", style=discord.ButtonStyle.red)
    async def reject_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """却下ボタン（赤）"""
        # 管理者か確認
        if interaction.user.id != ADMIN_USER_ID:
            await interaction.response.send_message(
                "❌ **エラー**: この操作は管理者のみ実行できます。",
                ephemeral=True
            )
            return
        
        await interaction.response.defer(thinking=True)
        
        # 却下メッセージ
        relative_path = os.path.relpath(self.file_path, ROOT_DIR)
        if relative_path == ".":
            relative_path = "/"
        else:
            relative_path = "/" + relative_path.replace("\\", "/")
        
        embed = discord.Embed(
            title="❌ 変更が却下されました",
            description=f"📁 `{relative_path}`",
            color=discord.Color.red()
        )
        embed.add_field(name="👤 却下者", value=f"<@{interaction.user.id}>", inline=False)
        embed.add_field(name="⏰ 時刻", value=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), inline=False)
        
        await interaction.followup.send(embed=embed)
        
        # 操作者に DM で通知
        try:
            user = await self.bot.fetch_user(self.user_id)
            await user.send(f"❌ あなたの変更が却下されました（`{relative_path}`）")
        except:
            pass


# ================== Bot 初期化 ==================
async def main():
    """Bot の起動"""
    # Intents の設定
    intents = discord.Intents.default()
    intents.message_content = True  # メッセージ内容の読み込みを許可
    intents.guilds = True
    intents.guild_messages = True
    
    bot = commands.Bot(command_prefix="/", intents=intents)
    
    @bot.event
    async def on_ready():
        print(f"✅ Bot が起動しました。ユーザー: {bot.user}")
        # スラッシュコマンドを同期
        try:
            synced = await bot.tree.sync()
            print(f"✅ {len(synced)} 個のスラッシュコマンドを同期しました")
        except Exception as e:
            print(f"❌ コマンド同期エラー: {e}")
    
    # Cog を追加
    await bot.add_cog(DMFMBot(bot))
    
    # Bot を起動
    await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    # ROOT_DIR が存在するか確認
    if not os.path.exists(ROOT_DIR):
        print(f"❌ エラー: ROOT_DIR '{ROOT_DIR}' が存在しません")
        exit(1)
    
    print(f"""
╔════════════════════════════════════════════════════════╗
║  Discord Minecraft File Manager (DMFM) Bot            ║
║  ファイルパス安全性チェック: ✅ 有効                  ║
║  バックアップ世代: 1 世代（最新のみ）                 ║
╚════════════════════════════════════════════════════════╝

📁 ROOT_DIR: {ROOT_DIR}
👤 Admin ID: {ADMIN_USER_ID}
📢 Approval Channel: {APPROVAL_CHANNEL_ID} (未設定の場合は 0)

⏳ Bot 起動中...
    """)
    
    asyncio.run(main())
