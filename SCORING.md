# How a run is scored

These are the exact rules. `score.py` in this kit and the scorer on the server each follow them, and the two are compared on thousands of outputs before the event. For the same output, the simulated seconds you compute locally are the simulated seconds the server computes. The board's number also holds the CPU time your container used on the server, which `make sample` does not measure, and it is the middle of 3 runs.

## Inputs

`/data/tests.json` is `{"tests": [...]}`. Each entry is `{"number": 12, "id": "tests/test_parser.py::TestParser::test_union", "file": "tests/test_parser.py", "seconds": 0.031}`. Numbers run from 0 with no gaps, in the order listed. `seconds` has three decimals. A second key, `skipped`, names tests that our environment skips. They cannot fail and have no number.

`/data/commits/<id>.patch` is one unified diff against the pinned sqlglot commit. The commit's id is the file name without `.patch`.

The answers, which only the sample set ships, are `{"commits": {"<id>": {"failing": [12, 845], "kind": "code"}}}`. `failing` lists the numbers of the tests that fail on that commit.

## Reading your output

Your program's stdout is decoded as UTF-8 and split on `\n`. When stdout ends with a newline, the empty piece after it is ignored. Empty stdout has no lines. Every other piece is a line, numbered from 1. A byte order mark is not removed, so a line that starts with one is not JSON.

Each line is checked in this order, and the first rule a line breaks rejects the run. The message names the rule and the line number, never the line's content.

| Order | Rule | Code |
| --- | --- | --- |
| 1 | The line is standard JSON. `NaN` and `Infinity` are not. | `not_json` |
| 2 | It is an object with exactly two keys, `commit` and `workers`. `commit` is a string and `workers` is a list. | `wrong_shape` |
| 3 | `commit` is the id of a commit in `/data/commits`. | `unknown_commit` |
| 4 | No earlier line has the same `commit`. | `repeated_commit` |
| 5 | Every item of `workers` is a list, and every item of those lists is a whole number. `3.0` counts as 3. `true`, `"3"` and `3.5` do not. | `wrong_shape` |
| 6 | Every number is a test number in `tests.json`. | `unknown_test` |
| 7 | No number appears twice in the line, in one worker or across workers. | `repeated_test` |
| 8 | After empty workers are dropped, at most 8 workers remain. | `too_many_workers` |

After the last line: every commit in `/data/commits` has a line (`missing_commit`, reported without a line number). Stdout that is not UTF-8 is `not_utf8`.

Lines may come in any order. Tests may come in any order inside a worker. Empty workers are dropped before anything is counted, so `[[1, 2], []]` is one worker.

## Cost of one commit

Arithmetic is in whole milliseconds, so no rounding error can separate two teams. A test's duration is `round(seconds * 1000)` milliseconds.

- A worker's load is the sum of its tests' durations.
- With no workers the cost is 0.
- Otherwise the cost is the largest load, plus the worker cost for every worker.

The worker cost is 2 seconds.

## Misses

A commit is a miss when at least one of its failing tests is in none of its workers. Each miss adds 300 seconds. The commit's own cost still counts.

## A run's total

    simulated = sum of commit costs + 300 s x misses
    total     = simulated + CPU time

CPU time is what every process in your container used, user and system together, from start to exit, in microseconds. The build is not charged.

## A submission's score

Your image runs 3 times. All 3 must be valid. The score is the middle of the 3 totals, rounded to a tenth of a second with halves rounded up: `floor((microseconds + 50000) / 100000) / 10`.

Lower is better. Equal scores rank the earlier submission first.
