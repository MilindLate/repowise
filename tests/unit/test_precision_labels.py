"""Unit tests for the labelled precision families: the dead-code mention oracle
(scripts/kg_validate/oracles/dead_code_mentions.py), finding keys, label
resolution, Cohen's kappa and label debt (scripts/kg_validate/labels.py,
precision.py). Local git repos and sqlite only; no network, no indexing."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
KG_VALIDATE = REPO_ROOT / "scripts" / "kg_validate"
sys.path.insert(0, str(KG_VALIDATE))

import labels  # noqa: E402
import precision  # noqa: E402
import score  # noqa: E402
from oracles.dead_code_mentions import MentionIndex, file_tokens  # noqa: E402

# --- mention oracle ----------------------------------------------------------


def _git_repo(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    return root


@pytest.fixture
def repo(tmp_path: Path) -> MentionIndex:
    return MentionIndex(
        _git_repo(
            tmp_path,
            {
                "src/pkg/mod.py": "def helper_fn():\n    return 1\n\n\ndef lonely():\n    pass\n",
                "src/pkg/used.py": "X = 1\n\n\ndef caller():\n    return X + X\n",
                "src/pkg/utils.py": "def util():\n    pass\n",
                "src/pkg/plugins/__init__.py": "",
                "scripts/build.js": "console.log('build')\n",
                "scripts/gen.ts": "export const unused = 1\n",
                "tools/unmentioned.py": "print('hi')\n",
                "examples/demo/demo.py": "def cli():\n    pass\n",
                "examples/demo/pyproject.toml": '[project.scripts]\ndemo = "demo:cli"\n',
                "examples/demo/settings.yaml": "demo: true\n",
                "package.json": json.dumps({"scripts": {"build": "node scripts/build.js"}}),
                "config.yaml": "handler: helper_fn\n",
                "README.md": "Run `python -m pkg.plugins` and see utils in the docs.\n",
                "notes.md": "The gen step lives in ./gen and the utils live elsewhere.\n",
                "bin.dat": "\0binary helper_fn",
                "vendorpkg/lib.py": "V = 1\n",
                "Makefile": "all:\n\tpython vendorpkg/lib.py\n",
            },
        )
    )


def test_symbol_mention_in_yaml_counts_and_definition_does_not(repo):
    hits = repo.mentions(
        {
            "kind": "unused_export",
            "file_path": "src/pkg/mod.py",
            "symbol_name": "helper_fn",
            "start_line": 1,
            "end_line": 2,
        }
    )
    assert hits == [("config.yaml", 1)]  # the binary file is skipped


def test_symbol_without_mentions_is_silent(repo):
    finding = {
        "kind": "unused_export",
        "file_path": "src/pkg/mod.py",
        "symbol_name": "lonely",
        "start_line": 5,
        "end_line": 6,
    }
    assert repo.mentions(finding) == []


def test_use_in_own_file_is_a_mention(repo):
    # Declaration on line 1 (no span given: first occurrence), uses on line 5.
    hits = repo.mentions(
        {"kind": "unused_internal", "file_path": "src/pkg/used.py", "symbol_name": "X"}
    )
    assert hits == [("src/pkg/used.py", 5)]


def test_file_mentioned_by_package_json_script(repo):
    assert repo.mentions({"kind": "unreachable_file", "file_path": "scripts/build.js"}) == [
        ("package.json", 1)
    ]


def test_python_package_mentioned_by_dotted_module_in_markdown(repo):
    hits = repo.mentions({"kind": "unreachable_file", "file_path": "src/pkg/plugins/__init__.py"})
    assert ("README.md", 1) in hits


def test_extensionless_specifier_mentions_a_file(repo):
    assert repo.mentions({"kind": "unreachable_file", "file_path": "scripts/gen.ts"}) == [
        ("notes.md", 1)
    ]


def test_bare_stem_never_matches_a_file(repo):
    # README.md and notes.md say "utils" as a bare word: not a mention.
    assert repo.mentions({"kind": "unreachable_file", "file_path": "src/pkg/utils.py"}) == []
    assert "utils" not in file_tokens("src/pkg/utils.py")
    assert "gen" not in file_tokens("scripts/gen")


def test_module_attr_reference_mentions_a_python_file(repo):
    # ``demo:cli`` in pyproject names the module; ``demo: true`` in YAML does not.
    hits = repo.mentions({"kind": "unreachable_file", "file_path": "examples/demo/demo.py"})
    assert hits == [("examples/demo/pyproject.toml", 2)]


def test_unmentioned_file_is_silent_and_self_mentions_do_not_count(repo):
    assert repo.mentions({"kind": "unreachable_file", "file_path": "tools/unmentioned.py"}) == []


def test_zombie_package_matches_its_directory_path(repo):
    hits = repo.mentions({"kind": "zombie_package", "file_path": "vendorpkg"})
    assert hits == [("Makefile", 2)]


def test_file_tokens_are_path_shaped():
    toks = file_tokens("src/flask/json/tag.py")
    assert {"tag.py", "json/tag.py", "json/tag", "/tag", "flask.json.tag", "json.tag"} <= set(toks)
    assert "tag" not in toks and "json" not in toks
    assert "flask.json" in file_tokens("src/flask/json/__init__.py")


# --- finding identity --------------------------------------------------------

# Ids minted by the hosted producer (modal_app/indexer/finding_identity.py and
# the dashboard's with_dead_code_id) for real findings, copied from a hosted
# snapshot's API responses. The harness must reproduce them exactly.
HOSTED_DEAD_CODE = [
    (
        {"kind": "unreachable_file", "file_path": ".pnpmfile.cjs", "symbol_name": None},
        "b648646d6177bef8926d0a74",
    ),
    (
        {
            "kind": "unused_export",
            "file_path": "docs/lib/kb/source-document.ts",
            "symbol_name": "extractGuideBody",
            "start_line": 129,
            "end_line": 140,
        },
        "d5ea1a7d2ca49590d2049bfb",
    ),
]
HOSTED_HEALTH = [
    (
        {
            "biomarker_type": "complex_method",
            "file_path": "docs/lib/source.ts",
            "function_name": "mdxToCleanMarkdown",
            "line_start": 355,
            "line_end": 627,
        },
        "da43f47f451dff68e9dc9fed",
    ),
    (
        {
            "biomarker_type": "serial_await_in_loop",
            "file_path": "docs/scripts/validate-links.ts",
            "function_name": "getFiles",
            "line_start": 291,
            "line_end": 291,
        },
        "c5b1e56486eef1fd623989b4",
    ),
]


@pytest.mark.parametrize(("row", "expected"), HOSTED_DEAD_CODE)
def test_dead_code_key_matches_hosted(row, expected):
    # Hosted keys dead code without a span even when the engine has one.
    assert labels.key_for("dead_code", row) == expected


@pytest.mark.parametrize(("row", "expected"), HOSTED_HEALTH)
def test_health_key_matches_hosted(row, expected):
    assert labels.key_for("health", row) == expected
    assert labels.key_for("perf", row) == expected


def test_finding_key_moves_with_span():
    a = labels.finding_key("a.py", "complex_method", "f", 1, 9)
    assert a != labels.finding_key("a.py", "complex_method", "f", 2, 9)
    assert len(a) == 24


# --- label files and verdicts ------------------------------------------------


def _row(key, label, labeler, **extra):
    return {"finding_key": key, "label": label, "labeler": labeler, **extra}


def test_resolve_human_beats_suggested_and_suggestions_are_opt_in():
    rows = [
        _row("k1", "FP", labels.SUGGESTED),
        _row("k1", "TP", "alice"),
        _row("k2", "TP", labels.SUGGESTED),
        _row("k3", None, None),  # queue row
        _row("k4", "unsure", "bob"),
        _row("k4", "TP", labels.SUGGESTED),
    ]
    assert {k: r["label"] for k, r in labels.resolve(rows).items()} == {
        "k1": "TP",
        "k4": "unsure",
    }
    assert {k: r["label"] for k, r in labels.resolve(rows, include_suggested=True).items()} == {
        "k1": "TP",
        "k2": "TP",
        "k4": "unsure",
    }


def test_verdict_views():
    assert labels.verdict("dead_code", "TP") is True
    assert labels.verdict("health", "FP") is False
    assert labels.verdict("health", "unsure") is None
    assert labels.verdict("perf", "actionable") is True
    assert labels.verdict("perf", "io_in_loop_inherent") is False
    assert labels.verdict("perf_n_plus_one", "actionable") is False
    assert labels.verdict("perf_n_plus_one", "true_n_plus_1") is True


def test_count_cells_precedence_label_then_oracle():
    findings = [{"key": k, "tier": "high"} for k in ("a", "b", "c", "d", "e")]
    rows = [
        _row("a", "FP", "alice"),  # a human FP beats the oracle's TP
        _row("c", "FP", labels.SUGGESTED),  # an opted-in suggestion does too
        _row("d", "TP", labels.SUGGESTED),
    ]
    oracle = {"a", "b", "c"}
    cells = labels.count_cells(findings, labels.resolve(rows, True), "dead_code", oracle)
    high = cells["high"]
    assert (high["findings"], high["tp"], high["fp"]) == (5, 2, 2)
    assert high["by_source"] == {
        "alice": {"tp": 0, "fp": 1},
        "oracle": {"tp": 1, "fp": 0},
        labels.SUGGESTED: {"tp": 1, "fp": 1},
    }
    # Without suggestions the oracle's auto-TP stands for "c"; "d" is unlabelled.
    high = labels.count_cells(findings, labels.resolve(rows), "dead_code", oracle)["high"]
    assert (high["tp"], high["fp"], high["by_source"]["oracle"]["tp"]) == (2, 1, 2)


def test_validate_flags_unknown_labels_and_reasons():
    rows = [
        _row("a", "TP", "alice", reason="truly_dead"),
        _row("b", "maybe", "alice"),
        _row("c", "FP", "alice", reason="vibes"),
        _row("d", None, None),
    ]
    problems = labels.validate(rows, "dead_code")
    assert len(problems) == 2 and "maybe" in problems[0] and "vibes" in problems[1]
    assert labels.validate([_row("p", "true_n_plus_1", "alice")], "perf") == []


@pytest.mark.parametrize("family", labels.FAMILIES)
def test_committed_label_files_are_valid(family):
    for path in sorted(labels.LABELS_DIR.glob(f"*/{family}.jsonl")):
        rows = labels.load_labels(path.parent.name, family)
        assert labels.validate(rows, family) == [], path
        for r in rows:
            assert len(r["finding_key"]) == 24 and len(r["sha"]) == 40, (path, r)
            if r.get("label"):
                assert r.get("labeler") and r.get("date"), (path, r)


# --- kappa -------------------------------------------------------------------


def test_cohen_kappa_known_values():
    # po = 0.75, pe = (2*1 + 2*3) / 16 = 0.5 -> kappa = 0.5
    assert labels.cohen_kappa(["TP", "TP", "FP", "FP"], ["TP", "FP", "FP", "FP"]) == 0.5
    assert labels.cohen_kappa(["TP", "FP"], ["TP", "FP"]) == 1.0
    assert labels.cohen_kappa(["TP", "TP"], ["TP", "TP"]) == 1.0
    assert labels.cohen_kappa(["TP", "FP"], ["FP", "TP"]) == -1.0
    assert labels.cohen_kappa([], []) is None
    with pytest.raises(ValueError):
        labels.cohen_kappa(["TP"], [])


def test_double_labelled_pairs_by_key_ignoring_suggestions():
    rows = [
        _row("a", "TP", "alice"),
        _row("a", "FP", "bob"),
        _row("b", "TP", "alice"),
        _row("b", "TP", labels.SUGGESTED),
        _row("c", "FP", "alice"),
        _row("c", "FP", "bob"),
    ]
    a, b, labelled = labels.double_labelled(rows)
    assert (a, b, labelled) == (["TP", "FP"], ["FP", "FP"], 3)


# --- scoring and label debt --------------------------------------------------


def _labelled_result(repo="r", split="dev"):
    return {
        "repo": repo,
        "split": split,
        "families": {
            "dead_code": {
                "tiers": {
                    "safe_to_delete": {"findings": 10, "tp": 6, "fp": 2, "by_source": {}},
                    "low": {"findings": 5, "tp": 0, "fp": 0, "by_source": {}},
                }
            },
            "perf": {
                "tiers": {"io_in_loop": {"findings": 3, "tp": 1, "fp": 2, "by_source": {}}},
                "n_plus_one": {"findings": 3, "tp": 0, "fp": 3, "by_source": {}},
            },
        },
    }


def test_family_metrics_for_labelled_families():
    m = precision.family_metrics(_labelled_result())
    assert m["dead_code.safe_to_delete"]["precision"] == 0.75
    assert m["dead_code"]["precision"] == 0.75
    assert m["dead_code.low"]["precision"] is None
    assert m["perf.io_in_loop"]["precision"] == pytest.approx(1 / 3)
    assert m["perf_n_plus_one"]["precision"] == 0.0


def test_evaluate_results_scores_only_judged_findings():
    thresholds = {
        "family": {
            "dead_code.safe_to_delete": {"precision": 0.7, "min_n": 60},
            "perf_n_plus_one": {"precision": 0.8},
        }
    }
    checks = {
        (c.threshold, c.metric): c
        for c in precision.evaluate_results([_labelled_result()], thresholds)
    }
    assert checks[("dead_code.safe_to_delete", "precision")].status == "pass"
    assert checks[("dead_code.safe_to_delete", "judged_n")].value == 8
    assert checks[("dead_code.safe_to_delete", "judged_n")].status == "fail"
    assert checks[("perf_n_plus_one", "precision")].status == "fail"


def test_label_debt_warns_only_for_gated_cells():
    thresholds = score.load_thresholds()
    debt = precision.label_debt([_labelled_result()], thresholds)
    cells = {d["cell"]: d for d in debt}
    # 10 findings, 8 judged: every finding of a cell under 40 must be judged.
    assert cells["dead_code.safe_to_delete"]["missing"] == 2
    assert "dead_code.low" not in cells  # no floor on the hidden tier
    assert "perf.io_in_loop" not in cells  # no perf floor per type
    assert "perf_n_plus_one" not in cells  # fully judged
    assert "LABEL DEBT" in precision.render_debt(debt)
    assert labels.debt({"findings": 500, "tp": 30, "fp": 5}) == 5


def test_heldout_aggregate_sums_labelled_counts():
    agg = precision.aggregate([_labelled_result("a", "heldout"), _labelled_result("b", "heldout")])
    assert agg["families"]["dead_code"]["tiers"]["safe_to_delete"] == {
        "findings": 20,
        "tp": 12,
        "fp": 4,
    }
    assert agg["families"]["perf"]["n_plus_one"]["fp"] == 6


# --- findings from an index --------------------------------------------------


def test_index_findings_tiers_and_keys(tmp_path):
    db_path = tmp_path / "wiki.db"
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE dead_code_findings (kind, file_path, symbol_name, symbol_kind, "
            "confidence, safe_to_delete, start_line, end_line, reason)"
        )
        db.executemany(
            "INSERT INTO dead_code_findings VALUES (?,?,?,?,?,?,?,?,?)",
            [
                ("unreachable_file", "src/old.py", None, None, 1.0, 1, None, None, "r"),
                ("unreachable_file", "src/settings.py", None, None, 0.9, 1, None, None, "r"),
                ("unused_export", "src/a.py", "f", "function", 0.5, 0, 3, 9, "r"),
                ("unused_internal", "src/a.py", "_g", "function", 0.3, 0, 11, 12, "r"),
            ],
        )
        db.execute(
            "CREATE TABLE health_findings (biomarker_type, file_path, function_name, "
            "line_start, line_end, severity, dimension, reason, details_json)"
        )
        db.executemany(
            "INSERT INTO health_findings VALUES (?,?,?,?,?,?,?,?,?)",
            [
                ("complex_method", "src/a.py", "f", 3, 9, "high", None, "ccn 12", "{}"),
                ("io_in_loop", "src/a.py", "f", 5, 5, "medium", "performance", "x (N+1)", "{}"),
            ],
        )
    dead = {
        f["file_path"] + str(f["symbol_name"]): f
        for f in labels.index_findings(db_path, "dead_code")
    }
    assert dead["src/old.pyNone"]["tier"] == "safe_to_delete"
    assert dead["src/settings.pyNone"]["tier"] == "high"  # a config-shaped path is capped
    assert dead["src/a.pyf"]["tier"] == "review"
    assert dead["src/a.py_g"]["tier"] == "low"
    assert dead["src/a.pyf"]["key"] == labels.finding_key("src/a.py", "unused_export", "f", 0, 0)
    health = labels.index_findings(db_path, "health")
    perf = labels.index_findings(db_path, "perf")
    assert [f["tier"] for f in health] == ["complex_method"]
    assert [f["tier"] for f in perf] == ["io_in_loop"]
    assert perf[0]["key"] == labels.finding_key("src/a.py", "io_in_loop", "f", 5, 5)
