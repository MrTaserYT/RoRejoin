"""RoRejoin — Discord webhook notifications.

Sends event messages (launches, crashes, kicks, kills) to a user-configured
webhook. Only usernames and events are ever sent — never cookies.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

from rr_const import DISCORD_HOSTS

def _is_discord_webhook(url: str) -> bool:
    try:
        p = urllib.parse.urlparse(url.strip())
    except Exception:
        return False
    if p.scheme != "https" or p.netloc.lower() not in DISCORD_HOSTS:
        return False
    return "/api/webhooks/" in p.path


def discord_send(url: str, username: str, avatar: str,
                 content: str) -> tuple[bool, str]:
    """Synchronously POST a message to a Discord webhook.

    Only ever sends the supplied text (account usernames + events). Never
    touches cookies or any secret. Returns (ok, error_message).
    """
    url = (url or "").strip()
    if not _is_discord_webhook(url):
        return False, "Not a valid Discord webhook URL."
    payload: dict = {"content": content[:1900],
                     "allowed_mentions": {"parse": []}}
    if username.strip():
        payload["username"] = username.strip()[:80]
    if avatar.strip():
        payload["avatar_url"] = avatar.strip()
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "RoRejoin")
    try:
        urllib.request.urlopen(req, timeout=10).read()
        return True, ""
    except urllib.error.HTTPError as e:
        return False, f"Discord returned HTTP {e.code}."
    except Exception as e:
        return False, f"Send failed: {e}"


def discord_notify(url: str, username: str, avatar: str, content: str) -> None:
    """Fire-and-forget version for use inside the watcher loop."""
    if not url:
        return
    threading.Thread(target=discord_send,
                     args=(url, username, avatar, content), daemon=True).start()


