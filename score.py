#!/usr/bin/env python3
"""Local scorer for the blast radius track. The rules are in SCORING.md.

Teams:    python3 score.py <data_dir> <answers.json> <output_file> [--cpu-seconds 1.5]
Harness:  python3 score.py --batch < request.json
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import sys
from pathlib import Path
from typing import Any

WORKER_COST_MILLISECONDS = 2_000
MISS_PENALTY_MILLISECONDS = 300_000
MAXIMUM_WORKERS = 8

MICROSECONDS_PER_MILLISECOND = 1_000
MICROSECONDS_PER_SECOND = 1_000_000

# CPython refuses to convert a longer digit string to int (its default
# sys.get_int_max_str_digits() limit), yet such a literal is still standard JSON.
LONGEST_INTEGER_LITERAL_PARSED_EXACTLY = 4300


class _NotJson(Exception):
    pass


def _reject_constant(constant_name: str) -> Any:
    raise _NotJson(constant_name)


def _parse_integer_literal(literal: str) -> int | float:
    if len(literal) > LONGEST_INTEGER_LITERAL_PARSED_EXACTLY:
        return float(literal)
    return int(literal)


def _parse_json_line(line: str) -> Any:
    try:
        return json.loads(
            line,
            parse_constant=_reject_constant,
            parse_int=_parse_integer_literal,
        )
    except (ValueError, RecursionError) as error:
        raise _NotJson(str(error)) from error


def _whole_number(value: Any) -> int | None:
    # bool is a subclass of int in Python, and SCORING.md says `true` is not a number.
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _duration_milliseconds(seconds: float) -> int:
    # Rounds halves up like JavaScript's Math.round, so the server's scorer and
    # this one agree even on a duration that does not have three decimals.
    return math.floor(seconds * 1000 + 0.5)


def _split_lines(text: str) -> list[str]:
    if text == "":
        return []
    pieces = text.split("\n")
    if text.endswith("\n"):
        pieces.pop()
    return pieces


def _rejection(code: str, line_number: int | None) -> dict:
    return {"ok": False, "code": code, "line": line_number}


def _read_workers(line_value: Any) -> list[list[int]] | None:
    workers: list[list[int]] = []
    for worker_value in line_value["workers"]:
        if not isinstance(worker_value, list):
            return None
        worker: list[int] = []
        for item in worker_value:
            test_number = _whole_number(item)
            if test_number is None:
                return None
            worker.append(test_number)
        workers.append(worker)
    return workers


def _has_line_shape(line_value: Any) -> bool:
    return (
        isinstance(line_value, dict)
        and set(line_value.keys()) == {"commit", "workers"}
        and isinstance(line_value["commit"], str)
        and isinstance(line_value["workers"], list)
    )


def evaluate_run(stdout: bytes, tests: list[dict], answers: dict) -> dict:
    """Like score_run, but a valid run names its missed commits instead of counting them."""
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError:
        return _rejection("not_utf8", None)

    duration_by_test_number = {
        test["number"]: _duration_milliseconds(test["seconds"]) for test in tests
    }
    seen_commits: set[str] = set()
    missed_commits: list[str] = []
    simulated_milliseconds = 0

    for line_number, line in enumerate(_split_lines(text), start=1):
        try:
            line_value = _parse_json_line(line)
        except _NotJson:
            return _rejection("not_json", line_number)

        if not _has_line_shape(line_value):
            return _rejection("wrong_shape", line_number)

        commit = line_value["commit"]
        if commit not in answers:
            return _rejection("unknown_commit", line_number)
        if commit in seen_commits:
            return _rejection("repeated_commit", line_number)
        seen_commits.add(commit)

        workers = _read_workers(line_value)
        if workers is None:
            return _rejection("wrong_shape", line_number)

        selected_test_numbers = [
            test_number for worker in workers for test_number in worker
        ]
        if any(
            test_number not in duration_by_test_number
            for test_number in selected_test_numbers
        ):
            return _rejection("unknown_test", line_number)

        distinct_test_numbers = set(selected_test_numbers)
        if len(distinct_test_numbers) != len(selected_test_numbers):
            return _rejection("repeated_test", line_number)

        occupied_workers = [worker for worker in workers if worker]
        if len(occupied_workers) > MAXIMUM_WORKERS:
            return _rejection("too_many_workers", line_number)

        if occupied_workers:
            largest_load = max(
                sum(duration_by_test_number[test_number] for test_number in worker)
                for worker in occupied_workers
            )
            simulated_milliseconds += (
                largest_load + WORKER_COST_MILLISECONDS * len(occupied_workers)
            )

        if any(
            failing_test_number not in distinct_test_numbers
            for failing_test_number in answers[commit]["failing"]
        ):
            missed_commits.append(commit)

    if any(commit not in seen_commits for commit in answers):
        return _rejection("missing_commit", None)

    simulated_milliseconds += MISS_PENALTY_MILLISECONDS * len(missed_commits)
    return {
        "ok": True,
        "simulated_milliseconds": simulated_milliseconds,
        "missed_commits": sorted(missed_commits),
    }


def score_run(stdout: bytes, tests: list[dict], answers: dict) -> dict:
    evaluation = evaluate_run(stdout, tests, answers)
    if not evaluation["ok"]:
        return evaluation
    return {
        "ok": True,
        "simulated_milliseconds": evaluation["simulated_milliseconds"],
        "misses": len(evaluation["missed_commits"]),
    }


def run_total_microseconds(simulated_milliseconds: int, cpu_microseconds: int) -> int:
    return simulated_milliseconds * MICROSECONDS_PER_MILLISECOND + cpu_microseconds


def _format_milliseconds(milliseconds: int) -> str:
    return f"{milliseconds // 1000}.{milliseconds % 1000:03d}"


def _format_microseconds(microseconds: int) -> str:
    whole_seconds = microseconds // MICROSECONDS_PER_SECOND
    remaining_microseconds = microseconds % MICROSECONDS_PER_SECOND
    return f"{whole_seconds}.{remaining_microseconds:06d}"


def _run_batch() -> int:
    request = json.load(sys.stdin)
    results = [
        score_run(base64.b64decode(encoded_output), request["tests"], request["answers"])
        for encoded_output in request["outputs"]
    ]
    json.dump(results, sys.stdout)
    sys.stdout.write("\n")
    return 0


def _run_report(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Score one run of your program against the sample set."
    )
    parser.add_argument("data_dir", type=Path, help="holds tests.json and commits/*.patch")
    parser.add_argument("answers", type=Path, help="answers.json of the sample set")
    parser.add_argument("output_file", type=Path, help="your program's saved stdout")
    parser.add_argument(
        "--cpu-seconds",
        type=float,
        help="CPU time your container used, user and system together, if you measured it",
    )
    options = parser.parse_args(arguments)

    tests = json.loads((options.data_dir / "tests.json").read_text("utf-8"))["tests"]
    answers = json.loads(options.answers.read_text("utf-8"))["commits"]

    # SCORING.md defines the known commits by the patch files, while score_run
    # takes them from the answers, so the report is only right when they agree.
    patch_commits = {
        patch_path.name.removesuffix(".patch")
        for patch_path in (options.data_dir / "commits").glob("*.patch")
    }
    if patch_commits != set(answers):
        print(
            f"{options.answers} does not cover the same commits as "
            f"{options.data_dir / 'commits'}: "
            f"{len(patch_commits - set(answers))} patches have no answer and "
            f"{len(set(answers) - patch_commits)} answers have no patch.",
            file=sys.stderr,
        )
        return 2

    evaluation = evaluate_run(options.output_file.read_bytes(), tests, answers)
    if not evaluation["ok"]:
        if evaluation["line"] is None:
            print(f"Rejected: {evaluation['code']}")
        else:
            print(f"Rejected: {evaluation['code']} on line {evaluation['line']}")
        return 1

    simulated_milliseconds = evaluation["simulated_milliseconds"]
    missed_commits = evaluation["missed_commits"]

    print(f"Simulated: {_format_milliseconds(simulated_milliseconds)} s")
    print(
        f"Misses:    {len(missed_commits)} "
        f"({_format_milliseconds(MISS_PENALTY_MILLISECONDS * len(missed_commits))} s "
        "of the simulated time)"
    )
    for commit in missed_commits:
        print(f"  missed {commit}")
    if options.cpu_seconds is None:
        print(
            "CPU:       not measured here. The simulated seconds above are exact. The board adds "
            "the CPU seconds your container used and takes the middle of 3 runs."
        )
        return 0
    cpu_microseconds = round(options.cpu_seconds * MICROSECONDS_PER_SECOND)
    total_microseconds = run_total_microseconds(simulated_milliseconds, cpu_microseconds)
    print(f"CPU:       {_format_microseconds(cpu_microseconds)} s")
    print(f"Total:     {_format_microseconds(total_microseconds)} s")
    return 0


def main(arguments: list[str]) -> int:
    if arguments == ["--batch"]:
        return _run_batch()
    return _run_report(arguments)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
