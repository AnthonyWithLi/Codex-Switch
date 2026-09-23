#!/usr/bin/env python3
"""Local Codex account switcher, mirroring server ~/.local/bin/csw.

Swaps ONLY ~/.codex/auth.json. Never writes config.toml. Never copies tokens
to or from the server — each machine keeps its own snapshots. ChatGPT OAuth
must be created by a native `codex login` on that machine; a Windows
auth.json cannot be reused on Linux (refresh will fail).

  personal        -> ~/.codex/auth.personal.json         (personal Plus)
  work-team       -> ~/.codex/auth.work-team.json        (team workspace)

Before switching, the live auth is saved back to the current account's
snapshot so in-use refresh tokens stay valid on switch-back.

--ensure-snapshots also copies the current live auth.json onto that
account's snapshot when the refresh token has changed. It never writes
live auth.json, never copies tokens from the other machine, and cannot
refresh the inactive account (that machine has no newer token for it).

Missing snapshots are created from live auth.json and local
~/.codex/account_backup copies whose account_id matches. Labels in
account_backup/profiles.json are ignored because they can disagree with
the file contents.

Before switching (and while refreshing snapshots) the account's access
token is probed against https://chatgpt.com/backend-api/codex/models. A
401 means the ChatGPT session was ended server-side, so switching is
refused with a "log in again" message. Without that check a revoked
account looks like a browser Work/Personal bug: Codex Desktop cannot
establish the account's workspace, falls back to the account's default
ChatGPT identity and shows a Work page with "You don't have access to
Work yet". Use --skip-token-check when offline.

Switching also strips `?surface=work` from Codex Desktop's restored
sidebar tabs (browser-sidebar-page-states.json): a tab last parked on
that URL reopens the ChatGPT Work product on the next launch, which is
the same misleading no-access page.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import socket
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

# Account definitions are loaded dynamically:
# 1. From config.json (in script dir or ~/.codex/config.json) if present.
# 2. Or auto-discovered from ~/.codex/auth.*.json snapshots by inspecting JWT claims.

def _load_accounts_from_config_or_discovery(home: Path | None = None):
    script_dir = Path(__file__).resolve().parent
    cfg_path = script_dir / "config.json"
    if not cfg_path.exists():
        cfg_path = Path.home() / ".codex" / "config.json"

    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(cfg, dict) and "accounts" in cfg and isinstance(cfg["accounts"], list) and len(cfg["accounts"]) > 0:
                accounts = list(cfg["accounts"])
                aliases = dict(cfg.get("aliases") or {})
                account_files = {item["name"]: item["file"] for item in accounts}
                account_by_name = {item["name"]: item for item in accounts}
                return accounts, account_files, account_by_name, aliases
        except Exception:
            pass

    target_home = home or (Path.home() / ".codex")
    discovered = []
    if target_home.is_dir():
        for p in sorted(target_home.glob("auth.*.json")):
            filename = p.name
            name = filename[len("auth."):-len(".json")]
            if name in ("json",) or name.startswith("unknown") or name.startswith("saved"):
                continue
            data = load_auth(p)
            aid = account_id_of(data)
            em = email_of(data)
            pl = plan_of(data)
            surface = "work" if pl in ("team", "business", "enterprise") else "personal"
            discovered.append({
                "name": name,
                "file": filename,
                "account_id": aid,
                "email": em,
                "surface": surface,
            })
    account_files = {item["name"]: item["file"] for item in discovered}
    account_by_name = {item["name"]: item for item in discovered}
    return discovered, account_files, account_by_name, {}

ACCOUNTS: list[dict[str, str]] = []
ACCOUNT_FILES: dict[str, str] = {}
ACCOUNT_BY_NAME: dict[str, dict[str, Any]] = {}
ACCOUNT_ALIASES: dict[str, str] = {}
BACKUP_RELATIVE = (
    Path("account_backup") / "a" / "auth.json",
    Path("account_backup") / "b" / "auth.json",
    Path("account_backup") / "c" / "auth.json",
    Path("account_backup") / "d" / "auth.json",
    Path("account_backup") / "windows" / "refresh_runtime" / "auth.json",
    Path("account_backup") / "windows" / "login_runtime" / "auth.json",
)


def _print(text: str) -> None:
    stream = sys.stdout
    encoding = stream.encoding or "utf-8"
    stream.write(text.encode(encoding, errors="replace").decode(encoding, errors="replace") + "\n")


def write_message_file(text: str) -> None:
    """Mirror an operator-facing message to a UTF-8 file, when asked to.

    The desktop shortcut runs this script through a hidden PowerShell 5.1
    window. PS 5.1 decodes a child process's stdout/stderr with the console
    code page (936 on this machine), so Chinese text printed here arrives in
    the popup as mojibake no matter which encoding we print with. The wrapper
    sets CSW_MESSAGE_FILE to a temp path; writing the message there lets it be
    read back explicitly as UTF-8. It is best-effort: if it fails, the caller
    still has the normal stdout/stderr text to fall back on.
    """
    path = os.environ.get("CSW_MESSAGE_FILE")
    if not path:
        return
    try:
        Path(path).write_text(text, encoding="utf-8")
    except OSError:
        pass


def emit(data: dict[str, Any], as_json: bool) -> None:
    if as_json:
        _print(json.dumps(data, ensure_ascii=False))
        return
    current = data.get("current") or f"unknown ({data.get('account_id_prefix') or ''})".strip()
    names = "|".join(data.get("accounts") or list(ACCOUNT_FILES))
    _print(f"Usage: csw <{names}>")
    _print(f"Current: {current}")
    for name in data.get("accounts") or list(ACCOUNT_FILES):
        snap = (data.get("snapshots") or {}).get(name) or {}
        if snap.get("exists") and snap.get("ok"):
            state = f"ok {snap.get('prefix')}"
        elif snap.get("exists"):
            state = f"mismatch {snap.get('prefix') or '-'}"
        else:
            state = "missing"
        _print(f"  snapshot {name}: {state}")
    for line in data.get("notes") or []:
        _print(line)
    token_status = data.get("current_token_status")
    if token_status == "revoked":
        _print("  ! ChatGPT session for the current account was ended by the server (http 401)")
    elif token_status == "ok":
        _print("  token check: session alive")
    meta = data.get("snapshot_meta") or {}
    if meta:
        _print("")
        _print("  %-20s %-11s %-11s %s" % ("account", "workspace", "tier", "session"))
        for name, item in meta.items():
            state = {
                "ok": "alive",
                "revoked": "REVOKED (run codex login for this account)",
                "expired": "access token expired (Codex refreshes it on use)",
                "unchecked": "not checked",
                "missing": "no snapshot",
                "network": "network error",
                "unknown": "unknown",
            }.get(item.get("token_status"), item.get("token_status") or "-")
            _print("  %-20s %-11s %-11s %s" % (
                name,
                item.get("workspace_short") or "-",
                item.get("plan_label") or "-",
                state,
            ))


def load_auth(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def account_id_of(data: dict[str, Any] | None) -> str:
    if not isinstance(data, dict):
        return ""
    tokens = data.get("tokens")
    if isinstance(tokens, dict):
        value = tokens.get("account_id")
        if isinstance(value, str):
            return value
    value = data.get("account_id")
    return value if isinstance(value, str) else ""


def _jwt_payload(token: str) -> dict[str, Any] | None:
    if not isinstance(token, str) or token.count(".") < 2:
        return None
    payload = token.split(".")[1]
    pad = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload + pad)
        data = json.loads(raw)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def email_of(data: dict[str, Any] | None) -> str:
    if not isinstance(data, dict):
        return ""
    tokens = data.get("tokens")
    if isinstance(tokens, dict):
        direct = tokens.get("email")
        if isinstance(direct, str) and direct.strip():
            return direct.strip().lower()
        claims = _jwt_payload(tokens.get("id_token") if isinstance(tokens.get("id_token"), str) else "")
        if claims:
            value = claims.get("email")
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
    value = data.get("email")
    return value.strip().lower() if isinstance(value, str) and value.strip() else ""


def refresh_token_of(data: dict[str, Any] | None) -> str:
    if not isinstance(data, dict):
        return ""
    tokens = data.get("tokens")
    if isinstance(tokens, dict):
        value = tokens.get("refresh_token")
        if isinstance(value, str) and value:
            return value
    value = data.get("refresh_token")
    return value if isinstance(value, str) and value else ""


def has_refresh_token(data: dict[str, Any] | None) -> bool:
    return bool(refresh_token_of(data))


def access_token_of(data: dict[str, Any] | None) -> str:
    if not isinstance(data, dict):
        return ""
    tokens = data.get("tokens")
    if isinstance(tokens, dict):
        value = tokens.get("access_token")
        if isinstance(value, str) and value:
            return value
    value = data.get("access_token")
    return value if isinstance(value, str) and value else ""


def access_token_expiry(data: dict[str, Any] | None) -> float:
    claims = _jwt_payload(access_token_of(data))
    if not claims:
        return 0.0
    value = claims.get("exp")
    return float(value) if isinstance(value, (int, float)) else 0.0


PLAN_LABELS = {
    "team": "团队",
    "business": "团队",
    "enterprise": "团队",
    "plus": "个人 Plus",
    "pro": "个人 Pro",
    "prolite": "个人轻量",
    "free": "免费",
}


def plan_of(data: dict[str, Any] | None) -> str:
    """Subscription tier of this session, e.g. 'plus' or 'team'.

    The access token is authoritative (it describes the session that is
    actually live); the id token is only a fallback.
    """
    claims = _jwt_payload(access_token_of(data)) or {}
    auth = claims.get("https://api.openai.com/auth")
    value = (auth or {}).get("chatgpt_plan_type") if isinstance(auth, dict) else None
    if not value:
        tokens = (data or {}).get("tokens")
        id_token = tokens.get("id_token") if isinstance(tokens, dict) else ""
        id_claims = _jwt_payload(id_token if isinstance(id_token, str) else "") or {}
        id_auth = id_claims.get("https://api.openai.com/auth")
        value = (id_auth or {}).get("chatgpt_plan_type") if isinstance(id_auth, dict) else None
    return str(value) if value else ""


def workspace_of(data: dict[str, Any] | None) -> str:
    """The ChatGPT workspace (chatgpt_account_id) this session is bound to.

    One login can hold several of these - e.g. personal Plus and team workspace,
    own refresh token*, which is why switching workspace cannot be done by
    editing the account_id field. The binding is decided during login.
    """
    claims = _jwt_payload(access_token_of(data)) or {}
    auth = claims.get("https://api.openai.com/auth")
    value = (auth or {}).get("chatgpt_account_id") if isinstance(auth, dict) else None
    if not value:
        value = account_id_of(data)
    return str(value) if value else ""

ACCOUNTS, ACCOUNT_FILES, ACCOUNT_BY_NAME, ACCOUNT_ALIASES = _load_accounts_from_config_or_discovery()



def snapshot_meta(home: Path, name: str, probe: bool = False) -> dict[str, Any]:
    """Describe one snapshot: which account, which workspace, which tier, alive?

    This is what lets the picker tell a personal workspace apart from
    a team workspace and warn about a snapshot whose session the server
    already ended.
    """
    info = snapshot_info(home, name)
    meta: dict[str, Any] = {
        "exists": info["exists"],
        "ok": info["ok"],
        "prefix": info["prefix"],
        "plan": "",
        "plan_label": "",
        "email": "",
        "workspace": "",
        "workspace_short": "",
        "token_status": "missing" if not info["ok"] else "unchecked",
        "token_detail": "",
    }
    if not info["ok"]:
        return meta
    data = load_auth(snapshot_path(home, name))
    plan = plan_of(data)
    workspace = workspace_of(data)
    meta["plan"] = plan
    meta["plan_label"] = PLAN_LABELS.get(plan.lower(), plan)
    meta["email"] = email_of(data)
    meta["workspace"] = workspace
    meta["workspace_short"] = prefix_of(workspace) if workspace else ""
    if not probe:
        return meta
    try:
        result = probe_token(data)
        meta["token_status"] = result["status"]
        meta["token_detail"] = result["detail"]
    except Exception as exc:  # noqa: BLE001 - a probe must never break the picker
        meta["token_status"] = "unknown"
        meta["token_detail"] = str(exc)
    return meta


# A cheap, read-only endpoint that Codex Desktop itself calls at startup. It is
# only used to answer "does this account still have a live session?". Never
# call /oauth/token for this: refreshing rotates the refresh token, and a
# rotated token that we then discard would kill this machine's snapshot.
PROBE_URL = "https://chatgpt.com/backend-api/codex/models?client_version=0.154.0"
PROBE_TIMEOUT = 15.0

# Local proxy ports worth adopting when nothing else says how to get out. On the
# server the only route to chatgpt.com is the SSH RemoteForward that maps
# 127.0.0.1:17890 to this PC's Clash (see ~/.bashrc there), and a non-interactive
# `ssh host python3 ...` never sources ~/.bashrc. Order matters: the forwarded
# port first, then the usual Clash/V2Ray defaults on this PC.
PROXY_CANDIDATES = (
    ("http://127.0.0.1:7897", "http://127.0.0.1:7890", "http://127.0.0.1:17890")
    if sys.platform == "win32"
    else ("http://127.0.0.1:17890", "http://127.0.0.1:7897", "http://127.0.0.1:7890")
)


def ensure_proxy_env() -> str | None:
    """Adopt a local proxy for the probe if the caller did not pick one.

    Returns the proxy URL that was adopted, or None when a proxy is already
    configured (or none of the candidates answers). Only ports that actually
    accept a TCP connection are adopted, so this cannot turn a working direct
    connection into a broken one by inventing a dead proxy.
    """
    for key in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        if os.environ.get(key):
            return None
    for candidate in PROXY_CANDIDATES:
        host, _, port = candidate.split("//", 1)[1].partition(":")
        try:
            with socket.create_connection((host, int(port)), timeout=0.4):
                pass
        except OSError:
            continue
        os.environ["http_proxy"] = candidate
        os.environ["https_proxy"] = candidate
        return candidate
    return None


_PROBE_CACHE: dict[str, dict[str, Any]] = {}


def probe_token(data: dict[str, Any] | None, timeout: float = PROBE_TIMEOUT) -> dict[str, Any]:
    """Return {'status': 'ok'|'revoked'|'expired'|'unknown', 'detail': str}.

    'ok'      the server accepted the token: the session is alive.
    'revoked' the server answered 401 for the account's own access token: the
              ChatGPT session was ended server-side (personal sign-out,
              password change, seat removed, ...). Switching to such an account
              is pointless — Codex Desktop cannot establish its workspace
              session, falls back to the account's default identity and lands on
              the ChatGPT Work page with "You don't have access to Work yet".
              Only a fresh `codex login` fixes it.
    'expired' the access token's own exp has passed, so there is nothing to
              probe with. This is routine and NOT a dead session: Codex mints a
              new access token from the refresh token on first use. We do not
              refresh here on purpose — a rotated refresh token that we then
              discarded would break the snapshot.
    'unknown' unexpected HTTP status or a network failure.
    """
    token = access_token_of(data)
    if not token:
        return {"status": "unknown", "detail": "no access token"}
    account_id = account_id_of(data)
    cache_key = f"{token}:{account_id}"
    cached = _PROBE_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached)

    expiry = access_token_expiry(data)
    if expiry and expiry - time.time() < 60:
        # An expired *access* token is routine: Codex refreshes it from the
        # refresh token on first use. Probing would need that refresh, and a
        # rotated refresh token we then discarded would break this snapshot, so
        # report the honest answer instead of guessing "dead".
        res = {
            "status": "expired",
            "detail": "access token expired; Codex will refresh it from the refresh token on use",
        }
        _PROBE_CACHE[cache_key] = res
        return dict(res)
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "User-Agent": "codex_cli_rs/0.154.0",
    }
    if account_id:
        headers["chatgpt-account-id"] = account_id
    request = urllib.request.Request(PROBE_URL, headers=headers)
    proxy = ensure_proxy_env()
    try:
        with urllib.request.urlopen(
            request, timeout=timeout, context=ssl.create_default_context()
        ) as response:
            response.read(1)
        res = {"status": "ok", "detail": "http %d" % response.status}
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        if exc.code == 401:
            detail = "http 401"
            code = ""
            try:
                parsed = json.loads(body)
                code = str(((parsed.get("error") or {}).get("code") or ""))
            except Exception:
                pass
            res = {"status": "revoked", "detail": (detail + " " + code).strip()}
        else:
            res = {"status": "unknown", "detail": "http %d" % exc.code}
    except Exception as exc:
        hint = " (via %s)" % proxy if proxy else ""
        res = {"status": "unknown", "detail": str(exc) + hint}

    _PROBE_CACHE[cache_key] = res
    return dict(res)


def prefix_of(account_id: str) -> str:
    return account_id[:8] if account_id else ""


def name_of(data: dict[str, Any] | None) -> str | None:
    account_id = account_id_of(data)
    if not account_id:
        return None
    email = email_of(data)
    id_matches = [item for item in ACCOUNTS if item["account_id"] == account_id]
    if not id_matches:
        return None
    if email:
        email_matches = [item for item in id_matches if item.get("email") == email]
        if len(email_matches) == 1:
            return email_matches[0]["name"]
        if len(email_matches) > 1:
            return None
    if len(id_matches) == 1:
        return id_matches[0]["name"]
    return None


def matches_account(data: dict[str, Any] | None, name: str) -> bool:
    spec = ACCOUNT_BY_NAME[name]
    if account_id_of(data) != spec["account_id"]:
        return False
    expected_email = spec.get("email") or ""
    if not expected_email:
        return True
    email = email_of(data)
    return (not email) or email == expected_email


def snapshot_path(home: Path, name: str) -> Path:
    return home / ACCOUNT_FILES[name]


def live_path(home: Path) -> Path:
    return home / "auth.json"


def restrict_mode(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def atomic_copy(src: Path, dst: Path, stamp_now: bool = False) -> None:
    """Copy src over dst atomically.

    copy2 keeps the source mtime on purpose: a snapshot's mtime then reads as
    "when that token was captured". stamp_now=True is used for writes whose
    mtime should read as "when this happened" (live auth.json, a refreshed
    snapshot) instead of inheriting the source's timestamp.
    """
    if src.resolve() == dst.resolve():
        restrict_mode(dst)
        if stamp_now:
            os.utime(dst, None)
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    shutil.copy2(src, tmp)
    if stamp_now:
        os.utime(tmp, None)
    restrict_mode(tmp)
    os.replace(tmp, dst)
    restrict_mode(dst)


_DESKTOP_ROOTS_OVERRIDE: list[Path] | None = None
_WEBVIEW_PROFILE_GLOBS = (
    "codex-browser-app",
    "Default",
    "Partitions/*",
    "Default/Partitions/*",
    "web/Codex",
    "web/Codex/codex-browser-app",
    "web/Codex/Default",
    "web/Codex/Default/Partitions/*",
)


def _is_real_codex_home(home: Path) -> bool:
    try:
        return home.resolve() == (Path.home() / ".codex").resolve()
    except OSError:
        return False


def desktop_data_roots() -> list[Path]:
    if _DESKTOP_ROOTS_OVERRIDE is not None:
        candidates = list(_DESKTOP_ROOTS_OVERRIDE)
    else:
        candidates = []
        local_app = os.environ.get("LOCALAPPDATA") or ""
        if local_app:
            packages = Path(local_app) / "Packages"
            if packages.is_dir():
                for pkg in packages.glob("OpenAI.Codex_*"):
                    roaming = pkg / "LocalCache" / "Roaming" / "Codex"
                    candidates.append(roaming)
                    candidates.append(roaming / "web" / "Codex")
                    candidates.append(roaming / "web" / "Codex" / "Default")
            candidates.append(Path(local_app) / "Codex")
        home = Path.home()
        candidates.append(home / ".config" / "Codex")
        candidates.append(home / "AppData" / "Roaming" / "Codex")
    seen: set[Path] = set()
    out: list[Path] = []
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen or not path.is_dir():
            continue
        seen.add(resolved)
        out.append(path)
    return out


def desktop_webview_profiles() -> list[Path]:
    """Chromium profiles that restore ChatGPT Work vs Personal."""
    seen: set[Path] = set()
    out: list[Path] = []

    def add(path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        if resolved in seen or not path.is_dir():
            return
        seen.add(resolved)
        out.append(path)

    for root in desktop_data_roots():
        add(root)
        for pattern in _WEBVIEW_PROFILE_GLOBS:
            if "*" in pattern:
                for match in root.glob(pattern):
                    add(match)
            else:
                add(root / pattern)
    return out


def _wipe_dir_contents(path: Path) -> tuple[bool, bool]:
    """Return (changed, locked)."""
    if not path.is_dir():
        return False, False
    changed = False
    locked = False
    for child in list(path.iterdir()):
        try:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
            changed = True
        except OSError:
            locked = True
    return changed, locked


def _wipe_chatgpt_indexeddb(indexeddb: Path) -> tuple[bool, bool]:
    if not indexeddb.is_dir():
        return False, False
    changed = False
    locked = False
    for child in list(indexeddb.iterdir()):
        name = child.name.lower()
        if "chatgpt.com" not in name and "openai.com" not in name:
            continue
        try:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
            changed = True
        except OSError:
            locked = True
    return changed, locked


def _clear_chatgpt_cookies(db_path: Path) -> tuple[bool, bool]:
    if not db_path.is_file():
        return False, False
    try:
        import sqlite3

        con = sqlite3.connect(str(db_path), timeout=1)
        try:
            tables = {
                row[0]
                for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "cookies" not in tables:
                return False, False
            con.execute(
                "DELETE FROM cookies WHERE host_key LIKE ? OR host_key LIKE ?",
                ("%chatgpt.com%", "%openai.com%"),
            )
            con.commit()
        finally:
            con.close()
        return True, False
    except Exception:
        return False, True


_PAGE_STATE_NAME = "browser-sidebar-page-states.json"
_SURFACE_PARAM = "surface"
_SURFACE_VALUES = ("work", "personal")


def _strip_surface_param(url: str) -> str:
    """Drop ?surface=work|personal from a ChatGPT URL, keep everything else."""
    if not isinstance(url, str) or _SURFACE_PARAM not in url:
        return url
    try:
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

        parts = urlsplit(url)
        if not parts.netloc or "chatgpt.com" not in parts.netloc.lower():
            return url
        query = parse_qsl(parts.query, keep_blank_values=True)
        kept = [
            (key, value)
            for key, value in query
            if not (key.lower() == _SURFACE_PARAM and value.lower() in _SURFACE_VALUES)
        ]
        if len(kept) == len(query):
            return url
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment)
        )
    except Exception:
        return url


def _normalize_surfaces(node: Any) -> int:
    """Recursively strip surface= from stored URLs. Returns how many changed."""
    changed = 0
    if isinstance(node, dict):
        for key, value in list(node.items()):
            if isinstance(value, str):
                new = _strip_surface_param(value)
                if new != value:
                    node[key] = new
                    changed += 1
            else:
                changed += _normalize_surfaces(value)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            if isinstance(value, str):
                new = _strip_surface_param(value)
                if new != value:
                    node[index] = new
                    changed += 1
            else:
                changed += _normalize_surfaces(value)
    return changed


def reset_desktop_sidebar_surfaces(notes: list[str], dry_run: bool) -> None:
    """Neutralise restored sidebar tabs that point at ?surface=work.

    Codex Desktop restores its sidebar browser tabs from
    `browser-sidebar-page-states.json`. A tab last parked on
    `https://chatgpt.com/...?surface=work` reopens the ChatGPT Work product
    on the next launch, which a personal/Plus account renders as
    "You don't have access to Work yet" — even when auth.json is correct.
    """
    touched = 0
    for profile in desktop_webview_profiles():
        path = profile / _PAGE_STATE_NAME
        if not path.is_file():
            continue
        if dry_run:
            notes.append(f"would strip surface= from {path}")
            touched += 1
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        changed = _normalize_surfaces(data)
        if not changed:
            continue
        try:
            backup = path.with_name(path.name + ".bak")
            if not backup.exists():
                shutil.copy2(path, backup)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
            touched += 1
        except OSError:
            notes.append(f"could not rewrite {path}")
    if touched:
        notes.append(
            f"{'would strip' if dry_run else 'stripped'} surface= from {touched} sidebar page-state file(s)"
        )


def reset_desktop_chatgpt_session(notes: list[str], dry_run: bool) -> None:
    """Drop Work/Personal UI restore so the next Desktop launch follows auth.json.

    Codex Desktop keeps the last ChatGPT product (Work vs Personal) in the
    embedded webview partition, not in auth.json. Session Storage on the
    outer profile is not enough: Local Storage, chatgpt.com IndexedDB, and
    cookies in `codex-browser-app` restore Work after a Plus login.
    """
    touched = 0
    locked = False
    for profile in desktop_webview_profiles():
        for rel in ("Session Storage", "Local Storage", "Service Worker"):
            store = profile / rel
            if not store.exists():
                continue
            if dry_run:
                notes.append(f"would reset desktop store {store}")
                touched += 1
                continue
            changed, store_locked = _wipe_dir_contents(store)
            locked = locked or store_locked
            if changed:
                touched += 1
        indexeddb = profile / "IndexedDB"
        if indexeddb.exists():
            if dry_run:
                notes.append(f"would reset chatgpt IndexedDB {indexeddb}")
                touched += 1
            else:
                changed, store_locked = _wipe_chatgpt_indexeddb(indexeddb)
                locked = locked or store_locked
                if changed:
                    touched += 1
        for rel in (
            Path("Network") / "Cookies",
            Path("Cookies"),
        ):
            cookie_db = profile / rel
            if dry_run:
                if cookie_db.is_file():
                    notes.append(f"would clear chatgpt cookies {cookie_db}")
                    touched += 1
                continue
            changed, store_locked = _clear_chatgpt_cookies(cookie_db)
            locked = locked or store_locked
            if changed:
                touched += 1
    if touched:
        notes.append(
            f"{'would reset' if dry_run else 'reset'} Codex Desktop Work/Personal session ({touched} stores)"
        )
    else:
        notes.append("no Codex Desktop session stores found to reset")
    reset_desktop_sidebar_surfaces(notes, dry_run)
    if locked:
        notes.append(
            "Desktop webview stores were locked; quit Codex Desktop completely and switch again"
        )


def iter_backup_auths(home: Path) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for rel in BACKUP_RELATIVE:
        path = home / rel
        if path.is_file():
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                found.append(path)
    auto = home / "account_backup" / "_autosave"
    if auto.is_dir():
        for path in sorted(auto.glob("*/auth.json")):
            if path.is_file():
                resolved = path.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    found.append(path)
    return found


def snapshot_info(home: Path, name: str) -> dict[str, Any]:
    path = snapshot_path(home, name)
    data = load_auth(path)
    account_id = account_id_of(data)
    exists = path.is_file()
    info: dict[str, Any] = {
        "exists": exists,
        "ok": bool(exists and matches_account(data, name) and has_refresh_token(data)),
        "prefix": prefix_of(account_id),
        "mtime": None,
        "path": str(path),
    }
    if exists:
        info["mtime"] = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    return info


def status_payload(home: Path, notes: list[str] | None = None) -> dict[str, Any]:
    live = load_auth(live_path(home))
    account_id = account_id_of(live)
    snapshots = {name: snapshot_info(home, name) for name in ACCOUNT_FILES}
    return {
        "current": name_of(live),
        "account_id_prefix": prefix_of(account_id),
        "live_exists": live_path(home).is_file(),
        "accounts": list(ACCOUNT_FILES),
        "snapshots": snapshots,
        "notes": notes or [],
    }


def best_source(home: Path, name: str) -> tuple[Path, str] | None:
    ranked: list[tuple[int, float, Path, str]] = []
    live = live_path(home)
    live_data = load_auth(live)
    if matches_account(live_data, name) and has_refresh_token(live_data):
        ranked.append((0, live.stat().st_mtime, live, "live"))
    snap = snapshot_path(home, name)
    snap_data = load_auth(snap)
    if matches_account(snap_data, name) and has_refresh_token(snap_data):
        ranked.append((1, snap.stat().st_mtime, snap, "snapshot"))
    for path in iter_backup_auths(home):
        data = load_auth(path)
        if not matches_account(data, name) or not has_refresh_token(data):
            continue
        ranked.append((2, path.stat().st_mtime, path, "backup"))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[0], -item[1]))
    _prio, _mtime, path, kind = ranked[0]
    return path, kind


def refresh_current_snapshot(home: Path, dry_run: bool, notes: list[str]) -> dict[str, Any]:
    """Copy live auth onto the current account snapshot if the token changed.

    Does not write live auth.json. The inactive account is left alone: this
    machine has no newer refresh token for it, and the other machine's token
    must not be copied here.
    """
    live_data = load_auth(live_path(home))
    current_name = name_of(live_data)
    if not current_name or not has_refresh_token(live_data):
        notes.append("skip refresh: live is not a known account with a refresh token")
        return {"account": None, "status": "skipped"}
    dst = snapshot_path(home, current_name)
    snap_data = load_auth(dst)
    if (
        matches_account(snap_data, current_name)
        and refresh_token_of(snap_data) == refresh_token_of(live_data)
    ):
        notes.append(f"snapshot {current_name} already matches live refresh token")
        return {"account": current_name, "status": "already_current"}
    notes.append(
        f"{'would refresh' if dry_run else 'refresh'} {current_name} snapshot from live"
    )
    if not dry_run:
        atomic_copy(live_path(home), dst, stamp_now=True)
    return {"account": current_name, "status": "updated"}


def ensure_snapshots(
    home: Path, dry_run: bool, check_token: bool = True, probe_all: bool = False
) -> dict[str, Any]:
    notes: list[str] = []
    created: list[str] = []
    skipped: list[str] = []
    missing: list[str] = []
    for name in ACCOUNT_FILES:
        info = snapshot_info(home, name)
        if info["ok"]:
            skipped.append(name)
            notes.append(f"keep {name} snapshot {info['prefix']}")
            continue
        source = best_source(home, name)
        if source is None:
            missing.append(name)
            notes.append(f"missing {name}: no local auth with matching account_id")
            continue
        src, kind = source
        if src.resolve() == snapshot_path(home, name).resolve() and info["ok"]:
            skipped.append(name)
            continue
        notes.append(f"{'would copy' if dry_run else 'copy'} {name} from {kind} {prefix_of(account_id_of(load_auth(src)))}")
        if not dry_run:
            atomic_copy(src, snapshot_path(home, name))
        created.append(name)
    refresh = refresh_current_snapshot(home, dry_run, notes)
    probe: dict[str, Any] = {"status": "skipped", "detail": "not checked"}
    live_data = load_auth(live_path(home))
    if check_token and not dry_run and name_of(live_data):
        probe = probe_token(live_data)
        if probe["status"] == "revoked":
            notes.append(
                f"current account session is revoked on the server ({probe['detail']}); "
                "Codex Desktop cannot open its workspace and shows the Work page with "
                "no access - run 'codex login' again for this account"
            )
    payload = status_payload(home, notes)
    payload["created"] = created
    payload["skipped"] = skipped
    payload["missing"] = missing
    payload["refresh_account"] = refresh["account"]
    payload["refresh_status"] = refresh["status"]
    payload["current_token_status"] = probe["status"]
    payload["current_token_detail"] = probe["detail"]
    payload["dry_run"] = dry_run
    # Per-snapshot account/workspace/tier/session state, so the picker can show
    # personal workspace next to team workspace and mark the ones whose
    # session the server already ended. Probing is opt-in because it costs one
    # HTTP round trip per snapshot. Probes are dispatched concurrently across
    # snapshots so the round trips overlap.
    if probe_all and not dry_run:
        ensure_proxy_env()
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=len(ACCOUNT_FILES)) as pool:
            futures = {
                name: pool.submit(snapshot_meta, home, name, True)
                for name in ACCOUNT_FILES
            }
            payload["snapshot_meta"] = {
                name: futures[name].result()
                for name in ACCOUNT_FILES
            }
    else:
        payload["snapshot_meta"] = {
            name: snapshot_meta(home, name, probe=False)
            for name in ACCOUNT_FILES
        }
    return payload


def suggest_same_workspace_alternative(home: Path, target: str) -> str:
    """Find another snapshot bound to the same ChatGPT workspace that is alive.

    Multiple accounts can point at the same team workspace
    (chatgpt_account_id cc511f28) but they are separate logins with separate
    sessions. When one is revoked the other may still be usable, so the refusal
    message can tell the user exactly which account to pick instead.
    Returns "" when nothing useful can be suggested.
    """
    wanted = (ACCOUNT_BY_NAME.get(target) or {}).get("account_id")
    if not wanted:
        return ""
    alive: list[str] = []
    dead: list[str] = []
    for name in ACCOUNT_FILES:
        if name == target:
            continue
        if (ACCOUNT_BY_NAME.get(name) or {}).get("account_id") != wanted:
            continue
        if not snapshot_info(home, name)["ok"]:
            continue
        try:
            status = probe_token(load_auth(snapshot_path(home, name)))["status"]
        except Exception:  # noqa: BLE001 - advice is best-effort only
            continue
        if status == "ok":
            alive.append(name)
        elif status == "revoked":
            dead.append(name)
    parts = []
    if alive:
        parts.append(
            f"好消息：同一工作区里 {('、'.join(alive))} 的会话仍然有效，"
            "直接改选它就能进该团队空间，不需要重新登录。"
        )
    if dead:
        parts.append(f"（同工作区的 {('、'.join(dead))} 也已失效。）")
    return "".join(parts)


def switch_account(
    home: Path,
    target: str,
    dry_run: bool,
    check_token: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    target = ACCOUNT_ALIASES.get(target, target)
    if target not in ACCOUNT_FILES:
        raise RuntimeError(f"Unknown account '{target}'. Available: {' '.join(ACCOUNT_FILES)}")
    dst = snapshot_path(home, target)
    info = snapshot_info(home, target)
    if not info["ok"]:
        raise RuntimeError(
            f"Snapshot missing or mismatched: {dst}. Run with --ensure-snapshots first."
        )
    # Warn about an account whose session the server appears to have ended.
    # Everything downstream would fail confusingly: Desktop opens the ChatGPT
    # Work page and reports no access, which looks like a browser
    # Work/Personal problem even though the credential is dead.
    #
    # This is a warning, not a veto. A 401 from a single probe is not proof that
    # the refresh token is dead -- the account_id header, a flaky proxy or a
    # transient server reply can all produce one, and Codex may well mint a
    # fresh access token from the refresh token on first use. Only an actual
    # switch settles it, so `force=True` (CLI: --force) is the way the caller
    # says "I know, try it anyway". Callers that want a decision from the user
    # should catch this error, ask, then retry with force=True.
    forced_past_revoked = False
    probe: dict[str, Any] = {"status": "skipped", "detail": "not checked"}
    if check_token and not dry_run and _is_real_codex_home(home):
        probe = probe_token(load_auth(dst))
        if probe["status"] == "revoked":
            if force:
                forced_past_revoked = True
            else:
                advice = suggest_same_workspace_alternative(home, target)
                # The "pick the team in the workspace picker" hint only applies
                # to a team seat. A personal account has exactly one workspace,
                # so mentioning a team reads like a second, unrelated problem.
                if (ACCOUNT_BY_NAME.get(target) or {}).get("surface") == "work":
                    where = "（登录后在工作区选择器里选中该团队）"
                else:
                    where = "（该账号只有个人工作区，登录完直接生效）"
                raise RuntimeError(
                    f"账号 {target} 的 ChatGPT 会话已被服务端吊销（{probe['detail']}）。"
                    "这不是浏览器里 Work/Personal 记录的问题：凭据失效时 Desktop 建不起该工作区会话，"
                    "仍会落到 ChatGPT Work 页面并显示无权限。"
                    f"修复办法：用该账号重新登录{where}，再点一次快捷方式。"
                    "命令行跑 codex login --device-auth 即可，不需要打开 ChatGPT 网页。"
                    "（如果只是想试一下，本机脚本加 --force 可以跳过这道检查。）"
                    + (advice or "")
                )
    live = live_path(home)
    current_name = name_of(load_auth(live))
    notes: list[str] = []
    if forced_past_revoked:
        notes.append(
            "forced past a revoked session (%s); Codex will try the refresh token"
            % probe["detail"]
        )
    if current_name is None and live.is_file():
        unknown = home / f"auth.unknown-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        notes.append(
            f"{'would save' if dry_run else 'save'} unrecognized live auth to {unknown.name}"
        )
        if not dry_run:
            atomic_copy(live, unknown)
    elif current_name and current_name != target:
        notes.append(
            f"{'would save' if dry_run else 'save'} live {current_name} back to its snapshot"
        )
        if not dry_run:
            atomic_copy(live, snapshot_path(home, current_name))
    elif current_name == target:
        notes.append(f"live already {target}; refresh snapshot from live")
        if not dry_run:
            atomic_copy(live, dst)
    notes.append(f"{'would switch' if dry_run else 'switch'} live auth.json -> {target}")
    if not dry_run:
        atomic_copy(dst, live, stamp_now=True)
        restrict_mode(live)
        verify = name_of(load_auth(live))
        if verify != target:
            raise RuntimeError(f"Switch wrote auth.json but current is {verify}, not {target}")
    if _is_real_codex_home(home):
        reset_desktop_chatgpt_session(notes, dry_run)
    payload = status_payload(home, notes)
    if dry_run:
        payload["current"] = target
        payload["account_id_prefix"] = prefix_of(ACCOUNT_BY_NAME[target]["account_id"])
    payload["switched"] = True
    payload["target"] = target
    payload["token_status"] = probe["status"]
    payload["token_detail"] = probe["detail"]
    payload["dry_run"] = dry_run
    payload["message"] = f"codex account -> {target}"
    return payload


def self_test() -> int:
    global ACCOUNTS, ACCOUNT_FILES, ACCOUNT_BY_NAME, ACCOUNT_ALIASES
    saved_state = (ACCOUNTS, ACCOUNT_FILES, ACCOUNT_BY_NAME, ACCOUNT_ALIASES)
    with tempfile.TemporaryDirectory(prefix="codex_local_csw_") as raw:
        home = Path(raw)
        id_a = "ada3b2cc-fba3-479a-8efd-4ecf82a79911"
        id_b = "9dadff8e-4050-4b24-ac14-5ace3e7a3434"
        id_team = "cc511f28-aeae-4f69-b447-2a0c9e32cee9"

        ACCOUNTS = [
            {"name": "test_b", "file": "auth.test_b.json", "account_id": id_b, "email": "b@example.com", "surface": "personal"},
            {"name": "test_a", "file": "auth.test_a.json", "account_id": id_a, "email": "a@example.com", "surface": "personal"},
            {"name": "test_team_a", "file": "auth.test_team_a.json", "account_id": id_team, "email": "a@example.com", "surface": "work"},
            {"name": "test_team_b", "file": "auth.test_team_b.json", "account_id": id_team, "email": "b@example.com", "surface": "work"},
        ]
        ACCOUNT_FILES = {item["name"]: item["file"] for item in ACCOUNTS}
        ACCOUNT_BY_NAME = {item["name"]: item for item in ACCOUNTS}
        ACCOUNT_ALIASES = {}

        def write_auth(
            path: Path, account_id: str, refresh: str = "fake", email: str | None = None
        ) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tokens: dict[str, Any] = {"account_id": account_id, "refresh_token": refresh}
            if email:
                tokens["email"] = email
            path.write_text(json.dumps({"tokens": tokens}, indent=2), encoding="utf-8")

        try:
            write_auth(live_path(home), id_b)
            write_auth(home / "account_backup" / "a" / "auth.json", id_a, email="a@example.com")
            write_auth(home / "account_backup" / "b" / "auth.json", id_b)

            ensured = ensure_snapshots(home, dry_run=False)
            assert ensured["snapshots"]["test_b"]["ok"], ensured
            assert ensured["snapshots"]["test_a"]["ok"], ensured
            assert name_of(load_auth(live_path(home))) == "test_b"
            assert ensured["refresh_status"] in {"updated", "already_current"}, ensured
            assert refresh_token_of(load_auth(snapshot_path(home, "test_b"))) == "fake"

            write_auth(live_path(home), id_b, refresh="fake-new")
            refreshed = ensure_snapshots(home, dry_run=False)
            assert refreshed["refresh_status"] == "updated", refreshed
            assert refreshed["refresh_account"] == "test_b", refreshed
            assert refresh_token_of(load_auth(snapshot_path(home, "test_b"))) == "fake-new"
            assert refresh_token_of(load_auth(snapshot_path(home, "test_a"))) == "fake"
            assert name_of(load_auth(live_path(home))) == "test_b"

            same = ensure_snapshots(home, dry_run=False)
            assert same["refresh_status"] == "already_current", same

            switched = switch_account(home, "test_a", dry_run=False)
            assert switched["current"] == "test_a", switched
            assert account_id_of(load_auth(live_path(home))) == id_a
            assert account_id_of(load_auth(snapshot_path(home, "test_b"))) == id_b

            switched_back = switch_account(home, "test_b", dry_run=False)
            assert switched_back["current"] == "test_b", switched_back
            assert account_id_of(load_auth(live_path(home))) == id_b
            assert account_id_of(load_auth(snapshot_path(home, "test_a"))) == id_a

            unknown = home / "account_backup" / "c" / "auth.json"
            write_auth(unknown, "00000000-0000-0000-0000-000000000000")
            again = ensure_snapshots(home, dry_run=False)
            assert "test_a" in again["skipped"]
            assert account_id_of(load_auth(snapshot_path(home, "test_a"))) == id_a

            fourth = "11111111-1111-1111-1111-111111111111"
            write_auth(live_path(home), fourth, refresh="fourth")
            from_unknown = switch_account(home, "test_a", dry_run=False)
            assert from_unknown["current"] == "test_a", from_unknown
            assert account_id_of(load_auth(live_path(home))) == id_a
            saved = list(home.glob("auth.unknown-*.json"))
            assert len(saved) == 1, saved
            assert account_id_of(load_auth(saved[0])) == fourth
            assert refresh_token_of(load_auth(saved[0])) == "fourth"
            assert account_id_of(load_auth(snapshot_path(home, "test_a"))) == id_a
            assert account_id_of(load_auth(snapshot_path(home, "test_b"))) == id_b

            write_auth(snapshot_path(home, "test_team_a"), id_team, refresh="a-native", email="a@example.com")
            write_auth(live_path(home), id_team, refresh="b-team", email="b@example.com")
            team = ensure_snapshots(home, dry_run=False)
            assert team["current"] == "test_team_b", team
            assert team["snapshots"]["test_team_b"]["ok"], team
            assert team["snapshots"]["test_team_a"]["ok"], team
            assert refresh_token_of(load_auth(snapshot_path(home, "test_team_a"))) == "a-native"
            assert refresh_token_of(load_auth(snapshot_path(home, "test_team_b"))) == "b-team"
            assert name_of(load_auth(live_path(home))) == "test_team_b"

            fake_root = home / "desktop-root"
            profile = fake_root / "Default" / "Local Storage"
            profile.mkdir(parents=True)
            (profile / "test.txt").write_text("x", encoding="utf-8")

            (fake_root / "Default" / _PAGE_STATE_NAME).write_text(
                json.dumps({"url": "https://chatgpt.com/codex?surface=work"}),
                encoding="utf-8",
            )
            global _DESKTOP_ROOTS_OVERRIDE
            _DESKTOP_ROOTS_OVERRIDE = [fake_root]
            reset_notes: list[str] = []
            reset_desktop_chatgpt_session(reset_notes, dry_run=False)
            assert not any(profile.iterdir())
            assert any("stripped surface=" in line for line in reset_notes), reset_notes
        finally:
            _DESKTOP_ROOTS_OVERRIDE = None
            ACCOUNTS, ACCOUNT_FILES, ACCOUNT_BY_NAME, ACCOUNT_ALIASES = saved_state
    _print("self-test ok")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Switch local Codex auth.json only")
    parser.add_argument(
        "target",
        nargs="?",
        help="Account name to switch to (e.g. personal, team)",
    )
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    parser.add_argument("--json", action="store_true", help="machine-readable status")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--ensure-snapshots",
        action="store_true",
        help="create missing snapshots; refresh current account snapshot from live; never changes live auth.json",
    )
    parser.add_argument(
        "--skip-token-check",
        action="store_true",
        help="do not probe chatgpt.com to check whether the account session is still alive",
    )
    parser.add_argument(
        "--probe-all",
        action="store_true",
        help="with --ensure-snapshots: also probe every snapshot and report its workspace and session state",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="switch even when the probe reports the session revoked (a single 401 is not proof; Codex may still refresh)",
    )
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()

    home = args.codex_home.expanduser()
    check_token = not args.skip_token_check
    try:
        if args.ensure_snapshots:
            payload = ensure_snapshots(
                home,
                args.dry_run,
                check_token=check_token,
                probe_all=args.probe_all,
            )
            emit(payload, args.json)
            return 0
        if args.target:
            payload = switch_account(
                home,
                args.target,
                args.dry_run,
                check_token=check_token,
                force=args.force,
            )
            emit(payload, args.json)
            if not args.json:
                _print(payload["message"])
                _print("  reopen Codex Desktop / start a new session for it to take effect")
            return 0
        emit(status_payload(home), args.json)
        return 0
    except Exception as exc:
        write_message_file(str(exc))
        _print(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
