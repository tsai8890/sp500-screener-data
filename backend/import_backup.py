"""Bring screens, watchlists and notes from the old hosted site into local storage.

  python -m backend.import_backup screener-backup-2026-10-27.json
  python -m backend.import_backup path/to/screener/      (a folder synced to GitHub:
                                                          screens.json, watchlists.json, notes/*.md)

The .json file is what "Download everything (.json)" in the old site's Settings tab
saves. Items are added or updated by id; a note only replaces a newer copy if it
is newer itself. Nothing is deleted. Safe to run more than once.
"""
from __future__ import annotations

import json
import os
import sys

from . import notes_store, screens_db


def _load(path: str) -> tuple[list, list, list]:
    if os.path.isdir(path):
        def items(name: str, key: str) -> list:
            try:
                with open(os.path.join(path, name), encoding="utf-8") as f:
                    return json.load(f).get(key) or []
            except FileNotFoundError:
                return []
        notes = []
        ndir = os.path.join(path, "notes")
        for name in sorted(os.listdir(ndir)) if os.path.isdir(ndir) else []:
            stem, ext = os.path.splitext(name)
            if ext == ".md":
                p = os.path.join(ndir, name)
                with open(p, encoding="utf-8") as f:
                    n = notes_store.from_markdown(stem, f.read(), int(os.stat(p).st_mtime * 1000))
                notes.append(n)
        return items("screens.json", "screens"), items("watchlists.json", "watchlists"), notes
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("screens") or [], data.get("watchlists") or [], data.get("notes") or []


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__)
        return 2
    screens, watchlists, notes = _load(argv[0])
    done = {"screens": 0, "watchlists": 0, "notes": 0}
    problems = []
    for kind, rows, save in (("screens", screens, screens_db.upsert),
                             ("watchlists", watchlists, screens_db.upsert_watchlist),
                             ("notes", notes, notes_store.upsert_note)):
        for row in rows:
            try:
                out = save(str(row.get("id")), row)
                if not out.get("stale"):
                    done[kind] += 1
            except (screens_db.InvalidScreen, ValueError) as e:
                problems.append(f"{kind} {row.get('id')!r}: {e}")
    print(f"Imported {done['screens']} screens, {done['watchlists']} watchlists, {done['notes']} notes "
          f"into {screens_db.DB_PATH} and {notes_store.NOTES_DIR}")
    for p in problems:
        print("  skipped " + p, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
