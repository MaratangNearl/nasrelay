"""Durable settings, encrypted server profiles, and transfer queue."""
import json
import os
import sqlite3
import threading
import uuid
from pathlib import Path

from cryptography.fernet import Fernet


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        keyfile = directory / "secret.key"
        try:
            fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
        self.crypto = Fernet(keyfile.read_bytes())
        self.lock = threading.RLock()
        self.db = sqlite3.connect(directory / "app.sqlite3", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS profiles (id TEXT PRIMARY KEY, body TEXT NOT NULL);
        """)
        self.db.commit()
        # Migrate the single-server configuration once, in the same transaction.
        with self.lock, self.db:
            legacy = self.get("profile")
            if legacy and not self.get("profiles_migrated", False):
                profile = self.unseal(legacy)
                profile.update(id=uuid.uuid4().hex, protocol=profile.get("protocol", "ftp"))
                self.db.execute("INSERT INTO profiles VALUES (?, ?)", (profile["id"], self.seal(profile)))
                self.db.execute("INSERT OR REPLACE INTO settings VALUES ('selected_profile', ?)", (json.dumps(profile["id"]),))
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('profiles_migrated', 'true')")
            self.db.execute("DELETE FROM settings WHERE key='profile'")

    def seal(self, value):
        return self.crypto.encrypt(json.dumps(value).encode()).decode()

    def unseal(self, value):
        return json.loads(self.crypto.decrypt(value.encode()))

    def get(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))

    def add_job(self, job):
        with self.lock, self.db:
            self.db.execute("INSERT INTO jobs VALUES (?, ?)", (job["id"], json.dumps(job)))

    def profiles(self):
        with self.lock:
            return [self.unseal(row[0]) for row in self.db.execute("SELECT body FROM profiles ORDER BY rowid")]

    def profile(self, profile_id):
        with self.lock:
            row = self.db.execute("SELECT body FROM profiles WHERE id=?", (profile_id,)).fetchone()
            if not row:
                raise KeyError(profile_id)
            return self.unseal(row[0])

    def save_profile(self, profile):
        with self.lock, self.db:
            self.db.execute("INSERT INTO profiles VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                            (profile["id"], self.seal(profile)))

    def delete_profile(self, profile_id):
        with self.lock, self.db:
            self.profile(profile_id)
            self.db.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
            if self.get("selected_profile") == profile_id:
                row = self.db.execute("SELECT id FROM profiles ORDER BY rowid LIMIT 1").fetchone()
                self.db.execute("INSERT OR REPLACE INTO settings VALUES ('selected_profile', ?)",
                                (json.dumps(row[0] if row else None),))

    def job(self, job_id):
        with self.lock:
            row = self.db.execute("SELECT body FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError("작업을 찾을 수 없습니다.")
            return json.loads(row[0])

    def patch(self, job_id, **changes):
        with self.lock, self.db:
            job = self.job(job_id)
            job.update(changes)
            self.db.execute("UPDATE jobs SET body=? WHERE id=?", (json.dumps(job), job_id))
            return job

    def jobs(self):
        with self.lock:
            rows = self.db.execute("SELECT body FROM jobs ORDER BY rowid DESC").fetchall()
            return [json.loads(row[0]) for row in rows]

    def close(self):
        self.db.close()
