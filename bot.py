"""Telegram Repeat Bot — controlled repeat utility with a 30-day license system."""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyParameters,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

import config
import database as db
import license_manager as lm
import system_metrics as sm

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("repeat-bot")

MAX_PREVIEW = 60
MAX_FOLDER_NUMBER = 999_999
MAX_FOLDER_TEXT_LENGTH = 4096
EXPIRY_CHECK_INTERVAL_SECONDS = 60
EXPIRY_TASK_KEY = "license_expiry_notification_task"
COMMAND_MENU_CONFIGURED_KEY = "command_menu_configured"

USER_COMMANDS = (
    BotCommand("start", "🚀 Start SpamPro v.01"),
    BotCommand("help", "📚 View bot help & commands"),
    BotCommand("ping", "🏓 Check bot latency"),
    BotCommand("id", "🆔 Show Telegram ID information"),
    BotCommand("repeat", "🔁 Repeat a message"),
    BotCommand("basic", "🐢 Use Basic Spam mode"),
    BotCommand("medium", "⚖️ Use Medium Spam mode"),
    BotCommand("aggressive", "🔥 Use Aggressive mode"),
    BotCommand("stop", "🛑 Stop an active repeat"),
    BotCommand("activate", "🔐 Activate a license"),
    BotCommand("license", "📋 View license status"),
    BotCommand("about", "🤖 About SpamPro v.01"),
    BotCommand("health", "❤️ Check live bot health"),
)
ADMIN_COMMANDS = USER_COMMANDS + (
    BotCommand("genkey", "🔑 Generate a license key"),
    BotCommand("keys", "📊 View license statistics"),
    BotCommand("revoke", "🚫 Revoke a license key"),
    BotCommand("scankey", "🔍 Check license key information"),
)


# --------------------------------------------------------------------------- #
# Task management
# --------------------------------------------------------------------------- #
@dataclass
class RepeatTask:
    user_id: int
    total: int
    mode: str = config.DEFAULT_MODE
    sent: int = 0
    cancelled: bool = False
    task: asyncio.Task | None = field(default=None, repr=False)


class TaskRegistry:
    """One active repeat task per user, with a cooldown between tasks."""

    def __init__(self) -> None:
        self._tasks: dict[int, RepeatTask] = {}
        self._last_finished: dict[int, float] = {}
        self._lock = asyncio.Lock()

    async def start(
        self, user_id: int, total: int, mode: str = config.DEFAULT_MODE
    ) -> RepeatTask | None:
        async with self._lock:
            if user_id in self._tasks:
                return None
            job = RepeatTask(user_id=user_id, total=total, mode=mode)
            self._tasks[user_id] = job
            return job

    def get(self, user_id: int) -> RepeatTask | None:
        return self._tasks.get(user_id)

    def cooldown_left(self, user_id: int) -> int:
        last = self._last_finished.get(user_id)
        if last is None:
            return 0
        remaining = config.COOLDOWN_SECONDS - (time.monotonic() - last)
        return max(0, int(remaining + 0.999))

    async def finish(self, user_id: int) -> None:
        async with self._lock:
            self._tasks.pop(user_id, None)
            self._last_finished[user_id] = time.monotonic()

    async def cancel(self, user_id: int) -> RepeatTask | None:
        job = self._tasks.get(user_id)
        if job is None:
            return None
        job.cancelled = True
        if job.task and not job.task.done():
            job.task.cancel()
        return job

    async def cancel_all(self) -> None:
        for user_id in list(self._tasks):
            await self.cancel(user_id)


registry = TaskRegistry()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def esc(text: str) -> str:
    return html.escape(text, quote=False)


def preview(text: str) -> str:
    clipped = text if len(text) <= MAX_PREVIEW else text[: MAX_PREVIEW - 1] + "…"
    return esc(clipped)


async def reply(update: Update, text: str) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


def admin_only(update: Update) -> bool:
    user = update.effective_user
    return bool(user and config.is_admin(user.id))


def requester_id(update: Update) -> int | None:
    """Return the user running a command, including channel posts.

    Telegram channel posts do not include a sender user. Only channel admins
    can publish there, so channel commands run against the configured admin's
    license while the bot's channel permissions provide the access boundary.
    """
    user = update.effective_user
    if user:
        return user.id
    channel_post = update.channel_post
    if (
        channel_post
        and channel_post.sender_chat
        and channel_post.sender_chat.type == "channel"
    ):
        return min(config.ADMIN_IDS) if config.ADMIN_IDS else None
    return None


def ping_latency_status(latency_ms: float) -> tuple[str, str]:
    if latency_ms < 100:
        return "🟢", "ᴇxᴄᴇʟʟᴇɴᴛ"
    if latency_ms <= 300:
        return "🟢", "ɢᴏᴏᴅ"
    if latency_ms <= 700:
        return "🟡", "ᴀᴠᴇʀᴀɢᴇ"
    return "🔴", "ʜɪɢʜ ʟᴀᴛᴇɴᴄʏ"


START_HELP_CALLBACK = "spampro_start_help"
START_PING_CALLBACK = "spampro_start_ping"
PING_REFRESH_CALLBACK = "spampro_ping_refresh"


def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📚 Help & Commands", callback_data=START_HELP_CALLBACK
                ),
                InlineKeyboardButton("🏓 Check Ping", callback_data=START_PING_CALLBACK),
            ]
        ]
    )


def start_welcome_text(user_name: str, latency_ms: float) -> str:
    indicator, label = ping_latency_status(latency_ms)
    return (
        "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
        "🤖 <b>sᴘᴀᴍᴘʀᴏ v.01</b>\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"👋 ʜᴇʟʟᴏ, <b>{esc(user_name)}</b>!\n\n"
        "🚀 ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ sᴘᴀᴍᴘʀᴏ v.01\n\n"
        "⚡ A fast and reliable Telegram utility bot designed with a clean "
        "and simple experience.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📊 ʟɪᴠᴇ sʏsᴛᴇᴍ sᴛᴀᴛᴜs\n\n"
        "🟢 sᴛᴀᴛᴜs: <b>ᴏɴʟɪɴᴇ</b>\n"
        f"🏓 ᴘɪɴɢ: <code>{latency_ms:.0f} ms</code> {indicator} "
        f"<b>{label}</b>\n"
        '🤖 ᴠᴇʀsɪᴏɴ: <b>"v.01"</b>\n'
        '⚙️ sʏsᴛᴇᴍ: <b>"ʀᴇᴀᴅʏ"</b>\n\n'
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "✨ ǫᴜɪᴄᴋ sᴛᴀʀᴛ\n\n"
        '📚 Use <code>/help</code> to view available commands.\n'
        '🏓 Use <code>/ping</code> to check the current bot latency.\n'
        '🆔 Use <code>/id</code> to view your Telegram information.\n\n'
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "❝ sᴍᴀʀᴛ. ғᴀsᴛ. sᴛᴀʙʟᴇ. ❞\n\n"
        "🤖 sᴘᴀᴍᴘʀᴏ v.01 — ʀᴇᴀᴅʏ ᴛᴏ sᴇʀᴠᴇ."
    )


def ping_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔄 ʀᴇғʀᴇsʜ sᴛᴀᴛᴜs", callback_data=PING_REFRESH_CALLBACK
                )
            ]
        ]
    )


def _metric_number(value: float | None, decimals: int = 1) -> str:
    return "N/A" if value is None else f"{value:.{decimals}f}"


def _metric_gb(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2f}"


def ping_panel_text(latency_ms: float, metrics: sm.SystemMetrics) -> str:
    latency_indicator, latency_label = ping_latency_status(latency_ms)
    status_indicator, status_label = sm.overall_status(metrics)
    gpu_model = esc(metrics.gpu.model or "ɴ/ᴀ")
    gpu_note = (
        "\nℹ️ ɴᴏ ɢᴘᴜ ᴍᴏɴɪᴛᴏʀɪɴɢ ᴀᴠᴀɪʟᴀʙʟᴇ"
        if not metrics.gpu.model
        else ""
    )
    return (
        "╭━━〔 🤖 <b>sᴘᴀᴍᴘʀᴏ v.01</b> 〕━━╮\n"
        "┃ 🏓 <b>ᴘᴏɴɢ! sʏsᴛᴇᴍ sᴛᴀᴛᴜs</b>\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━╯\n"
        "⚡ <b>ʀᴇsᴘᴏɴsᴇ</b>\n"
        f"🏓 ʟᴀᴛᴇɴᴄʏ: <code>{latency_ms:.0f} ms</code> "
        f"{latency_indicator} <b>{latency_label}</b>\n"
        "🟢 sᴛᴀᴛᴜs: <b>ᴏɴʟɪɴᴇ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🖥️ <b>ʜᴏsᴛ sʏsᴛᴇᴍ</b>\n"
        f"🧠 ᴄᴘᴜ: <code>{esc(metrics.cpu_model or 'N/A')}</code>\n"
        f"📊 ᴄᴘᴜ ᴜsᴀɢᴇ: <code>{_metric_number(metrics.cpu_usage_percent)}%</code>\n"
        f"🧵 ᴄᴏʀᴇs: <code>{metrics.cpu_cores or 'N/A'}</code>\n\n"
        f"🎮 ɢᴘᴜ: <code>{gpu_model}</code>\n"
        f"📈 ɢᴘᴜ ᴜsᴀɢᴇ: "
        f"<code>{_metric_number(metrics.gpu.usage_percent)}%</code>\n"
        f"🌡️ ɢᴘᴜ ᴛᴇᴍᴘ: "
        f"<code>{_metric_number(metrics.gpu.temperature_c)}°C</code>"
        f"{gpu_note}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "💾 <b>ᴍᴇᴍᴏʀʏ & sᴛᴏʀᴀɢᴇ</b>\n"
        f"🧮 ʀᴀᴍ: <code>{_metric_gb(metrics.ram_used_gb)} / "
        f"{_metric_gb(metrics.ram_total_gb)} GB</code>\n"
        f"📊 ʀᴀᴍ ᴜsᴀɢᴇ: "
        f"<code>{_metric_number(metrics.ram_usage_percent)}%</code>\n\n"
        f"💽 sᴛᴏʀᴀɢᴇ: <code>{_metric_gb(metrics.disk_used_gb)} / "
        f"{_metric_gb(metrics.disk_total_gb)} GB</code>\n"
        f"📦 ғʀᴇᴇ sᴘᴀᴄᴇ: <code>{_metric_gb(metrics.disk_free_gb)} GB</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚙️ <b>ʙᴏᴛ sʏsᴛᴇᴍ</b>\n"
        f"⏱️ ᴜᴘᴛɪᴍᴇ: <code>{esc(sm.format_uptime(metrics.uptime_seconds))}</code>\n"
        f"🐍 ᴘʏᴛʜᴏɴ: <code>{esc(metrics.python_version)}</code>\n"
        '🤖 ʙᴏᴛ ᴠᴇʀsɪᴏɴ: <code>"v.01"</code>\n'
        f"🌐 ᴘʟᴀᴛғᴏʀᴍ: <code>{esc(metrics.platform_name)}</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📡 <b>ɴᴇᴛᴡᴏʀᴋ</b>\n"
        f"🌐 ʜᴏsᴛ: <code>{esc(metrics.hostname)}</code>\n"
        '📡 ᴄᴏɴɴᴇᴄᴛɪᴏɴ: 🟢 <b>"sᴛᴀʙʟᴇ"</b>\n\n'
        f"📊 ᴏᴠᴇʀᴀʟʟ: {status_indicator} <b>{status_label}</b>\n\n"
        "❝ ғᴀsᴛ ᴇɴᴏᴜɢʜ ᴛᴏ ʀᴇsᴘᴏɴᴅ. "
        "sᴛᴀʙʟᴇ ᴇɴᴏᴜɢʜ ᴛᴏ sᴛᴀʏ. ❞\n\n"
        "🤖 sᴘᴀᴍᴘʀᴏ v.01"
    )


NO_LICENSE = (
    "🔐 <b>License Required</b>\n\n"
    "You don't have an active license.\n\n"
    "Please activate a valid key using:\n"
    "<code>/activate YOUR-KEY</code>"
)
NOT_ADMIN = "🚫 <b>Not Authorized</b>\n\nThis command is restricted to admins."


# --------------------------------------------------------------------------- #
# Basic commands
# --------------------------------------------------------------------------- #
async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    user = update.effective_user
    user_name = (
        user.first_name
        if user and user.first_name
        else user.username
        if user and user.username
        else "there"
    )
    keyboard = start_keyboard()
    started = time.perf_counter()
    try:
        response = await message.reply_text(
            "🤖 <b>sᴘᴀᴍᴘʀᴏ v.01</b>\n\n⏳ Preparing your live system panel…",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    except TelegramError:
        return

    latency_ms = (time.perf_counter() - started) * 1000
    welcome_text = start_welcome_text(user_name, latency_ms)
    try:
        await response.edit_text(
            welcome_text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    except TelegramError as exc:
        # Keep the measured value visible even if Telegram cannot edit the
        # original response (for example, an old or restricted message).
        log.warning("Could not update /start welcome panel: %s", exc)
        try:
            await message.reply_text(
                welcome_text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        except TelegramError:
            pass


async def cmd_start_button(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not query:
        return

    await query.answer()
    if query.data == START_HELP_CALLBACK:
        await cmd_help(update, context)
    elif query.data == START_PING_CALLBACK:
        await cmd_ping(update, context)
    elif query.data == PING_REFRESH_CALLBACK:
        await refresh_ping_message(query.message)


async def cmd_help(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    text = (
        "📚 <b>Available Commands</b>\n\n"
        "🏓 <code>/ping</code> — live system latency\n"
        "🆔 <code>/id</code> — user and chat information\n"
        "🔁 <code>/repeat &lt;count&gt; &lt;message&gt;</code> (Medium Spam)\n"
        "⚡ <code>/repeat &lt;count&gt; &lt;mode&gt; &lt;message&gt;</code>\n"
        "🐢 Basic Spam: <code>basic</code> (2s per message)\n"
        "⚖️ Medium Spam: <code>medium</code> (1s per message)\n"
        "🔥 Aggressive Domination: <code>aggressive</code> (0.5s per message)\n"
        "🎯 Reply to a message + <code>/repeat</code>\n"
        "🛑 <code>/stop</code>\n"
        "🔐 <code>/activate &lt;key&gt;</code>\n"
        "📋 <code>/license</code>\n"
        "🤖 <code>/about</code>\n"
        "❤️ <code>/health</code>\n"
        "🔍 <code>/scankey &lt;key&gt;</code> — admin only"
    )
    if admin_only(update):
        bot_name = getattr(context.bot, "username", None) or "BotName"
        text += (
            "\n\n👑 <b>Admin Commands</b>\n\n"
            "🔑 <code>/genkey</code>\n"
            "📊 <code>/keys</code>\n"
            "🚫 <code>/revoke &lt;key&gt;</code>\n"
            "\n📂 <b>Folder Examples</b>\n"
            "💾 Save/update in bot DM: "
            "<code>/folder1 Welcome to our group!</code>\n"
            f"🚀 Use in a group: <code>@{esc(bot_name)} /folder1 5</code>\n"
            "🗑️ Delete permanently: <code>/deletefolder1</code>\n"
            "Use any folder number in place of <code>1</code>."
        )
    await reply(update, text)


async def cmd_ping(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    started = time.perf_counter()
    try:
        response = await message.reply_text(
            "🏓 <b>ᴘᴏɴɢ!</b>\n\n⏳ Measuring live response latency…",
            parse_mode=ParseMode.HTML,
            reply_markup=ping_keyboard(),
        )
    except TelegramError:
        return

    latency_ms = (time.perf_counter() - started) * 1000
    metrics = sm.collect_system_metrics()
    text = ping_panel_text(latency_ms, metrics)
    try:
        await response.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=ping_keyboard(),
        )
    except TelegramError as exc:
        log.warning("Could not update ping response: %s", exc)


async def refresh_ping_message(message) -> None:
    """Refresh one existing ping message instead of sending a new message."""
    if not message:
        return

    started = time.perf_counter()
    try:
        await message.edit_text(
            "🏓 <b>ᴘᴏɴɢ!</b>\n\n⏳ Refreshing live system metrics…",
            parse_mode=ParseMode.HTML,
            reply_markup=ping_keyboard(),
        )
    except TelegramError as exc:
        log.warning("Could not start ping refresh: %s", exc)
        return

    latency_ms = (time.perf_counter() - started) * 1000
    metrics = sm.collect_system_metrics()
    try:
        await message.edit_text(
            ping_panel_text(latency_ms, metrics),
            parse_mode=ParseMode.HTML,
            reply_markup=ping_keyboard(),
        )
    except TelegramError as exc:
        log.warning("Could not finish ping refresh: %s", exc)


async def cmd_id(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message:
        return

    user = update.effective_user
    user_id = str(user.id) if user else "Unavailable for channel posts"
    chat_id = str(chat.id) if chat else "Unavailable"
    lines = [
        "╭━━━〔 🤖 <b>sᴘᴀᴍᴘʀᴏ v.01</b> 〕━━━╮",
        "┃ 🆔 <b>ɪᴅᴇɴᴛɪᴛʏ ɪɴғᴏʀᴍᴀᴛɪᴏɴ</b>",
        "╰━━━━━━━━━━━━━━━━━━━━╯",
        "",
        f'👤 ᴜsᴇʀ ɪᴅ: <code>"{esc(user_id)}"</code>',
        f'💬 ᴄʜᴀᴛ ɪᴅ: <code>"{esc(chat_id)}"</code>',
    ]
    if message.reply_to_message:
        lines.append(
            f'📩 ʀᴇᴘʟɪᴇᴅ ᴍᴇssᴀɢᴇ ɪᴅ: '
            f'<code>"{message.reply_to_message.message_id}"</code>'
        )
    await reply(update, "\n".join(lines))


def _display_name(update: Update) -> str:
    user = update.effective_user
    if user and user.first_name:
        return user.first_name
    if user and user.username:
        return user.username
    return "there"


async def cmd_about(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(
        update,
        "╭━━━〔 🤖 <b>sᴘᴀᴍᴘʀᴏ v.01</b> 〕━━━╮\n"
        "┃ ✨ <b>ᴀʙᴏᴜᴛ ᴛʜᴇ ʙᴏᴛ</b>\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"👋 ʜᴇʟʟᴏ, <b>{esc(_display_name(update))}</b>!\n\n"
        "🚀 ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ sᴘᴀᴍᴘʀᴏ v.01\n\n"
        '⚡ ᴠᴇʀsɪᴏɴ: <code>"v.01"</code>\n'
        '🤖 ᴛʏᴘᴇ: <code>"ᴛᴇʟᴇɢʀᴀᴍ ʙᴏᴛ"</code>\n'
        '🐍 ʀᴜɴᴛɪᴍᴇ: <code>"Python"</code>\n'
        '📡 sᴛᴀᴛᴜs: 🟢 <b>"ᴏɴʟɪɴᴇ"</b>\n\n'
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "✨ <b>ғᴇᴀᴛᴜʀᴇs</b>\n\n"
        "🏓 ʟɪᴠᴇ ʟᴀᴛᴇɴᴄʏ\n"
        "🔁 ʀᴇᴘᴇᴀᴛ sʏsᴛᴇᴍ\n"
        "🔐 ʟɪᴄᴇɴsᴇ sʏsᴛᴇᴍ\n"
        "📊 sʏsᴛᴇᴍ ᴍᴏɴɪᴛᴏʀɪɴɢ\n"
        "⚡ ʀᴇᴀʟ-ᴛɪᴍᴇ sᴛᴀᴛᴜs\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "❝ sɪᴍᴘʟᴇ ᴛᴏ ᴜsᴇ. ᴘᴏᴡᴇʀғᴜʟ ᴜɴᴅᴇʀ ᴛʜᴇ ʜᴏᴏᴅ. ❞\n\n"
        "🤖 sᴘᴀᴍᴘʀᴏ v.01",
    )


def _health_badge(
    state: bool | None,
    healthy_label: str,
    warning: bool = False,
) -> tuple[str, str]:
    if state is None:
        return "⚪", "ɴ/ᴀ — ᴄʜᴇᴄᴋ ᴜɴᴀᴠᴀɪʟᴀʙʟᴇ"
    if not state:
        return "🔴", "ᴜɴʜᴇᴀʟᴛʜʏ"
    if warning:
        return "🟡", "ᴡᴀʀɴɪɴɢ"
    return "🟢", healthy_label


def _health_panel_text(
    *,
    api_ok: bool | None,
    api_latency_ms: float | None,
    database_ok: bool | None,
    process_ok: bool | None,
    storage_ok: bool | None,
    storage_warning: bool,
    memory_ok: bool | None,
    memory_warning: bool,
    cpu_ok: bool | None,
    cpu_warning: bool,
    uptime: str,
    overall: tuple[str, str],
) -> str:
    bot_badge = _health_badge(process_ok, "ᴏᴘᴇʀᴀᴛɪᴏɴᴀʟ")
    process_badge = _health_badge(process_ok, "ʀᴜɴɴɪɴɢ")
    api_badge = _health_badge(api_ok, "ᴏᴋ")
    db_badge = _health_badge(database_ok, "ᴏᴋ")
    storage_badge = _health_badge(storage_ok, "ᴏᴋ", storage_warning)
    memory_badge = _health_badge(memory_ok, "ɴᴏʀᴍᴀʟ", memory_warning)
    cpu_badge = _health_badge(cpu_ok, "ɴᴏʀᴍᴀʟ", cpu_warning)
    latency = "N/A" if api_latency_ms is None else f"{api_latency_ms:.0f} ms"
    return (
        "╭━━━〔 ❤️ <b>sʏsᴛᴇᴍ ʜᴇᴀʟᴛʜ</b> 〕━━━╮\n"
        "┃ 🩺 <b>ʟɪᴠᴇ ʜᴇᴀʟᴛʜ ᴄʜᴇᴄᴋ</b>\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"🤖 ʙᴏᴛ: {bot_badge[0]} <b>{bot_badge[1]}</b>\n"
        f"📡 ᴛᴇʟᴇɢʀᴀᴍ ᴀᴘɪ: {api_badge[0]} <b>{api_badge[1]}</b>\n"
        f"🗄️ ᴅᴀᴛᴀʙᴀsᴇ: {db_badge[0]} <b>{db_badge[1]}</b>\n"
        f"💾 sᴛᴏʀᴀɢᴇ: {storage_badge[0]} <b>{storage_badge[1]}</b>\n"
        f"🧠 ᴍᴇᴍᴏʀʏ: {memory_badge[0]} <b>{memory_badge[1]}</b>\n"
        f"⚙️ ᴘʀᴏᴄᴇss: {process_badge[0]} <b>{process_badge[1]}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🏓 ʟᴀᴛᴇɴᴄʏ: <code>{latency}</code>\n"
        f"⏱️ ᴜᴘᴛɪᴍᴇ: <code>{esc(uptime)}</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{overall[0]} ᴏᴠᴇʀᴀʟʟ: <b>{overall[1]}</b>\n\n"
        "❝ ᴀʟʟ sʏsᴛᴇᴍs ᴀʀᴇ ʀᴜɴɴɪɴɢ sᴍᴏᴏᴛʜʟʏ. ❞"
    )


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    metrics = sm.collect_system_metrics()
    started = time.perf_counter()
    api_ok: bool | None = None
    try:
        await context.bot.get_me()
        api_ok = True
    except TelegramError:
        api_ok = False
    except Exception:
        api_ok = None
    api_latency_ms = (
        (time.perf_counter() - started) * 1000 if api_ok is not None else None
    )

    try:
        database_ok: bool | None = db.fetch_one("SELECT 1") is not None
    except Exception:
        database_ok = False

    process_ok: bool | None
    if sm.psutil is None:
        process_ok = None
    else:
        try:
            process_ok = sm.psutil.Process(os.getpid()).is_running()
        except (OSError, RuntimeError):
            process_ok = False

    storage_ok = metrics.disk_free_gb is not None
    storage_warning = bool(
        metrics.disk_usage_percent is not None
        and metrics.disk_usage_percent >= 90
    )
    memory_ok = metrics.ram_usage_percent is not None
    memory_warning = bool(
        metrics.ram_usage_percent is not None
        and metrics.ram_usage_percent >= 80
    )
    cpu_ok = metrics.cpu_usage_percent is not None
    cpu_warning = bool(
        metrics.cpu_usage_percent is not None
        and metrics.cpu_usage_percent >= 80
    )

    critical_failed = any(
        state is False for state in (api_ok, database_ok, process_ok, storage_ok)
    )
    unavailable = any(
        state is None
        for state in (api_ok, database_ok, process_ok, storage_ok, memory_ok, cpu_ok)
    )
    critical_load = any(
        value is not None and value >= 95
        for value in (
            metrics.cpu_usage_percent,
            metrics.ram_usage_percent,
            metrics.disk_usage_percent,
        )
    )
    if critical_failed or critical_load:
        overall = ("🔴", "ᴜɴʜᴇᴀʟᴛʜʏ")
    elif unavailable or memory_warning or cpu_warning or storage_warning:
        overall = ("🟡", "ᴡᴀʀɴɪɴɢ")
    else:
        overall = ("🟢", "ʜᴇᴀʟᴛʜʏ")

    await reply(
        update,
        _health_panel_text(
            api_ok=api_ok,
            api_latency_ms=api_latency_ms,
            database_ok=database_ok,
            process_ok=process_ok,
            storage_ok=storage_ok,
            storage_warning=storage_warning,
            memory_ok=memory_ok,
            memory_warning=memory_warning,
            cpu_ok=cpu_ok,
            cpu_warning=cpu_warning,
            uptime=sm.format_uptime(metrics.uptime_seconds),
            overall=overall,
        ),
    )


# --------------------------------------------------------------------------- #
# License commands
# --------------------------------------------------------------------------- #
async def cmd_genkey(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not admin_only(update):
        await reply(update, NOT_ADMIN)
        return
    key = lm.generate_key()
    log.info("Admin %s generated a new license key", update.effective_user.id)
    await reply(
        update,
        "🔑 <b>License Key Generated</b>\n\n"
        f"🎫 Key: <code>{esc(key)}</code>\n"
        f"⏳ Duration: {config.LICENSE_DAYS} Days\n"
        "🟡 Status: UNUSED\n\n"
        "Share this key privately with the user.",
    )


async def cmd_activate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    if not context.args:
        await reply(
            update,
            "⚠️ <b>Key Missing</b>\n\nUsage:\n"
            "<code>/activate SPAMBOT-91NK-N19S</code>",
        )
        return

    try:
        lic = lm.activate(context.args[0], user.id)
    except lm.ActivationError as exc:
        messages = {
            "invalid": "❌ <b>Invalid License</b>\n\nThe provided license key is invalid or does not exist.",
            "used": "⚠️ <b>Key Already Used</b>\n\nThis license key has already been activated.",
            "revoked": "🚫 <b>License Revoked</b>\n\nThis license key has been revoked and cannot be used.",
            "already_licensed": "🟢 <b>Already Licensed</b>\n\nYou already have an active license. Use <code>/license</code> to view it.",
        }
        await reply(update, messages.get(exc.reason, messages["invalid"]))
        return

    log.info("User %s activated license %s", user.id, lic.masked())
    await reply(
        update,
        "✅ <b>License Activated</b>\n\n"
        f"🔑 License: <code>{esc(lic.key)}</code>\n"
        f"⏳ Duration: {config.LICENSE_DAYS} Days\n"
        "🟢 Status: ACTIVE\n\n"
        "Your license has been activated successfully.",
    )


async def cmd_license(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    lic = lm.get_user_license(user.id)
    if lic is None or lic.status == lm.UNUSED:
        await reply(
            update,
            "⚪ <b>No Active License</b>\n\nYou don't currently have a valid license.",
        )
        return
    if lic.status == lm.REVOKED:
        await reply(
            update,
            "🚫 <b>License Revoked</b>\n\nYour license has been revoked. "
            "Please activate a new license to continue.",
        )
        return
    if lic.status == lm.EXPIRED:
        await reply(
            update,
            "🔴 <b>License Expired</b>\n\nYour 30-day license has expired.\n\n"
            "Please activate a new license to continue.",
        )
        return

    activated = lic.activated_at.strftime("%d %b %Y") if lic.activated_at else "—"
    expires = lic.expires_at.strftime("%d %b %Y") if lic.expires_at else "—"
    await reply(
        update,
        "🔐 <b>Your License</b>\n\n"
        "🟢 Status: ACTIVE\n"
        f"📅 Activated: <code>{activated}</code>\n"
        f"⏳ Expires: <code>{expires}</code>\n"
        f"🕐 Remaining: {lic.days_remaining} days",
    )


async def cmd_keys(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not admin_only(update):
        await reply(update, NOT_ADMIN)
        return
    s = lm.stats()
    await reply(
        update,
        "🔐 <b>License Statistics</b>\n\n"
        f"🎫 Total Keys: {s['total']}\n"
        f"🟡 Unused: {s['unused']}\n"
        f"🟢 Active: {s['active']}\n"
        f"🔴 Expired: {s['expired']}\n"
        f"🚫 Revoked: {s['revoked']}",
    )


async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not admin_only(update):
        await reply(update, NOT_ADMIN)
        return
    if not context.args:
        await reply(
            update,
            "⚠️ <b>Key Missing</b>\n\nUsage:\n<code>/revoke SPAMBOT-91NK-N19S</code>",
        )
        return
    key = lm.normalize(context.args[0])
    if not lm.revoke(key):
        await reply(
            update,
            "❌ <b>Invalid License</b>\n\nThe provided license key is invalid, "
            "does not exist, or is already revoked.",
        )
        return

    lic = lm.get_license(key)
    if lic and lic.user_id:
        job = await registry.cancel(lic.user_id)
        if job:
            log.info("Cancelled running task for user %s after revoke", lic.user_id)
    await reply(
        update,
        "🚫 <b>License Revoked</b>\n\nThe selected license has been successfully revoked.",
    )


def _masked_scan_key(key: str) -> str:
    prefix = lm.normalize(key).split("-", 1)[0]
    return f"{prefix}-****-****"


def _license_status_display(status: str) -> tuple[str, str]:
    return {
        lm.UNUSED: ("🟡", "ᴜɴᴜsᴇᴅ"),
        lm.ACTIVE: ("🟢", "ᴀᴄᴛɪᴠᴇ"),
        lm.EXPIRED: ("🔴", "ᴇxᴘɪʀᴇᴅ"),
        lm.REVOKED: ("⚫", "ʀᴇᴠᴏᴋᴇᴅ"),
    }.get(status, ("❓", "ᴜɴᴋɴᴏᴡɴ"))


def _format_license_date(value) -> str:
    return value.astimezone().strftime("%d %b %Y, %H:%M %Z") if value else "—"


async def cmd_scankey(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not admin_only(update):
        await reply(update, NOT_ADMIN)
        return
    if not context.args:
        await reply(
            update,
            "╭━━━〔 🔍 <b>ᴋᴇʏ sᴄᴀɴɴᴇʀ</b> 〕━━━╮\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "Usage:\n"
            "<code>/scankey &lt;LICENSE_KEY&gt;</code>",
        )
        return

    raw_key = context.args[0]
    if not lm.is_valid_key_format(raw_key):
        await reply(
            update,
            "🔍 <b>ᴋᴇʏ sᴄᴀɴ ʀᴇsᴜʟᴛ</b>\n\n"
            "❓ ɴᴏᴛ ғᴏᴜɴᴅ\n"
            "The key format is invalid or the key does not exist.",
        )
        return

    lm.refresh_expired()
    lic = lm.get_license(raw_key)
    if lic is None:
        await reply(
            update,
            "🔍 <b>ᴋᴇʏ sᴄᴀɴ ʀᴇsᴜʟᴛ</b>\n\n"
            "❓ ɴᴏᴛ ғᴏᴜɴᴅ\n"
            "No matching license key was found.",
        )
        return

    indicator, status = _license_status_display(lic.status)
    user_value = str(lic.user_id) if lic.user_id is not None else "—"
    remaining = f"{lic.days_remaining} days" if lic.expires_at else "—"
    await reply(
        update,
        "╭━━━〔 🔍 <b>ᴋᴇʏ sᴄᴀɴ ʀᴇsᴜʟᴛ</b> 〕━━━╮\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"🔑 ᴋᴇʏ: <code>{esc(_masked_scan_key(lic.key))}</code>\n"
        f"📊 sᴛᴀᴛᴜs: {indicator} <b>{status}</b>\n"
        f'👤 ᴜsᴇʀ: <code>"{esc(user_value)}"</code>\n'
        f"📅 ᴀᴄᴛɪᴠᴀᴛᴇᴅ: <code>{esc(_format_license_date(lic.activated_at))}</code>\n"
        f"⏳ ᴇxᴘɪʀᴇs: <code>{esc(_format_license_date(lic.expires_at))}</code>\n"
        f"⌛ ʀᴇᴍᴀɪɴɪɴɢ: <code>{remaining}</code>",
    )


# --------------------------------------------------------------------------- #
# Repeat system
# --------------------------------------------------------------------------- #
def parse_repeat_args(
    args: list[str], default_mode: str = config.DEFAULT_MODE
) -> tuple[int, str, str] | str:
    """Return (count, message, mode) or an error code."""
    if not args:
        return "missing_message"
    raw_count = args[0]
    if not raw_count.isdigit():
        return "invalid_count"
    count = int(raw_count)
    if count < config.MIN_REPEAT:
        return "invalid_count"
    if count > config.MAX_REPEAT:
        return "limit_exceeded"
    mode = default_mode
    message_start = 1
    if len(args) > 1 and args[1].lower() in config.SPAM_MODES:
        mode = args[1].lower()
        message_start = 2
    message = " ".join(args[message_start:]).strip()
    if not message:
        return "missing_message"
    return count, message, mode


async def _run_repeat(
    job: RepeatTask,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    reply_to: int | None,
    message_thread_id: int | None = None,
) -> None:
    stopped_reason: str | None = None
    try:
        for _ in range(job.total):
            if job.cancelled:
                break
            try:
                send_kwargs = {
                    "chat_id": chat_id,
                    "text": text,
                }
                if message_thread_id is not None:
                    send_kwargs["message_thread_id"] = message_thread_id
                if reply_to is not None:
                    send_kwargs["reply_parameters"] = ReplyParameters(
                        message_id=reply_to,
                        allow_sending_without_reply=False,
                    )
                else:
                    send_kwargs["allow_sending_without_reply"] = True
                await context.bot.send_message(**send_kwargs)
                job.sent += 1
            except RetryAfter as exc:
                # Telegram asked us to slow down — honour it exactly, never bypass.
                wait_for = float(exc.retry_after) + 1
                log.warning("Rate limited, waiting %.1fs", wait_for)
                await asyncio.sleep(wait_for)
                continue
            except Forbidden:
                stopped_reason = "🚫 The bot is not allowed to send messages here."
                break
            except TelegramError as exc:
                log.warning("Telegram error while repeating: %s", exc)
                stopped_reason = f"⚠️ Telegram error: {esc(str(exc))}"
                break

            if job.sent < job.total:
                await asyncio.sleep(config.SPAM_MODES[job.mode])
    except asyncio.CancelledError:
        job.cancelled = True
        raise
    finally:
        await registry.finish(job.user_id)
        try:
            if job.cancelled:
                summary = (
                    "🛑 <b>Repeat Cancelled</b>\n\n"
                    f"📤 Messages sent: {job.sent}/{job.total}"
                )
            elif stopped_reason:
                summary = (
                    "🛑 <b>Repeat Stopped</b>\n\n"
                    f"{stopped_reason}\n"
                    f"📤 Messages sent: {job.sent}/{job.total}"
                )
            else:
                summary = (
                    "✅ <b>Repeat Completed</b>\n\n"
                    f"📤 Messages sent: {job.sent}\n"
                    "🎉 Task completed successfully!"
                )
            await context.bot.send_message(
                chat_id=chat_id, text=summary, parse_mode=ParseMode.HTML
            )
        except TelegramError:
            pass


async def cmd_repeat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    forced_mode: str | None = None,
) -> None:
    user_id = requester_id(update)
    message = update.effective_message
    if user_id is None or not message:
        return

    # Validate the license before every task.
    if not lm.has_active_license(user_id):
        await reply(update, NO_LICENSE)
        return

    parsed = parse_repeat_args(context.args or [], forced_mode or config.DEFAULT_MODE)
    if parsed == "missing_message":
        await reply(
            update,
            "⚠️ <b>Message Missing</b>\n\nUsage:\n"
            "<code>/repeat 10 Hello everyone 👋</code>",
        )
        return
    if parsed == "invalid_count":
        await reply(
            update,
            "❌ <b>Invalid Count</b>\n\n"
            f"Enter a number between {config.MIN_REPEAT} and {config.MAX_REPEAT}.",
        )
        return
    if parsed == "limit_exceeded":
        await reply(
            update,
            "🚫 <b>Limit Exceeded</b>\n\n"
            f"Maximum allowed repetitions: {config.MAX_REPEAT}.",
        )
        return

    count, text, mode = parsed  # type: ignore[misc]

    if registry.get(user_id):
        await reply(
            update,
            "⏳ <b>Task Already Running</b>\n\n"
            "You already have an active repeat task.\n\n"
            "Use <code>/stop</code> to cancel it first.",
        )
        return

    cooldown = registry.cooldown_left(user_id)
    if cooldown:
        await reply(
            update,
            f"🧊 <b>Cooldown Active</b>\n\nPlease wait {cooldown} second(s) "
            "before starting another repeat task.",
        )
        return

    job = await registry.start(user_id, count, mode)
    if job is None:
        await reply(
            update,
            "⏳ <b>Task Already Running</b>\n\nUse <code>/stop</code> to cancel it first.",
        )
        return

    target = message.reply_to_message
    reply_to = target.message_id if target else None
    message_thread_id = getattr(message, "message_thread_id", None)

    if reply_to:
        await reply(
            update,
            "🎯 <b>Reply Repeat Started</b>\n\n"
            f'📝 Message: "{preview(text)}"\n'
            f"🔢 Count: {count}\n"
            "🎯 Target: Replied message\n"
            f"🚦 Mode: {config.SPAM_MODE_LABELS[mode]}\n"
            f"⏱️ Delay: {config.SPAM_MODES[mode]:g} second(s)",
        )
    else:
        await reply(
            update,
            "🚀 <b>Repeat Started</b>\n\n"
            f'📝 Message: "{preview(text)}"\n'
            f"🔢 Count: {count}\n"
            f"🚦 Mode: {config.SPAM_MODE_LABELS[mode]}\n"
            f"⏱️ Delay: {config.SPAM_MODES[mode]:g} second(s)\n"
            "📤 Delivery: Normal",
        )

    job.task = asyncio.create_task(
        _run_repeat(
            job,
            context,
            message.chat_id,
            text,
            reply_to,
            message_thread_id,
        )
    )


async def cmd_basic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_repeat(update, context, forced_mode="basic")


async def cmd_medium(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_repeat(update, context, forced_mode="medium")


async def cmd_aggressive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_repeat(update, context, forced_mode="aggressive")


def _utf16_slice(text: str, start: int, end: int | None = None) -> str:
    encoded = text.encode("utf-16-le")
    stop = None if end is None else end * 2
    return encoded[start * 2 : stop].decode("utf-16-le", errors="ignore")


def _command_parts(message, entity) -> tuple[str, str, str | None]:
    token = message.parse_entity(entity)
    if not token.startswith("/"):
        return "", "", None
    command, separator, target = token[1:].partition("@")
    command = command.lower()
    tail_start = entity.offset + entity.length
    tail = _utf16_slice(message.text, tail_start)
    if separator:
        target = target.lower()
    else:
        target = None
    return command, tail, target


async def _message_mentions_bot(message, context, command_target: str | None) -> bool:
    username = context.bot.username
    if not username:
        try:
            me = await context.bot.get_me()
            username = me.username
        except TelegramError:
            return False
    if not username:
        return False
    username = username.casefold()
    if command_target is not None:
        return command_target.casefold() == username

    for entity in message.entities or []:
        try:
            if entity.type == "mention":
                mention = message.parse_entity(entity).lstrip("@").casefold()
                if mention == username:
                    return True
            elif entity.type == "text_mention":
                mentioned_user = getattr(entity, "user", None)
                if mentioned_user and mentioned_user.id == context.bot.id:
                    return True
        except (AttributeError, ValueError):
            continue
    return False


async def _create_or_delete_folder(
    update: Update,
    folder_id: int,
    content: str,
    deleting: bool,
) -> None:
    if not admin_only(update):
        await reply(update, NOT_ADMIN)
        return
    chat = update.effective_chat
    if not chat or chat.type != "private":
        await reply(
            update,
            "🔐 Folder creation and deletion are available in an admin private chat.",
        )
        return
    if deleting:
        try:
            removed = db.delete_folder(folder_id)
        except Exception:
            log.exception("Could not delete folder %s", folder_id)
            await reply(
                update,
                "⚠️ Could not delete this folder right now. Please try again.",
            )
            return
        if removed:
            await reply(
                update,
                f"🗑️ <b>Folder {folder_id} Deleted</b>\n\n"
                "Its saved message has been permanently removed.",
            )
        else:
            await reply(
                update,
                f"📂 <b>Folder {folder_id} Not Found</b>\n\n"
                "There is no saved folder with that number.",
            )
        return

    if not content.strip():
        await reply(
            update,
            f"📝 <b>Folder {folder_id}</b>\n\n"
            f"Send the text to save:\n<code>/folder{folder_id} Your message</code>",
        )
        return
    if len(content) > MAX_FOLDER_TEXT_LENGTH:
        await reply(
            update,
            f"⚠️ Folder text is too long. The maximum is "
            f"{MAX_FOLDER_TEXT_LENGTH} characters.",
        )
        return

    try:
        db.save_folder(
            folder_id,
            content,
            datetime.now(timezone.utc).isoformat(),
        )
    except Exception:
        log.exception("Could not save folder %s", folder_id)
        await reply(
            update,
            "⚠️ Could not save this folder right now. Please try again.",
        )
        return
    await reply(
        update,
        f"✅ <b>Folder {folder_id} Saved</b>\n\n"
        "Your message is stored securely and ready to use in a group.",
    )


async def _run_folder_in_group(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    folder_id: int,
    tail: str,
) -> None:
    user_id = requester_id(update)
    message = update.effective_message
    if user_id is None or not message:
        return
    if not lm.has_active_license(user_id):
        await reply(update, NO_LICENSE)
        return

    args = tail.split()
    if len(args) != 1 or not args[0].isdigit():
        await reply(
            update,
            "⚠️ <b>Spam Amount Required</b>\n\n"
            f"Usage: <code>/folder{folder_id} &lt;count&gt;</code>",
        )
        return
    count = int(args[0])
    if count < config.MIN_REPEAT:
        await reply(
            update,
            f"❌ Count must be between {config.MIN_REPEAT} and {config.MAX_REPEAT}.",
        )
        return
    if count > config.MAX_REPEAT:
        await reply(
            update,
            f"🚫 Maximum allowed repetitions: {config.MAX_REPEAT}.",
        )
        return

    try:
        saved_text = db.get_folder(folder_id)
    except Exception:
        log.exception("Could not read folder %s", folder_id)
        await reply(update, "⚠️ Folder storage is temporarily unavailable.")
        return
    if not saved_text:
        await reply(
            update,
            f"📂 <b>Folder {folder_id} Does Not Exist</b>\n\n"
            "Ask an administrator to create it in a private chat.",
        )
        return

    if registry.get(user_id):
        await reply(
            update,
            "⏳ <b>Task Already Running</b>\n\n"
            "Use <code>/stop</code> before starting another repeat task.",
        )
        return
    cooldown = registry.cooldown_left(user_id)
    if cooldown:
        await reply(
            update,
            f"🧊 <b>Cooldown Active</b>\n\nWait {cooldown} second(s) "
            "before starting another task.",
        )
        return

    mode = config.DEFAULT_MODE
    job = await registry.start(user_id, count, mode)
    if job is None:
        await reply(update, "⏳ <b>Task Already Running</b>")
        return
    await reply(
        update,
        f"📂 <b>Folder {folder_id} Repeat Started</b>\n\n"
        f"🔢 Count: {count}\n"
        f"🚦 Mode: {config.SPAM_MODE_LABELS[mode]}\n"
        f"⏱️ Delay: {config.SPAM_MODES[mode]:g} second(s)",
    )
    job.task = asyncio.create_task(
        _run_repeat(
            job,
            context,
            message.chat_id,
            saved_text,
            None,
            getattr(message, "message_thread_id", None),
        )
    )


async def cmd_folder_dispatch(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle dynamic /folderN and /deletefolderN command names."""
    message = update.effective_message
    chat = update.effective_chat
    if not message or not message.text or not chat:
        return
    for entity in message.entities or []:
        if entity.type != "bot_command":
            continue
        try:
            command, tail, command_target = _command_parts(message, entity)
        except (AttributeError, ValueError, UnicodeError):
            continue
        match = re.fullmatch(r"(deletefolder|folder)(\d+)", command)
        if not match:
            continue
        operation, number_text = match.groups()
        folder_id = int(number_text)
        if not 1 <= folder_id <= MAX_FOLDER_NUMBER:
            await reply(
                update,
                f"⚠️ Folder number must be between 1 and {MAX_FOLDER_NUMBER}.",
            )
            return

        if chat.type == "private":
            if command_target is not None:
                username = context.bot.username
                if username and command_target.casefold() != username.casefold():
                    return
            content = tail[1:] if tail[:1].isspace() else tail
            await _create_or_delete_folder(
                update,
                folder_id,
                content,
                deleting=operation == "deletefolder",
            )
            return

        if chat.type not in {"group", "supergroup"}:
            return
        if operation == "deletefolder":
            if admin_only(update):
                await reply(
                    update,
                    "🔐 Manage folders with <code>/deletefolderN</code> "
                    "in an admin private chat.",
                )
            return
        if not await _message_mentions_bot(message, context, command_target):
            return
        await _run_folder_in_group(update, context, folder_id, tail)
        return


async def cmd_channel_post(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Dispatch supported commands received as channel_post updates.

    CommandHandler handles normal private/group messages, but channel posts
    need a MessageHandler because they have no effective user.
    """
    message = update.channel_post
    if not message or not message.text or not message.entities:
        return

    entity = message.entities[0]
    if entity.type != "bot_command" or entity.offset != 0:
        return

    command = message.text[1 : entity.length].split("@", 1)[0].lower()
    handler = {
        "repeat": cmd_repeat,
        "basic": cmd_basic,
        "medium": cmd_medium,
        "aggressive": cmd_aggressive,
        "stop": cmd_stop,
        "ping": cmd_ping,
        "id": cmd_id,
        "about": cmd_about,
        "health": cmd_health,
    }.get(command)
    if handler is None:
        return

    context.args = message.text.split()[1:]
    await handler(update, context)


async def cmd_stop(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = requester_id(update)
    if user_id is None:
        return
    job = await registry.cancel(user_id)
    if job is None:
        await reply(
            update,
            "ℹ️ <b>No Active Task</b>\n\nYou don't have any running repeat task.",
        )
        return
    await reply(
        update,
        "🛑 <b>Repeat Stopped</b>\n\n"
        "Your active repeat task has been cancelled successfully.",
    )


def _expiry_notification_text(lic: lm.License, milestone: str, days: int) -> str:
    if milestone == lm.NOTIFY_EXPIRED:
        return (
            "╭━━━〔 🤖 <b>sᴘᴀᴍᴘʀᴏ v.01</b> 〕━━━╮\n"
            "┃ 🔴 <b>ʟɪᴄᴇɴsᴇ ᴇxᴘɪʀᴇᴅ</b>\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "Your SpamPro v.01 license has expired.\n\n"
            f"📅 ᴇxᴘɪʀᴇᴅ: <code>{esc(_format_license_date(lic.expires_at))}</code>\n\n"
            "🔐 Please activate a new license to continue."
        )
    return (
        "╭━━━〔 🤖 <b>sᴘᴀᴍᴘʀᴏ v.01</b> 〕━━━╮\n"
        "┃ 🔔 <b>ʟɪᴄᴇɴsᴇ ʀᴇᴍɪɴᴅᴇʀ</b>\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "🔑 Your SpamPro v.01 license is expiring soon.\n\n"
        f"⏳ ʀᴇᴍᴀɪɴɪɴɢ: <code>{days} days</code>\n\n"
        "❝ ᴋᴇᴇᴘ ʏᴏᴜʀ ʟɪᴄᴇɴsᴇ ᴀᴄᴛɪᴠᴇ. ❞"
    )


async def send_due_license_notifications(bot) -> None:
    """Send each due expiry milestone once using persistent SQLite state."""
    for lic in lm.licenses_for_expiry_notifications():
        due = lm.expiry_milestone(lic)
        if due is None or lic.user_id is None:
            continue
        milestone, days = due
        if lm.notification_sent(lic.key, milestone):
            continue

        text = _expiry_notification_text(lic, milestone, days)
        try:
            await bot.send_message(
                chat_id=lic.user_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
        except Forbidden:
            # The user may have blocked/deleted the bot. Do not retry forever.
            log.info(
                "Could not deliver %s expiry notice to user %s; marking handled",
                milestone,
                lic.user_id,
            )
            lm.record_notification(lic.key, milestone, lic.user_id)
        except TelegramError as exc:
            # Transient Telegram errors remain eligible for the next check.
            log.warning(
                "Could not deliver %s expiry notice for %s: %s",
                milestone,
                lic.masked(),
                exc,
            )
        else:
            lm.record_notification(lic.key, milestone, lic.user_id)


async def expiry_notification_loop(application: Application) -> None:
    try:
        while True:
            try:
                await send_due_license_notifications(application.bot)
            except Exception:
                log.exception("License expiry notification check failed")
            await asyncio.sleep(EXPIRY_CHECK_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        raise


def _command_signature(commands: list[BotCommand] | tuple[BotCommand, ...]) -> tuple[tuple[str, str], ...]:
    return tuple((command.command, command.description) for command in commands)


async def configure_command_menu(application: Application) -> None:
    """Keep default and admin Telegram menu scopes synchronized."""
    if application.bot_data.get(COMMAND_MENU_CONFIGURED_KEY):
        return

    bot = application.bot
    try:
        default_scope = BotCommandScopeDefault()
        current_default = await bot.get_my_commands(scope=default_scope)
        if _command_signature(current_default) != _command_signature(USER_COMMANDS):
            await bot.set_my_commands(list(USER_COMMANDS), scope=default_scope)

        for admin_id in sorted(config.ADMIN_IDS):
            admin_scope = BotCommandScopeChat(chat_id=admin_id)
            try:
                current_admin = await bot.get_my_commands(scope=admin_scope)
                if _command_signature(current_admin) != _command_signature(ADMIN_COMMANDS):
                    await bot.set_my_commands(list(ADMIN_COMMANDS), scope=admin_scope)
            except TelegramError as exc:
                # A user who has not opened the bot yet may not have a chat
                # scope available. The safe default menu remains active.
                log.warning(
                    "Could not configure admin command menu for %s: %s",
                    admin_id,
                    exc,
                )
    except TelegramError as exc:
        log.warning("Could not configure Telegram command menu: %s", exc)
        return

    application.bot_data[COMMAND_MENU_CONFIGURED_KEY] = True


# --------------------------------------------------------------------------- #
# Errors & lifecycle
# --------------------------------------------------------------------------- #
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error: %s", context.error, exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong. Please try again."
            )
        except TelegramError:
            pass


async def on_startup(application: Application) -> None:
    await configure_command_menu(application)
    existing = application.bot_data.get(EXPIRY_TASK_KEY)
    if existing and not existing.done():
        return
    application.bot_data[EXPIRY_TASK_KEY] = asyncio.create_task(
        expiry_notification_loop(application),
        name="license-expiry-notifications",
    )


async def on_shutdown(application: Application) -> None:
    expiry_task = application.bot_data.pop(EXPIRY_TASK_KEY, None)
    if expiry_task and not expiry_task.done():
        expiry_task.cancel()
        await asyncio.gather(expiry_task, return_exceptions=True)
    log.info("Shutting down — cancelling active repeat tasks")
    await registry.cancel_all()
    await asyncio.sleep(0.1)


def build_application() -> Application:
    config.validate()
    db.init_db()
    lm.refresh_expired()

    app = (
        ApplicationBuilder()
        .token(config.BOT_TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )
    app.add_handler(
        CallbackQueryHandler(
            cmd_start_button,
            pattern=(
                f"^({START_HELP_CALLBACK}|{START_PING_CALLBACK}|"
                f"{PING_REFRESH_CALLBACK})$"
            ),
        )
    )
    app.add_handler(
        MessageHandler(filters.TEXT, cmd_folder_dispatch),
        group=-1,
    )
    app.add_handler(
        MessageHandler(
            filters.UpdateType.CHANNEL_POST,
            cmd_channel_post,
        )
    )
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("about", cmd_about))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("repeat", cmd_repeat))
    app.add_handler(CommandHandler("basic", cmd_basic))
    app.add_handler(CommandHandler("medium", cmd_medium))
    app.add_handler(CommandHandler("aggressive", cmd_aggressive))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("activate", cmd_activate))
    app.add_handler(CommandHandler("license", cmd_license))
    app.add_handler(CommandHandler("genkey", cmd_genkey))
    app.add_handler(CommandHandler("keys", cmd_keys))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("scankey", cmd_scankey))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    app = build_application()
    modes = ", ".join(
        f"{config.SPAM_MODE_LABELS[name]}={delay:g}s"
        for name, delay in config.SPAM_MODES.items()
    )
    log.info("Repeat Bot is running (max=%s, modes=%s)", config.MAX_REPEAT, modes)
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
