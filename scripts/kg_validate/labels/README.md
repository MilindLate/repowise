# Precision labels

Human-confirmed ground truth, one directory per matrix repo name
(`labels/<repo>/`). Labels are made against the repo's pinned SHA in
`matrix.toml`; record it in each file so a SHA bump shows which labels
need re-checking. Never invent labels: an absent file means "not labelled".

## `entry_points.json` (read by `oracles/entry_points.py`)

The reviewer's confirmed entry points (P@5 is scored against this set when
it exists; recall stays against the manifest oracle).

```json
{
  "sha": "<pinned 40-char sha>",
  "reviewer": "<who confirmed it>",
  "entry_points": ["src/cli.ts", {"path": "pkg/__main__.py", "note": "python -m pkg"}]
}
```

Entries are repo-relative POSIX paths, as plain strings or `{path, note}`.
A bare JSON list of paths is also accepted.

## `identity.jsonl` (read by `oracles/identity.py`)

One alias set per line: emails that belong to the same person. Emails not
listed stay as git/mailmap clusters them. Emails are case-insensitive.

```json
{"person": "Jane Doe", "emails": ["jane@example.com", "12345+jdoe@users.noreply.github.com"], "evidence": "same GitHub login in PR history"}
```

## Finding labels (`dead_code.jsonl`, `health.jsonl`, `perf.jsonl`)

One verdict (or queue) row per finding, keyed by the hosted finding id. The
format, labels, reason codes, sampling and agreement rules are in
`../LABELING.md`; `../labels.py` loads, samples and checks them, and
`run.py --precision --families dead_code,health,perf` scores them.

`score.py --labels` also accepts the generic form
`{"family": ..., "finding_key": ..., "label": "tp"}` for ad-hoc findings files.
