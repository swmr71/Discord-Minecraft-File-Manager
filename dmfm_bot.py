import os
import difflib
import asyncio
import json
import aiohttp
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List

import discord
from discord import app_commands
from discord.ext import commands

# ================== 設定セクション ==================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "") # 空文字でも環境から提供される前提
ROOT_DIR = os.getenv("MINECRAFT_ROOT", "/home/minecraft/server")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "1218055725352484904"))
APPROVAL_CHANNEL_ID = int(os.getenv("APPROVAL_CHANNEL_ID", "0"))

BACKUP_SUFFIX = ".bak"

# ================== AI レビュー関数 (Gemini API) ==================
async def get_ai_review(filename: str, original: str, modified: str, diff: str) -> dict:
    """
    Gemini APIを使用して、ファイルの変更を検証する。
    全文とdiffの両方を送り、構造化データ(JSON)で結果を受け取る。
    """
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent?key={GEMINI_API_KEY}"
    
    system_prompt = """
    あなたはマインクラフトサーバー管理とセキュリティの専門家です。
    ユーザーから提出された「ファイル変更前後の全文」と「差分(diff)」を分析し、JSON形式で回答してください。
    
    チェック項目:
    1. 構文エラーがないか（YAML, JSON, Propertiesなど）
    2. セキュリティリスク（権限設定の悪化、不正なパス指定など）
    3. サーバー負荷への影響
    4. 変更意図の正当性

    応答は必ず以下のJSON形式にしてください:
    {
        "status": "safe" | "warning" | "danger",
        "summary": "変更内容の1行要約",
        "details": ["ポイント1", "ポイント2"],
        "critical_issue": "重大な問題がある場合は記述、なければnull"
    }
    """

    user_query = f"""
    ファイル名: {filename}

    --- 変更前の全文 ---
    {original if original else "(新規作成)"}

    --- 変更後の全文 ---
    {modified}

    --- 差分(diff) ---
    {diff}
    """

    payload = {
        "contents": [{"parts": [{"text": user_query}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "generationConfig": {
            "responseMimeType": "application/json"
        }
    }

    # 指数関数的バックオフによるリトライ
    for i in range(5):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        text = result['candidates'][0]['content']['parts'][0]['text']
                        return json.loads(text)
                    elif resp.status == 429: # Rate Limit
                        await asyncio.sleep(2 ** i)
                    else:
                        break
        except Exception:
            await asyncio.sleep(2 ** i)
    
    return {
        "status": "warning",
        "summary": "AI解析に失敗しました",
        "details": ["手動での慎重な確認を推奨します"],
        "critical_issue": "API接続エラー"
    }

# ================== パス安全性チェック等 (既存関数) ==================
def sanitize_path(user_path: str) -> Optional[Path]:
    try:
        full_path = os.path.abspath(os.path.join(ROOT_DIR, user_path))
        root_abs = os.path.abspath(ROOT_DIR)
        if os.path.commonpath([full_path, root_abs]) != root_abs:
            return None
        return Path(full_path)
    except Exception:
        return None

def read_file(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding='utf-8')
    except Exception:
        return None

def write_file(path: Path, content: str) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return True
    except Exception:
        return False

def generate_diff(original: str, modified: str, filename: str) -> str:
    original_lines = original.splitlines(keepends=True)
    modified_lines = modified.splitlines(keepends=True)
    diff = ''.join(list(difflib.unified_diff(
        original_lines, modified_lines, fromfile=f"a/{filename}", tofile=f"b/{filename}", lineterm=''
    )))
    return diff if diff else "（変更なし）"

class PendingEdit:
    def __init__(self, user_id: int, file_path: Path, is_new: bool, original_content: str = ""):
        self.user_id = user_id
        self.file_path = file_path
        self.is_new = is_new
        self.original_content = original_content

# ================== Discord Bot ==================
class DMFMBot(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.pending_edits: Dict[int, PendingEdit] = {}
        self.user_cwd: Dict[int, str] = {}

    def get_cwd(self, user_id: int) -> str:
        return self.user_cwd.get(user_id, "/")

    @app_commands.command(name="edit", description="既存ファイルの編集")
    async def edit_command(self, interaction: discord.Interaction, file_path: str):
        await interaction.response.defer()
        full_path = sanitize_path(file_path)
        if not full_path or not full_path.is_file():
            await interaction.followup.send("❌ 無効なパスまたはファイルが見つかりません。")
            return
        
        content = read_file(full_path)
        self.pending_edits[interaction.user.id] = PendingEdit(interaction.user.id, full_path, False, content)
        
        msg = "📝 編集内容をリプライでアップロードしてください。"
        if len(content) > 1500:
            file_obj = discord.File(fp=__import__('io').StringIO(content), filename=full_path.name)
            await interaction.followup.send(msg, file=file_obj)
        else:
            await interaction.followup.send(f"{msg}\n```\n{content}\n```")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.reference: return
        pending = self.pending_edits.get(message.author.id)
        if not pending or not message.attachments: return
        
        try:
            attachment = message.attachments[0]
            new_content = (await attachment.read()).decode('utf-8')
            
            # 承認プロセスへ
            await self._approval_phase(message, pending, new_content)
            del self.pending_edits[message.author.id]
        except Exception as e:
            await message.reply(f"❌ エラー: {e}")

    async def _approval_phase(self, source_msg: discord.Message, pending: PendingEdit, modified: str):
        channel = self.bot.get_channel(APPROVAL_CHANNEL_ID)
        if not channel: return await source_msg.reply("❌ 承認チャンネルが未設定です。")
        
        rel_path = os.path.relpath(pending.file_path, ROOT_DIR)
        diff_text = generate_diff(pending.original_content, modified, rel_path)
        
        # 初期Embed送信（解析中）
        status_color = discord.Color.light_grey()
        embed = discord.Embed(title="🔍 AI解析中...", description=f"ファイル: `{rel_path}`", color=status_color)
        embed.add_field(name="👤 提案者", value=source_msg.author.mention)
        
        panel = await channel.send(embed=embed)
        await source_msg.reply("✅ AI解析を開始しました。承認を待ってください。")

        # AIレビュー実行 (全文+diff)
        review = await get_ai_review(rel_path, pending.original_content, modified, diff_text)
        
        # Embedの更新
        colors = {"safe": discord.Color.green(), "warning": discord.Color.orange(), "danger": discord.Color.red()}
        new_embed = discord.Embed(
            title=f"🔔 承認リクエスト: {rel_path}",
            color=colors.get(review['status'], discord.Color.blue())
        )
        new_embed.add_field(name="🤖 AI判定", value=f"**[{review['status'].upper()}]** {review['summary']}", inline=False)
        
        details = "\n".join([f"• {d}" for d in review['details']])
        new_embed.add_field(name="📝 レビュー詳細", value=details or "なし", inline=False)
        
        if review.get('critical_issue'):
            new_embed.add_field(name="⚠️ 重大な問題", value=review['critical_issue'], inline=False)

        # diffが長すぎる場合はAIの要約のみ表示し、全文は表示しない（文字数制限対策）
        display_diff = diff_text[:800] + "\n...(以下略)" if len(diff_text) > 800 else diff_text
        new_embed.add_field(name="📋 差分(一部)", value=f"```diff\n{display_diff}\n```", inline=False)
        
        view = ApprovalView(self.bot, pending.user_id, pending.file_path, modified)
        await panel.edit(embed=new_embed, view=view)

class ApprovalView(discord.ui.View):
    def __init__(self, bot, user_id, file_path, content):
        super().__init__(timeout=None)
        self.bot = bot
        self.user_id = user_id
        self.file_path = file_path
        self.content = content

    @discord.ui.button(label="承認", style=discord.ButtonStyle.green)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != ADMIN_USER_ID: return
        
        # バックアップと書き込み
        if Path(self.file_path).exists():
            Path(str(self.file_path) + BACKUP_SUFFIX).write_text(Path(self.file_path).read_text(encoding='utf-8'), encoding='utf-8')
        
        Path(self.file_path).write_text(self.content, encoding='utf-8')
        
        await interaction.response.edit_message(content="✅ **承認済み・反映完了**", view=None)
        user = await self.bot.fetch_user(self.user_id)
        await user.send(f"✅ `{self.file_path.name}` の変更が承認されました。")

    @discord.ui.button(label="却下", style=discord.ButtonStyle.red)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != ADMIN_USER_ID: return
        await interaction.response.edit_message(content="❌ **却下されました**", view=None)
        user = await self.bot.fetch_user(self.user_id)
        await user.send(f"❌ `{self.file_path.name}` の変更は却下されました。")

async def main():
    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="/", intents=intents)
    @bot.event
    async def on_ready():
        await bot.tree.sync()
        print(f"Logged in as {bot.user}")
    await bot.add_cog(DMFMBot(bot))
    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())

