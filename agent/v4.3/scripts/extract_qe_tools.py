#!/usr/bin/env python3
"""Extract QE_TOOLS_PY embedded as a TS template literal from
WarpAgentLab/src/lib/qe-helpers.ts.

Decodes the JS template-literal escape sequences that appear in the embedded
Python source: \\\\ -> \\, \\` -> `, \\n -> LF, \\t -> TAB, \\$ -> $.
For unknown escapes (\\w, \\d, \\s, ...) we keep both characters: that way
TS source `\\\\w` -> `\\w` (Python regex semantics preserved). A stray `\\w`
in source (which JS would silently drop the backslash for) becomes `\\w` in
output here too — safer than dropping the backslash for our use case.
"""
import re
from pathlib import Path

SRC = Path("/path/to/WarpAgentLab/src/lib/qe-helpers.ts")
DST = Path("/path/to/solvers/current/qe_tools.py")

text = SRC.read_text(encoding="utf-8")
m = re.search(r"export const QE_TOOLS_PY = `(.*?)`;\s*$", text, re.DOTALL | re.MULTILINE)
if not m:
    raise SystemExit("could not locate QE_TOOLS_PY template literal in qe-helpers.ts")

raw = m.group(1)
assert "${" not in raw, "unexpected TS interpolation in QE_TOOLS_PY"

KNOWN = {"\\": "\\", "`": "`", "n": "\n", "t": "\t", "r": "\r",
         "$": "$", "'": "'", '"': '"', "0": "\0"}

out = []
i = 0
while i < len(raw):
    if raw[i] == "\\" and i + 1 < len(raw):
        c = raw[i + 1]
        out.append(KNOWN.get(c, "\\" + c))
        i += 2
    else:
        out.append(raw[i])
        i += 1
py = "".join(out)

DST.write_text(py, encoding="utf-8")
print(f"wrote {DST} ({len(py.splitlines())} lines, {len(py)} bytes)")
