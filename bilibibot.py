import os
import re
import asyncio
import logging
import discord
from discord import app_commands
from dotenv import load_dotenv
import yt_dlp

logging.basicConfig(level=logging.INFO)
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("DISCORD_GUILD_ID")

BILIBILI_URL_REGEX = r'(https?://(?:www\.|b23\.tv/|v\.bilibili\.com/)[^\s]+)'

# FFmpeg 参数：将 Referer、User-Agent 以及重连机制全部以命令行参数形式注入
FFMPEG_BEFORE_OPTIONS = (
    '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 '
    '-headers "Referer: https://www.bilibili.com/\r\n'
    'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36\r\n"'
)


class GuildMusicState:
    """记录每个服务器的播放状态"""
    def __init__(self):
        self.queue = []            # 待播放歌曲列表
        self.text_channel = None   # 发送播放通知的文字频道
        self.playing = False       # 是否正在播放


music_states = {}


def get_state(guild_id: int) -> GuildMusicState:
    if guild_id not in music_states:
        music_states[guild_id] = GuildMusicState()
    return music_states[guild_id]


class MyBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # 如果填写了测试服务器 ID，仅向该服务器快速同步（方便本地开发调试）
        if GUILD_ID and GUILD_ID.strip():
            try:
                guild = discord.Object(id=int(GUILD_ID))
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                logging.info(f"已成功将指令同步至测试 Guild: {GUILD_ID}")
            except ValueError:
                logging.warning("环境变量 DISCORD_GUILD_ID 格式不正确，尝试进行全局同步...")
                await self.tree.sync()
                logging.info("已成功进行全局指令同步。")
        else:
            # 线上公开发布模式：同步全局指令（覆盖所有服务器）
            await self.tree.sync()
            logging.info("未提供 DISCORD_GUILD_ID，已成功向 Discord 平台注册全局指令。")

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")


bot = MyBot()


# ---------- 工具函数 ----------

def fetch_metadata(url_or_bvid: str) -> dict:
    """快速提取视频标题与元数据，不提取可能过期的音频直链"""
    if re.match(r'^(BV[0-9A-Za-z]+|av[0-9]+)$', url_or_bvid):
        target_url = f"https://www.bilibili.com/video/{url_or_bvid}"
    else:
        target_url = url_or_bvid

    ydl_opts = {
        'extract_flat': True,
        'quiet': True,
        'no_warnings': True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(target_url, download=False)
        return {
            "title": info.get("title", "未知标题"),
            "uploader": info.get("uploader", "未知UP主"),
            "webpage_url": info.get("webpage_url", target_url),
            "thumbnail": info.get("thumbnail")
        }


def fetch_audio_url(webpage_url: str) -> str:
    """即将播放时实时获取最新的音频直链"""
    ydl_opts = {
        'format': 'bestaudio[abr<=128]/bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'http_headers': {
            'User-Agent': (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            'Referer': 'https://www.bilibili.com/',
        },
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(webpage_url, download=False)
        audio_url = None
        if 'url' in info:
            audio_url = info['url']
        else:
            formats = info.get('formats', [])
            for f in formats:
                if f.get('acodec') != 'none' and f.get('vcodec') == 'none':
                    audio_url = f['url']
                    break
            if not audio_url and formats:
                audio_url = formats[0]['url']

        if not audio_url:
            raise Exception("未找到可播放的音频流")

        return audio_url


def play_next(guild_id: int, voice_client: discord.VoiceClient):
    """播放队列中的下一首"""
    state = get_state(guild_id)

    if not voice_client or not voice_client.is_connected() or not state.queue:
        state.playing = False
        return

    next_song = state.queue.pop(0)

    try:
        # 在播放前实时解析直链
        audio_url = fetch_audio_url(next_song["webpage_url"])
    except Exception as e:
        logging.error(f"解析音频失败: {e}")
        if state.text_channel:
            asyncio.run_coroutine_threadsafe(
                state.text_channel.send(f"❌ 解析【{next_song['title']}】失败，自动跳过。"),
                bot.loop
            )
        play_next(guild_id, voice_client)
        return

    def after_playing(error):
        if error:
            logging.error(f"播放错误: {error}")
        asyncio.run_coroutine_threadsafe(
            play_next_async(guild_id, voice_client),
            bot.loop
        )

    try:
        source = discord.FFmpegPCMAudio(
            audio_url,
            before_options=FFMPEG_BEFORE_OPTIONS,
            options='-vn'
        )
        voice_client.play(source, after=after_playing)
    except Exception as e:
        logging.error(f"启动 FFmpeg 播放失败: {e}")
        state.playing = False
        return

    if state.text_channel:
        embed = discord.Embed(
            title=next_song["title"],
            url=next_song["webpage_url"],
            description=f"**UP主：** {next_song['uploader']}"
        )
        if next_song.get("thumbnail"):
            embed.set_thumbnail(url=next_song["thumbnail"])

        asyncio.run_coroutine_threadsafe(
            state.text_channel.send(content="🎵 **正在播放：**", embed=embed),
            bot.loop
        )


async def play_next_async(guild_id: int, voice_client: discord.VoiceClient):
    play_next(guild_id, voice_client)


async def add_to_queue_and_play(ctx, input_text: str):
    guild_id = ctx.guild.id
    state = get_state(guild_id)

    user = getattr(ctx, 'user', getattr(ctx, 'author', None))
    voice_channel = user.voice.channel

    voice_client = ctx.guild.voice_client
    if not voice_client:
        voice_client = await voice_channel.connect()
    elif voice_client.channel != voice_channel:
        await voice_client.move_to(voice_channel)

    state.text_channel = ctx.channel

    song_info = await asyncio.to_thread(fetch_metadata, input_text)
    state.queue.append(song_info)

    if not state.playing:
        state.playing = True
        play_next(guild_id, voice_client)
        return song_info, True
    else:
        return song_info, False


# ---------- 指令列表 ----------

@bot.tree.command(name="ping", description="测试机器人状态")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!")


@bot.tree.command(name="play", description="播放或添加歌曲到队列 (支持链接/BV号)")
@app_commands.describe(input_text="B站链接或BV号")
async def play(interaction: discord.Interaction, input_text: str):
    if not interaction.user.voice:
        await interaction.response.send_message("你需要先加入一个语音频道。", ephemeral=True)
        return

    await interaction.response.defer()
    try:
        song_info, is_playing_now = await add_to_queue_and_play(interaction, input_text)

        if is_playing_now:
            await interaction.followup.send("🎵 **已开始加载并播放！**")
        else:
            state = get_state(interaction.guild.id)
            position = len(state.queue)
            embed = discord.Embed(
                title=song_info["title"],
                url=song_info["webpage_url"],
                description=f"已加入队列第 **{position}** 位"
            )
            await interaction.followup.send(content="✅ **已加入播放列表：**", embed=embed)
    except Exception as e:
        await interaction.followup.send(f"处理失败：{str(e)[:1900]}")


@bot.tree.command(name="skip", description="切歌 / 跳过当前播放的歌曲")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or (not vc.is_playing() and not vc.is_paused()):
        await interaction.response.send_message("当前没有正在播放的音乐。", ephemeral=True)
        return

    vc.stop()
    await interaction.response.send_message("⏭️ **已跳过当前歌曲！**")


@bot.tree.command(name="stop", description="停止播放并清空播放列表，断开连接")
async def stop(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    state = get_state(guild_id)
    vc = interaction.guild.voice_client

    state.queue.clear()
    state.playing = False

    if vc:
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        await vc.disconnect()

    await interaction.response.send_message("⏹️ **已停止播放，清空队列并退出频道。**")


@bot.tree.command(name="queue", description="查看当前的播放列表")
async def queue(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    q = state.queue

    if not q:
        await interaction.response.send_message("📋 **当前播放列表为空。**")
        return

    description = ""
    for idx, song in enumerate(q, start=1):
        description += f"**{idx}.** [{song['title']}]({song['webpage_url']}) - `{song['uploader']}`\n"

    embed = discord.Embed(
        title="📋 待播放列表",
        description=description[:4000],
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed)


# ---------- 消息监听：贴链接自动点歌 ----------

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    matches = re.findall(BILIBILI_URL_REGEX, message.content)
    if not matches:
        return

    if not message.author.voice or not message.author.voice.channel:
        return

    try:
        url = matches[0]
        song_info, is_playing_now = await add_to_queue_and_play(message, url)

        if not is_playing_now:
            state = get_state(message.guild.id)
            position = len(state.queue)
            embed = discord.Embed(
                title=song_info["title"],
                url=song_info["webpage_url"],
                description=f"已加入队列第 **{position}** 位"
            )
            await message.channel.send(content="✅ **检测到 B 站链接，已加入播放列表：**", embed=embed)
    except Exception as e:
        await message.channel.send(f"无法处理该链接：{str(e)[:1900]}")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("请在 .env 文件中设置 DISCORD_TOKEN")
    bot.run(TOKEN)

    