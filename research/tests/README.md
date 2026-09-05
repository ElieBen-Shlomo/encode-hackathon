# Tests

Fast, deterministic checks of the pipeline: no model calls, no credits, no golden file opened by anything but
the evaluator. About 90 seconds.

```sh
cd research && uv run --with pytest pytest tests -q
```

| file | what it pins |
|---|---|
| `test_scorer.py` | grader semantics (`values_equal`: numeric strings, 2-dp rounding, bool vs number, empty vs zero, dates round to the nearest day), oracle on 10 tasks, init-as-prediction fails, missing prediction fails, summary maths, LibreOffice recalc round-trip |
| `test_positions.py` | `answer_position` parsing: 4-range, whole-column `A:G`, Arabic/Chinese sheet names, non-breaking-space prefix, exact graded cell counts, all 400 expand |
| `test_digest.py` | the prompt view renders every edge workbook and announces true dimensions; windowing; values-only; `verification_snapshot` types and missing-sheet flag |
| `test_verify_tools.py` | `diff_workbooks` (changed vs unexpected, type-aware, added sheets), `formula_cells`, `format_verification`, `inspect_range`, `assert_blank`, `assert_sorted`, playbook keyword selection |
| `test_harness_mock.py` | `agent/run.py` null and agent modes end to end with the mock: one prediction line and output per task, trace contract, edit → review → finish protocol, resume without duplicates, action parsing, trace truncation, model failure and invalid-action paths |
| `test_sandbox.py` | `run_python` / `run_bash`: success, env paths, timeout (kills the whole process group, backgrounded grandchildren included), non-zero exit, deleted output, input workbook untouched, API keys stripped |
| `test_models.py` | mock protocol; Tinker adapter with a fake sampler and renderer (text parts only, token counts, params passed through) |
| `test_baseline.py` | `parse_answer` (think tags, fences), `write_output` types, `build_prompt`, `load_env`; `tinker_predict` default renderer, ladder order, config loading |
| `test_golden_hygiene.py` | static scan (nothing under `agent/` or `research/baseline` references golden paths outside an allowlist) and a runtime trap on every workbook open during a mock agent run |
| `test_tinker_parsing.py` | Tinker adapter with a fake sampler/renderer: thinking kept for the trace, a truncated reply is never returned as the answer, per-effort renderers and default caps, the prebuilt constructor `agent_predict.py` uses, retries (typed transient classification, backoff, budget, per-attempt deadline), the step-down ladder on truncation |
| `test_renderers.py` | all nine `--digest` views render every edge workbook; answer-range visibility in the render meta; grid shows formulas → values, array formulas, defined names and types; non-ASCII sheet names; budget trimming keeps header and answer rows; compact collapses homogeneous runs; layouts share grid content |
| `test_analysis.py` | failure attribution buckets, paired bootstrap, reading-probe generator and grader |
| `test_config_defaults.py` | the shipped defaults are the study's pick: `SolveConfig`, `run.py` flags, `qwen.yaml`, the Docker image installs the `tinker` extra, local-limit API (None keeps, 0 is an error) |
| `test_local_concurrency.py` | per-kind thread pools bound what actually runs, cheap reads never queue behind sandbox waits, finished event loops are released, the render cache survives concurrent eviction |
| `test_teacher_sft.py` | `teacher/build_sft.py` replays a verified teacher script into a trajectory rendered with the inference view (`SolveConfig()`, grid) and the harness protocol; reference workbooks stay closed (`needs_recalc` stubbed) |

## Expected failures (documented gaps, `xfail(strict=True)`)

On this branch the sandbox and truncation gaps are fixed and asserted as plain tests; the remaining expected failures are:

These describe behaviour the code does not have yet. They are reported as `x` and do not fail the run; the day
one is fixed it turns into an unexpected pass and the marker must be removed.

- `test_verify_tools.py`, `test_digest.py`: `answer_sheet` is null for all 275 cell-level tasks, so
  `formula_cells` never finds graded formulas there (auto-recalculation never runs), unqualified
  `expected_changes` are always reported as unexpected, and the verification snapshot says `SHEET MISSING`.
  The grader resolves a null sheet to the active sheet; these helpers should too.
