"""Generate synthetic single-edit patches against the pinned sqlglot, for tuning.

usage: python mutate.py SQLGLOT_SRC OUT_DIR COUNT [SEED]
Writes OUT_DIR/commits/<id>.patch. Ground truth comes from verify.py.
These are only for measuring misses; nothing here goes into the map.
"""

import difflib
import glob
import os
import random
import re
import sys

src, out, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
rng = random.Random(int(sys.argv[4]) if len(sys.argv) > 4 else 1)
os.makedirs(os.path.join(out, "commits"), exist_ok=True)

py = [p for p in glob.glob(os.path.join(src, "sqlglot", "**", "*.py"), recursive=True)]
tests_py = [p for p in glob.glob(os.path.join(src, "tests", "**", "test_*.py"), recursive=True)]
fixtures = [p for p in glob.glob(os.path.join(src, "tests", "fixtures", "**", "*.sql"), recursive=True)]
COMMENTS = ["# Cheap check first", "# Kept for backwards compatibility", "# TODO: revisit", "# Intentionally not cached",
            "# Mirrors the behavior of the reference implementation", "# Keep in sync with the generator"]

SWAPS = [(" is not ", " is "), (" is ", " is not "), (" == ", " != "), (" != ", " == "), (" and ", " or "),
         (" or ", " and "), (" < ", " <= "), (" > ", " >= "), (" is None", " == None"), ("not ", ""),
         (" in ", " not in "), ("True", "False"), ("False", "True"), (" + 1", " - 1"), ("[0]", "[1]"),
         ("[-1]", "[0]"), (" is NormalizationStrategy", " == NormalizationStrategy")]


def rel(p):
    return os.path.relpath(p, src).replace(os.sep, "/")


def mk_patch(path, old, new):
    r = rel(path)
    diff = difflib.unified_diff(old, new, f"a/{r}", f"b/{r}", n=3)
    body = "".join(diff)
    return f"diff --git a/{r} b/{r}\nindex 0000000..1111111 100644\n" + body


def code_mut(lines):
    for _ in range(200):
        i = rng.randrange(len(lines))
        ln = lines[i]
        s = ln.strip()
        if not s or s.startswith(("#", '"""', "'''", "@", "def ", "class ", "import ", "from ")):
            continue
        kind = rng.random()
        if kind < 0.55:
            opts = [(a, b) for a, b in SWAPS if a in ln]
            if not opts:
                continue
            a, b = rng.choice(opts)
            idx = [m.start() for m in re.finditer(re.escape(a), ln)]
            j = rng.choice(idx)
            new = ln[:j] + b + ln[j + len(a):]
            return i, [new], "swap"
        if kind < 0.75:
            ms = list(re.finditer(r"(?<![\w.])(\d+)(?![\w.])", ln))
            if not ms:
                continue
            m = rng.choice(ms)
            v = int(m.group(1))
            new = ln[: m.start()] + str(v + rng.choice([1, -1] if v else [1])) + ln[m.end():]
            return i, [new], "int"
        if kind < 0.9:
            ms = list(re.finditer(r'"([A-Za-z_ ]{2,})"', ln))
            if not ms:
                continue
            m = rng.choice(ms)
            new = ln[: m.start(1)] + m.group(1) + "X" + ln[m.end(1):]
            return i, [new], "str"
        # delete a simple one-line statement
        if s.endswith((":", ",", "(", "[", "{", "\\")) or s.startswith(("return", "else", "elif", "try", "except", "finally")):
            continue
        if ln.count("(") != ln.count(")") or ln.count("[") != ln.count("]") or ln.count("{") != ln.count("}"):
            continue
        return i, [], "del"
    return None


def comment_mut(lines):
    for _ in range(100):
        i = rng.randrange(len(lines))
        nxt = lines[i]
        if not nxt.strip():
            continue
        ind = re.match(r"\s*", nxt).group(0)
        return i, [ind + rng.choice(COMMENTS) + "\n", nxt], "comment"
    return None


def fixture_mut(lines):
    for _ in range(100):
        i = rng.randrange(len(lines))
        ln = lines[i]
        if not ln.strip() or ln.startswith("#") or ln.startswith("--"):
            continue
        j = rng.randrange(1, max(2, len(ln) - 1))
        new = ln[:j] + rng.choice(["x", " 1", "(", ""]) + ln[j + (1 if rng.random() < 0.3 else 0):]
        if new == ln:
            continue
        return i, [new], "fixture"
    return None


def test_mut(lines):
    for _ in range(200):
        i = rng.randrange(len(lines))
        ln = lines[i]
        ms = list(re.finditer(r'"([^"\\]{3,})"', ln))
        if not ms or ln.strip().startswith("#"):
            continue
        m = rng.choice(ms)
        k = rng.randrange(len(m.group(1)))
        g = m.group(1)
        new = ln[: m.start(1)] + g[:k] + g[k + 1:] + ln[m.end(1):]
        return i, [new], "testedit"
    return None


n = 0
attempts = 0
while n < count and attempts < count * 50:
    attempts += 1
    r = rng.random()
    if r < 0.6:
        path, fn = rng.choice(py), code_mut
    elif r < 0.72:
        path, fn = rng.choice(py), comment_mut
    elif r < 0.9:
        path, fn = rng.choice(fixtures), fixture_mut
    else:
        path, fn = rng.choice(tests_py), test_mut
    lines = open(path, encoding="utf-8").read().splitlines(keepends=True)
    if not lines:
        continue
    res = fn(lines)
    if res is None:
        continue
    i, repl, kind = res
    new = lines[:i] + repl + lines[i + 1:]
    if new == lines:
        continue
    cid = f"m{n:03d}{kind[0]}"
    with open(os.path.join(out, "commits", cid + ".patch"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(mk_patch(path, lines, new))
    n += 1
print(f"wrote {n} mutants", file=sys.stderr)
