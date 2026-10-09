# 🤖 Telegram Repeat Bot

A clean, modular Telegram bot that repeats a message a controlled number of times —
either as normal messages or as repeated replies to a specific message — protected by
a 30-day license key system backed by SQLite.

The bot works entirely through Telegram's normal Bot API. It waits between messages,
honours `RetryAfter` responses, and never attempts to bypass rate limits, flood
protection, or permissions.

---

## 📁 Project structure

```text
telegram-bot/
├── bot.py               # handlers, repeat engine, task registry
├── config.py            # environment configuration
├── database.py          # SQLite helpers + license/notification schema
├── license_manager.py   # key generation, activation, expiry, revocation, reminders
├── tests/test_bot.py    # offline tests
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md
└── licenses.db          # created automatically on first run
```

---

## 1. 🤖 Create the bot with BotFather

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts (name, then username ending in `bot`).

## 2. 🔑 Get the token

BotFather replies with a token like `123456789:AA...`. Keep it private — never commit it.

Get your own numeric user ID from [@userinfobot](https://t.me/userinfobot); that is your admin ID.

## 3. ⚙️ Configure environment variables

```bash
cd telegram-bot
cp .env.example .env
```

Edit `.env`:

```ini
BOT_TOKEN=123456789:AA...
ADMIN_IDS=123456789,987654321
MAX_REPEAT=100
COOLDOWN_SECONDS=10
LICENSE_DAYS=30
KEY_PREFIX=SPAMBOT
```

`.env` is already git-ignored. Admins are identified by numeric ID only, never by username.

## 4. 📦 Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Python 3.10+ is required. SQLite comes from the standard library.

## 5. ▶️ Start the bot

```bash
python bot.py
```

The database `licenses.db` is created automatically.

## 6. 👥 Add it to a group

1. Open the group → **Add members** → search your bot's username.
2. Give it permission to send messages.
3. For the bot to see ordinary commands in groups, either disable privacy mode
   in BotFather (`/setprivacy` → Disable) or address commands as
   `/repeat@YourBot`.

## 6a. 📣 Use the bot in channels

1. Add the bot as a channel administrator.
2. Grant it **Post Messages** permission.
3. Post commands such as
   `/repeat 5 medium Hello` or `/aggressive 5 Hello`.

Channel posts do not carry the publishing user's Telegram ID. Channel commands
therefore use the configured admin's active license, while Telegram's channel
administrator permissions control who can publish commands. If you reply to a
message when sending the command, every repeated message is sent as a reply to
that original message.

---

## 7. 🔐 How the license system works

- Every license lasts **30 days from activation**, not from generation.
- Statuses: `UNUSED` → `ACTIVE` → `EXPIRED`, or `REVOKED` at any time.
- A key can be activated **once**, by a single Telegram user.
- The license is re-validated before **every** `/repeat` task.
- Stored per key: key, status, created_at, activated_at, expires_at, user_id.

## 8. 👑 How admins generate keys

```text
/genkey     →  🎫 SPAMBOT-91NK-N19S (UNUSED, 30 days)
/keys       →  totals for unused / active / expired / revoked
/revoke SPAMBOT-91NK-N19S
```

Keys use `secrets` (cryptographically secure) and the format `SPAMBOT-XXXX-XXXX`.
Revoking a key also cancels that user's running task.

## 8a. 📂 Admin message folders

Admins can save reusable messages in a private chat with the bot:

```text
/folder1 Welcome to our server!
/folder2 Another saved message
/deletefolder1
```

In a group, a user with an active license can run a saved message by mentioning
the bot:

```text
@YourBot /folder2 5
```

Folder messages are stored in the existing SQLite database. Folder repeats use
the normal repeat count limit, cooldown, task cancellation, and Telegram
rate-limit handling.

## 9. 🎫 How users activate keys

```text
/activate SPAMBOT-91NK-N19S
/license                      → status, activation date, expiry, days remaining
```

## 10. 🏓 System and admin utilities

```text
/ping                         → live Telegram response latency
/id                           → user, chat, and replied-message IDs
/scankey SPAMBOT-91NK-N19S    → admin-only license lookup
```

`/scankey` validates the key format before looking it up and never displays the
complete key. It uses the existing `licenses` table and existing license
statuses.

The bot checks expiry milestones automatically every minute and stores sent
milestones in the same SQLite database:

- 7 days remaining
- 3 days remaining
- 1 day remaining
- Expired

Each milestone is sent at most once, survives restarts, and is marked handled
if the user has blocked or deleted the bot.

## 11. 🔁 Normal repeat mode

```text
/repeat 10 Hello everyone 👋
```

Sends 10 separate messages in the current chat using Medium Spam (1 second per
message). The mode can be selected either as a shortcut command or as the
second argument to `/repeat`:

```text
/basic 10 Hello everyone 👋
/medium 10 Hello everyone 👋
/aggressive 10 Hello everyone 👋

/repeat 10 basic Hello everyone 👋
/repeat 10 medium Hello everyone 👋
/repeat 10 aggressive Hello everyone 👋
```

The three modes are:

- Basic Spam — 2 seconds per message
- Medium Spam — 1 second per message
- Aggressive Domination — 0.5 seconds per message

## 12. 🎯 Reply mode

Reply to any message, then send:

```text
/repeat 5 Main yahin hoon 😂
```

All 5 messages are sent as replies to that same original message.

## 13. 🛑 Stopping a task

```text
/stop
```

Cancels your running task cleanly and reports how many messages were sent
(e.g. `📤 Messages sent: 12/20`). One task per user, with a cooldown between tasks.
On shutdown, all active tasks are cancelled gracefully.

---

## ⚠️ Error messages

| Input | Response |
| --- | --- |
| `/repeat 10` | ⚠️ Message Missing |
| `/repeat abc Hello` | ❌ Invalid Count (1–100) |
| `/repeat 150 Hello` | 🚫 Limit Exceeded |
| Task already active | ⏳ Task Already Running |
| No license | 🔐 License Required |
| Bad key | ❌ Invalid License |
| Reused key | ⚠️ Key Already Used |

## 🧪 Tests

```bash
pip install pytest
python -m pytest tests -q
```

Covers key format, activation, duplicate activation, revocation, 30-day expiry,
persistence across restarts, admin authentication, argument validation
(1 / 5 / 100 / invalid / over-limit / missing message), single-task enforcement,
cooldown, stop, shutdown cleanup, ping latency thresholds, key scanning format
validation, expiry milestone selection, persistent notification deduplication,
and scheduler lifecycle. Rate limiting (`RetryAfter`) is handled in `_run_repeat`
by sleeping for exactly the duration Telegram requests.

## 🛡️ Platform rule

This project deliberately contains no rate-limit bypass, flood evasion,
multi-account spam, or permission workaround. Use the bot only in chats where
you are welcome.
