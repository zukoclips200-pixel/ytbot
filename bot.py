"""YouTube clipping bot: everyone in your server can use it."""
import asyncio, logging, os, re, time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import captions, editor, smart, vod
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
    def __init__(self, clip, user_id, message=None):
        super().__init__(timeout=300)
        self.clip, self.user_id, self.message = clip, user_id, message
        self.o = editor.EditOpts()
        m = re.search(r"-# (\w+) •", (message.content if message else "") or "")
        self.o.vtype = m.group(1) if m else ""
        t = re.search(r"🎬 \*\*(.+?)\*\*", (message.content if message else "") or "")
        if t and self.clip.get("slug") == "vod":
            self.o.title = t.group(1)[:60]
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
        self.cap_btn.label = {"auto": "Captions: auto", "on": "Captions: on", "off": "Captions: off"}[self.o.captions]
        self.cap_btn.style = (discord.ButtonStyle.secondary if self.o.captions == "off"
                              else discord.ButtonStyle.success)
        self.game_btn.label = f"🎮 Gaming layout: {self.o.gaming}"
        self.game_btn.style = (discord.ButtonStyle.success if self.o.gaming == "auto"
                               else discord.ButtonStyle.secondary)
        self.style_sel.placeholder = f"Caption colour: {captions.CAPTION_STYLES[self.o.cap_style][0]}"

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

    @discord.ui.select(row=4, placeholder="Caption colour",
                       options=[discord.SelectOption(label=v[0], value=k)
                                for k, v in captions.CAPTION_STYLES.items()])
    async def style_sel(self, interaction, select):
        self.o.cap_style = select.values[0]
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

    @discord.ui.button(row=3, label="Captions: auto")
    async def cap_btn(self, interaction, button):
        modes = editor.CAPTION_MODES
        self.o.captions = modes[(modes.index(self.o.captions) + 1) % len(modes)]
        self.sync()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(row=3, label="🎮 Gaming layout: auto")
    async def game_btn(self, interaction, button):
        self.o.gaming = "off" if self.o.gaming == "auto" else "auto"
        self.sync()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(row=3, label="Start edit", style=discord.ButtonStyle.success, emoji="✅")
    async def start_btn(self, interaction, button):
        await interaction.response.edit_message(content="⏳ Editing your clip…", view=None)
        self.stop()
        client: ClipBot = interaction.client
        src = Path(self.clip["path"] or "missing.mp4")
        if not src.exists():
            # The saved file is gone (e.g. after an update): grab the clip from the Discord message instead.
            att = self.message.attachments[0] if self.message and self.message.attachments else None
            if not att:
                return await interaction.edit_original_response(content="❌ Source clip expired.")
            src = editor.OUT_DIR / f"src_{self.message.id}.mp4"
            await att.save(src)
        dst = editor.OUT_DIR / f"{src.stem}_edit_{int(time.time())}.mp4"
        try:
            async with client.edit_sem:
                notes = await editor.autoedit(src, dst, self.o, OVERLAY, FONT_PATH)
        except Exception as e:
            log.exception("edit failed")
            return await interaction.edit_original_response(content=f"❌ Edit failed: {str(e)[:300]}")
        ch = client.chan("autoedit_id") or interaction.channel
        msg = await ch.send(content=f"✂️ Edited clip" + (f" • {notes}" if notes else ""), file=discord.File(dst))
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
        row = client.db.clip_by_message(interaction.message.id)
        clip = dict(row) if row else {"path": "", "slug": "vod"}
        await interaction.response.send_message(
            "**Auto-edit options**\n✨ *Smart 9:16* picks split screen, face tracking or a gaming layout by itself.\n"
            "🎮 *Gaming layout: auto* puts the streamer on top and the gameplay below when it finds a webcam. "
            "Tap it to switch it off.",
            view=EditView(clip, interaction.user.id, interaction.message), ephemeral=True)


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
        self.edit_sem = asyncio.Semaphore(2)

    async def setup_hook(self):
        self.add_view(ClipView())
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
        for key, name in (("autoedit_id", "autoedit"), ("vod_id", "video-clips")):
            ch = self.chan(key) or discord.utils.get(cat.text_channels, name=name)
            if not ch:
                ch = await g.create_text_channel(name, category=cat, overwrites=ow)
            self.db.set_cfg(key, str(ch.id))

    # --- posting clips ---
    async def post_clip(self, slug, path: Path, reason: str, ch=None, content=None):
        ch = ch or self.chan("vod_id")
        if not ch:
            raise RuntimeError("The channel for clips is missing.")
        msg = await ch.send(content=content or f"🎬 **{slug}** — {reason}", file=discord.File(path))
        await msg.edit(view=ClipView(msg.attachments[0].url))
        self.db.add_clip(path.stem, slug, str(path), msg.id)

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
    try:
        await bot.post_clip("vod", path, f"clip ({vod.fmt_ts(s)}–{vod.fmt_ts(e)})", i.channel)
    except Exception as ex:
        log.exception("post failed")
        return await i.followup.send(f"❌ Couldn't post the clip: {str(ex)[:300]}", ephemeral=True)
    await i.followup.send("✅ Posted here.", ephemeral=True)


@bot.tree.command(description="AI finds the best complete moments in a YouTube video and clips them")
@app_commands.describe(url="YouTube video link (not a live stream)", count="How many clips (1-8)",
                       start="Optional: only look from here, e.g. 0:00 or 4:00:00 (for long streams)",
                       end="Optional: only look up to here, e.g. 4:00:00 (up to 4 hours after start)")
@app_commands.guild_only()
async def autoclip(i: discord.Interaction, url: str, count: app_commands.Range[int, 1, 8] = 5,
                   start: str = "", end: str = ""):
    try:
        vod.check_url(url)
        s0 = vod.parse_ts(start) if start.strip() else None
        e0 = vod.parse_ts(end) if end.strip() else None
    except ValueError as ex:
        return await i.response.send_message(f"❌ {ex}", ephemeral=True)
    if s0 is not None and e0 is not None and e0 <= s0:
        return await i.response.send_message("❌ End must be after start.", ephemeral=True)
    part = ""
    if s0 is not None or e0 is not None:
        part = f" (from {vod.fmt_ts(s0 or 0)} to {vod.fmt_ts(e0) if e0 else 'the end'})"
    await i.response.send_message("⏳ Started. I'll post progress in this channel.", ephemeral=True)
    note = await i.channel.send(f"⏳ Working on <{url}>{part} for {i.user.mention}…")

    async def status(text):
        try:
            await note.edit(content=f"{text}\n<{url}>{part}")
        except discord.HTTPException:
            pass

    try:
        vtype, source, clips = await smart.find_clips(url, count, status, s0, e0)
    except Exception as ex:
        log.exception("autoclip")
        return await status(f"❌ {str(ex)[:400]}")

    done, last_err = 0, ""
    for n, c in enumerate(clips, 1):
        await status(f"🎬 Cutting clip {n}/{len(clips)}…")
        try:
            async with bot.edit_sem:
                path = await vod.cut(url, c["start"], c["end"])
            text = (f"🎬 **{c['title']}**\n{c['caption']}\n"
                    f"-# {vtype} • {vod.fmt_ts(c['start'])}–{vod.fmt_ts(c['end'])} • score {c['score']:.0f}/10")
            await bot.post_clip("vod", path, "", i.channel, content=text[:1900])
            done += 1
        except Exception as ex:
            last_err = str(ex)[:200]
            log.exception("cut failed")
    msg = f"✅ Posted {done}/{len(clips)} clips. Video type: **{vtype}** (transcript from {source})."
    if done < len(clips) and last_err:
        msg += f"\n⚠️ Last error: {last_err}"
    await status(msg)


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
    await i.followup.send(f"✅ Saved {n} cookies ({yt} for YouTube). Try /autoclip again.{note}", ephemeral=True)


if __name__ == "__main__":
    bot.run(TOKEN)
