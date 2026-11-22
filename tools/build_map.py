"""Turn raw per-test traces into map.pkl.z (run once, offline).

usage: python build_map.py TESTS_JSON TRACE_DIR[,TRACE_DIR2] SQLGLOT_SRC OUT
Everything in the map comes from running the pinned commit's own tests.
"""

import ast
import glob
import json
import os
import pickle
import re
import sys
import zlib
from collections import defaultdict

tests_json, trace_dirs, src, out = sys.argv[1:5]
tests = json.load(open(tests_json))["tests"]
ids = [t["id"] for t in tests]

cov = defaultdict(lambda: defaultdict(set))  # rel -> line -> {test idx}
opened = defaultdict(set)
listed = defaultdict(set)
inst = defaultdict(set)
status = {}


def rel(p):
    return p[len("/src/"):] if p.startswith("/src/") else None


for td in trace_dirs.split(","):
    for t in tests:
        i = t["number"]
        d = os.path.join(td, str(i))
        rc_path = os.path.join(d, "rc")
        if not os.path.exists(rc_path):
            print("missing trace", i, t["id"], file=sys.stderr)
            continue
        status.setdefault(i, open(rc_path).read().split()[0])
        for pj in glob.glob(os.path.join(d, "p*.json")):
            try:
                data = json.load(open(pj))
            except Exception as ex:
                print("bad", pj, ex, file=sys.stderr)
                continue
            for f, lines in data["lines"].items():
                r = rel(f)
                if r is None:
                    continue
                c = cov[r]
                for ln in lines:
                    c[ln].add(i)
            for f in data["opened"]:
                r = rel(f)
                if r and not r.endswith(".pyc") and "__pycache__" not in r:
                    opened[r].add(i)
            for f in data["listed"]:
                r = rel(f)
                if r is not None:
                    listed[r.rstrip("/")].add(i)
            for k in data.get("inst", []):
                inst[k].add(i)

sets = []
intern = {}


def sid(s):
    fs = frozenset(s)
    if fs not in intern:
        intern[fs] = len(sets)
        sets.append(fs)
    return intern[fs]


# module importers + name references, from the pinned source
mod_imp = {}
refs = defaultdict(set)
class_attr_defs = defaultdict(int)  # name -> number of classes binding it at class level
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
for root in ("sqlglot", "tests"):
    for path in glob.glob(os.path.join(src, root, "**", "*.py"), recursive=True):
        r = os.path.relpath(path, src).replace(os.sep, "/")
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except SyntaxError:
            continue
        c = cov.get(r, {})
        imp = set()
        for st in tree.body:
            for ln in range(st.lineno, (st.end_lineno or st.lineno) + 1):
                imp |= c.get(ln, set())
        mod_imp[r] = imp
        if root == "sqlglot":
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    for st in node.body:
                        tg = []
                        if isinstance(st, ast.Assign):
                            tg = [t.id for t in st.targets if isinstance(t, ast.Name)]
                        elif isinstance(st, (ast.AnnAssign, ast.AugAssign)) and isinstance(st.target, ast.Name):
                            tg = [st.target.id]
                        elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            tg = [st.name]
                        for n in tg:
                            class_attr_defs[n] += 1
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                name = node.id
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                name = node.attr
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and IDENT.match(node.value):
                name = node.value
            elif isinstance(node, ast.alias):
                name = node.name.split(".")[-1]
            if name is None:
                continue
            ln = getattr(node, "lineno", None)
            if ln is None:
                continue
            for l2 in range(ln, (getattr(node, "end_lineno", None) or ln) + 1):
                refs[name] |= c.get(l2, set())
        # string constants that embed identifiers dynamically (f"{x}_sql" style)
        # are not resolvable; method add/remove falls back to class usage.

m = {
    "tests": ids,
    "sets": sets,
    "cov": {f: {ln: sid(s) for ln, s in lines.items()} for f, lines in cov.items()},
    "opened": {f: sid(s) for f, s in opened.items()},
    "listed": {f: sid(s) for f, s in listed.items()},
    "inst": {k: sid(s) for k, s in inst.items()},
    "refs": {k: sid(s) for k, s in refs.items() if s},
    "mod_imp": {k: sid(s) for k, s in mod_imp.items()},
    "class_attr_defs": dict(class_attr_defs),
}
blob = zlib.compress(pickle.dumps(m, protocol=4), 9)
open(out, "wb").write(blob)
bad = {i: s for i, s in status.items() if s != "0"}
print(
    f"tests {len(ids)} traced {len(status)} nonzero {len(bad)} files {len(cov)} sets {len(sets)} "
    f"inst {len(inst)} refs {len(refs)} -> {len(blob) / 1e6:.2f} MB",
    file=sys.stderr,
)
for i, s in sorted(bad.items())[:40]:
    print("  rc", s, ids[i], file=sys.stderr)
