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

## Finding labels (`<family>.jsonl`, read by `score.py --labels`)

One verdict per finding, keyed by the finding's `key`:

```json
{"family": "dead_code", "finding_key": "src/old.py", "label": "tp", "note": "no references, not a plugin"}
```

`label` is `tp`/`fp` (also `true`/`false`, `correct`/`wrong`, `yes`/`no`);
`unsure` is ignored. `repo` is implied by the directory when scored with
`--repo <repo>`.

## Suggested labels (`<family>.suggested.jsonl`, not read by any scorer)

Machine- or agent-proposed verdicts waiting for a person. Same line shape as
finding labels plus `"status": "suggested"`. A reviewer confirms by copying a
line into `<family>.jsonl` (dropping `status`, adding `"reviewer"`); nothing
scores a `.suggested` file, so it can never pass for ground truth.

### `decision_reverts.suggested.jsonl` (card D15)

One line per commit that revert-based supersession treats as reverted at
HEAD, i.e. a decision resting only on it would flip to `superseded`:

```json
{"family": "decision_reverts", "finding_key": "<target sha>", "revert": "<revert sha>", "rule": "body|subject|pr",
 "target_subject": "...", "revert_subject": "...", "head": "<sha scanned>",
 "label": "tp", "status": "suggested", "note": "revert diff is the exact inverse of the target (patch-id match)"}
```

`tp` means the matched commit really undoes the target and the change was
not re-landed later; `fp` means either is wrong. `head` is the SHA scanned
(the pinned SHA for matrix repos, the clone's HEAD otherwise).
