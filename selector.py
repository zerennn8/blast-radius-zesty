"""Blast Radius selector.

For each patch against the pinned sqlglot commit, work out which tests can be
affected, using a map built once from the pinned commit (per-test line
coverage, files each test opened, classes each test instantiated, and name
references), then pack the chosen tests onto workers.

Principle: when unsure, include. A miss costs 300 s; an extra test costs ms.
"""

from __future__ import annotations

import ast
import json
import os
import pickle
import re
import sys
import zlib
from difflib import SequenceMatcher

DATA = os.environ.get("BR_DATA", "/data")
APP = os.path.dirname(os.path.abspath(__file__))
SRC = os.environ.get("BR_SRC", os.path.join(APP, "sqlglot"))
MAP = os.environ.get("BR_MAP", os.path.join(APP, "map.pkl.z"))
WORKER_MS = 2000
MAX_WORKERS = 8


def log(*a):
    print(*a, file=sys.stderr)


# --------------------------------------------------------------------------
# map access


class Map:
    def __init__(self, path):
        with open(path, "rb") as fh:
            m = pickle.loads(zlib.decompress(fh.read()))
        self.test_ids = m["tests"]  # map index -> test id
        self.sets = m["sets"]  # set id -> frozenset of map indexes
        self.cov = m["cov"]  # file -> {line: set id}; negative line = code entered
        self.opened = m["opened"]  # file -> set id (read as data)
        self.listed = m["listed"]  # dir -> set id
        self.inst = m["inst"]  # "module:qualname" -> set id
        self.refs = m["refs"]  # name -> set id (tests covering a Load reference)
        self.mod_imp = m["mod_imp"]  # file -> set id (tests that imported the module)
        self.class_attr_defs = m.get("class_attr_defs", {})  # name -> classes defining it
        self.all = frozenset(range(len(self.test_ids)))
        self.file_tests = {}
        for i, tid in enumerate(self.test_ids):
            self.file_tests.setdefault(tid.split("::", 1)[0], set()).add(i)

    def s(self, sid):
        return self.sets[sid] if sid is not None else frozenset()

    def lines(self, f, lo, hi):
        c = self.cov.get(f)
        out = set()
        if not c:
            return out
        for ln in range(lo, hi + 1):
            sid = c.get(ln)
            if sid is not None:
                out |= self.sets[sid]
        return out

    def entered(self, f, firstlines):
        c = self.cov.get(f)
        out = set()
        if c:
            for ln in firstlines:
                sid = c.get(-ln)
                if sid is not None:
                    out |= self.sets[sid]
        return out

    def importers(self, f):
        sid = self.mod_imp.get(f)
        if sid is not None:
            return set(self.sets[sid])
        # unknown module: everything that touched the file at all
        out = set()
        for sid in (self.cov.get(f) or {}).values():
            out |= self.sets[sid]
        return out

    def usage(self, modname, qual):
        sid = self.inst.get(f"{modname}:{qual}")
        return None if sid is None else set(self.sets[sid])

    def ref(self, name):
        sid = self.refs.get(name)
        return set(self.sets[sid]) if sid is not None else set()


# --------------------------------------------------------------------------
# patch parsing and application

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text):
    files = []
    cur = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("diff --git "):
            cur = {"old": None, "new": None, "hunks": [], "binary": False}
            m = re.match(r"diff --git a/(.*) b/(.*)$", ln)
            if m:
                cur["old"], cur["new"] = m.group(1), m.group(2)
            files.append(cur)
        elif ln.startswith("--- ") and (cur is None or (not cur["hunks"] and i + 1 < len(lines) and lines[i + 1].startswith("+++ "))):
            if cur is None or cur["hunks"]:
                cur = {"old": None, "new": None, "hunks": [], "binary": False}
                files.append(cur)
            p = ln[4:].split("\t")[0]
            cur["old"] = None if p == "/dev/null" else (p[2:] if p.startswith("a/") else p)
            p = lines[i + 1][4:].split("\t")[0]
            cur["new"] = None if p == "/dev/null" else (p[2:] if p.startswith("b/") else p)
            i += 1
        elif ln.startswith("new file mode") and cur is not None:
            cur["created"] = True
        elif ln.startswith("deleted file mode") and cur is not None:
            cur["deleted"] = True
        elif ln.startswith("rename from ") and cur is not None:
            cur["old"] = ln[len("rename from "):]
        elif ln.startswith("rename to ") and cur is not None:
            cur["new"] = ln[len("rename to "):]
        elif ln.startswith("Binary files") or ln.startswith("GIT binary patch"):
            if cur is not None:
                cur["binary"] = True
        elif ln.startswith("@@") and cur is not None:
            m = HUNK.match(ln)
            if m:
                h = {
                    "os": int(m.group(1)),
                    "ol": int(m.group(2)) if m.group(2) is not None else 1,
                    "ns": int(m.group(3)),
                    "nl": int(m.group(4)) if m.group(4) is not None else 1,
                    "lines": [],
                }
                cur["hunks"].append(h)
                o, n = h["ol"], h["nl"]
                i += 1
                while i < len(lines) and (o > 0 or n > 0):
                    b = lines[i]
                    if b.startswith("\\"):
                        i += 1
                        continue
                    tag = b[:1] if b else " "
                    if tag not in " +-":
                        break
                    h["lines"].append((tag, b[1:]))
                    if tag in " -":
                        o -= 1
                    if tag in " +":
                        n -= 1
                    i += 1
                while i < len(lines) and lines[i].startswith("\\"):
                    i += 1
                continue
        i += 1
    for f in files:
        if f.get("created"):
            f["old"] = None
        if f.get("deleted"):
            f["new"] = None
    return files


def apply_hunks(old_text, hunks):
    """Return new text, or None when the hunks do not apply cleanly."""
    old = old_text.split("\n")
    out = []
    pos = 0
    for h in hunks:
        want = [t for tag, t in h["lines"] if tag in " -"]
        start = h["os"] - 1 if h["ol"] > 0 else h["os"]
        found = None
        for delta in range(0, 200):
            for s in (start + delta, start - delta):
                if s < pos or s > len(old):
                    continue
                if old[s : s + len(want)] == want:
                    found = s
                    break
            if found is not None:
                break
        if found is None:
            return None
        out.extend(old[pos:found])
        out.extend(t for tag, t in h["lines"] if tag in " +")
        pos = found + len(want)
    out.extend(old[pos:])
    return "\n".join(out)


# --------------------------------------------------------------------------
# AST diff


def dump(n):
    return ast.dump(n, include_attributes=False)


class NoneCmp(ast.NodeTransformer):
    """`x is None` and `x == None` (and `is not` / `!=`) behave the same for every
    type sqlglot compares: no sqlglot __eq__ treats None as equal. Normalize them."""

    def visit_Compare(self, n):
        self.generic_visit(n)
        ops = []
        for i, op in enumerate(n.ops):
            left = n.left if i == 0 else n.comparators[i - 1]
            right = n.comparators[i]
            none = any(isinstance(x, ast.Constant) and x.value is None for x in (left, right))
            if none and isinstance(op, ast.Eq):
                op = ast.Is()
            elif none and isinstance(op, ast.NotEq):
                op = ast.IsNot()
            ops.append(op)
        n.ops = ops
        return n


STMT_FIELDS = ("body", "orelse", "finalbody", "handlers")


def header_dump(n):
    """Dump a compound node with its statement lists emptied."""
    saved = {}
    for f in STMT_FIELDS:
        if hasattr(n, f):
            saved[f] = getattr(n, f)
            setattr(n, f, [])
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        saved["__doc"] = None
    try:
        return dump(n)
    finally:
        for f, v in saved.items():
            if f != "__doc":
                setattr(n, f, v)


def is_noexec(n):
    return (
        isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)
    ) or isinstance(n, (ast.Global, ast.Nonlocal))


def bound_names(n):
    """Names a class/module-level statement binds."""
    out = set()
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        out.add(n.name)
    elif isinstance(n, (ast.Import, ast.ImportFrom)):
        for a in n.names:
            out.add((a.asname or a.name).split(".")[0])
    else:
        for sub in ast.walk(n):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, (ast.Store, ast.Del)):
                out.add(sub.id)
            elif isinstance(sub, ast.Attribute) and isinstance(sub.ctx, (ast.Store, ast.Del)):
                out.add(sub.attr)
    return out


class _Blank(ast.NodeTransformer):
    def visit_Constant(self, n):
        return ast.Constant(value=None)

    def generic_visit(self, n):
        super().generic_visit(n)
        for f in ("op",):
            if hasattr(n, f) and isinstance(getattr(n, f), (ast.operator, ast.boolop, ast.unaryop)):
                setattr(n, f, ast.Add() if isinstance(getattr(n, f), ast.operator) else (ast.And() if isinstance(getattr(n, f), ast.boolop) else ast.Not()))
        if isinstance(n, ast.Compare):
            n.ops = [ast.Eq() for _ in n.ops]
        return n


def shape(n):
    import copy

    return dump(_Blank().visit(copy.deepcopy(n)))


def load_names(n):
    names, attrs = set(), set()
    for sub in ast.walk(n):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
            names.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            attrs.add(sub.attr)
    return names, attrs


def diff_sites(a, b, out):
    """Collect minimal old-side nodes where a and b differ."""
    if type(a) is not type(b):
        out.append(a)
        return
    if isinstance(a, ast.AST):
        for f in a._fields:
            va, vb = getattr(a, f, None), getattr(b, f, None)
            if isinstance(va, list) and isinstance(vb, list):
                if len(va) != len(vb):
                    out.append(a)
                    return
                for x, y in zip(va, vb):
                    if isinstance(x, ast.AST) or isinstance(y, ast.AST):
                        diff_sites(x, y, out)
                    elif x != y:
                        out.append(a)
                        return
            elif isinstance(va, ast.AST) or isinstance(vb, ast.AST):
                diff_sites(va, vb, out)
            elif va != vb or type(va) is not type(vb):
                out.append(a)
                return
    elif a != b:
        out.append(a)


def lambda_only_sites(a, b):
    """If every difference between a and b lies inside the body of a lambda
    (not its arguments), return those lambdas (old side); else None."""
    sites = []
    diff_sites(a, b, sites)
    if not sites:
        return None
    parents = {}
    for p in ast.walk(a):
        for c in ast.iter_child_nodes(p):
            parents[id(c)] = p
    lams = []
    for s in sites:
        n, child = s, None
        found = None
        while n is not None:
            if isinstance(n, ast.Lambda) and child is not None and child is n.body:
                found = n
                break
            child, n = n, parents.get(id(n))
        if found is None:
            return None
        lams.append(found)
    return lams


class Differ:
    """Collect change events between two module ASTs."""

    def __init__(self):
        self.events = []

    def run(self, old_mod, new_mod):
        self.body(old_mod.body, new_mod.body, ("module", None), None)

    def body(self, olds, news, ctx, parent):
        od = [dump(s) for s in olds]
        nd = [dump(s) for s in news]
        sm = SequenceMatcher(None, od, nd, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            o, n = list(olds[i1:i2]), list(news[j1:j2])
            pairs, lo, ln = self.pair(o, n)
            for a, b in pairs:
                self.pair_changed(a, b, ctx, parent)
            for a in lo:
                self.ev("del", ctx, a, None, parent, olds, i1, i2)
            for b in ln:
                self.ev("ins", ctx, None, b, parent, olds, i1, i2)

    @staticmethod
    def key(s):
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return ("def", s.name)
        return ("stmt", type(s).__name__)

    def pair(self, o, n):
        pairs = []
        used = set()
        lo = []
        # defs/classes pair by name, everything else in order by type
        for a in o:
            k = self.key(a)
            for j, b in enumerate(n):
                if j not in used and self.key(b) == k:
                    pairs.append((a, b))
                    used.add(j)
                    break
            else:
                lo.append(a)
        ln = [b for j, b in enumerate(n) if j not in used]
        if len(lo) == len(ln):  # same count left over: positional replace
            for a, b in zip(lo, ln):
                pairs.append((a, b))
            lo, ln = [], []
        return pairs, lo, ln

    def pair_changed(self, a, b, ctx, parent):
        if type(a) is type(b) and any(hasattr(a, f) for f in STMT_FIELDS):
            if header_dump(a) == header_dump(b):
                if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    sub = ("func", a)
                elif isinstance(a, ast.ClassDef):
                    sub = ("class", a if ctx[0] != "class" else a, ctx)
                else:
                    sub = ctx
                for f in STMT_FIELDS:
                    if hasattr(a, f):
                        if f == "handlers":
                            self.handlers(a.handlers, b.handlers, sub, a)
                        else:
                            self.body(getattr(a, f), getattr(b, f), sub, a)
                return
        self.ev("chg", ctx, a, b, parent, None, 0, 0)

    def handlers(self, ho, hn, ctx, parent):
        if len(ho) != len(hn):
            self.ev("chg", ctx, parent, parent, parent, None, 0, 0)
            return
        for a, b in zip(ho, hn):
            if dump(a) == dump(b):
                continue
            if header_dump(a) == header_dump(b):
                self.body(a.body, b.body, ctx, a)
            else:
                self.ev("chg", ctx, a, a, parent, None, 0, 0)

    def ev(self, kind, ctx, a, b, parent, olds, i1, i2):
        anchors = []
        if kind == "ins" and olds is not None:
            for k in range(i1 - 1, -1, -1):
                if not is_noexec(olds[k]):
                    anchors.append(olds[k])
                    break
            for k in range(i2, len(olds)):
                if not is_noexec(olds[k]):
                    anchors.append(olds[k])
                    break
        self.events.append((kind, ctx, a, b, parent, anchors))


# --------------------------------------------------------------------------
# selection


def modname_of(path):
    p = path[:-3] if path.endswith(".py") else path
    p = p.replace("/", ".")
    if p.endswith(".__init__"):
        p = p[: -len(".__init__")]
    return p


def qualname(cls_ctx):
    """ctx = ("class", node, parentctx) -> dotted qualname."""
    parts = []
    c = cls_ctx
    while c and c[0] == "class":
        parts.append(c[1].name)
        c = c[2]
    return ".".join(reversed(parts))


def def_firstlines(n):
    lines = {n.lineno}
    for d in getattr(n, "decorator_list", []):
        lines.add(d.lineno)
    return lines


class Selector:
    def __init__(self, m: Map):
        self.m = m
        self.builtins = set(dir(__builtins__)) if not isinstance(__builtins__, dict) else set(__builtins__)

    def read_src(self, rel):
        p = os.path.join(SRC, rel)
        try:
            with open(p, encoding="utf-8") as fh:
                return fh.read()
        except (OSError, UnicodeDecodeError):
            return None

    def select(self, patch_text):
        sel = set()
        why = []
        files = parse_patch(patch_text)
        if not files:
            if patch_text.strip():
                why.append("unparsed patch -> all")
                return set(self.m.all), why
            return sel, why
        for f in files:
            s, w = self.select_file(f)
            sel |= s
            why.append(w)
        return sel, why

    def data_file(self, rel, created_or_deleted):
        m = self.m
        out = set(m.s(m.opened.get(rel)))
        d = os.path.dirname(rel)
        if created_or_deleted:
            while d:
                out |= m.s(m.listed.get(d))
                d = os.path.dirname(d)
        return out

    def select_file(self, f):
        m = self.m
        old, new = f["old"], f["new"]
        rel = old or new
        if f.get("binary"):
            return self.data_file(rel, True) | (m.importers(rel) if rel.endswith(".py") else set()), f"{rel}: binary"
        if not rel.endswith(".py"):
            return self.non_py(f)
        if old is None:  # new python file
            out = self.data_file(new, True)
            return out | self.new_module(new), f"{new}: new module"
        if new is None or (old != new):
            out = m.importers(old) | self.data_file(old, True) | m.ref(os.path.basename(old)[:-3])
            if new:
                out |= self.data_file(new, True)
            return out, f"{old}: deleted/renamed module"
        old_text = self.read_src(old)
        if old_text is None:
            return set(m.all), f"{old}: source missing -> all"
        new_text = apply_hunks(old_text, f["hunks"])
        if new_text is None:
            return m.importers(old) | self.data_file(old, False) | self.whole_file(old), f"{old}: patch failed -> file"
        # tests that read this source as text (pytest's own reads of test modules
        # for assertion rewriting do not count: the AST check covers those)
        data_readers = set() if old.startswith("tests/") else set(m.s(m.opened.get(old)))
        if new_text == old_text:
            return data_readers, f"{old}: no-op"
        try:
            ot = ast.parse(old_text)
        except SyntaxError:
            return m.importers(old) | self.whole_file(old), f"{old}: old unparsable"
        try:
            nt = ast.parse(new_text)
        except (SyntaxError, ValueError):
            return m.importers(old) | data_readers, f"{old}: syntax error -> importers"
        NoneCmp().visit(ot)
        NoneCmp().visit(nt)
        if self.encoding_touched(f):
            return m.importers(old) | data_readers, f"{old}: encoding line"
        if dump(ot) == dump(nt):
            return data_readers, f"{old}: AST-equal"
        d = Differ()
        d.run(ot, nt)
        out = set(data_readers)
        notes = []
        for e in d.events:
            s, note = self.resolve(old, ot, e)
            out |= s
            notes.append(f"{note}:{len(s)}")
        return out, f"{old}: " + ",".join(notes)

    @staticmethod
    def encoding_touched(f):
        for h in f["hunks"]:
            if h["os"] <= 2:
                for tag, t in h["lines"]:
                    if tag != " " and re.match(r"^[ \t\f]*#.*?coding[:=]", t):
                        return True
        return False

    def whole_file(self, rel):
        out = set()
        for sid in (self.m.cov.get(rel) or {}).values():
            out |= self.m.sets[sid]
        return out

    def new_module(self, rel):
        # a new module only matters to code that imports or discovers it
        m = self.m
        name = modname_of(rel)
        out = m.ref(name.split(".")[-1])
        pkg_init = os.path.join(os.path.dirname(rel), "__init__.py").replace("\\", "/")
        out |= m.importers(pkg_init) if rel.startswith("tests/") is False and name.count(".") >= 1 and self.discovers(pkg_init) else set()
        return out

    def discovers(self, pkg_init):
        return True

    def non_py(self, f):
        m = self.m
        old, new = f["old"], f["new"]
        rel = old or new
        cd = old is None or new is None or old != new
        out = self.data_file(rel, cd)
        if new and new != rel:
            out |= self.data_file(new, cd)
        base = os.path.basename(rel)
        if base in ("pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini", "setup.py"):
            if old and new and old == new and self.config_equal(f):
                return set(), f"{rel}: config unchanged"
            return out, f"{rel}: config"
        if not out and rel.startswith("tests/"):
            out = self.mentions(rel)
            return out, f"{rel}: data(unseen open) {len(out)}"
        return out, f"{rel}: data {len(out)}"

    def config_equal(self, f):
        rel = f["old"]
        old_text = self.read_src(rel)
        if old_text is None:
            return False
        new_text = apply_hunks(old_text, f["hunks"])
        if new_text is None:
            return False
        try:
            if rel.endswith(".toml"):
                import tomllib

                return tomllib.loads(old_text) == tomllib.loads(new_text)
            if rel.endswith((".cfg", ".ini")):
                import configparser

                a, b = configparser.RawConfigParser(), configparser.RawConfigParser()
                a.read_string(old_text)
                b.read_string(new_text)
                return {s: dict(a[s]) for s in a} == {s: dict(b[s]) for s in b}
        except Exception:
            return False
        return False

    def mentions(self, rel):
        """Tests whose test file mentions the data file's name or its folder."""
        m = self.m
        base = os.path.basename(rel)
        stem = os.path.splitext(base)[0]
        folder = os.path.basename(os.path.dirname(rel))
        out = set()
        for tf, idxs in m.file_tests.items():
            txt = self.read_src(tf) or ""
            if base in txt or stem in txt or (folder and folder != "fixtures" and folder in txt):
                out |= idxs
        if not out:
            out = set(m.all)
        return out

    # ---- event resolution

    def resolve(self, rel, tree, e):
        kind, ctx, a, b, parent, anchors = e
        m = self.m
        is_test = rel.startswith("tests/")
        if kind == "chg" and a is not None and b is not None:
            lams = lambda_only_sites(a, b)
            if lams:
                # every difference sits inside lambda bodies: only callers can tell
                return m.entered(rel, {la.lineno for la in lams}), "lambda"
        if ctx[0] == "func":
            if kind == "ins":
                out = set()
                for an in anchors:
                    out |= m.lines(rel, an.lineno, an.end_lineno)
                    if not isinstance(an, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        pass
                if not out:
                    out = self.func_entry(rel, ctx[1], parent)
                return out, "f+"
            node = a
            out = m.lines(rel, node.lineno, node.end_lineno)
            if kind == "chg" and isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out |= m.entered(rel, def_firstlines(a))
            if not out and kind == "chg":
                # statement never ran: only tests reaching its parent could change
                out = set()
            return out, "f"
        # class or module level
        if ctx[0] == "class":
            return self.class_level(rel, tree, ctx, kind, a, b, is_test)
        return self.module_level(rel, tree, kind, a, b, is_test)

    def func_entry(self, rel, fn, parent):
        m = self.m
        out = m.entered(rel, def_firstlines(fn))
        if parent is not None and parent is not fn and hasattr(parent, "lineno"):
            out |= m.lines(rel, parent.lineno, parent.lineno)
        return out

    def safe_change(self, tree, a, b):
        """True when b differs from a only in literals/operators, or uses only
        names and attributes that already appear in the old module."""
        if a is None or b is None:
            return b is None or self.names_known(tree, b)
        if shape(a) == shape(b):
            return True
        return self.names_known(tree, b)

    def names_known(self, tree, b):
        names, attrs = load_names(b)
        if not hasattr(tree, "_br_known"):
            kn, ka = set(), set()
            for sub in ast.walk(tree):
                if isinstance(sub, ast.Name):
                    kn.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    ka.add(sub.attr)
                elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    kn.add(sub.name)
                elif isinstance(sub, ast.alias):
                    kn.add((sub.asname or sub.name).split(".")[0])
                elif isinstance(sub, ast.arg):
                    kn.add(sub.arg)
            tree._br_known = (kn, ka)
        kn, ka = tree._br_known
        return names <= (kn | self.builtins) and attrs <= ka

    def class_level(self, rel, tree, ctx, kind, a, b, is_test):
        m = self.m
        cls = ctx[1]
        qual = qualname(ctx)
        names = set()
        if a is not None:
            names |= bound_names(a)
        if b is not None:
            names |= bound_names(b)
        if is_test:
            return self.test_class_level(rel, cls, qual, kind, a, b, names), "tc"
        usage = m.usage(modname_of(rel), qual)
        imp = m.importers(rel)
        if usage is None:
            usage = imp  # never instantiated: anything importing it
        safe = self.safe_change(tree, a, b)
        removed = (bound_names(a) - bound_names(b)) if (a is not None and b is not None) else (bound_names(a) if a is not None else set())
        if not safe:
            return imp, "c!"
        out = set()
        for n in names:
            r = m.ref(n)
            if n in removed and m.class_attr_defs.get(n, 0) < 2:
                out |= r  # attribute gone everywhere: anything referencing the name
            else:
                out |= r & usage
        fn = a if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef)) else None
        if fn is not None:
            out |= m.lines(rel, fn.body[0].lineno, fn.end_lineno) | m.entered(rel, def_firstlines(fn))
            if kind != "chg" or (b is not None and getattr(b, "name", None) != fn.name):
                out |= usage  # method appeared/vanished/renamed: dynamic dispatch
        elif isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)) and a is None:
            out |= usage
        if isinstance(a, ast.ClassDef) or isinstance(b, ast.ClassDef):
            out |= usage
        return out, "c"

    def test_class_level(self, rel, cls, qual, kind, a, b, names):
        m = self.m
        out = set()
        prefix = f"{rel}::{qual.split('.')[0]}::"
        test_names = {n for n in names if n.startswith("test")}
        for i in m.file_tests.get(rel, ()):
            tid = m.test_ids[i]
            if tid.startswith(prefix):
                if test_names and kind != "chg" and not (names - test_names):
                    if tid.split("::")[-1].split("[")[0] in test_names:
                        out.add(i)
                else:
                    out.add(i)
        # subclasses elsewhere (e.g. a shared Validator base): tests that ran its methods
        for st in cls.body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)) and st.body:
                out |= m.lines(rel, st.body[0].lineno, st.end_lineno)
        if not test_names or names - test_names:
            out |= self.subclass_users(rel, cls)
        return out

    def subclass_users(self, rel, cls):
        # tests in other files whose classes inherit from cls (by name in their source)
        m = self.m
        out = set()
        pat = re.compile(r"class\s+\w+\s*\([^)]*\b" + re.escape(cls.name) + r"\b")
        for tf, idxs in m.file_tests.items():
            if tf == rel:
                continue
            txt = self.read_src(tf) or ""
            if cls.name in txt and pat.search(txt):
                out |= idxs
        return out

    def module_level(self, rel, tree, kind, a, b, is_test):
        m = self.m
        imp = m.importers(rel)
        node = a if a is not None else b
        if is_test:
            return imp | self.whole_file_tests(rel), "tm"
        if isinstance(node, (ast.Import, ast.ImportFrom)) or (a is not None and b is not None and type(a) is not type(b)):
            return imp, "m-import"
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = a
            out = set()
            names = (bound_names(a) if a is not None else set()) | (bound_names(b) if b is not None else set())
            if fn is not None:
                out |= m.lines(rel, fn.body[0].lineno, fn.end_lineno) | m.entered(rel, def_firstlines(fn))
            if not self.safe_change(tree, a, b):
                return imp, "m-def!"
            for n in names:
                out |= m.ref(n) & imp
            if kind != "chg" or (b is not None and a is not None and a.name != b.name):
                for n in names:
                    out |= m.ref(n)
            return out, "m-def"
        if isinstance(node, ast.ClassDef):
            # class header change / added / removed class
            names = (bound_names(a) if a is not None else set()) | (bound_names(b) if b is not None else set())
            out = set()
            for n in names:
                out |= m.ref(n)
            if a is not None:
                u = m.usage(modname_of(rel), a.name)
                out |= u if u is not None else imp
            if not self.safe_change(tree, a, b):
                out |= imp
            return out, "m-class"
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and self.safe_change(tree, a, b):
            names = (bound_names(a) if a is not None else set()) | (bound_names(b) if b is not None else set())
            out = set()
            for n in names:
                out |= m.ref(n) & imp
            if a is None or (b is not None and bound_names(a) - bound_names(b)) or b is None:
                for n in names:
                    out |= m.ref(n)
            # module-level code reading the name runs at import: covered by ref sets
            return out, "m-assign"
        return imp, "m-other"

    def whole_file_tests(self, rel):
        return set(self.m.file_tests.get(rel, ()))


# --------------------------------------------------------------------------
# packing


def pack(nums, dur):
    """Longest-first onto the least-loaded worker, for every worker count;
    keep the count with the smallest max load + 2 s per worker."""
    if not nums:
        return []
    order = sorted(nums, key=lambda n: (-dur[n], n))
    best = None
    for k in range(1, min(MAX_WORKERS, len(order)) + 1):
        loads = [0] * k
        bins = [[] for _ in range(k)]
        for n in order:
            j = loads.index(min(loads))
            bins[j].append(n)
            loads[j] += dur[n]
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, bins)
        if max(loads) == dur[order[0]]:
            break  # cannot get lower than the longest test
    return [b for b in best[1] if b]


# --------------------------------------------------------------------------


def main():
    with open(os.path.join(DATA, "tests.json"), encoding="utf-8") as fh:
        tj = json.load(fh)
    tests = tj["tests"]
    dur = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    num_by_id = {t["id"]: t["number"] for t in tests}
    try:
        m = Map(MAP)
    except Exception as ex:  # no map: run everything, never miss
        log("map load failed", ex)
        m = None
    if m is not None:
        idx_to_num = {i: num_by_id[tid] for i, tid in enumerate(m.test_ids) if tid in num_by_id}
        unknown = [t["number"] for t in tests if t["id"] not in set(m.test_ids)]
        sel = Selector(m)
    commits_dir = os.path.join(DATA, "commits")
    out_lines = []
    for name in sorted(os.listdir(commits_dir)):
        if not name.endswith(".patch"):
            continue
        cid = name[: -len(".patch")]
        try:
            with open(os.path.join(commits_dir, name), encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            if m is None:
                nums = set(dur)
                why = ["no map"]
            else:
                chosen, why = sel.select(text)
                nums = {idx_to_num[i] for i in chosen if i in idx_to_num}
                if nums or any("->" in w for w in why):
                    nums |= set(unknown)
        except Exception as ex:
            log(f"{cid}: error {ex!r} -> all")
            nums = set(dur)
            why = ["error"]
        workers = pack(nums, dur)
        log(f"{cid}: {len(nums)} tests | " + " ; ".join(why))
        out_lines.append(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")))
    sys.stdout.write("\n".join(out_lines) + ("\n" if out_lines else ""))


if __name__ == "__main__":
    main()
