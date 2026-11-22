"""Ground truth for tuning: apply each patch to a copy of /src and run the given
tests (inside the labeling image). Reports which of them fail.

usage: python verify.py COMMITS_DIR RUN_JSON OUT_JSON [JOBS]
RUN_JSON = {"<cid>": ["tests/...::id", ...]}  (tests to run for that commit)
"""

import json
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, "/app")
from selector import apply_hunks, parse_patch  # noqa: E402

commits, run_json, out_json = sys.argv[1:4]
jobs = sys.argv[4] if len(sys.argv) > 4 else "20"
todo = json.load(open(run_json))
res = json.load(open(out_json)) if os.path.exists(out_json) else {}
W = "/tmp/w"


def _cap():
    import resource

    lim = 3 * 1024**3  # per process: a runaway test fails instead of starving the box
    resource.setrlimit(resource.RLIMIT_AS, (lim, lim))
FAIL = re.compile(r"^(?:SUB)?(?:FAILED|ERROR)\S*\s+(tests/\S+?)(?:\s+-\s|\s*$)")

for cid, ids in sorted(todo.items(), key=lambda kv: len(kv[1])):
    if cid in res:
        continue
    if os.path.exists(W):
        shutil.rmtree(W)
    shutil.copytree("/src", W, symlinks=True)
    text = open(os.path.join(commits, cid + ".patch"), encoding="utf-8").read()
    ok = True
    for f in parse_patch(text):
        old, new = f["old"], f["new"]
        if old and new and old == new:
            p = os.path.join(W, old)
            t = open(p, encoding="utf-8").read()
            nt = apply_hunks(t, f["hunks"])
            if nt is None:
                ok = False
                break
            open(p, "w", encoding="utf-8", newline="").write(nt)
        elif old is None and new:
            os.makedirs(os.path.dirname(os.path.join(W, new)), exist_ok=True)
            nt = apply_hunks("", f["hunks"])
            open(os.path.join(W, new), "w", encoding="utf-8").write(nt or "")
        elif new is None and old:
            os.remove(os.path.join(W, old))
    if not ok:
        res[cid] = {"error": "apply"}
        continue
    failing = set()
    if ids:
        open("/tmp/keep.txt", "w").write(chr(10).join(ids))
        env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONPATH=f"{W}:/app/tools", PYTHONDONTWRITEBYTECODE="1",
                   BR_KEEP="/tmp/keep.txt")
        # run whole files: many explicit node ids make pytest re-collect modules
        files = sorted({i.split("::")[0] for i in ids})
        p = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-n", jobs, "-p", "brfilter", "-rfE", "-o", "console_output_style=classic", "--no-header",
             "--timeout", "90", *files],
            cwd=W, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=1800,
        )
        txt = p.stdout.decode(errors="replace")
        idset = set(ids)
        for line in txt.splitlines():
            m = FAIL.match(line)
            if m:
                tid = m.group(1)
                if tid in idset:
                    failing.add(tid)
                else:  # file-level error (import failure): every selected test in that file
                    fpath = tid.split("::")[0]
                    failing |= {i for i in ids if i.split("::")[0] == fpath}
        if p.returncode not in (0, 1) and not failing:
            res[cid] = {"error": f"rc{p.returncode}", "tail": txt[-1500:]}
            json.dump(res, open(out_json, "w"))
            continue
    res[cid] = {"failing": sorted(failing)}
    print(cid, len(ids), "->", len(failing), file=sys.stderr, flush=True)
    json.dump(res, open(out_json, "w"))
