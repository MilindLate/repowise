# Labelling protocol: dead code, health, perf

These three families have no complete mechanical oracle, so their precision
is measured on a labelled sample. `run.py --precision --families
dead_code,health,perf` scores every finding the index emits against
`labels/<repo>/<family>.jsonl` (plus, for dead code, the mention oracle) and
reports the rest as unlabelled. Labels are made against the repo's pinned SHA
in `matrix.toml`. Never invent a label: an absent row means "not labelled".

## Files and rows

One JSON object per line in `labels/<repo>/dead_code.jsonl`,
`health.jsonl` or `perf.jsonl`:

```json
{"finding_key": "b648646d6177bef8926d0a74", "sha": "<pinned sha>", "label": "FP",
 "reason": "framework_loaded", "labeler": "jdoe", "date": "2026-10-02",
 "tier": "review", "kind": "unreachable_file", "file_path": ".pnpmfile.cjs", "note": "pnpm hook"}
```

- `finding_key` is the hosted public identity of the finding
  (`labels.finding_key`, the same recipe as hosted
  `modal_app/indexer/finding_identity.py`): sha1 of
  `file_path::kind::symbol::line_start::line_end`, 24 hex chars. Health and
  perf findings use `biomarker_type`, `function_name` and the line span.
  Dead-code findings key without a span (`::0::0`), because the hosted
  dead-code artifact carries none. The same key is what the dashboard's
  triage endpoints use, so a label and a user's "false positive" click name
  the same finding. Moving a finding (path, symbol or span) makes a new key;
  labels for keys no longer emitted are reported as `stale_labels`.
  Some keys name more than one finding: a `hidden_coupling` key has no
  partner in it, an `error_handling` key no sub-kind, a dead-code key no
  span. A label covers every finding with its key, so label such a key only
  with a verdict that holds for all of them (else `unsure`); the queue lists
  each key once.
- `sha`, `label`, `reason`, `labeler`, `date` are the verdict. Every other
  field is context copied in by the queue so the file reads on its own.
- A **queue row** has `label: null`. Label it in place, or append a new row
  with the same key: for each labeller the last row wins.

## Labels

| Family | Labels | Scored as correct |
|---|---|---|
| dead_code | `TP` (really unused) / `FP` / `unsure` | `TP` |
| health | `TP` (accurate and fairly described) / `FP` / `unsure` | `TP` |
| perf | `true_n_plus_1` / `io_in_loop_inherent` / `actionable` / `FP` / `unsure` | `perf`: `true_n_plus_1` or `actionable`; `perf_n_plus_one`: `true_n_plus_1` |

`unsure` is recorded but never scored. Perf labels:

- `true_n_plus_1`: one round-trip per item where a batched form exists
  (`WHERE id IN`, bulk fetch, `select_related`, a batch API).
- `actionable`: not an N+1, but worth changing (serial awaits that could
  fan out, repeated work that could be hoisted or cached).
- `io_in_loop_inherent`: I/O in a loop is the point (write N output files,
  a retry or polling loop, following redirects, walking up directories, a
  queue worker).
- `FP`: there is no I/O in a loop at that location, or the boundary is
  misread badly enough that the finding is wrong.

`perf_n_plus_one` scores only findings whose text says "N+1".

## Reason codes

Dead code (required on every verdict):

| Code | Meaning |
|---|---|
| `truly_dead` | Nothing uses it; deleting it changes nothing (the TP code). |
| `used_in_file` | Used in its own file (a call, a reference, passed as a value). |
| `used_elsewhere` | Imported or referenced from another source file (the analyzer missed the edge, e.g. an unresolved path alias). |
| `framework_loaded` | Loaded by convention or by name at runtime: plugin dirs, route/page files, `conftest.py`, Sphinx `conf.py`, dynamic `import()`/`__import__`. |
| `build_input` | Read by a build or codegen step, or listed in a build manifest. |
| `public_api` | Exported for users of the package (in `__all__`, a package entry, the docs' API). |
| `decorator_registered` | Registered by a decorator (`@app.route`, `@nox.session`, `@x.handle`). |
| `test_only` | Used only by tests (a helper tests import). The label is still FP. |
| `config_file` | A config file some tool reads by name (`tsdown.config.ts`, `.pnpmfile.cjs`). |

Health: `accurate` (TP), `wrong_location`, `wrong_number` (a count or
percentage that is false), `wrong_text`, `not_a_smell` (true numbers but not
a problem worth showing: an import block called a clone, a data literal).
Perf takes a free-text `note` instead of a code.

## The mention oracle (dead code)

`oracles/dead_code_mentions.py` scans every tracked file for the finding's
name outside its own definition (symbols: the identifier; files: path-shaped
tokens, never the bare stem). A finding with **zero** mentions is auto-labelled
`TP` with source `oracle` and needs no human row. Any mention puts the
finding in the queue: a mention is a reason to look, not proof of use.

The oracle is itself a claim. The queue also samples up to 10 auto-TPs per
cell (rows carrying `"oracle": "no_mentions"`); label them like any other
row. `labels.py summary` reports how many of those checks agreed. Runtime
loading by convention (Sphinx `conf.py`, `cmd_<name>.py` plugins) produces
zero mentions and is exactly what those checks catch.

## Sampling

```bash
python scripts/kg_validate/labels.py queue /path/to/indexed/clone --repo click --family dead_code
```

- Cells: dead code (kind x tier: `safe_to_delete` / `high` / `review` /
  `low`), health and perf (biomarker type).
- Up to **40** findings per cell (all of them when a cell is smaller), with a
  fixed seed, skipping keys already in the file. Re-running only adds.
- A gated cell (one with a floor in `thresholds.toml`) judged below that
  target prints as **LABEL DEBT** in `run.py --precision`: its precision is
  unmeasured, not good.

## Who labels

- A **human** labeller writes their own name in `labeler`. The seed labels
  from the 2026-09 audit carry `audit-2026-09` and count as human.
- **Claude may pre-label** with `labeler: "claude-suggested"` after reading
  the code. Suggestions never count by default: `run.py --precision
  --include-suggested` adds them to the tp/fp counts and prints
  `UNCONFIRMED`. A human confirms a suggestion by appending a row with their
  own name (agree or not). Baselines never include suggestions.
- Precedence per finding: a human label, then (if opted in) a suggestion,
  then the oracle's auto-TP. A suggestion outranks the oracle because it was
  made by reading the code.

## Agreement

A second labeller independently labels at least **20%** of each repo's
human-labelled findings (append rows with their own name; do not look at
the first label). Check it with:

```bash
python scripts/kg_validate/labels.py kappa --repo click --family dead_code
```

It prints the double-labelled share and Cohen's kappa and fails below
kappa 0.7 or 20% coverage. Below 0.7 the protocol is ambiguous: discuss the
disagreements, sharpen the reason-code definitions above, then relabel.
