"""Offline checks for the license system, argument parsing and task registry.

Run with:  python -m pytest telegram-bot/tests -q
(no Telegram network access required)
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.error import TelegramError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    os.environ["BOT_TOKEN"] = "test-token"
    os.environ["ADMIN_IDS"] = "111,222"
    import config
    import database as db
    import license_manager as lm

    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "licenses.db"))
    monkeypatch.setattr(config, "ADMIN_IDS", {111, 222})
    db.init_db()
    yield lm


# --------------------------- license key format ---------------------------- #
def test_key_format_and_uniqueness():
    import license_manager as lm

    keys = {lm.generate_key() for _ in range(50)}
    assert len(keys) == 50
    for key in keys:
        prefix, a, b = key.split("-")
        assert prefix == "SPAMBOT"
        assert len(a) == len(b) == 4
        assert all(c in lm.ALPHABET for c in a + b)


# ------------------------------- activation -------------------------------- #
def test_activation_flow():
    import license_manager as lm

    key = lm.generate_key()
    lic = lm.activate(key, 500)
    assert lic.status == lm.ACTIVE
    assert lm.has_active_license(500)
    assert (lic.expires_at - lic.activated_at).days == 30


def test_invalid_key():
    import license_manager as lm

    with pytest.raises(lm.ActivationError) as exc:
        lm.activate("SPAMBOT-0000-0000", 501)
    assert exc.value.reason == "invalid"


def test_duplicate_activation():
    import license_manager as lm

    key = lm.generate_key()
    lm.activate(key, 502)
    with pytest.raises(lm.ActivationError) as exc:
        lm.activate(key, 503)
    assert exc.value.reason == "used"


def test_revoked_key_cannot_activate():
    import license_manager as lm

    key = lm.generate_key()
    assert lm.revoke(key) is True
    with pytest.raises(lm.ActivationError) as exc:
        lm.activate(key, 504)
    assert exc.value.reason == "revoked"


def test_expiration_after_30_days():
    import database as db
    import license_manager as lm

    key = lm.generate_key()
    lic = lm.activate(key, 505)
    past = lm.now() - timedelta(days=1)
    db.execute(
        "UPDATE licenses SET expires_at = ? WHERE key = ?",
        (past.isoformat(), lic.key),
    )
    assert lm.has_active_license(505) is False
    assert lm.get_user_license(505).status == lm.EXPIRED


def test_persistence_across_restart():
    import database as db
    import license_manager as lm

    key = lm.generate_key()
    lm.activate(key, 506)
    db.init_db()  # simulates a restart re-opening the same file
    assert lm.has_active_license(506)


def test_stats():
    import license_manager as lm

    k1, k2, k3 = lm.generate_key(), lm.generate_key(), lm.generate_key()
    lm.activate(k1, 601)
    lm.revoke(k3)
    s = lm.stats()
    assert s == {"total": 3, "unused": 1, "active": 1, "expired": 0, "revoked": 1}
    assert k2  # unused


def test_admin_check():
    import config

    assert config.is_admin(111) is True
    assert config.is_admin(999) is False
    assert config.is_admin(None) is False


def test_ping_latency_status_thresholds():
    import bot

    assert bot.ping_latency_status(99.9)[1] == "ᴇxᴄᴇʟʟᴇɴᴛ"
    assert bot.ping_latency_status(100)[1] == "ɢᴏᴏᴅ"
    assert bot.ping_latency_status(300)[1] == "ɢᴏᴏᴅ"
    assert bot.ping_latency_status(300.1)[1] == "ᴀᴠᴇʀᴀɢᴇ"
    assert bot.ping_latency_status(700)[1] == "ᴀᴠᴇʀᴀɢᴇ"
    assert bot.ping_latency_status(700.1)[1] == "ʜɪɢʜ ʟᴀᴛᴇɴᴄʏ"


def test_system_metrics_are_real_and_gpu_fallback_is_safe(monkeypatch):
    import system_metrics as sm

    monkeypatch.setattr(sm.shutil, "which", lambda _: None)
    metrics = sm.collect_system_metrics()

    assert metrics.cpu_cores and metrics.cpu_cores > 0
    assert metrics.ram_total_gb and metrics.ram_total_gb > 0
    assert metrics.disk_total_gb and metrics.disk_total_gb > 0
    assert metrics.uptime_seconds is not None
    assert metrics.gpu.model is None
    assert sm.format_uptime(metrics.uptime_seconds).endswith(("m", "h", "d"))


def test_system_status_uses_dynamic_resource_thresholds():
    import system_metrics as sm

    base = dict(
        cpu_model="test CPU",
        cpu_usage_percent=10.0,
        cpu_cores=4,
        gpu=sm.GPUInfo(),
        ram_used_gb=1.0,
        ram_total_gb=8.0,
        ram_usage_percent=20.0,
        disk_used_gb=10.0,
        disk_total_gb=100.0,
        disk_free_gb=90.0,
        disk_usage_percent=10.0,
        uptime_seconds=120.0,
        python_version="3.13.0",
        platform_name="Linux test",
        hostname="safe-host",
    )
    assert sm.overall_status(sm.SystemMetrics(**base))[1] == "ᴇxᴄᴇʟʟᴇɴᴛ"
    base["cpu_usage_percent"] = 65.0
    assert sm.overall_status(sm.SystemMetrics(**base))[1] == "ᴍᴏᴅᴇʀᴀᴛᴇ"
    base["cpu_usage_percent"] = 90.0
    assert sm.overall_status(sm.SystemMetrics(**base))[1] == "ʜɪɢʜ ʟᴏᴀᴅ"


def test_ping_panel_contains_live_metrics_and_refresh_button():
    import bot
    import system_metrics as sm

    metrics = sm.SystemMetrics(
        cpu_model="Test CPU & Host",
        cpu_usage_percent=12.5,
        cpu_cores=8,
        gpu=sm.GPUInfo(),
        ram_used_gb=2.0,
        ram_total_gb=8.0,
        ram_usage_percent=25.0,
        disk_used_gb=40.0,
        disk_total_gb=100.0,
        disk_free_gb=60.0,
        disk_usage_percent=40.0,
        uptime_seconds=3661.0,
        python_version="3.13.0",
        platform_name="Linux test",
        hostname="safe-host",
    )

    text = bot.ping_panel_text(47.0, metrics)

    assert "47 ms" in text
    assert "Test CPU &amp; Host" in text
    assert "ɴᴏ ɢᴘᴜ" in text
    assert "2.00 / 8.00 GB" in text
    assert "1h 1m" in text
    assert "sᴀғᴇ-ʜᴏsᴛ" not in text  # hostname is displayed as actual safe-host
    assert "safe-host" in text
    assert bot.ping_keyboard().inline_keyboard[0][0].callback_data == (
        bot.PING_REFRESH_CALLBACK
    )


def test_ping_command_measures_api_latency_and_edits_panel(monkeypatch):
    import bot
    import system_metrics as sm

    metrics = sm.SystemMetrics(
        cpu_model="Test CPU",
        cpu_usage_percent=5.0,
        cpu_cores=4,
        gpu=sm.GPUInfo(),
        ram_used_gb=1.0,
        ram_total_gb=4.0,
        ram_usage_percent=25.0,
        disk_used_gb=2.0,
        disk_total_gb=20.0,
        disk_free_gb=18.0,
        disk_usage_percent=10.0,
        uptime_seconds=60.0,
        python_version="3.13.0",
        platform_name="Linux test",
        hostname="safe-host",
    )

    class FakeResponse:
        def __init__(self):
            self.edits = []

        async def edit_text(self, text, **kwargs):
            self.edits.append((text, kwargs))

    class FakeMessage:
        def __init__(self):
            self.response = FakeResponse()
            self.sent = []

        async def reply_text(self, text, **kwargs):
            self.sent.append((text, kwargs))
            return self.response

    async def scenario():
        clock = iter((40.0, 40.082))
        monkeypatch.setattr(bot.time, "perf_counter", lambda: next(clock))
        monkeypatch.setattr(sm, "collect_system_metrics", lambda: metrics)
        message = FakeMessage()
        update = SimpleNamespace(effective_message=message)
        await bot.cmd_ping(update, None)
        assert "Measuring live response latency" in message.sent[0][0]
        assert "82 ms" in message.response.edits[0][0]
        assert "Test CPU" in message.response.edits[0][0]
        assert message.response.edits[0][1]["reply_markup"].inline_keyboard

    asyncio.run(scenario())


def test_ping_refresh_edits_existing_message(monkeypatch):
    import bot
    import system_metrics as sm

    metrics = sm.SystemMetrics(
        cpu_model="Test CPU",
        cpu_usage_percent=1.0,
        cpu_cores=2,
        gpu=sm.GPUInfo(),
        ram_used_gb=1.0,
        ram_total_gb=4.0,
        ram_usage_percent=25.0,
        disk_used_gb=2.0,
        disk_total_gb=20.0,
        disk_free_gb=18.0,
        disk_usage_percent=10.0,
        uptime_seconds=60.0,
        python_version="3.13.0",
        platform_name="Linux test",
        hostname="safe-host",
    )

    class FakeMessage:
        def __init__(self):
            self.edits = []

        async def edit_text(self, text, **kwargs):
            self.edits.append((text, kwargs))

    async def scenario():
        clock = iter((30.0, 30.123))
        monkeypatch.setattr(bot.time, "perf_counter", lambda: next(clock))
        monkeypatch.setattr(sm, "collect_system_metrics", lambda: metrics)
        message = FakeMessage()
        await bot.refresh_ping_message(message)
        assert len(message.edits) == 2
        assert "Refreshing live system metrics" in message.edits[0][0]
        assert "123 ms" in message.edits[1][0]

    asyncio.run(scenario())


def test_start_welcome_panel_uses_measured_latency_and_personalizes():
    import bot

    class FakeResponse:
        def __init__(self):
            self.edits = []

        async def edit_text(self, text, **kwargs):
            self.edits.append((text, kwargs))

    class FakeMessage:
        def __init__(self):
            self.sent = []
            self.response = FakeResponse()

        async def reply_text(self, text, **kwargs):
            self.sent.append((text, kwargs))
            return self.response

    async def scenario():
        clock = iter((10.0, 10.047, 20.0, 20.047))
        original_clock = bot.time.perf_counter
        bot.time.perf_counter = lambda: next(clock)
        try:
            for first_name in ("Asha", "Ravi <Admin>"):
                message = FakeMessage()
                update = SimpleNamespace(
                    effective_message=message,
                    effective_user=SimpleNamespace(
                        first_name=first_name,
                        username=None,
                    ),
                )
                await bot.cmd_start(update, None)

                assert "⏳ Preparing your live system panel" in message.sent[0][0]
                final_text, final_kwargs = message.response.edits[0]
                assert "47 ms" in final_text
                assert f"<b>{bot.esc(first_name)}</b>" in final_text
                assert final_kwargs["parse_mode"] == "HTML"
                keyboard = final_kwargs["reply_markup"]
                callbacks = [
                    button.callback_data
                    for row in keyboard.inline_keyboard
                    for button in row
                ]
                assert callbacks == [
                    bot.START_HELP_CALLBACK,
                    bot.START_PING_CALLBACK,
                ]
        finally:
            bot.time.perf_counter = original_clock

    asyncio.run(scenario())


def test_about_panel_personalizes_without_inventing_developer_info():
    import bot

    class FakeMessage:
        def __init__(self):
            self.sent = []

        async def reply_text(self, text, **kwargs):
            self.sent.append((text, kwargs))

    async def scenario():
        message = FakeMessage()
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(first_name="Asha & Co", username=None),
        )
        await bot.cmd_about(update, None)
        text = message.sent[0][0]
        assert "sᴘᴀᴍᴘʀᴏ v.01" in text
        assert "Asha &amp; Co" in text
        assert "ᴛᴇʟᴇɢʀᴀᴍ ʙᴏᴛ" in text
        assert "developer" not in text.lower()

    asyncio.run(scenario())


def test_folder_examples_in_help_are_admin_only():
    import bot

    class FakeMessage:
        def __init__(self):
            self.sent = []

        async def reply_text(self, text, **kwargs):
            self.sent.append((text, kwargs))

    async def scenario():
        context = SimpleNamespace(bot=SimpleNamespace(username="SpamProBot"))
        regular_message = FakeMessage()
        await bot.cmd_help(
            SimpleNamespace(
                effective_message=regular_message,
                effective_user=SimpleNamespace(id=999),
            ),
            context,
        )
        assert "Folder Examples" not in regular_message.sent[0][0]
        assert "/folder1" not in regular_message.sent[0][0]

        admin_message = FakeMessage()
        await bot.cmd_help(
            SimpleNamespace(
                effective_message=admin_message,
                effective_user=SimpleNamespace(id=111),
            ),
            context,
        )
        admin_help = admin_message.sent[0][0]
        assert "Folder Examples" in admin_help
        assert "/folder1 Welcome to our group!" in admin_help
        assert "@SpamProBot /folder1 5" in admin_help
        assert "/deletefolder1" in admin_help

    asyncio.run(scenario())


def test_health_panel_reports_real_checks_and_latency(monkeypatch):
    import bot
    import system_metrics as sm

    metrics = sm.SystemMetrics(
        cpu_model="Test CPU",
        cpu_usage_percent=5.0,
        cpu_cores=4,
        gpu=sm.GPUInfo(),
        ram_used_gb=1.0,
        ram_total_gb=8.0,
        ram_usage_percent=12.5,
        disk_used_gb=10.0,
        disk_total_gb=100.0,
        disk_free_gb=90.0,
        disk_usage_percent=10.0,
        uptime_seconds=3661.0,
        python_version="3.13.0",
        platform_name="Linux test",
        hostname="safe-host",
    )

    class FakeMessage:
        def __init__(self):
            self.sent = []

        async def reply_text(self, text, **kwargs):
            self.sent.append((text, kwargs))

    class FakeBot:
        async def get_me(self):
            return SimpleNamespace(id=1)

    async def scenario():
        monkeypatch.setattr(bot.time, "perf_counter", iter((50.0, 50.042)).__next__)
        monkeypatch.setattr(sm, "collect_system_metrics", lambda: metrics)
        monkeypatch.setattr(bot.db, "fetch_one", lambda *_: {"ok": 1})
        message = FakeMessage()
        update = SimpleNamespace(effective_message=message)
        context = SimpleNamespace(bot=FakeBot())
        await bot.cmd_health(update, context)
        text = message.sent[0][0]
        assert "42 ms" in text
        assert "ᴅᴀᴛᴀʙᴀsᴇ: 🟢" in text
        assert "ᴘʀᴏᴄᴇss: 🟢 <b>ʀᴜɴɴɪɴɢ</b>" in text
        assert "ᴏᴠᴇʀᴀʟʟ: <b>ʜᴇᴀʟᴛʜʏ</b>" in text
        assert "1h 1m" in text

    asyncio.run(scenario())


def test_health_panel_handles_unavailable_checks_without_exposing_errors(monkeypatch):
    import bot
    import system_metrics as sm

    metrics = sm.SystemMetrics(
        cpu_model=None,
        cpu_usage_percent=None,
        cpu_cores=None,
        gpu=sm.GPUInfo(),
        ram_used_gb=None,
        ram_total_gb=None,
        ram_usage_percent=None,
        disk_used_gb=None,
        disk_total_gb=None,
        disk_free_gb=None,
        disk_usage_percent=None,
        uptime_seconds=None,
        python_version="3.13.0",
        platform_name="Linux test",
        hostname="safe-host",
    )

    class FakeMessage:
        def __init__(self):
            self.sent = []

        async def reply_text(self, text, **kwargs):
            self.sent.append((text, kwargs))

    class OfflineBot:
        async def get_me(self):
            raise TelegramError("private internal failure")

    async def scenario():
        monkeypatch.setattr(sm, "collect_system_metrics", lambda: metrics)
        monkeypatch.setattr(
            bot.db,
            "fetch_one",
            lambda *_: (_ for _ in ()).throw(RuntimeError("database secret")),
        )
        message = FakeMessage()
        update = SimpleNamespace(effective_message=message)
        await bot.cmd_health(update, SimpleNamespace(bot=OfflineBot()))
        text = message.sent[0][0]
        assert "N/A" in text
        assert "ᴜɴʜᴇᴀʟᴛʜʏ" in text
        assert "private internal failure" not in text
        assert "database secret" not in text

    asyncio.run(scenario())


def test_repeat_uses_reply_parameters_and_forum_thread():
    import bot

    class FakeBot:
        def __init__(self):
            self.calls = []

        async def send_message(self, **kwargs):
            self.calls.append(kwargs)

    async def scenario():
        fake = FakeBot()
        context = SimpleNamespace(bot=fake)
        job = bot.RepeatTask(user_id=900, total=1)
        await bot._run_repeat(job, context, 12345, "hello", 678, 42)
        repeat_call = fake.calls[0]
        assert "reply_to_message_id" not in repeat_call
        assert repeat_call["message_thread_id"] == 42
        assert repeat_call["reply_parameters"].message_id == 678
        assert repeat_call["reply_parameters"].allow_sending_without_reply is False

    asyncio.run(scenario())


def _fake_command_message(text, entities, *, chat_type="private", user_id=111):
    class FakeMessage:
        def __init__(self):
            self.text = text
            self.entities = entities
            self.chat_id = 456
            self.message_thread_id = 42 if chat_type in {"group", "supergroup"} else None
            self.replies = []

        def parse_entity(self, entity):
            return bot._utf16_slice(
                self.text, entity.offset, entity.offset + entity.length
            )

        async def reply_text(self, text, **kwargs):
            self.replies.append((text, kwargs))

    import bot

    message = FakeMessage()
    update = SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(type=chat_type),
        effective_user=SimpleNamespace(id=user_id, first_name="Admin"),
    )
    return update, message


def test_folder_create_delete_preserves_text_and_persists():
    import database as db
    import bot

    async def scenario():
        command_entity = SimpleNamespace(type="bot_command", offset=0, length=8)
        update, message = _fake_command_message("/folder1  Hello there  ", [command_entity])
        context = SimpleNamespace(bot=SimpleNamespace(username="SpamProBot"))
        await bot.cmd_folder_dispatch(update, context)
        assert db.get_folder(1) == " Hello there  "

        db.init_db()
        assert db.get_folder(1) == " Hello there  "
        assert "Folder 1 Saved" in message.replies[0][0]

        delete_entity = SimpleNamespace(
            type="bot_command",
            offset=0,
            length=len("/deletefolder1"),
        )
        delete_update, delete_message = _fake_command_message(
            "/deletefolder1", [delete_entity]
        )
        await bot.cmd_folder_dispatch(delete_update, context)
        assert db.get_folder(1) is None
        assert "Folder 1 Deleted" in delete_message.replies[0][0]

    asyncio.run(scenario())


def test_folder_creation_is_admin_only():
    import database as db
    import bot

    async def scenario():
        entity = SimpleNamespace(type="bot_command", offset=0, length=8)
        update, message = _fake_command_message(
            "/folder1 Private text", [entity], user_id=999
        )
        await bot.cmd_folder_dispatch(
            update, SimpleNamespace(bot=SimpleNamespace(username="SpamProBot"))
        )
        assert db.get_folder(1) is None
        assert "Not Authorized" in message.replies[0][0]

    asyncio.run(scenario())


def test_group_folder_command_requires_bot_mention_and_dispatches():
    import bot

    async def scenario():
        mentions = []

        async def fake_run(update, context, folder_id, tail):
            mentions.append((folder_id, tail))

        original = bot._run_folder_in_group
        bot._run_folder_in_group = fake_run
        try:
            plain_command = SimpleNamespace(
                type="bot_command", offset=0, length=len("/folder1")
            )
            update, _ = _fake_command_message(
                "/folder1 5", [plain_command], chat_type="group", user_id=999
            )
            context = SimpleNamespace(bot=SimpleNamespace(username="SpamProBot"))
            await bot.cmd_folder_dispatch(update, context)
            assert mentions == []

            mention = SimpleNamespace(type="mention", offset=0, length=11)
            command = SimpleNamespace(
                type="bot_command", offset=12, length=len("/folder1")
            )
            update, _ = _fake_command_message(
                "@SpamProBot /folder1 5",
                [mention, command],
                chat_type="supergroup",
                user_id=999,
            )
            await bot.cmd_folder_dispatch(update, context)
            assert mentions == [(1, " 5")]
        finally:
            bot._run_folder_in_group = original

    asyncio.run(scenario())


def test_folder_group_execution_uses_license_and_repeat_registry(monkeypatch):
    import bot
    import license_manager as lm

    class FakeRegistry:
        def __init__(self):
            self.job = None
            self.started = []

        def get(self, _):
            return None

        def cooldown_left(self, _):
            return 0

        async def start(self, user_id, total, mode):
            self.started.append((user_id, total, mode))
            self.job = bot.RepeatTask(user_id=user_id, total=total, mode=mode)
            return self.job

        async def finish(self, _):
            return None

    class FakeBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, **kwargs):
            self.sent.append(kwargs)

    async def scenario():
        registry = FakeRegistry()
        fake_bot = FakeBot()
        monkeypatch.setattr(bot, "registry", registry)
        monkeypatch.setattr(lm, "has_active_license", lambda _: True)
        monkeypatch.setattr(bot.db, "get_folder", lambda _: "Saved group message")
        message = SimpleNamespace(
            reply_to_message=None,
            chat_id=456,
            message_thread_id=42,
            replies=[],
            reply_text=lambda text, **kwargs: asyncio.sleep(0),
        )
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=999),
            effective_chat=SimpleNamespace(type="supergroup"),
        )
        context = SimpleNamespace(bot=fake_bot)
        await bot._run_folder_in_group(update, context, 1, " 2")
        await registry.job.task
        assert registry.started == [(999, 2, bot.config.DEFAULT_MODE)]
        assert fake_bot.sent[0]["text"] == "Saved group message"
        assert fake_bot.sent[0]["message_thread_id"] == 42

    asyncio.run(scenario())


def test_license_key_format_validation():
    import config
    import license_manager as lm

    assert lm.is_valid_key_format(f"{config.KEY_PREFIX}-ABCD-1234") is True
    assert lm.is_valid_key_format(f"{config.KEY_PREFIX}-OBCD-1234") is False
    assert lm.is_valid_key_format("not-a-license-key") is False


def test_expiry_milestones_and_persistent_notification_state():
    import database as db
    import license_manager as lm

    key = lm.generate_key()
    lic = lm.activate(key, 701)
    db.execute(
        "UPDATE licenses SET expires_at = ? WHERE key = ?",
        ((lm.now() + timedelta(days=6, hours=23)).isoformat(), key),
    )
    assert lm.expiry_milestone(lm.get_license(key))[0] == lm.NOTIFY_7_DAYS

    db.execute(
        "UPDATE licenses SET expires_at = ? WHERE key = ?",
        ((lm.now() + timedelta(days=2, hours=23)).isoformat(), key),
    )
    assert lm.expiry_milestone(lm.get_license(key))[0] == lm.NOTIFY_3_DAYS

    db.execute(
        "UPDATE licenses SET expires_at = ? WHERE key = ?",
        ((lm.now() + timedelta(hours=23)).isoformat(), key),
    )
    assert lm.expiry_milestone(lm.get_license(key))[0] == lm.NOTIFY_1_DAY

    db.execute(
        "UPDATE licenses SET expires_at = ? WHERE key = ?",
        ((lm.now() - timedelta(seconds=1)).isoformat(), key),
    )
    assert lm.expiry_milestone(lm.get_license(key))[0] == lm.NOTIFY_EXPIRED

    assert lm.record_notification(key, lm.NOTIFY_EXPIRED, 701) is True
    assert lm.record_notification(key, lm.NOTIFY_EXPIRED, 701) is False
    db.init_db()  # state survives a database reconnect/restart
    assert lm.notification_sent(key, lm.NOTIFY_EXPIRED) is True


def test_expiry_notification_sender_does_not_duplicate():
    import bot
    import database as db
    import license_manager as lm

    key = lm.generate_key()
    lm.activate(key, 702)
    db.execute(
        "UPDATE licenses SET expires_at = ? WHERE key = ?",
        ((lm.now() - timedelta(seconds=1)).isoformat(), key),
    )

    class FakeBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, **kwargs):
            self.sent.append(kwargs)

    async def scenario():
        fake = FakeBot()
        await bot.send_due_license_notifications(fake)
        await bot.send_due_license_notifications(fake)
        assert len(fake.sent) == 1
        assert "ʟɪᴄᴇɴsᴇ ᴇxᴘɪʀᴇᴅ" in fake.sent[0]["text"]

    asyncio.run(scenario())


def test_expiry_scheduler_has_one_task_and_cleans_up():
    import bot

    async def scenario():
        application = SimpleNamespace(bot_data={})
        first_task = None

        async def wait_forever(_):
            await asyncio.sleep(3600)

        original = bot.expiry_notification_loop
        original_menu = bot.configure_command_menu
        bot.expiry_notification_loop = wait_forever
        bot.configure_command_menu = lambda _: asyncio.sleep(0)
        try:
            await bot.on_startup(application)
            first_task = application.bot_data[bot.EXPIRY_TASK_KEY]
            await bot.on_startup(application)
            assert application.bot_data[bot.EXPIRY_TASK_KEY] is first_task
        finally:
            await bot.on_shutdown(application)
            bot.expiry_notification_loop = original
            bot.configure_command_menu = original_menu
        assert first_task.done()

    asyncio.run(scenario())


def test_command_menu_syncs_once_for_default_and_admin_scopes():
    import bot
    import config
    from telegram import BotCommandScopeChat, BotCommandScopeDefault

    class FakeBot:
        def __init__(self):
            self.current = {}
            self.set_calls = []

        @staticmethod
        def _scope_key(scope):
            if isinstance(scope, BotCommandScopeDefault):
                return "default"
            return f"chat:{scope.chat_id}"

        async def get_my_commands(self, *, scope):
            return self.current.get(self._scope_key(scope), [])

        async def set_my_commands(self, commands, *, scope):
            key = self._scope_key(scope)
            self.current[key] = list(commands)
            self.set_calls.append(key)

    async def scenario():
        fake = FakeBot()
        application = SimpleNamespace(bot=fake, bot_data={})
        original_admins = config.ADMIN_IDS
        config.ADMIN_IDS = {111}
        try:
            await bot.configure_command_menu(application)
            await bot.configure_command_menu(application)
        finally:
            config.ADMIN_IDS = original_admins

        assert fake.set_calls == ["default", "chat:111"]
        assert [command.command for command in fake.current["default"]] == [
            command.command for command in bot.USER_COMMANDS
        ]
        assert [command.command for command in fake.current["chat:111"]] == [
            command.command for command in bot.ADMIN_COMMANDS
        ]

    asyncio.run(scenario())


# ----------------------------- argument parsing ---------------------------- #
@pytest.mark.parametrize(
    "args,expected",
    [
        (["1", "Hello"], (1, "Hello", "medium")),
        (["5", "Hello"], (5, "Hello", "medium")),
        (["100", "Hello"], (100, "Hello", "medium")),
        (["10", "Hello", "everyone", "👋"], (10, "Hello everyone 👋", "medium")),
        (["10", "basic", "Hello"], (10, "Hello", "basic")),
        (["10", "medium", "Hello"], (10, "Hello", "medium")),
        (["10", "aggressive", "Hello"], (10, "Hello", "aggressive")),
        (["abc", "Hello"], "invalid_count"),
        (["0", "Hello"], "invalid_count"),
        (["-3", "Hello"], "invalid_count"),
        (["150", "Hello"], "limit_exceeded"),
        (["10"], "missing_message"),
        ([], "missing_message"),
    ],
)
def test_parse_repeat_args(args, expected):
    import bot

    assert bot.parse_repeat_args(args) == expected


def test_parse_repeat_args_honors_forced_default_mode():
    import bot

    assert bot.parse_repeat_args(["10", "Hello"], "basic") == (10, "Hello", "basic")


def test_channel_posts_use_configured_admin_as_license_owner():
    import bot

    channel_post = type(
        "ChannelPost",
        (),
        {"sender_chat": type("SenderChat", (), {"type": "channel"})()},
    )()
    update = type(
        "ChannelUpdate",
        (),
        {"effective_user": None, "channel_post": channel_post},
    )()

    assert bot.requester_id(update) == 111


# ------------------------------ task registry ------------------------------ #
def test_single_task_per_user_and_stop():
    import bot

    async def scenario():
        reg = bot.TaskRegistry()
        job = await reg.start(7, 10)
        assert job is not None
        assert await reg.start(7, 10) is None  # duplicate blocked
        assert await reg.cancel(7) is job
        await reg.finish(7)
        assert reg.get(7) is None
        assert await reg.cancel(7) is None  # nothing running

    asyncio.run(scenario())


def test_cooldown():
    import bot
    import config

    async def scenario():
        reg = bot.TaskRegistry()
        await reg.start(8, 3)
        await reg.finish(8)
        assert reg.cooldown_left(8) <= config.COOLDOWN_SECONDS
        assert reg.cooldown_left(9) == 0

    asyncio.run(scenario())


def test_cancel_all_on_shutdown():
    import bot

    async def scenario():
        reg = bot.TaskRegistry()
        a = await reg.start(1, 5)
        b = await reg.start(2, 5)
        await reg.cancel_all()
        assert a.cancelled and b.cancelled

    asyncio.run(scenario())
