"""Check the switch policy around a probed 401: warn once, never silently refuse.

The rule this guards: a 401 from probe_token is a warning, not a veto. A single
401 can be wrong (wrong chatgpt-account-id header, flaky proxy, transient server
reply) and Codex may still mint a fresh access token from the refresh token, so
the picker asks once and switches if the user says yes. --force skips the
question; -Silent counts as "no" so a background run cannot hang.

Everything runs against a throwaway copy of ~/.codex with the Desktop webview
roots redirected to an empty temp dir, so the real snapshots and the real
ChatGPT session state are never touched. probe_token is stubbed, so no network.

  python check_switch_policy.py
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("csw", HERE / "codex_local_csw.py")
csw = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(csw)

failures: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  -> " + extra) if extra and not ok else ""))
    if not ok:
        failures.append(name)


def stub(status: str) -> None:
    csw.probe_token = lambda data, timeout=None: {
        "status": status,
        "detail": "http 401 token_revoked",
    }


def live_name(home: pathlib.Path) -> str | None:
    data = json.loads((home / "auth.json").read_text(encoding="utf-8"))
    return csw.name_of(data)


def write_test_auth(path: pathlib.Path, account_id: str, email: str = "", plan: str = "plus") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tokens = {
        "account_id": account_id,
        "refresh_token": "mock-refresh",
        "access_token": "mock-access",
        "email": email,
    }
    data = {"tokens": tokens, "account_id": account_id, "email": email}
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="csw-policy-"))
    try:
        home = tmp / ".codex"
        home.mkdir()
        (tmp / "desktop").mkdir()

        # Define test accounts
        test_accounts = [
            {
                "name": "test_personal",
                "file": "auth.test_personal.json",
                "account_id": "00000000-0000-0000-0000-000000000001",
                "email": "personal@example.com",
                "surface": "personal",
            },
            {
                "name": "test_team",
                "file": "auth.test_team.json",
                "account_id": "00000000-0000-0000-0000-000000000002",
                "email": "team@example.com",
                "surface": "work",
            },
        ]
        csw.ACCOUNTS = test_accounts
        csw.ACCOUNT_FILES = {item["name"]: item["file"] for item in test_accounts}
        csw.ACCOUNT_BY_NAME = {item["name"]: item for item in test_accounts}
        csw.ACCOUNT_ALIASES = {}

        write_test_auth(home / "auth.test_personal.json", test_accounts[0]["account_id"], test_accounts[0]["email"])
        write_test_auth(home / "auth.test_team.json", test_accounts[1]["account_id"], test_accounts[1]["email"])
        write_test_auth(home / "auth.json", test_accounts[0]["account_id"], test_accounts[0]["email"])

        csw._DESKTOP_ROOTS_OVERRIDE = [tmp / "desktop"]
        csw._is_real_codex_home = lambda h: True
        start = live_name(home)

        print("A. 没有 --force：报错拒绝，且不写 auth.json")
        stub("revoked")
        try:
            csw.switch_account(home, "test_personal", dry_run=False, force=False)
            check("raises without force", False, "no exception")
            msg = ""
        except RuntimeError as exc:
            msg = str(exc)
            check("raises without force", True)
            check("names the account", "test_personal" in msg)
            check("personal wording, no team talk", "个人工作区" in msg, msg)
            check("points at --force", "--force" in msg, msg)
        check("live auth.json untouched by the refusal", live_name(home) == start, str(live_name(home)))

        print("B. 加 --force：放行，并留下可审计的 note")
        stub("revoked")
        try:
            payload = csw.switch_account(home, "test_personal", dry_run=False, force=True)
            notes = " | ".join(str(n) for n in payload.get("notes") or [])
            check("force=True switches", live_name(home) == "test_personal", str(live_name(home)))
            check("note records the forced switch", "forced past a revoked session" in notes, notes)
        except Exception as exc:  # noqa: BLE001
            check("force=True switches", False, repr(exc))

        print("C. 团队账号的措辞要提到工作区选择器")
        stub("revoked")
        try:
            csw.switch_account(home, "test_team", dry_run=False, force=False)
            check("raises for team account", False, "no exception")
        except RuntimeError as exc:
            check("raises for team account", True)
            check("team wording mentions the picker", "工作区选择器" in str(exc), str(exc))

        print("D. 会话正常时不受影响")
        stub("ok")
        try:
            payload = csw.switch_account(home, "test_personal", dry_run=False, force=False)
            notes = " | ".join(str(n) for n in payload.get("notes") or [])
            check("alive account still switches", live_name(home) == "test_personal", str(live_name(home)))
            check("no forced note when alive", "forced past" not in notes, notes)
        except Exception as exc:  # noqa: BLE001
            check("alive account still switches", False, repr(exc))

        print("E. --force 已注册为 CLI 参数")
        help_text = subprocess.run(
            [sys.executable, str(HERE / "codex_local_csw.py"), "--help"],
            capture_output=True,
            text=True,
        ).stdout
        check("--force documented in --help", "--force" in help_text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("FAILURES: %d" % len(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
