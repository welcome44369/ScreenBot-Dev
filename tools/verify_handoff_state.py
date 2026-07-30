"""Read-only verification for the ScreenBot Dev repository handoff."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_ROOT = ROOT.resolve()
EXPECTED_BRANCH = "dev/screenbot-next"
DEV_URL = "https://github.com/welcome44369/ScreenBot-Dev.git"
LEGACY_URL = "https://github.com/welcome44369/ScreenBot.git"


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def check(label: str, actual: str, expected: str) -> bool:
    passed = actual.replace("\\", "/") == expected.replace("\\", "/")
    print(f"{label}: {'PASS' if passed else 'FAIL'} ({actual})")
    return passed


def main() -> int:
    checks = [
        check("worktree", git("rev-parse", "--show-toplevel"), str(EXPECTED_ROOT)),
        check("branch", git("branch", "--show-current"), EXPECTED_BRANCH),
        check("origin fetch", git("remote", "get-url", "origin"), DEV_URL),
        check("origin push", git("remote", "get-url", "--push", "origin"), DEV_URL),
        check("legacy fetch", git("remote", "get-url", "screenbot-readonly"), LEGACY_URL),
        check("legacy push", git("remote", "get-url", "--push", "screenbot-readonly"), "DISABLED"),
    ]
    status = git("status", "--branch", "--short")
    print(f"status: {status or 'clean'}")
    print("HANDOFF_STATE=" + ("PASS" if all(checks) else "FAIL"))
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
