"""
Discord AI 자동화 봇
discord.py + Claude API 연동

설치:
    pip install discord.py anthropic python-dotenv aiohttp

.env 파일:
    DISCORD_TOKEN=your_bot_token
    ANTHROPIC_API_KEY=sk-ant-...
    ALLOWED_CHANNELS=채널ID1,채널ID2  (선택)
    BOT_PREFIX=!

Discord Developer Portal:
    - Bot > MESSAGE CONTENT INTENT 활성화
    - OAuth2 > bot 스코프 선택 후 서버 초대
"""

import os, asyncio, json, re
from datetime import datetime, timedelta
from collections import defaultdict
import discord
from discord.ext import commands, tasks
from discord import app_commands
import anthropic
from dotenv import load_dotenv

load_dotenv()

# ─── 설정 ─────────────────────────────────────────────────────────────────────
DISCORD_TOKEN  = os.getenv("DISCORD_TOKEN")
ANTHROPIC_KEY  = os.getenv("ANTHROPIC_API_KEY")
BOT_PREFIX     = os.getenv("BOT_PREFIX", "!")
ALLOWED_CH     = [int(x) for x in os.getenv("ALLOWED_CHANNELS","").split(",") if x.strip()]

claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# 봇 설정
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents, help_command=None)

# 대화 히스토리 (유저별)
conversation_history: dict[int, list] = defaultdict(list)
MAX_HISTORY = 10  # 유저당 최대 대화 기록 수

# Rate limiting (유저당 분당 최대 요청 수)
rate_limit: dict[int, list] = defaultdict(list)
MAX_RPM = 5

# ─── 헬퍼 ─────────────────────────────────────────────────────────────────────
def is_rate_limited(user_id: int) -> bool:
    now = datetime.now()
    cutoff = now - timedelta(minutes=1)
    rate_limit[user_id] = [t for t in rate_limit[user_id] if t > cutoff]
    if len(rate_limit[user_id]) >= MAX_RPM:
        return True
    rate_limit[user_id].append(now)
    return False

def is_allowed_channel(channel_id: int) -> bool:
    return not ALLOWED_CH or channel_id in ALLOWED_CH

async def call_claude(user_id: int, user_message: str, system_prompt: str = None) -> str:
    """Claude API 호출 (대화 히스토리 포함)"""
    history = conversation_history[user_id]
    history.append({"role": "user", "content": user_message})
    # 히스토리 제한
    if len(history) > MAX_HISTORY * 2:
        history = history[-MAX_HISTORY * 2:]
        conversation_history[user_id] = history

    kwargs = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 800,
        "messages": history
    }
    if system_prompt:
        kwargs["system"] = system_prompt

    response = claude.messages.create(**kwargs)
    reply = response.content[0].text
    conversation_history[user_id].append({"role": "assistant", "content": reply})
    return reply

async def send_long(ctx_or_channel, text: str):
    """2000자 초과 시 분할 전송"""
    if len(text) <= 1990:
        await ctx_or_channel.send(text)
    else:
        parts = [text[i:i+1990] for i in range(0, len(text), 1990)]
        for p in parts:
            await ctx_or_channel.send(p)
            await asyncio.sleep(0.5)

# ─── 이벤트 ───────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"✅ 봇 로그인: {bot.user} (ID: {bot.user.id})")
    print(f"   서버 수: {len(bot.guilds)}")
    try:
        synced = await bot.tree.sync()
        print(f"   슬래시 커맨드 동기화: {len(synced)}개")
    except Exception as e:
        print(f"   커맨드 동기화 실패: {e}")
    daily_summary.start()


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    await bot.process_commands(message)

    # 봇 멘션 시 AI 응답
    if bot.user.mentioned_in(message) and not message.mention_everyone:
        if not is_allowed_channel(message.channel.id):
            return
        if is_rate_limited(message.author.id):
            await message.reply("⏱️ 너무 빠르게 요청하고 있어요! 잠시 후 다시 시도해주세요.")
            return

        user_text = message.content.replace(f"<@{bot.user.id}>", "").strip()
        if not user_text:
            await message.reply("안녕하세요! 무엇을 도와드릴까요? 💬")
            return

        async with message.channel.typing():
            system = f"""당신은 '{message.guild.name if message.guild else 'DM'}' Discord 서버의 친절한 AI 보조자입니다.
사용자 이름: {message.author.display_name}
현재 채널: #{message.channel.name if hasattr(message.channel,'name') else 'DM'}
응답은 Discord 마크다운을 활용해 명확하게 작성하세요. 500자 이내로 답변하세요."""
            try:
                reply = await call_claude(message.author.id, user_text, system)
                await message.reply(reply[:1990])
            except Exception as e:
                await message.reply(f"❌ 오류가 발생했습니다: `{str(e)[:100]}`")

    # 키워드 자동 반응
    content_lower = message.content.lower()
    keyword_responses = {
        "안녕": "👋 안녕하세요! 무엇을 도와드릴까요?",
        "도움말": f"📚 `{BOT_PREFIX}help` 명령어로 도움말을 확인하세요!",
        "감사": "😊 천만에요!",
    }
    for kw, resp in keyword_responses.items():
        if kw in content_lower and not message.content.startswith(BOT_PREFIX):
            if kw == "안녕" and message.guild:  # 서버에서만 반응
                await message.add_reaction("👋")
            break


@bot.event
async def on_member_join(member: discord.Member):
    """새 멤버 환영 메시지 (AI 생성)"""
    channel = member.guild.system_channel
    if not channel:
        return
    try:
        welcome_msg = await call_claude(
            0,  # 시스템 메시지
            f"Discord 서버 '{member.guild.name}'에 '{member.display_name}'이라는 새 멤버가 가입했습니다. 따뜻하고 짧은 환영 메시지를 2~3문장으로 작성해주세요."
        )
        embed = discord.Embed(
            title=f"🎉 {member.display_name}님 환영합니다!",
            description=welcome_msg,
            color=discord.Color.purple()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        await channel.send(embed=embed)
    except:
        await channel.send(f"🎉 **{member.display_name}**님, 환영합니다!")

# ─── 프리픽스 커맨드 ──────────────────────────────────────────────────────────
@bot.command(name="chat", aliases=["c", "ai"])
async def chat_cmd(ctx, *, message: str):
    """💬 AI와 대화 (!chat 메시지)"""
    if is_rate_limited(ctx.author.id):
        await ctx.reply("⏱️ 잠시 후 다시 시도해주세요!")
        return
    async with ctx.typing():
        reply = await call_claude(ctx.author.id, message)
        await send_long(ctx, f"**{ctx.author.display_name}** → {reply}")


@bot.command(name="clear", aliases=["reset"])
async def clear_history(ctx):
    """🔄 대화 기록 초기화"""
    conversation_history[ctx.author.id] = []
    await ctx.reply("✅ 대화 기록이 초기화됐습니다!")


@bot.command(name="summarize", aliases=["sum"])
async def summarize_cmd(ctx, count: int = 20):
    """📝 채널 최근 메시지 요약"""
    if count > 50: count = 50
    async with ctx.typing():
        messages = []
        async for msg in ctx.channel.history(limit=count+1):
            if not msg.author.bot:
                messages.append(f"{msg.author.display_name}: {msg.content[:200]}")
        messages.reverse()
        if not messages:
            await ctx.reply("요약할 메시지가 없습니다.")
            return
        text = "\n".join(messages)
        summary = await call_claude(
            ctx.author.id,
            f"아래 Discord 대화를 3~5문장으로 요약해주세요:\n{text}"
        )
        embed = discord.Embed(title=f"📝 최근 {count}개 메시지 요약", description=summary, color=0x9B5CFF)
        await ctx.send(embed=embed)


@bot.command(name="translate", aliases=["tr"])
async def translate_cmd(ctx, lang: str, *, text: str):
    """🌐 텍스트 번역 (!translate ko/en/ja 텍스트)"""
    lang_names = {"ko": "한국어", "en": "영어", "ja": "일본어", "zh": "중국어", "es": "스페인어"}
    lang_name = lang_names.get(lang, lang)
    async with ctx.typing():
        result = await call_claude(
            ctx.author.id,
            f"다음 텍스트를 {lang_name}로 번역해주세요. 번역본만 출력하세요:\n{text}"
        )
        embed = discord.Embed(title=f"🌐 {lang_name} 번역", description=f"**원문:** {text[:200]}\n**번역:** {result}", color=0x00E5FF)
        await ctx.send(embed=embed)


@bot.command(name="analyze")
async def analyze_cmd(ctx, *, text: str = None):
    """🔍 텍스트 감정/의도 분석"""
    if not text and ctx.message.reference:
        ref = await ctx.channel.fetch_message(ctx.message.reference.message_id)
        text = ref.content
    if not text:
        await ctx.reply("분석할 텍스트를 입력하거나 메시지를 reply해주세요.")
        return
    async with ctx.typing():
        analysis = await call_claude(
            ctx.author.id,
            f"다음 텍스트의 감정, 의도, 주요 키워드를 분석해주세요:\n{text}"
        )
        embed = discord.Embed(title="🔍 텍스트 분석", color=0xF59E0B)
        embed.add_field(name="원문", value=text[:500], inline=False)
        embed.add_field(name="분석 결과", value=analysis[:1000], inline=False)
        await ctx.send(embed=embed)


@bot.command(name="poll")
async def poll_cmd(ctx, question: str, *options):
    """📊 투표 생성 (!poll "질문" "옵션1" "옵션2" ...)"""
    if len(options) < 2:
        await ctx.reply("옵션을 2개 이상 입력해주세요!")
        return
    emojis = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    desc = "\n".join([f"{emojis[i]} {opt}" for i, opt in enumerate(options[:10])])
    embed = discord.Embed(title=f"📊 {question}", description=desc, color=0x10B981)
    embed.set_footer(text=f"투표 by {ctx.author.display_name}")
    msg = await ctx.send(embed=embed)
    for i in range(min(len(options), 10)):
        await msg.add_reaction(emojis[i])


@bot.command(name="help")
async def help_cmd(ctx):
    """봇 도움말"""
    embed = discord.Embed(title="🤖 AI 봇 도움말", color=0x9B5CFF)
    commands_list = [
        (f"`{BOT_PREFIX}chat [메시지]`", "AI와 대화"),
        (f"`{BOT_PREFIX}clear`", "대화 기록 초기화"),
        (f"`{BOT_PREFIX}summarize [개수]`", "채널 메시지 요약"),
        (f"`{BOT_PREFIX}translate [언어] [텍스트]`", "텍스트 번역"),
        (f"`{BOT_PREFIX}analyze [텍스트]`", "감정/의도 분석"),
        (f"`{BOT_PREFIX}poll [질문] [옵션들]`", "투표 생성"),
        ("봇 멘션 @Bot", "AI에게 직접 질문"),
    ]
    for cmd, desc in commands_list:
        embed.add_field(name=cmd, value=desc, inline=False)
    await ctx.send(embed=embed)

# ─── 슬래시 커맨드 ────────────────────────────────────────────────────────────
@bot.tree.command(name="ask", description="AI에게 질문하기")
@app_commands.describe(question="AI에게 물어볼 내용")
async def slash_ask(interaction: discord.Interaction, question: str):
    await interaction.response.defer()
    if is_rate_limited(interaction.user.id):
        await interaction.followup.send("⏱️ 잠시 후 다시 시도해주세요!", ephemeral=True)
        return
    reply = await call_claude(interaction.user.id, question)
    embed = discord.Embed(description=reply[:4000], color=0x9B5CFF)
    embed.set_author(name=f"{interaction.user.display_name}의 질문", icon_url=interaction.user.display_avatar.url)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="emotion", description="감정 분석하기")
@app_commands.describe(text="분석할 텍스트")
async def slash_emotion(interaction: discord.Interaction, text: str):
    await interaction.response.defer()
    prompt = f"""다음 텍스트의 감정을 분석해주세요. 이모지와 함께 간결하게 답해주세요:
감정 종류, 강도(1~10), 한 줄 코멘트
텍스트: {text}"""
    result = await call_claude(interaction.user.id, prompt)
    embed = discord.Embed(title="💭 감정 분석", color=0xEC4899)
    embed.add_field(name="텍스트", value=text[:500])
    embed.add_field(name="분석", value=result[:500])
    await interaction.followup.send(embed=embed)

# ─── 주기적 작업 ──────────────────────────────────────────────────────────────
@tasks.loop(hours=24)
async def daily_summary():
    """매일 자정 채널 일간 요약 (설정된 채널에)"""
    summary_channel_id = int(os.getenv("SUMMARY_CHANNEL_ID", "0"))
    if not summary_channel_id:
        return
    channel = bot.get_channel(summary_channel_id)
    if not channel:
        return
    messages = []
    async for msg in channel.history(limit=100, after=datetime.now().replace(hour=0,minute=0,second=0)):
        if not msg.author.bot:
            messages.append(f"{msg.author.display_name}: {msg.content[:100]}")
    if not messages:
        return
    summary = await call_claude(0, f"오늘 Discord 채널의 대화를 운영자 관점에서 요약:\n" + "\n".join(messages[:30]))
    embed = discord.Embed(title="📅 오늘의 채널 요약", description=summary, color=0x9B5CFF,
                          timestamp=datetime.now())
    await channel.send(embed=embed)

# ─── 실행 ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("❌ DISCORD_TOKEN이 설정되지 않았습니다. .env 파일을 확인하세요.")
    elif not ANTHROPIC_KEY:
        print("❌ ANTHROPIC_API_KEY가 설정되지 않았습니다. .env 파일을 확인하세요.")
    else:
        print("🚀 Discord AI 봇을 시작합니다...")
        bot.run(DISCORD_TOKEN)
