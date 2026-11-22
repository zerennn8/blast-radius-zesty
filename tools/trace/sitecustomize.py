"""Per-process line tracer used only to build the coverage map (never at run time).

Active when BR_OUT is set. Records every (file, line) executed under BR_SRC and
every file under BR_SRC that the process opens or lists, then writes one JSON
file per process into BR_OUT at exit. Child processes inherit the env, so
subprocess-based tests are traced too.
"""

import os

_OUT = os.environ.get("BR_OUT")
if _OUT:
    import atexit
    import builtins
    import io
    import json
    import sys

    _SRC = os.environ.get("BR_SRC", "/src").rstrip("/") + "/"
    _lines: dict = {}
    _opened: set = set()
    _listed: set = set()
    mon = sys.monitoring
    TOOL = 5
    mon.use_tool_id(TOOL, "br")
    DISABLE = mon.DISABLE

    def _on_line(code, line):
        f = code.co_filename
        if f.startswith(_SRC):
            s = _lines.get(f)
            if s is None:
                s = _lines[f] = set()
            s.add(line)
        return DISABLE

    def _on_start(code, offset):
        # Function entry: the "def" line itself is not a LINE event inside the body.
        f = code.co_filename
        if f.startswith(_SRC):
            s = _lines.get(f)
            if s is None:
                s = _lines[f] = set()
            s.add(-code.co_firstlineno)  # negative = code object entered
        return DISABLE

    mon.register_callback(TOOL, mon.events.LINE, _on_line)
    mon.register_callback(TOOL, mon.events.PY_START, _on_start)
    mon.set_events(TOOL, mon.events.LINE | mon.events.PY_START)

    def _norm(p):
        try:
            p = os.fspath(p)
            if isinstance(p, bytes):
                p = p.decode()
            if not isinstance(p, str):
                return None
            return os.path.realpath(p)
        except Exception:
            return None

    _orig_open = builtins.open

    def _open(file, *a, **k):
        p = _norm(file) if not isinstance(file, int) else None
        if p and p.startswith(_SRC):
            _opened.add(p)
        return _orig_open(file, *a, **k)

    builtins.open = _open
    io.open = _open

    _orig_listdir = os.listdir
    _orig_scandir = os.scandir

    def _listdir(path="."):
        p = _norm(path)
        if p and p.startswith(_SRC):
            _listed.add(p)
        return _orig_listdir(path)

    def _scandir(path="."):
        p = _norm(path)
        if p and p.startswith(_SRC):
            _listed.add(p)
        return _orig_scandir(path)

    os.listdir = _listdir
    os.scandir = _scandir

    # Record which sqlglot classes get instantiated: wrap every own __init__ of
    # every class defined in a sqlglot module, right after the module loads.
    _inst: set = set()
    if os.environ.get("BR_INST"):
        import functools
        import importlib.abc
        import importlib.machinery

        def _wrap_classes(obj, modname, seen):
            for v in list(vars(obj).values()):
                if isinstance(v, type) and v.__module__ == modname and v not in seen:
                    seen.add(v)
                    init = v.__dict__.get("__init__")
                    if callable(init) and not getattr(init, "_br", False):

                        def mk(orig):
                            @functools.wraps(orig)
                            def __init__(self, *a, **k):
                                _inst.add(type(self))
                                return orig(self, *a, **k)

                            __init__._br = True
                            return __init__

                        try:
                            v.__init__ = mk(init)
                        except Exception:
                            pass
                    _wrap_classes(v, modname, seen)

        class _Loader(importlib.abc.Loader):
            def __init__(self, inner):
                self.inner = inner

            def create_module(self, spec):
                return self.inner.create_module(spec)

            def exec_module(self, module):
                self.inner.exec_module(module)
                try:
                    _wrap_classes(module, module.__name__, set())
                except Exception:
                    pass

        class _Finder(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path, target=None):
                if not (name == "sqlglot" or name.startswith("sqlglot.")):
                    return None
                spec = importlib.machinery.PathFinder.find_spec(name, path)
                if spec is not None and spec.loader is not None and not isinstance(spec.loader, _Loader):
                    spec.loader = _Loader(spec.loader)
                return spec

        sys.meta_path.insert(0, _Finder())

    def _dump():
        try:
            mon.set_events(TOOL, 0)
        except Exception:
            pass
        insts = set()
        for c in _inst:
            for b in c.__mro__:
                m = getattr(b, "__module__", "") or ""
                if m == "sqlglot" or m.startswith("sqlglot."):
                    insts.add(f"{m}:{b.__qualname__}")
        data = {
            "argv": sys.argv,
            "lines": {f: sorted(s) for f, s in _lines.items()},
            "opened": sorted(_opened),
            "listed": sorted(_listed),
            "inst": sorted(insts),
        }
        path = os.path.join(_OUT, f"p{os.getpid()}.json")
        with _orig_open(path, "w") as fh:
            json.dump(data, fh)

    atexit.register(_dump)
