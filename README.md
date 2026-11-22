# Blast Radius entry (team zesty)

Selects the sqlglot tests a patch can break, and packs them onto workers.

- `map.pkl.z`: built once from the pinned commit `eb11aec` by running every test in its own
  process under a `sys.monitoring` line tracer (`tools/trace`). It holds per-test line coverage
  (import-time lines included, so it doubles as the import map), the files each test opened or
  listed, the sqlglot classes each test instantiated, and name-reference sets. No patch-keyed data.
- `selector.py`: applies each patch to the pinned source, diffs old/new ASTs statement by statement,
  and maps each change to tests: function-body statements -> tests that ran them; class-level
  attributes -> tests that reference the name and use the class; module-level/import/syntax errors
  -> every importer; data/fixture files -> tests that opened them; AST-identical edits -> only tests
  that read the source as text. Then LPT packing over 1..8 workers, minimizing max load + 2 s/worker.

Rebuild the map: see `tools/` (`Dockerfile.label`, `trace/run_all.py`, `build_map.py`).
