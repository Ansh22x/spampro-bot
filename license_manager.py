"""License generation, activation, expiry and revocation."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil

import config
import database as db

ALPHABET = "ABCDEFGHIJKLMNPQRSTUVWXYZ123456789"  # no O/0 ambiguity

UNUSED, ACTIVE, EXPIRED, REVOKED = "UNUSED", "ACTIVE", "EXPIRED", "REVOKED"


def now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class License:
    key: str
    status: str
    created_at: datetime
    activated_at: datetime | None
    expires_at: datetime | None
    user_id: int | None

    @property
    def days_remaining(self) -> int:
        if not self.expires_at:
            return 0
        delta = self.expires_at - now()
        return max(0, -(-delta.total_seconds() // 86400).__int__())

    def masked(self) -> str:
        parts = self.key.split("-")
        return f"{parts[0]}-****-{parts[-1][-2:]}" if len(parts) >= 3 else "****"


def _row_to_license(row) -> License:
    return License(
        key=row["key"],
        status=row["status"],
        created_at=_parse(row["created_at"]) or now(),
        activated_at=_parse(row["activated_at"]),
        expires_at=_parse(row["expires_at"]),
        user_id=row["user_id"],
    )


def _block() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(4))


def generate_key() -> str:
    """Create and persist a new unused license key."""
    for _ in range(20):
        key = f"{config.KEY_PREFIX}-{_block()}-{_block()}"
        existing = db.fetch_one("SELECT id FROM licenses WHERE key = ?", (key,))
        if existing:
            continue
        db.execute(
            "INSERT INTO licenses (key, status, created_at) VALUES (?, ?, ?)",
            (key, UNUSED, _iso(now())),
        )
        return key
    raise RuntimeError("Could not generate a unique license key")


def normalize(key: str) -> str:
    return key.strip().upper()


def is_valid_key_format(key: str) -> bool:
    """Validate the expected key shape before querying SQLite."""
    normalized = normalize(key)
    parts = normalized.split("-")
    return (
        len(parts) == 3
        and parts[0] == config.KEY_PREFIX
        and len(parts[1]) == 4
        and len(parts[2]) == 4
        and all(character in ALPHABET for character in parts[1] + parts[2])
    )


def get_license(key: str) -> License | None:
    row = db.fetch_one("SELECT * FROM licenses WHERE key = ?", (normalize(key),))
    return _row_to_license(row) if row else None


def _expire_if_due(lic: License) -> License:
    if lic.status == ACTIVE and lic.expires_at and lic.expires_at <= now():
        db.execute("UPDATE licenses SET status = ? WHERE key = ?", (EXPIRED, lic.key))
        return License(lic.key, EXPIRED, lic.created_at, lic.activated_at, lic.expires_at, lic.user_id)
    return lic


def get_user_license(user_id: int) -> License | None:
    """Most relevant license for a user: active first, else latest."""
    rows = db.fetch_all(
        "SELECT * FROM licenses WHERE user_id = ? ORDER BY activated_at DESC", (user_id,)
    )
    licenses = [_expire_if_due(_row_to_license(r)) for r in rows]
    for lic in licenses:
        if lic.status == ACTIVE:
            return lic
    return licenses[0] if licenses else None


def has_active_license(user_id: int) -> bool:
    lic = get_user_license(user_id)
    return bool(lic and lic.status == ACTIVE)


class ActivationError(Exception):
    """Raised with a machine-readable reason code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def activate(key: str, user_id: int) -> License:
    lic = get_license(key)
    if lic is None:
        raise ActivationError("invalid")
    lic = _expire_if_due(lic)
    if lic.status == REVOKED:
        raise ActivationError("revoked")
    if lic.status in (ACTIVE, EXPIRED):
        raise ActivationError("used")
    if has_active_license(user_id):
        raise ActivationError("already_licensed")

    start = now()
    expires = start + timedelta(days=config.LICENSE_DAYS)
    changed = db.execute(
        "UPDATE licenses SET status = ?, activated_at = ?, expires_at = ?, user_id = ? "
        "WHERE key = ? AND status = ?",
        (ACTIVE, _iso(start), _iso(expires), user_id, lic.key, UNUSED),
    )
    if changed != 1:
        raise ActivationError("used")
    return License(lic.key, ACTIVE, lic.created_at, start, expires, user_id)


def revoke(key: str) -> bool:
    lic = get_license(key)
    if lic is None or lic.status == REVOKED:
        return False
    db.execute("UPDATE licenses SET status = ? WHERE key = ?", (REVOKED, lic.key))
    return True


def refresh_expired() -> int:
    return db.execute(
        "UPDATE licenses SET status = ? WHERE status = ? AND expires_at IS NOT NULL AND expires_at <= ?",
        (EXPIRED, ACTIVE, _iso(now())),
    )


NOTIFY_7_DAYS = "7_days"
NOTIFY_3_DAYS = "3_days"
NOTIFY_1_DAY = "1_day"
NOTIFY_EXPIRED = "expired"


def licenses_for_expiry_notifications() -> list[License]:
    """Return activated licenses that can produce a reminder or expiry notice."""
    refresh_expired()
    rows = db.fetch_all(
        "SELECT * FROM licenses WHERE status IN (?, ?) "
        "AND expires_at IS NOT NULL AND user_id IS NOT NULL "
        "ORDER BY expires_at ASC",
        (ACTIVE, EXPIRED),
    )
    return [_row_to_license(row) for row in rows]


def expiry_milestone(
    lic: License, at: datetime | None = None
) -> tuple[str, int] | None:
    """Return the nearest due reminder milestone for a license."""
    if not lic.expires_at or lic.user_id is None:
        return None
    current = at or now()
    remaining_seconds = (lic.expires_at - current).total_seconds()
    if remaining_seconds <= 0:
        return NOTIFY_EXPIRED, 0

    remaining_days = ceil(remaining_seconds / 86400)
    if remaining_days <= 1:
        return NOTIFY_1_DAY, 1
    if remaining_days <= 3:
        return NOTIFY_3_DAYS, 3
    if remaining_days <= 7:
        return NOTIFY_7_DAYS, 7
    return None


def notification_sent(key: str, milestone: str) -> bool:
    row = db.fetch_one(
        "SELECT 1 FROM license_notifications WHERE license_key = ? AND milestone = ?",
        (normalize(key), milestone),
    )
    return row is not None


def record_notification(key: str, milestone: str, user_id: int) -> bool:
    """Record a sent milestone once; returns False if already recorded."""
    changed = db.execute(
        "INSERT OR IGNORE INTO license_notifications "
        "(license_key, milestone, user_id, sent_at) VALUES (?, ?, ?, ?)",
        (normalize(key), milestone, user_id, _iso(now())),
    )
    return changed == 1


def stats() -> dict[str, int]:
    refresh_expired()
    rows = db.fetch_all("SELECT status, COUNT(*) AS c FROM licenses GROUP BY status")
    counts = {r["status"]: r["c"] for r in rows}
    return {
        "total": sum(counts.values()),
        "unused": counts.get(UNUSED, 0),
        "active": counts.get(ACTIVE, 0),
        "expired": counts.get(EXPIRED, 0),
        "revoked": counts.get(REVOKED, 0),
    }
