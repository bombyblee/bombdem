"""Save/restore table stacks to a JSON file so a table survives a bot restart.

Only seats, stacks and settings are stored, never cards. A hand that was still
running when we saved counts as cancelled: chips bet in it go back to their owners.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone

from holdem.game import Table

VERSION = 1


def _read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"version": VERSION, "tables": {}}
    data.setdefault("tables", {})
    return data


def _write(path: str, data: dict) -> None:
    # Write to a temp file then rename, so a crash mid-write can't corrupt the save.
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tables-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def snapshot(table: Table, title: str, owner_id: int, turn_timeout: int) -> dict:
    sb, bb = table.pending_blinds or (table.small_blind, table.big_blind)
    players = []
    for p in table.players:
        if p.leaving:
            continue
        stack = p.stack + (p.total_bet if table.in_progress else 0)  # refund a running hand
        players.append(
            {
                "user_id": p.user_id,
                "name": p.name,
                "stack": stack,
                "buy_in": p.buy_in,
                "rebuys": p.rebuys,
                "away": p.away,
            }
        )
    return {
        "title": title,
        "owner_id": owner_id,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "starting_stack": table.starting_stack,
        "small_blind": sb,
        "big_blind": bb,
        "turn_timeout": turn_timeout,
        "hands_played": table.hand_no,
        "players": players,
    }


def save(path: str, snapshots: dict[int, dict]) -> None:
    """Upsert the given tables; tables saved earlier but not loaded right now are kept."""
    data = _read(path)
    for chat_id, snap in snapshots.items():
        data["tables"][str(chat_id)] = snap
    data["version"] = VERSION
    _write(path, data)


def get(path: str, chat_id: int) -> dict | None:
    return _read(path)["tables"].get(str(chat_id))


def delete(path: str, chat_id: int) -> None:
    data = _read(path)
    if data["tables"].pop(str(chat_id), None) is not None:
        _write(path, data)


def restore(chat_id: int, snap: dict) -> Table:
    stack = snap["starting_stack"]
    # Saved blinds may exceed what the constructor allows (set later via /blinds),
    # so build with minimal blinds and then apply the saved ones.
    table = Table(chat_id, starting_stack=stack, small_blind=1, big_blind=1)
    table.set_blinds(snap["small_blind"], snap["big_blind"])
    table.hand_no = snap.get("hands_played", 0)
    for d in snap["players"]:
        p = table.add_player(d["user_id"], d["name"])
        p.stack = d["stack"]
        p.buy_in = d.get("buy_in", stack)
        p.rebuys = d.get("rebuys", 0)
        p.away = d.get("away", False)
    return table
