#!/usr/bin/env python3
"""
Channel media listing using a *user* Telegram session (not a bot).

User accounts can call messages.Search / get_chat_history.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _emit(payload: dict) -> None:
    print(json.dumps(payload), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--query", default="")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--api-id", required=True)
    parser.add_argument("--api-hash", required=True)
    parser.add_argument("--session", default="user")
    args = parser.parse_args()

    limit = max(1, min(args.limit, 50))
    offset = max(0, args.offset)
    query = (args.query or "").strip()
    workdir = args.workdir
    os.makedirs(workdir, exist_ok=True)

    session_path = os.path.join(workdir, f"{args.session}.session")
    if not os.path.isfile(session_path):
        _emit(
            {
                "ok": False,
                "error": "User session missing — authorize the user account first",
                "need_user_auth": True,
            }
        )
        return 1

    try:
        chat_id: int | str
        chat_id = int(str(args.chat_id).strip())
    except ValueError:
        chat_id = str(args.chat_id).strip()

    def chat_msg_to_link(cid, msg_id: int) -> str:
        text = str(cid).strip()
        if text.startswith("-100") and text[4:].isdigit():
            return f"https://t.me/c/{text[4:]}/{msg_id}"
        if text.lstrip("-").isdigit():
            return f"https://t.me/c/{text.lstrip('-')}/{msg_id}"
        return f"https://t.me/{text.lstrip('@')}/{msg_id}"

    def media_info(message):
        if not message or getattr(message, "empty", False):
            return None
        if not hasattr(message, "id"):
            return None
        for attr in ("document", "video", "audio", "animation", "voice", "video_note"):
            media = getattr(message, attr, None)
            if media is None:
                continue
            size = int(getattr(media, "file_size", 0) or 0)
            name = (getattr(media, "file_name", None) or "").strip()
            caption = (message.caption or "").strip()
            display = caption or name or f"message_{message.id}"
            date_ts = 0.0
            if message.date is not None:
                try:
                    date_ts = float(message.date.timestamp())
                except Exception:
                    date_ts = 0.0
            return {
                "msg_id": int(message.id),
                "caption": caption,
                "name": display,
                "file_name": name,
                "size": size,
                "date": date_ts,
                "link": chat_msg_to_link(chat_id, int(message.id)),
            }
        return None

    try:
        from pyrogram import Client
        from pyrogram.enums import MessagesFilter
    except Exception as exc:
        _emit({"ok": False, "error": f"pyrogram import failed: {exc}"})
        return 2

    fetch_n = min(80, max(limit + 10, limit))
    client = Client(
        args.session,
        api_id=int(args.api_id),
        api_hash=args.api_hash,
        workdir=workdir,
        no_updates=True,
        in_memory=False,
    )

    try:
        # connect() only — never start()/authorize() (those prompt on stdin).
        is_authorized = client.connect()
        if not is_authorized:
            _emit(
                {
                    "ok": False,
                    "error": "User session is not authorized — use Authorize User again",
                    "need_user_auth": True,
                }
            )
            return 1

        if query:
            batch = list(
                client.search_messages(
                    chat_id,
                    query=query,
                    limit=fetch_n,
                    offset=offset,
                )
            )
            mode = "search"
        else:
            batch = list(
                client.search_messages(
                    chat_id,
                    filter=MessagesFilter.DOCUMENT,
                    limit=fetch_n,
                    offset=offset,
                )
            )
            mode = "history"

        items = []
        for msg in batch:
            info = media_info(msg)
            if info:
                items.append(info)
                if len(items) >= limit:
                    break

        _emit(
            {
                "ok": True,
                "items": items,
                "mode": mode,
                "query": query,
                "offset": offset,
                "next_offset": offset + len(batch),
                "has_more": len(batch) >= fetch_n,
                "chat_id": str(chat_id),
            }
        )
        return 0
    except Exception as exc:
        err = str(exc)
        need = "auth" in err.lower() or "session" in err.lower() or "unauthorized" in err.lower()
        _emit({"ok": False, "error": err, "need_user_auth": need})
        return 1
    finally:
        try:
            client.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
