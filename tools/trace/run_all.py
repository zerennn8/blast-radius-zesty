"""Run every test in its own process under the tracer (inside the labeling image).

usage: python run_all.py TESTS_JSON OUT_DIR [JOBS] [ONLY_NUMBERS_COMMA]
Writes OUT_DIR/<number>/p<pid>.json per traced process and OUT_DIR/<number>/rc.
"""

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

tests = json.load(open(sys.argv[1]))["tests"]
out = sys.argv[2]
jobs = int(sys.argv[3]) if len(sys.argv) > 3 else os.cpu_count()
if len(sys.argv) > 4:
    keep = {int(x) for x in sys.argv[4].split(",")}
    tests = [t for t in tests if t["number"] in keep]

TRACE_DIR = os.path.dirname(os.path.abspath(__file__))


def run(t):
    d = os.path.join(out, str(t["number"]))
    if os.path.exists(os.path.join(d, "rc")):
        return 0
    os.makedirs(d, exist_ok=True)
    for f in os.listdir(d):
        os.remove(os.path.join(d, f))
    env = dict(os.environ)
    env.update(
        BR_OUT=d,
        BR_SRC="/src",
        PYTHONPATH=f"{TRACE_DIR}:/src",
        SKIP_INTEGRATION="0",
        PYTHONDONTWRITEBYTECODE="1",
    )
    t0 = time.time()
    p = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x", t["id"]],
        cwd="/src",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=900,
    )
    with open(os.path.join(d, "log"), "wb") as fh:
        fh.write(p.stdout[-4000:])
    with open(os.path.join(d, "rc"), "w") as fh:
        fh.write(f"{p.returncode} {time.time() - t0:.2f}")
    return p.returncode


t0 = time.time()
with ThreadPoolExecutor(jobs) as ex:
    rcs = list(ex.map(run, sorted(tests, key=lambda t: -t["seconds"])))
bad = [r for r in rcs if r != 0]
print(f"done {len(rcs)} tests in {time.time() - t0:.0f}s, nonzero rc: {len(bad)}", file=sys.stderr)
