"""Notes as plain Markdown files: one file per note in data/notes/<id>.md.

Each file starts with a small front matter block, then the note itself:

    ---
    id: n-mvk7fc6yqkbl
    pinned: true
    created: 2026-10-23T00:00:59.002Z
    updated: 2026-10-23T00:01:05.720Z
    ---

    # AAPL
    ...

You can open, edit, add or delete these files with any editor while the server
runs; the page picks the change up on its next load. A file edited by hand
counts as updated at its modification time. A new file without front matter
works too: its id is the file name (letters, digits, - and _ only).

Folder: data/notes (override with SCREENER_NOTES_DIR).
"""
from __future__ import annotations

import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone

from .common import DATA_DIR
from .screens_db import ID_RE, MAX_NOTE_CHARS, MAX_NOTES, InvalidScreen, validate_note

NOTES_DIR = os.environ.get("SCREENER_NOTES_DIR", os.path.join(DATA_DIR, "notes"))

_lock = threading.Lock()
_FRONT = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n(?:\r?\n)?(.*)$", re.S)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def _ms(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.strip().replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def to_markdown(n: dict) -> str:
    return (f"---\nid: {n['id']}\npinned: {'true' if n.get('pinned') else 'false'}\n"
            f"created: {_iso(n['created'])}\nupdated: {_iso(n['updated'])}\n---\n\n{n['body']}")


def from_markdown(note_id: str, text: str, mtime_ms: int) -> dict:
    m = _FRONT.match(text)
    meta: dict[str, str] = {}
    if m:
        for line in m.group(1).splitlines():
            k, sep, v = line.partition(":")
            if sep:
                meta[k.strip()] = v.strip()
    body = m.group(2) if m else text
    updated = _ms(meta.get("updated")) or mtime_ms
    # edited by hand after the server last wrote it: the file's own time wins
    if mtime_ms > updated + 2000:
        updated = mtime_ms
    created = _ms(meta.get("created")) or min(updated, mtime_ms)
    return {"id": note_id, "body": body, "pinned": meta.get("pinned", "").lower() == "true",
            "created": created, "updated": updated}


def _file(note_id: str, folder: str) -> str:
    return os.path.join(folder, note_id + ".md")


def _read(note_id: str, folder: str) -> dict | None:
    path = _file(note_id, folder)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        mtime = int(os.stat(path).st_mtime * 1000)
    except OSError:
        return None
    return from_markdown(note_id, text, mtime)


def _write(n: dict, folder: str) -> None:
    """Atomic write, then set the file's time to the note's own `updated` time,
    so a later hand edit (which bumps the file time) is recognisable."""
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(to_markdown(n))
    os.utime(tmp, (time.time(), n["updated"] / 1000))
    os.replace(tmp, _file(n["id"], folder))


def list_notes(folder: str = NOTES_DIR) -> list[dict]:
    with _lock:
        try:
            names = os.listdir(folder)
        except OSError:
            return []
        notes = []
        for name in names:
            stem, ext = os.path.splitext(name)
            if ext.lower() != ".md" or not ID_RE.match(stem):
                continue
            n = _read(stem, folder)
            if n:
                notes.append(n)
    notes.sort(key=lambda n: (not n["pinned"], -(n["updated"] or 0)))
    return notes


def upsert_note(note_id: str, body: dict, folder: str = NOTES_DIR) -> dict:
    """Save a note. The client's own `updated` time is kept (so a note edited
    offline keeps its real edit time), but an older copy never overwrites a newer one."""
    n = validate_note(note_id, body)
    now = int(time.time() * 1000)
    updated = n["updated"] or now
    with _lock:
        cur = _read(note_id, folder)
        if cur and cur["updated"] > updated:   # stale write: keep what is stored
            return {**cur, "stale": True}
        if not cur and os.path.isdir(folder) and \
                sum(1 for x in os.listdir(folder) if x.endswith(".md")) >= MAX_NOTES:
            raise InvalidScreen(f"at most {MAX_NOTES} notes")
        created = cur["created"] if cur else (n["created"] or now)
        saved = {"id": note_id, "body": n["body"], "pinned": n["pinned"], "created": created, "updated": updated}
        _write(saved, folder)
    return saved


def delete_note(note_id: str, folder: str = NOTES_DIR) -> bool:
    if not ID_RE.match(note_id or ""):
        return False
    with _lock:
        try:
            os.remove(_file(note_id, folder))
            return True
        except FileNotFoundError:
            return False


def migrate_from_sqlite(db_path: str, folder: str = NOTES_DIR) -> int:
    """One-time move of notes kept in the old SQLite `notes` table into Markdown
    files. Notes that already have a newer file are left alone. Returns how many
    were written; the table is emptied afterwards so this runs only once."""
    import sqlite3
    if not os.path.exists(db_path):
        return 0
    conn = sqlite3.connect(db_path)
    try:
        if not conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='notes'").fetchone():
            return 0
        rows = conn.execute("SELECT id, body, pinned, created, updated FROM notes").fetchall()
        written = 0
        with _lock:
            for nid, text, pinned, created, updated in rows:
                if not ID_RE.match(nid or "") or len(text or "") > MAX_NOTE_CHARS:
                    continue
                cur = _read(nid, folder)
                if cur and cur["updated"] >= (updated or 0):
                    continue
                _write({"id": nid, "body": text or "", "pinned": bool(pinned),
                        "created": created or updated, "updated": updated or created}, folder)
                written += 1
        with conn:
            conn.execute("DELETE FROM notes")
        return written
    finally:
        conn.close()
