#!/usr/bin/env python3
"""
User-account Telegram login (subprocess — same Python as tg-upload).

Uses a keep-alive session: send_code and sign_in happen on the SAME connected
Client. Splitting them across processes was causing PhoneCodeExpired even with
fresh codes (session file races + auth-key mismatch).
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _client(workdir: str, session: str, api_id: str, api_hash: str):
    from pyrogram import Client

    os.makedirs(workdir, exist_ok=True)
    return Client(
        session,
        api_id=int(api_id),
        api_hash=api_hash,
        workdir=workdir,
        no_updates=True,
        in_memory=False,
    )


def _emit(payload: dict) -> None:
    print(json.dumps(payload), flush=True)


def _read_confirm() -> dict:
    line = sys.stdin.readline()
    if not line:
        return {}
    try:
        data = json.loads(line)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def cmd_login(args) -> int:
    """Connect → send_code → wait stdin → sign_in (optional 2FA) → exit."""
    phone = (args.phone or "").strip().replace(" ", "")
    if not phone.startswith("+"):
        _emit({"ok": False, "error": "Phone must include country code, e.g. +60123456789"})
        return 1

    try:
        from pyrogram.errors import (
            SessionPasswordNeeded,
            PhoneCodeInvalid,
            PhoneCodeExpired,
            BadRequest,
        )
    except Exception as exc:
        _emit({"ok": False, "error": f"pyrogram import failed: {exc}"})
        return 2

    client = _client(args.workdir, args.session, args.api_id, args.api_hash)
    try:
        client.connect()
        sent = client.send_code(phone)
        phone_code_hash = sent.phone_code_hash
        _emit(
            {
                "ok": True,
                "phone": phone,
                "phone_code_hash": phone_code_hash,
                "pending": True,
                "message": "Login code sent — paste the newest code, then Confirm once",
            }
        )

        # Wait for UI confirm (code / optional password)
        while True:
            req = _read_confirm()
            if not req:
                _emit({"ok": False, "error": "Login cancelled (no confirm received)"})
                return 1

            code = str(req.get("code") or "").strip().replace(" ", "")
            password = str(req.get("password") or "").strip()

            try:
                if password and not code:
                    client.check_password(password)
                else:
                    if not code:
                        _emit(
                            {
                                "ok": False,
                                "error": "Enter the login code from Telegram",
                                "pending": True,
                            }
                        )
                        continue
                    try:
                        client.sign_in(phone, phone_code_hash, code)
                    except SessionPasswordNeeded:
                        if not password:
                            _emit(
                                {
                                    "ok": False,
                                    "need_2fa": True,
                                    "pending": True,
                                    "phone": phone,
                                    "message": "Two-step verification enabled — enter your cloud password",
                                }
                            )
                            continue
                        client.check_password(password)

                me = client.get_me()
                name = " ".join(
                    x for x in [getattr(me, "first_name", None), getattr(me, "last_name", None)] if x
                ).strip() or (me.username or str(me.id))
                _emit(
                    {
                        "ok": True,
                        "pending": False,
                        "user_authorized": True,
                        "user": name,
                        "message": f"User authorized as {name}",
                    }
                )
                return 0

            except PhoneCodeInvalid:
                _emit(
                    {
                        "ok": False,
                        "error": "Invalid login code — check the digits (same Send code is still valid)",
                        "pending": True,
                    }
                )
                continue
            except PhoneCodeExpired:
                _emit(
                    {
                        "ok": False,
                        "error": (
                            "Telegram rejected this code as expired/used. "
                            "Click Send code once for a new code (do not reuse older messages)."
                        ),
                        "pending": False,
                    }
                )
                return 1
            except BadRequest as exc:
                _emit({"ok": False, "error": f"Telegram rejected login: {exc}", "pending": False})
                return 1
            except Exception as exc:
                _emit({"ok": False, "error": f"Failed to confirm login: {exc}", "pending": False})
                return 1
    except Exception as exc:
        _emit({"ok": False, "error": f"Failed to send login code: {exc}"})
        return 1
    finally:
        try:
            client.disconnect()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_login = sub.add_parser("login")
    p_login.add_argument("--phone", required=True)
    p_login.add_argument("--workdir", required=True)
    p_login.add_argument("--api-id", required=True)
    p_login.add_argument("--api-hash", required=True)
    p_login.add_argument("--session", default="user")

    args = parser.parse_args()
    if args.cmd == "login":
        return cmd_login(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
