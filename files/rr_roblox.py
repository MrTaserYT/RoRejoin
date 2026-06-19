"""RoRejoin — Roblox web API: auth tickets, presence, launch URIs, servers.

Everything that talks to Roblox over HTTP: authenticating a cookie, fetching a
one-time auth ticket and building the launch URI, resolving share links,
listing public servers, and reading presence (which game/server an account is
actually in — the signal behind kick/wrong-game detection and the
different-servers collision splitter).
"""

from __future__ import annotations

import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from rr_const import (AUTH_TICKET_URL, USERS_AUTH_URL, SHARE_RESOLVE_URL, UA,
                      DEFAULT_SERVER_CAP, LOG_DIR, JOIN_RE)

def _post(url: str, cookie: str, csrf: str | None = None, body: bytes = b""):
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Cookie", f".ROBLOSECURITY={cookie}")
    req.add_header("Referer", "https://www.roblox.com/")
    req.add_header("Origin", "https://www.roblox.com")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", UA)
    if csrf:
        req.add_header("X-CSRF-TOKEN", csrf)
    return urllib.request.urlopen(req, timeout=15)


def get_account_info(cookie: str) -> tuple[int | None, str | None]:
    req = urllib.request.Request(USERS_AUTH_URL)
    req.add_header("Cookie", f".ROBLOSECURITY={cookie}")
    req.add_header("User-Agent", UA)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
            return data.get("id"), data.get("name")
    except Exception:
        return None, None


def get_csrf(cookie: str) -> str | None:
    try:
        resp = _post(AUTH_TICKET_URL, cookie)
        return resp.headers.get("x-csrf-token")
    except urllib.error.HTTPError as e:
        return e.headers.get("x-csrf-token")
    except Exception:
        return None


def get_auth_ticket(cookie: str) -> str | None:
    csrf = get_csrf(cookie)
    if not csrf:
        return None
    try:
        resp = _post(AUTH_TICKET_URL, cookie, csrf)
        return resp.headers.get("rbx-authentication-ticket")
    except urllib.error.HTTPError as e:
        return e.headers.get("rbx-authentication-ticket")
    except Exception:
        return None


def build_launch_uri(ticket: str, place_id: str, instance_id: str | None = None,
                     browser_tracker_id: int | None = None) -> str:
    """Build the roblox-player launch URI, matching the exact format used by
    Roblox Account Manager (the widely-working multi-account tool). Joining a
    specific server uses request=RequestGameJob with &gameId=<jobId>; open
    matchmaking uses request=RequestGame. The trailing +channel: field and the
    isPlayTogetherGame flag are intentionally omitted to match RAM."""
    if instance_id:
        # join one SPECIFIC server instance
        pl = ("https://assetgame.roblox.com/game/PlaceLauncher.ashx"
              f"?request=RequestGameJob&placeId={place_id}&gameId={instance_id}")
    else:
        # RequestGame -> Roblox drops you into an open server (matchmaking)
        pl = ("https://assetgame.roblox.com/game/PlaceLauncher.ashx"
              f"?request=RequestGame&placeId={place_id}&isPlayTogetherGame=false")
    pl_enc = urllib.parse.quote(pl, safe="")
    launch_ms = int(time.time() * 1000)
    btid = browser_tracker_id or random.randint(100_000_000_000, 999_999_999_999)
    return (f"roblox-player:1+launchmode:play+gameinfo:{ticket}"
            f"+launchtime:{launch_ms}+browsertrackerid:{btid}"
            f"+placelauncherurl:{pl_enc}"
            f"+robloxLocale:en_us+gameLocale:en_us")


def parse_place_id(raw: str) -> str | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    m = re.search(r"/games/(\d+)", raw)
    if m:
        return m.group(1)
    return raw if raw.isdigit() else None


def parse_share_link(raw: str) -> tuple[str | None, str | None]:
    """Pull (code, link_type) out of a Roblox share URL (or a bare code).
    e.g. https://www.roblox.com/share?code=ABC&type=ExperienceInvite."""
    raw = (raw or "").strip()
    if not raw:
        return None, None
    try:
        u = urllib.parse.urlparse(raw)
        if u.query:
            qs = urllib.parse.parse_qs(u.query)
            code = (qs.get("code") or [None])[0]
            ltype = (qs.get("type") or ["ExperienceInvite"])[0]
            if code:
                return code, ltype
    except Exception:
        pass
    if re.fullmatch(r"[A-Za-z0-9_\-]{10,}", raw):   # looks like a bare code
        return raw, "ExperienceInvite"
    return None, None


def _find_first(blob, keys: tuple):
    """Depth-first search a nested dict/list for the first present key."""
    stack = [blob]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k in keys:
                v = cur.get(k)
                if v not in (None, "", 0):
                    return v
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def resolve_share_link(cookie: str, code: str, link_type: str
                       ) -> tuple[str | None, str | None]:
    """Resolve a share link to (place_id, instance_id). instance_id is the
    specific server when the invite points at one, else None (open server).
    Returns (None, None) if it can't be resolved."""
    payload = json.dumps({"linkId": code,
                          "linkType": link_type or "ExperienceInvite"}).encode()

    def _try(token):
        resp = _post(SHARE_RESOLVE_URL, cookie, token, payload)
        return json.loads(resp.read().decode("utf-8", "ignore"))

    csrf = get_csrf(cookie)
    try:
        data = _try(csrf)
    except urllib.error.HTTPError as e:
        new = e.headers.get("x-csrf-token")
        if not new:
            return None, None
        try:
            data = _try(new)
        except Exception:
            return None, None
    except Exception:
        return None, None
    place = _find_first(data, ("placeId", "rootPlaceId"))
    inst = _find_first(data, ("gameInstanceId", "instanceId", "gameId",
                              "placeJobId", "jobId"))
    if place is None:
        return None, None
    return str(place), (str(inst) if inst else None)


def fetch_public_servers(place_id: str, cookie: str | None = None,
                         want: int = 50, min_free: int = 4, log=None) -> list[str]:
    """Return a list of job IDs (server instance IDs) for public running
    servers of a place, ordered lowest-ping first (with room).

    Only servers with room are kept. Note: Roblox's listed ping is sometimes
    inaccurate, so the ordering is best-effort. Returns [] on any error.
    """
    base = (f"https://games.roblox.com/v1/games/{place_id}/servers/Public"
            f"?sortOrder=Desc&limit=100")
    # collect (ping, free_slots, jid) for every server that has room
    rated: list[tuple[float, int, str]] = []   # has a real ping value
    unrated: list[tuple[int, str]] = []        # no ping data → order by room
    cursor = ""
    pages = 0
    total_seen = 0
    rej_full = 0
    try:
        while pages < 6 and (len(rated) + len(unrated)) < want:
            url = base + (f"&cursor={cursor}" if cursor else "")
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            if cookie:
                req.add_header("Cookie", f".ROBLOSECURITY={cookie}")
            with urllib.request.urlopen(req, timeout=12) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
            rows = data.get("data", [])
            total_seen += len(rows)
            for s in rows:
                jid = s.get("id")
                if not jid:
                    continue
                # 'playing' is NOT the live count on this endpoint (it equals
                # maxPlayers on nearly every server); the real occupancy is the
                # length of 'playerTokens'. Fall back only if tokens are absent.
                mx = (s.get("maxPlayers") or s.get("maxPlayerCount")
                      or s.get("capacity") or 0)
                mx = int(mx or 0)
                if mx <= 0:
                    mx = DEFAULT_SERVER_CAP
                toks = s.get("playerTokens")
                if isinstance(toks, list):
                    pl = len(toks)
                else:
                    pl = s.get("playing")
                    if pl is None:
                        pl = s.get("playerCount") or 0
                pl = int(pl or 0)
                free = mx - pl
                # playerTokens is only a SAMPLE of connected players, so a small
                # count doesn't prove the server is open — but a count at/above
                # capacity definitely means full. Require a real gap so we don't
                # keep picking servers that are actually full and bounce the
                # join into matchmaking (which caused same-server collisions).
                if free < max(2, min_free):
                    rej_full += 1
                    continue
                ping = s.get("ping")
                # Roblox's listed ping is frequently bogus (we've seen 2000+ ms),
                # so treat absurd values as "unknown" rather than sorting on them.
                if isinstance(ping, (int, float)) and 0 < ping < 400:
                    rated.append((float(ping), free, str(jid)))
                else:
                    unrated.append((free, str(jid)))
            cursor = data.get("nextPageCursor") or ""
            pages += 1
            if not cursor:
                break
    except urllib.error.HTTPError as e:
        if log:
            log(f"server list HTTP {e.code} (the game may hide its server list, "
                f"or the cookie was rejected).")
    except Exception as e:
        if log:
            log(f"server list fetch failed: {type(e).__name__}.")

    # Order PRIMARILY by lowest ping. Among servers with a real ping value, keep
    # only ones with at least `min_free` slots when we have enough of them (so we
    # still avoid near-full servers that bounce joins into matchmaking), but
    # never sacrifice ping just to grab an emptier server — that was the bug that
    # produced 200 ms picks. Free slots is only a tiebreaker for equal ping.
    rated.sort(key=lambda t: (t[0], -t[1]))    # ping asc, then most-empty
    roomy_rated = [t for t in rated if t[1] >= min_free]
    use = roomy_rated if len(roomy_rated) >= want else rated
    unrated.sort(key=lambda t: -t[0])          # most-empty first (no ping info)
    result = [jid for _, _, jid in use] + [jid for _, jid in unrated]

    if log:
        if use:
            best = int(use[0][0]) if isinstance(use[0], tuple) and len(use[0]) == 3 else None
            worst = int(use[-1][0]) if isinstance(use[-1], tuple) and len(use[-1]) == 3 else None
            png = (f", ping {best}–{worst} ms" if best is not None else "")
        else:
            png = ""
        log(f"server list: saw {total_seen}, {len(result)} joinable "
            f"({len(roomy_rated)} roomy+rated, {rej_full} full){png}.")
    return result


def _interruptible_sleep(seconds: float, stop=None) -> bool:
    """Sleep in small slices, checking the stop callable between them. Returns
    True if a stop was requested (so callers can abort), False if it slept the
    full duration. Keeps network-probe pauses from blocking a Stop request."""
    end = time.time() + max(0.0, seconds)
    while time.time() < end:
        try:
            if stop and stop():
                return True
        except Exception:
            pass
        time.sleep(min(0.1, end - time.time()) if end > time.time() else 0)
    return bool(stop and stop()) if stop else False


def fetch_presence(user_ids: list[int], cookie: str) -> dict[int, str]:
    """Return {userId: gameId} for the accounts that are currently in-game.

    The presence API reports the ACTUAL server (gameId / jobId) each user is in
    — this is ground truth, unlike the server we *requested* at launch (Roblox
    can silently reroute a join into matchmaking). Used to detect when two
    accounts ended up in the same server. Best-effort: returns {} on failure.
    """
    out: dict[int, str] = {}
    ids = [int(u) for u in user_ids if u]
    if not ids:
        return out
    url = "https://presence.roblox.com/v1/presence/users"
    body = json.dumps({"userIds": ids}).encode()

    def _try(token):
        resp = _post(url, cookie, token, body)
        return json.loads(resp.read().decode("utf-8", "ignore"))

    data = None
    try:
        data = _try(None)
    except urllib.error.HTTPError as e:
        tok = e.headers.get("x-csrf-token")
        if tok:
            try:
                data = _try(tok)
            except Exception:
                return out
        else:
            return out
    except Exception:
        return out
    if not isinstance(data, dict):
        return out
    for p in data.get("userPresences", []):
        # userPresenceType 2 == InGame; gameId is the server instance (jobId)
        if p.get("userPresenceType") == 2:
            uid = p.get("userId")
            gid = p.get("gameId")
            if uid and gid:
                out[int(uid)] = str(gid)
    return out


def presence_detail(user_id: int, cookie: str) -> tuple[str, set[str], str | None]:
    """One-account presence with a definite state, which place it's in, AND the
    server (gameId/jobId):
        ("ingame", {placeId, rootPlaceId}, gameId)  – in a game right now
        ("out",    set(),                  None)     – online/away/offline
        ("unknown", set(),                 None)     – lookup failed; callers must
                    NOT treat this as 'left'/'wrong game'/'collision'.
    The place set holds both placeId and rootPlaceId (as strings) so callers can
    match a configured place whether it's a root or a sub-place. gameId is the
    server instance, used to detect two accounts sharing a server."""
    ids = [int(user_id)] if user_id else []
    if not ids:
        return "unknown", set(), None
    url = "https://presence.roblox.com/v1/presence/users"
    body = json.dumps({"userIds": ids}).encode()

    def _try(token):
        resp = _post(url, cookie, token, body)
        return json.loads(resp.read().decode("utf-8", "ignore"))

    data = None
    try:
        data = _try(None)
    except urllib.error.HTTPError as e:
        tok = e.headers.get("x-csrf-token")
        if not tok:
            return "unknown", set(), None
        try:
            data = _try(tok)
        except Exception:
            return "unknown", set(), None
    except Exception:
        return "unknown", set(), None
    if not isinstance(data, dict):
        return "unknown", set(), None
    for p in data.get("userPresences", []):
        if p.get("userId") and int(p["userId"]) == int(user_id):
            # 2 == InGame; anything else (0 offline, 1 online, 3 studio) = out
            if p.get("userPresenceType") != 2:
                return "out", set(), None
            places = {str(p[k]) for k in ("placeId", "rootPlaceId")
                      if p.get(k)}
            gid = p.get("gameId")
            return "ingame", places, (str(gid) if gid else None)
    return "out", set(), None


def presence_state(user_id: int, cookie: str) -> str:
    """Back-compat thin wrapper: just the state, ignoring place/server."""
    return presence_detail(user_id, cookie)[0]


def detect_last_place() -> str | None:
    try:
        files = sorted(LOG_DIR.glob("*.log"), key=lambda f: -f.stat().st_mtime)
    except OSError:
        return None
    for f in files[:12]:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        hits = JOIN_RE.findall(text)
        if hits:
            return hits[-1][1]
    return None

