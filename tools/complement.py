"""usage: python complement.py TESTS_JSON SELECTION_JSONL OUT_RUN_JSON
For each commit, list the test ids the selector did NOT pick (to check for misses)."""
import json, sys
tests = json.load(open(sys.argv[1]))["tests"]
run = {}
for line in open(sys.argv[2]):
    d = json.loads(line)
    picked = {n for w in d["workers"] for n in w}
    run[d["commit"]] = [t["id"] for t in tests if t["number"] not in picked]
json.dump(run, open(sys.argv[3], "w"))
print(sum(len(v) for v in run.values()), "tests to verify over", len(run), "commits", file=sys.stderr)
