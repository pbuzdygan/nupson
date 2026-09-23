from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.migrate()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def migrate(self) -> None:
        with self._lock, self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                    expires_at INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS hosts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    mac TEXT NOT NULL UNIQUE,
                    address TEXT NOT NULL DEFAULT '',
                    broadcast TEXT NOT NULL DEFAULT '255.255.255.255',
                    check_port INTEGER,
                    policy TEXT NOT NULL DEFAULT 'was_online',
                    wave INTEGER NOT NULL DEFAULT 1,
                    delay_seconds INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_online INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    data TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS outages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    restored_at TEXT,
                    completed_at TEXT,
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS wake_queue (
                    outage_id INTEGER NOT NULL REFERENCES outages(id),
                    host_id INTEGER NOT NULL REFERENCES hosts(id),
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (outage_id, host_id)
                );
                CREATE TABLE IF NOT EXISTS telemetry (
                    recorded_at INTEGER PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT '',
                    battery_charge REAL,
                    runtime_seconds REAL,
                    load_percent REAL,
                    input_voltage REAL,
                    output_voltage REAL,
                    power_watts REAL,
                    power_estimated INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS client_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    address TEXT NOT NULL UNIQUE,
                    server_address TEXT NOT NULL,
                    platform TEXT NOT NULL DEFAULT 'debian',
                    policy TEXT NOT NULL DEFAULT 'inherit',
                    delay_seconds INTEGER NOT NULL DEFAULT 600,
                    final_delay_seconds INTEGER NOT NULL DEFAULT 5,
                    config_version INTEGER NOT NULL DEFAULT 1,
                    generated_version INTEGER NOT NULL DEFAULT 0,
                    connected INTEGER NOT NULL DEFAULT 0,
                    last_seen TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_created_idx ON events(created_at DESC);
                CREATE INDEX IF NOT EXISTS telemetry_recorded_idx
                    ON telemetry(recorded_at);
                CREATE INDEX IF NOT EXISTS sessions_expires_idx
                    ON sessions(expires_at);
                """
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return default if row is None else json.loads(row["value"])

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock, self.connect() as db:
            db.execute(
                "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (key, json.dumps(value, separators=(",", ":")), utcnow()),
            )

    def delete_settings(self, *keys: str) -> None:
        if not keys:
            return
        placeholders = ",".join("?" for _ in keys)
        with self._lock, self.connect() as db:
            db.execute(
                f"DELETE FROM settings WHERE key IN ({placeholders})",  # noqa: S608
                keys,
            )

    def settings(self) -> dict[str, Any]:
        with self.connect() as db:
            rows = db.execute("SELECT key,value FROM settings").fetchall()
        return {row["key"]: json.loads(row["value"]) for row in rows}

    def user_count(self) -> int:
        with self.connect() as db:
            row = db.execute("SELECT COUNT(*) AS count FROM users").fetchone()
        return int(row["count"])

    def create_user(self, username: str, password_hash: str) -> None:
        with self._lock, self.connect() as db:
            db.execute(
                "INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                (username, password_hash, utcnow()),
            )

    def password_hash(self, username: str) -> str | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT password_hash FROM users WHERE username=?", (username,)
            ).fetchone()
        return str(row["password_hash"]) if row else None

    def create_session(self, token_hash: str, username: str, expires_at: int) -> None:
        with self._lock, self.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires_at < ?", (int(time.time()),))
            db.execute(
                "INSERT INTO sessions(token_hash,username,expires_at,created_at) VALUES(?,?,?,?)",
                (token_hash, username, expires_at, utcnow()),
            )

    def session_valid(self, token_hash: str) -> bool:
        now = int(time.time())
        with self._lock, self.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
            row = db.execute(
                "SELECT 1 FROM sessions WHERE token_hash=? AND expires_at>=?",
                (token_hash, now),
            ).fetchone()
        return row is not None

    def delete_session(self, token_hash: str) -> None:
        with self._lock, self.connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))

    def add_event(self, kind: str, message: str, level: str = "info", data: Any = None) -> int:
        with self._lock, self.connect() as db:
            cursor = db.execute(
                "INSERT INTO events(created_at,level,kind,message,data) VALUES(?,?,?,?,?)",
                (utcnow(), level, kind, message, json.dumps(data or {}, separators=(",", ":"))),
            )
            return int(cursor.lastrowid)

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(row), "data": json.loads(row["data"])} for row in rows]

    def prune_events(self, days: int) -> int:
        days = max(1, min(days, 3650))
        with self._lock, self.connect() as db:
            cursor = db.execute(
                "DELETE FROM events WHERE julianday(created_at) < julianday('now', ?)",
                (f"-{days} days",),
            )
            return cursor.rowcount

    def add_telemetry(self, sample: dict[str, Any], recorded_at: int | None = None) -> None:
        timestamp = int(time.time()) if recorded_at is None else int(recorded_at)
        with self._lock, self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO telemetry("
                "recorded_at,status,battery_charge,runtime_seconds,load_percent,"
                "input_voltage,output_voltage,power_watts,power_estimated) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    timestamp,
                    str(sample.get("status", "")),
                    sample.get("battery_charge"),
                    sample.get("runtime_seconds"),
                    sample.get("load_percent"),
                    sample.get("input_voltage"),
                    sample.get("output_voltage"),
                    sample.get("power_watts"),
                    int(bool(sample.get("power_estimated"))),
                ),
            )

    def prune_telemetry(self, days: int) -> int:
        cutoff = int(time.time()) - max(1, min(days, 3650)) * 86400
        with self._lock, self.connect() as db:
            cursor = db.execute("DELETE FROM telemetry WHERE recorded_at < ?", (cutoff,))
            return cursor.rowcount

    def telemetry(self, start: int, end: int, max_points: int = 360) -> dict[str, Any]:
        duration = max(1, end - start)
        bucket = max(60, math.ceil(duration / max_points / 60) * 60)
        with self.connect() as db:
            rows = db.execute(
                "SELECT (recorded_at / ?) * ? AS timestamp,"
                "AVG(battery_charge) AS battery_charge,"
                "AVG(runtime_seconds) AS runtime_seconds,"
                "AVG(load_percent) AS load_percent,"
                "AVG(input_voltage) AS input_voltage,"
                "AVG(output_voltage) AS output_voltage,"
                "AVG(power_watts) AS power_watts,"
                "MAX(power_estimated) AS power_estimated,"
                "MAX(CASE WHEN status LIKE '%OB%' THEN 1 ELSE 0 END) AS on_battery "
                "FROM telemetry WHERE recorded_at BETWEEN ? AND ? "
                "GROUP BY timestamp ORDER BY timestamp",
                (bucket, bucket, start, end),
            ).fetchall()
            summary = db.execute(
                "WITH samples AS ("
                "SELECT recorded_at,power_watts,"
                "LAG(recorded_at) OVER (ORDER BY recorded_at) AS previous_at "
                "FROM telemetry WHERE recorded_at BETWEEN ? AND ?"
                ") SELECT COUNT(*) AS sample_count,AVG(power_watts) AS average_power_watts,"
                "MAX(power_watts) AS peak_power_watts,"
                "SUM(power_watts * MIN(recorded_at-previous_at,300))/3600000.0 "
                "AS energy_kwh FROM samples",
                (start, end),
            ).fetchone()
        return {
            "from": start,
            "to": end,
            "bucket_seconds": bucket,
            "points": [dict(row) for row in rows],
            "summary": dict(summary),
        }

    def hosts(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM hosts ORDER BY wave,name").fetchall()
        return [dict(row) for row in rows]

    def save_host(self, host: dict[str, Any], host_id: int | None = None) -> dict[str, Any]:
        values = (
            host["name"],
            host["mac"].upper(),
            host.get("address", ""),
            host.get("broadcast", "255.255.255.255"),
            host.get("check_port"),
            host.get("policy", "was_online"),
            int(host.get("wave", 1)),
            int(host.get("delay_seconds", 0)),
            int(bool(host.get("enabled", True))),
            utcnow(),
        )
        with self._lock, self.connect() as db:
            if host_id is None:
                cursor = db.execute(
                    "INSERT INTO hosts(name,mac,address,broadcast,check_port,policy,wave,"
                    "delay_seconds,enabled,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    values,
                )
                host_id = int(cursor.lastrowid)
            else:
                db.execute(
                    "UPDATE hosts SET name=?,mac=?,address=?,broadcast=?,check_port=?,"
                    "policy=?,wave=?,"
                    "delay_seconds=?,enabled=?,updated_at=? WHERE id=?",
                    (*values, host_id),
                )
            row = db.execute("SELECT * FROM hosts WHERE id=?", (host_id,)).fetchone()
        if row is None:
            raise KeyError(host_id)
        return dict(row)

    def delete_host(self, host_id: int) -> None:
        with self._lock, self.connect() as db:
            db.execute("DELETE FROM wake_queue WHERE host_id=?", (host_id,))
            db.execute("DELETE FROM hosts WHERE id=?", (host_id,))

    def set_host_online(self, host_id: int, online: bool) -> None:
        with self._lock, self.connect() as db:
            db.execute(
                "UPDATE hosts SET last_online=?,updated_at=? WHERE id=?",
                (online, utcnow(), host_id),
            )

    def client_profiles(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM client_profiles ORDER BY name").fetchall()
        profiles = []
        for row in rows:
            profile = dict(row)
            if profile["connected"]:
                profile["status"] = "connected"
            elif profile["last_seen"]:
                profile["status"] = "disconnected"
            else:
                profile["status"] = "waiting"
            profile["config_current"] = profile["generated_version"] == profile["config_version"]
            profiles.append(profile)
        return profiles

    def client_profile(self, profile_id: int) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM client_profiles WHERE id=?", (profile_id,)).fetchone()
        if row is None:
            raise KeyError(profile_id)
        return dict(row)

    def save_client_profile(
        self, profile: dict[str, Any], profile_id: int | None = None
    ) -> dict[str, Any]:
        now = utcnow()
        values = (
            profile["name"],
            profile["address"],
            profile["server_address"],
            profile["platform"],
            profile["policy"],
            int(profile["delay_seconds"]),
            int(profile["final_delay_seconds"]),
        )
        with self._lock, self.connect() as db:
            if profile_id is None:
                cursor = db.execute(
                    "INSERT INTO client_profiles(name,address,server_address,platform,policy,"
                    "delay_seconds,final_delay_seconds,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (*values, now, now),
                )
                profile_id = int(cursor.lastrowid)
            else:
                cursor = db.execute(
                    "UPDATE client_profiles SET name=?,address=?,server_address=?,platform=?,"
                    "policy=?,delay_seconds=?,final_delay_seconds=?,updated_at=?,"
                    "config_version=config_version+1 WHERE id=?",
                    (*values, now, profile_id),
                )
                if cursor.rowcount == 0:
                    raise KeyError(profile_id)
            row = db.execute("SELECT * FROM client_profiles WHERE id=?", (profile_id,)).fetchone()
        return dict(row)

    def delete_client_profile(self, profile_id: int) -> None:
        with self._lock, self.connect() as db:
            cursor = db.execute("DELETE FROM client_profiles WHERE id=?", (profile_id,))
            if cursor.rowcount == 0:
                raise KeyError(profile_id)

    def mark_client_config_generated(self, profile_id: int) -> None:
        with self._lock, self.connect() as db:
            cursor = db.execute(
                "UPDATE client_profiles SET generated_version=config_version WHERE id=?",
                (profile_id,),
            )
            if cursor.rowcount == 0:
                raise KeyError(profile_id)

    def invalidate_client_configs(self, inherited_only: bool = False) -> None:
        query = "UPDATE client_profiles SET config_version=config_version+1,updated_at=?"
        parameters: tuple[Any, ...] = (utcnow(),)
        if inherited_only:
            query += " WHERE policy='inherit'"
        with self._lock, self.connect() as db:
            db.execute(query, parameters)

    def sync_client_connections(self, addresses: list[str]) -> list[str]:
        connected = sorted(set(addresses))
        now = utcnow()
        with self._lock, self.connect() as db:
            if connected:
                placeholders = ",".join("?" for _ in connected)
                db.execute(
                    f"UPDATE client_profiles SET connected=0 "  # noqa: S608
                    f"WHERE connected=1 AND address NOT IN ({placeholders})",
                    connected,
                )
            else:
                db.execute("UPDATE client_profiles SET connected=0 WHERE connected=1")
            for address in connected:
                db.execute(
                    "UPDATE client_profiles SET connected=1,last_seen=? WHERE address=? AND ("
                    "connected=0 OR last_seen IS NULL OR "
                    "julianday(last_seen) < julianday('now','-1 minute'))",
                    (now, address),
                )
            known = {
                str(row["address"])
                for row in db.execute("SELECT address FROM client_profiles").fetchall()
            }
        return [address for address in connected if address not in known]

    def start_outage(self) -> int:
        with self._lock, self.connect() as db:
            open_row = db.execute(
                "SELECT id FROM outages WHERE status!='completed' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if open_row:
                return int(open_row["id"])
            cursor = db.execute(
                "INSERT INTO outages(started_at,status) VALUES(?, 'outage')", (utcnow(),)
            )
            return int(cursor.lastrowid)

    def open_outage(self) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM outages WHERE status!='completed' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def update_outage(self, outage_id: int, status: str) -> None:
        with self._lock, self.connect() as db:
            if status == "recovery_pending":
                db.execute(
                    "UPDATE outages SET status=?, restored_at=? WHERE id=?",
                    (status, utcnow(), outage_id),
                )
            elif status == "completed":
                db.execute(
                    "UPDATE outages SET status=?, completed_at=? WHERE id=?",
                    (status, utcnow(), outage_id),
                )
            else:
                db.execute("UPDATE outages SET status=? WHERE id=?", (status, outage_id))

    def prepare_wake_queue(self, outage_id: int) -> None:
        with self._lock, self.connect() as db:
            hosts = db.execute("SELECT id,policy,last_online FROM hosts WHERE enabled=1").fetchall()
            for host in hosts:
                eligible = host["policy"] == "always" or (
                    host["policy"] == "was_online" and host["last_online"]
                )
                if eligible:
                    db.execute(
                        "INSERT OR IGNORE INTO wake_queue(outage_id,host_id,updated_at) "
                        "VALUES(?,?,?)",
                        (outage_id, host["id"], utcnow()),
                    )

    def wake_queue(self, outage_id: int) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT w.status,w.attempts,h.* FROM wake_queue w JOIN hosts h ON h.id=w.host_id "
                "WHERE w.outage_id=? ORDER BY h.wave,h.name",
                (outage_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_wake(self, outage_id: int, host_id: int, status: str, attempts: int) -> None:
        with self._lock, self.connect() as db:
            db.execute(
                "UPDATE wake_queue SET status=?,attempts=?,updated_at=? "
                "WHERE outage_id=? AND host_id=?",
                (status, attempts, utcnow(), outage_id, host_id),
            )
