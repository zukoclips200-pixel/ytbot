"""YouTube clipping bot: everyone in your server can use it."""
import asyncio, logging, os, time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import clipper, editor, vod
from db import DB

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

TOKEN = os.environ["DISCORD_TOKEN"]
FONT_PATH = os.getenv("FONT_PATH") or None
OVERLAY = Path("assets/overlay.png")
RETENTION_HOURS = 48


def owner_ok(interaction: discord.Interaction) -> bool:
    """Simple mode: anyone inside a server can use the bot."""
    return interaction.guild is not None


# ───────────────────────── auto-edit buttons ─────────────────────────
class TitleModal(discord.ui.Modal, title="Clip title"):
    text = discord.ui.TextInput(label="Title (empty = none)", required=False, max_length=60)

    def __init__(self, parent):
        super().__init__()
        self.parent = parent
        self.text.default = parent.o.title

    async def on_submit(self, interaction: discord.Interaction):
        self.parent.o.title = str(self.text)
        self.parent.sync()
        await interaction.response.edit_message(view=self.parent)


class EditView(discord.ui.View):
    def __init__(self, clip, user_id):
        super().__init__(timeout=300)
        self.clip, self.user_id = clip, user_id
        self.o = editor.EditOpts()
        self.sync()

    async def interaction_check(self, interaction):
        return interaction.user.id == self.user_id

    def sync(self):
        self.size_sel.placeholder = f"Size: {editor.SIZES[self.o.size][0]}"
        self.color_sel.placeholder = f"Colour: {editor.COLORS[self.o.color][0]}"
        for btn, on in ((self.blur_btn, self.o.blur), (self.flip_btn, self.o.flip),
                        (self.short_btn, self.o.shorten), (self.overlay_btn, self.o.overlay)):
            btn.style = discord.ButtonStyle.success if on else discord.ButtonStyle.secondary
        self.title_btn.label = f"Title: {self.o.title[:20]}" if self.o.title else "Add title"

    @discord.ui.select(row=0, placeholder="Size",
                       options=[discord.SelectOption(label=v[0], value=k) for k, v in editor.SIZES.items()])
    async def size_sel(self, interaction, select):
        self.o.size = select.values[0]
        self.sync()
        await interaction.response.edit_message(view=self)

    @discord.ui.select(row=1, placeholder="Colour",
                       options=[discord.SelectOption(label=v[0], value=k) for k, v in editor.COLORS.items()])
    async def color_sel(self, interaction, select):
        self.o.color = select.values[0]
        self.sync()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(row=2, label="Blur bg")
    async def blur_btn(self, interaction, button):
        self.o.blur = not self.o.blur
        self.sync()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(row=2, label="Flip")
    async def flip_btn(self, interaction, button):
        self.o.flip = not self.o.flip
        self.sync()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(row=2, label="Shorten")
    async def short_btn(self, interaction, button):
        self.o.shorten = not self.o.shorten
        self.sync()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(row=2, label="Overlay")
    async def overlay_btn(self, interaction, button):
        self.o.overlay = not self.o.overlay
        self.sync()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(row=2, label="Add title")
    async def title_btn(self, interaction, button):
        await interaction.response.send_modal(TitleModal(self))

    @discord.ui.button(row=3, label="Start edit", style=discord.ButtonStyle.success, emoji="✅")
    async def start_btn(self, interaction, button):
        await interaction.response.edit_message(content="⏳ Editing your clip…", view=None)
        self.stop()
        client: ClipBot = interaction.client
        src = Path(self.clip["path"])
        if not src.exists():
            return await interaction.edit_original_response(content="❌ Source clip expired.")
        dst = editor.OUT_DIR / f"{src.stem}_edit_{int(time.time())}.mp4"
        try:
            async with client.edit_sem:
                await editor.autoedit(src, dst, self.o, OVERLAY, FONT_PATH)
        except Exception as e:
            log.exception("edit failed")
            return await interaction.edit_original_response(content=f"❌ Edit failed: {str(e)[:300]}")
        ch = client.chan("autoedit_id") or interaction.channel
        msg = await ch.send(content=f"Edited **{self.clip['slug']}** clip", file=discord.File(dst))
        v = discord.ui.View()
        v.add_item(discord.ui.Button(label="⬇️ Download", url=msg.attachments[0].url))
        await msg.edit(view=v)
        await interaction.edit_original_response(content=f"✅ Done → {msg.jump_url}")


class ClipView(discord.ui.View):
    """Persistent: keeps working after restarts."""

    def __init__(self, download_url=None):
        super().__init__(timeout=None)
        if download_url:
            self.add_item(discord.ui.Button(label="⬇️ Download", url=download_url))

    @discord.ui.button(label="✂️ Auto-Edit", style=discord.ButtonStyle.primary, custom_id="cfa:autoedit")
    async def autoedit(self, interaction: discord.Interaction, button):
        if not owner_ok(interaction):
            return await interaction.response.send_message("Please use this inside a server.", ephemeral=True)
        client: ClipBot = interaction.client
        clip = client.db.clip_by_message(interaction.message.id)
        if not clip:
            return await interaction.response.send_message("Clip not found (expired?).", ephemeral=True)
        await interaction.response.send_message("**Auto-edit options**", view=EditView(clip, interaction.user.id),
                                                ephemeral=True)


# ───────────────────────── bot ─────────────────────────
class OwnerTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if owner_ok(interaction):
            return True
        await interaction.response.send_message("Please use this inside a server.", ephemeral=True)
        return False


class ClipBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = OwnerTree(self)
        self.db = DB()
        self.recorders: dict[str, clipper.Recorder] = {}
        self.notified: set[str] = set()
        self.edit_sem = asyncio.Semaphore(2)

    async def setup_hook(self):
        self.add_view(ClipView())
        self.watch_streams.start()
        self.maintenance.start()

    async def on_ready(self):
        log.info("Logged in as %s", self.user)
        for g in self.guilds:
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
            try:
                await self.ensure_channels(g)
            except discord.Forbidden:
                log.error("Bot needs the 'Manage Channels' permission in %s", g.name)

    # --- channels the bot makes for itself ---
    def channel_overwrites(self, g: discord.Guild):
        """Everyone can see and click the buttons; only the bot posts."""
        return {
            g.default_role: discord.PermissionOverwrite(view_channel=True, read_message_history=True,
                                                        send_messages=False),
            g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True,
                                              embed_links=True, read_message_history=True),
        }

    def chan(self, key):
        cid = int(self.db.get_cfg(key) or 0)
        return self.get_channel(cid) if cid else None

        async def ensure_channels(self, g: discord.Guild):
        """Reuses the YT Clipper channels that already exist, so a restart doesn't make new ones."""
        ow = self.channel_overwrites(g)
        cat = self.chan("category_id") or discord.utils.get(g.categories, name="YT Clipper")
        if not cat:
            cat = await g.create_category("YT Clipper", overwrites=ow)
        self.db.set_cfg("category_id", str(cat.id))
        for key, name in (("autoedit_id", "autoedit"), ("vod_id", "video-clips"), ("alerts_id", "live-alerts")):
            ch = self.chan(key) or discord.utils.get(cat.text_channels, name=name)
            if not ch:
                ch = await g.create_text_channel(name, category=cat, overwrites=ow)
            self.db.set_cfg(key, str(ch.id))

    # --- posting clips ---
    async def post_clip(self, slug, path: Path, reason: str):
        if slug == "vod":
            ch = self.chan("vod_id")
        else:
            s = self.db.get_streamer(slug)
            ch = self.get_channel(s["channel_id"]) if s else None
        if not ch:
            return
        msg = await ch.send(content=f"🎬 **{slug}** — {reason}", file=discord.File(path))
        await msg.edit(view=ClipView(msg.attachments[0].url))
        self.db.add_clip(path.stem, slug, str(path), msg.id)

    # --- live detection ---
    @tasks.loop(seconds=45)
    async def watch_streams(self):
        for s in self.db.list_streamers():
            slug = s["slug"]
            rec = self.recorders.get(slug)
            if rec and not rec.done():
                continue
            url = await clipper.get_stream_url(slug)
            if not url:
                self.notified.discard(slug)
                continue
            rec = clipper.Recorder(slug, url, self.post_clip)
            self.recorders[slug] = rec
            rec.start()
            if slug not in self.notified:
                self.notified.add(slug)
                if ch := self.chan("alerts_id"):
                    await ch.send(f"🔴 **{slug}** is live on YouTube! {clipper.channel_url(slug)}")

    @watch_streams.before_loop
    async def _w1(self):
        await self.wait_until_ready()

    # --- cleanup of old files ---
    @tasks.loop(minutes=30)
    async def maintenance(self):
        now = time.time()
        for c in self.db.old_clips(now - RETENTION_HOURS * 3600):
            Path(c["path"]).unlink(missing_ok=True)
            self.db.delete_clip(c["id"])
        for f in editor.OUT_DIR.glob("*.mp4"):
            if now - f.stat().st_mtime > RETENTION_HOURS * 3600:
                f.unlink(missing_ok=True)

    @maintenance.before_loop
    async def _w2(self):
        await self.wait_until_ready()


bot = ClipBot()


# ───────────────────────── commands ─────────────────────────
@bot.tree.command(description="Watch a YouTube channel and auto-clip when it goes live")
@app_commands.describe(slug="YouTube @handle, e.g. mrbeast (not the channel ID)")
@app_commands.guild_only()
async def addstreamer(i: discord.Interaction, slug: str):
    slug = slug.lower().strip().lstrip("@")
    if bot.db.get_streamer(slug):
        return await i.response.send_message("Already added.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    ch = await i.guild.create_text_channel(f"{slug}-clips", category=bot.chan("category_id"),
                                           overwrites=bot.channel_overwrites(i.guild))
    bot.db.add_streamer(slug, "yt", ch.id, 0)
    await i.followup.send(f"✅ Watching **{slug}** → {ch.mention}", ephemeral=True)


@bot.tree.command(description="Stop watching a YouTube channel")
@app_commands.guild_only()
async def removestreamer(i: discord.Interaction, slug: str):
    slug = slug.lower().strip().lstrip("@")
    s = bot.db.get_streamer(slug)
    if not s:
        return await i.response.send_message("Not found.", ephemeral=True)
    if rec := bot.recorders.pop(slug, None):
        await rec.stop()
    if ch := i.guild.get_channel(s["channel_id"]):
        await ch.delete()
    bot.db.remove_streamer(slug)
    await i.response.send_message(f"Removed **{slug}**.", ephemeral=True)


@bot.tree.command(description="List the YouTube channels being watched")
@app_commands.guild_only()
async def streamers(i: discord.Interaction):
    rows = bot.db.list_streamers()
    text = "\n".join(f"• {r['slug']}" for r in rows) or "None yet. Use /addstreamer."
    await i.response.send_message(text, ephemeral=True)


@bot.tree.command(description="Clip the last 15 seconds of a live stream right now")
@app_commands.guild_only()
async def clip(i: discord.Interaction, streamer: str):
    slug = streamer.lower().strip().lstrip("@")
    rec = bot.recorders.get(slug)
    if not rec or rec.done():
        return await i.response.send_message("They're not live right now.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    path = await rec.make_clip()
    if not path:
        return await i.followup.send("Not enough recorded yet, try again in a few seconds.", ephemeral=True)
    await bot.post_clip(slug, path, "manual clip")
    await i.followup.send("✅ Clipped!", ephemeral=True)


@bot.tree.command(description="Cut a specific moment from a YouTube video")
@app_commands.describe(url="YouTube video link", start="e.g. 1:23:45 or 5025",
                       end="e.g. 1:24:15 (clip can be 3-90 seconds)")
@app_commands.guild_only()
async def clipvideo(i: discord.Interaction, url: str, start: str, end: str):
    try:
        s, e = vod.parse_ts(start), vod.parse_ts(end)
        vod.check_url(url)
    except ValueError as ex:
        return await i.response.send_message(f"❌ {ex}", ephemeral=True)
    if not 3 <= e - s <= vod.MAX_CLIP:
        return await i.response.send_message(f"❌ Clip must be 3–{vod.MAX_CLIP} seconds.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    try:
        async with bot.edit_sem:
            path = await vod.cut(url, s, e)
    except Exception as ex:
        log.exception("clipvideo")
        return await i.followup.send(f"❌ {str(ex)[:300]}", ephemeral=True)
    await bot.post_clip("vod", path, f"clip ({vod.fmt_ts(s)}–{vod.fmt_ts(e)})")
    await i.followup.send("✅ Posted in #video-clips.", ephemeral=True)


@bot.tree.command(description="Auto-find the best moments in a YouTube video and clip them")
@app_commands.describe(url="YouTube video link (not a live stream)", count="How many clips (1-8)")
@app_commands.guild_only()
async def autoclip(i: discord.Interaction, url: str, count: app_commands.Range[int, 1, 8] = 5):
    try:
        vod.check_url(url)
    except ValueError as ex:
        return await i.response.send_message(f"❌ {ex}", ephemeral=True)
    await i.response.defer(ephemeral=True)
    try:
        moments, method = await vod.find_moments(url, count)
    except Exception as ex:
        log.exception("autoclip")
        return await i.followup.send(f"❌ {str(ex)[:300]}", ephemeral=True)
    await i.followup.send(f"🔍 Found {len(moments)} moments using **{method}**. Cutting…", ephemeral=True)
    done = 0
    for s, e, why in moments:
        try:
            async with bot.edit_sem:
                path = await vod.cut(url, s, e)
            await bot.post_clip("vod", path, f"{why} @ {vod.fmt_ts(s)}")
            done += 1
        except Exception:
            log.exception("cut failed")
    await i.followup.send(f"✅ Posted {done}/{len(moments)} clips in #video-clips.", ephemeral=True)

@bot.tree.command(description="Give the bot your YouTube cookies so YouTube stops blocking it")
@app_commands.describe(file="Your cookies.txt file exported from youtube.com")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def ytcookies(i: discord.Interaction, file: discord.Attachment):
    if file.size > 1_000_000:
        return await i.response.send_message("❌ That file is too big to be a cookies file.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    text = (await file.read()).decode("utf-8", errors="ignore")
    n = vod.save_cookies(text)
    if not n:
        return await i.followup.send("❌ I couldn't find any cookies in that file. Export it again as cookies.txt.",
                                     ephemeral=True)
    yt = sum(1 for line in text.splitlines() if "youtube.com" in line)
    note = "" if yt else "\n⚠️ None of them are for youtube.com, so this probably won't help."
    await i.followup.send(f"✅ Saved {n} cookies ({yt} for YouTube). Try /autoclip again.{note}\n"
                          "If Railway redeploys the bot, send the file again.", ephemeral=True)
if __name__ == "__main__":
    bot.run(TOKEN)
