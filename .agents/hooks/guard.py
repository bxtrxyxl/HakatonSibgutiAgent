"""PreToolUse safety guard for Antigravity hooks (.agents/hooks.json).

Replicates opencode.json permissions: blocks reading/editing .env files
(except .env.example) and blocks destructive shell / git commands.
"""

from __future__ import annotations

import json
import os
import re
import sys

BLOCKED_SHELL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\bgit\s+add\s+[^\n]*(-f|--force)\b"), "Запрещено использовать git add -f / --force."),
    (re.compile(r"(?i)\bgit\s+add\s+[^\n]*(?:^|[\s/\\])\.env(?!\.example\b)"), "Запрещено добавлять .env в git."),
    (re.compile(r"(?i)\bgit\s+add\s+[^\n]*\.venv\.lnk\b"), "Запрещено добавлять .venv.lnk в git."),
    (re.compile(r"(?i)\bgit\s+reset\s+--hard\b"), "Запрещено выполнять git reset --hard."),
    (re.compile(r"(?i)\bgit\s+clean\s+[^\n]*-[^\s]*f"), "Запрещено выполнять git clean -f."),
    (re.compile(r"(?i)\brm\s+[^\n]*-(?:rf|fr|r|f)\b"), "Запрещено удалять файлы через rm -r/-f."),
    (
        re.compile(r"(?i)\bRemove-Item\b[^\n]*-(?:Recurse|Force)\b"),
        "Запрещено массовое удаление через Remove-Item -Recurse/-Force.",
    ),
    (
        re.compile(
            r"(?i)\b(Get-Content|gc|cat|type|more|head|tail)\b[^\n]*(?:^|[\s'\"/\\])\.env(?!\.example\b)"
        ),
        "Запрещено читать .env через терминал (секреты не должны попадать в контекст).",
    ),
]


def _is_forbidden_env_path(path: str) -> bool:
    if not path:
        return False
    base = os.path.basename(path.replace("\\", "/")).lower()
    if base == ".env.example":
        return False
    return base == ".env" or base.startswith(".env.")


def _deny(reason: str) -> None:
    sys.stdout.write(json.dumps({"decision": "deny", "reason": reason}, ensure_ascii=False))


def _allow() -> None:
    sys.stdout.write(json.dumps({"decision": "allow"}))


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        _allow()
        return

    tool_call = payload.get("toolCall") or {}
    name = str(tool_call.get("name") or "").lower()
    args = tool_call.get("args") or {}

    if name in {"read_file", "edit_file", "write_file", "view_file"}:
        path = str(args.get("Path") or args.get("path") or args.get("AbsolutePath") or "")
        if _is_forbidden_env_path(path):
            _deny(
                "Чтение и изменение .env запрещено политикой безопасности "
                "(используйте .env.example и src/config.py)."
            )
            return

    if name == "run_command":
        cmd = str(args.get("CommandLine") or args.get("command") or "")
        for pattern, reason in BLOCKED_SHELL_PATTERNS:
            if pattern.search(cmd):
                _deny(reason)
                return

    _allow()


if __name__ == "__main__":
    main()
