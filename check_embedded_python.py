"""Compile-check the python that start_codex_desktop.ps1 embeds as here-strings.

The script builds its remote helper by concatenating two single-quoted
here-strings and substituting __SWITCH_TO__ / __SWITCH_FORCE__ placeholders.
A syntax error in there is invisible locally: it only surfaces as a confusing
"重启服务器 app-server 失败" on the server. Run this after touching any code
between @' and '@ in start_codex_desktop.ps1.

  python check_embedded_python.py
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

SRC = pathlib.Path(__file__).resolve().parent / "start_codex_desktop.ps1"
# Single-quoted here-strings look like:  @' <newline> ... <newline> '<@ (indented)
BLOCK = re.compile(r"@'\r?\n(.*?)\r?\n[ \t]*'@", re.S)
# The two halves of the remote switch helper; they are concatenated in order.
HELPER_BLOCKS = (1, 2)


def main() -> int:
    text = SRC.read_bytes().decode("utf-8")
    blocks = BLOCK.findall(text)
    print("here-string blocks found: %d" % len(blocks))

    bad = 0
    for index, block in enumerate(blocks):
        if not re.search(r"^\s*(import|from|def|SWITCH_TO|SWITCH_FORCE)", block, re.M):
            continue
        code = stub(block)
        try:
            ast.parse(code)
            print("  block %d: python OK (%d lines)" % (index, len(code.splitlines())))
        except SyntaxError as exc:
            bad += 1
            print("  block %d: SYNTAX ERROR line %s: %s" % (index, exc.lineno, exc.msg))

    joined = stub("".join(blocks[i] for i in HELPER_BLOCKS))
    try:
        ast.parse(joined)
        print("concatenated helper: python OK (%d lines)" % len(joined.splitlines()))
    except SyntaxError as exc:
        bad += 1
        print("concatenated helper: SYNTAX ERROR line %s: %s" % (exc.lineno, exc.msg))

    print("bad: %d" % bad)
    return 1 if bad else 0


def stub(block: str) -> str:
    return block.replace("__SWITCH_TO__", '"test-account"').replace(
        "__SWITCH_FORCE__", "False"
    ).replace("__CSW_PATH__", '"~/.local/bin/codex_local_csw.py"')


if __name__ == "__main__":
    sys.exit(main())
