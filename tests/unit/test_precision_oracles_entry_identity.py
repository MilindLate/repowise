"""Unit tests for the entry-point and identity oracles (scripts/kg_validate/oracles)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "kg_validate"))

from oracles import entry_points, identity  # noqa: E402


def _write(root: Path, rel: str, text: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# --- entry points ------------------------------------------------------------


def test_package_json_bin_main_exports_with_dist_to_src(tmp_path):
    _write(
        tmp_path,
        "packages/cli/package.json",
        json.dumps({"bin": {"tool": "./dist/cli.js"}, "main": "lib/index.js"}),
    )
    _write(tmp_path, "packages/cli/src/cli.ts")  # dist/ not in the checkout
    _write(tmp_path, "packages/cli/lib/index.js")  # committed, used as-is
    _write(
        tmp_path,
        "packages/sdk/package.json",
        json.dumps(
            {
                "exports": {
                    ".": {"types": "./dist/index.d.ts", "import": "./dist/index.mjs"},
                    "./extra": "./dist/extra.js",
                }
            }
        ),
    )
    _write(tmp_path, "packages/sdk/src/index.ts")
    _write(tmp_path, "packages/sdk/src/extra.ts")
    _write(tmp_path, "packages/gone/package.json", json.dumps({"bin": "./dist/missing.js"}))
    got = entry_points.manifest_entry_points(tmp_path)
    assert got == {
        "packages/cli/src/cli.ts": "package.json bin",
        "packages/cli/lib/index.js": "package.json main",
        "packages/sdk/src/index.ts": "package.json exports",
    }


def test_pyproject_project_and_poetry_scripts(tmp_path):
    _write(
        tmp_path,
        "pyproject.toml",
        '[project]\nname = "a"\n[project.scripts]\na = "a.cli:main"\nb = "a.tools:run"\n'
        '[tool.poetry.scripts]\nc = { reference = "a.other:go", type = "console" }\n',
    )
    _write(tmp_path, "src/a/__init__.py")
    _write(tmp_path, "src/a/cli.py")
    _write(tmp_path, "src/a/tools/__init__.py")
    _write(tmp_path, "src/a/other.py")
    got = entry_points.manifest_entry_points(tmp_path)
    assert set(got) == {"src/a/cli.py", "src/a/tools/__init__.py", "src/a/other.py"}


def test_pyproject_setuptools_package_dirs(tmp_path):
    _write(
        tmp_path,
        "pyproject.toml",
        '[project.scripts]\nx = "ns.cli.main:run"\ny = "ns.core.tool:run"\n'
        '[tool.setuptools.package-dir]\n"ns.cli" = "packages/cli/src/ns/cli"\n'
        '[tool.setuptools.packages.find]\nwhere = ["packages/core/src"]\n',
    )
    _write(tmp_path, "packages/cli/src/ns/cli/main.py")
    _write(tmp_path, "packages/core/src/ns/core/tool.py")
    # Declared, but under a test tree: not a product entry point.
    _write(tmp_path, "tests/fixtures/app/package.json", json.dumps({"main": "index.js"}))
    _write(tmp_path, "tests/fixtures/app/index.js")
    assert set(entry_points.manifest_entry_points(tmp_path)) == {
        "packages/cli/src/ns/cli/main.py",
        "packages/core/src/ns/core/tool.py",
    }


def test_go_package_main_skips_tests_and_testdata(tmp_path):
    _write(tmp_path, "cmd/app/main.go", "// doc\npackage main\n\nfunc main() {}\n")
    _write(tmp_path, "cmd/app/util.go", "package main\n\nfunc helper() {}\n")
    _write(tmp_path, "cmd/app/main_test.go", "package main\n\nfunc main() {}\n")
    _write(tmp_path, "lib/lib.go", "package lib\n\nfunc main() {}\n")
    _write(tmp_path, "internal/testdata/x/main.go", "package main\n\nfunc main() {}\n")
    assert entry_points.manifest_entry_points(tmp_path) == {"cmd/app/main.go": "go package main"}


def test_jvm_main_and_spring_boot(tmp_path):
    _write(
        tmp_path,
        "src/main/java/a/App.java",
        "@SpringBootApplication\npublic class App { public static void main(String[] a) {} }\n",
    )
    _write(
        tmp_path,
        "src/main/java/a/Tool.java",
        "class Tool { public static void main(String... a) {} }",
    )
    _write(tmp_path, "src/main/kotlin/a/Main.kt", "package a\n\nfun main(args: Array<String>) {}\n")
    _write(tmp_path, "src/main/java/a/Lib.java", "class Lib { void main() {} }")
    _write(tmp_path, "src/test/java/a/T.java", "class T { public static void main(String[] a) {} }")
    assert entry_points.manifest_entry_points(tmp_path) == {
        "src/main/java/a/App.java": "spring boot application",
        "src/main/java/a/Tool.java": "jvm main",
        "src/main/kotlin/a/Main.kt": "jvm main",
    }


def test_nextjs_app_and_pages_routes(tmp_path):
    _write(tmp_path, "web/package.json", json.dumps({"dependencies": {"next": "15.0.0"}}))
    _write(tmp_path, "web/app/page.tsx")
    _write(tmp_path, "web/app/blog/[slug]/page.tsx")
    _write(tmp_path, "web/app/api/hook/route.ts")
    _write(tmp_path, "web/app/blog/layout.tsx")
    _write(tmp_path, "web/app/_components/page.tsx")  # private folder
    _write(tmp_path, "web/src/pages/index.tsx")
    _write(tmp_path, "web/src/pages/api/user.ts")
    _write(tmp_path, "web/src/pages/index.test.tsx")
    # Not a Next.js package: app/ is just a folder.
    _write(tmp_path, "other/package.json", json.dumps({"name": "other"}))
    _write(tmp_path, "other/app/page.tsx")
    got = entry_points.manifest_entry_points(tmp_path)
    assert set(got) == {
        "web/app/page.tsx",
        "web/app/blog/[slug]/page.tsx",
        "web/app/api/hook/route.ts",
        "web/src/pages/index.tsx",
        "web/src/pages/api/user.ts",
    }


def test_pruned_dirs_ignored(tmp_path):
    _write(tmp_path, "node_modules/dep/package.json", json.dumps({"bin": "cli.js"}))
    _write(tmp_path, "node_modules/dep/cli.js")
    assert entry_points.manifest_entry_points(tmp_path) == {}


def test_gold_set_loader_formats(tmp_path):
    assert entry_points.load_gold("r", tmp_path) is None
    _write(
        tmp_path,
        "r/entry_points.json",
        json.dumps({"sha": "x", "entry_points": ["a.ts", {"path": "b.py", "note": "n"}]}),
    )
    assert entry_points.load_gold("r", tmp_path) == ["a.ts", "b.py"]
    _write(tmp_path, "s/entry_points.json", json.dumps(["c.go"]))
    assert entry_points.load_gold("s", tmp_path) == ["c.go"]


def test_entry_point_score_p_at_5_and_recall():
    manifest = {"a", "b", "c", "d"}
    ranked = ["a", "x", "b", "y", "c", "d"]
    m = entry_points.score(ranked, manifest)
    assert m["p_at_5"] == pytest.approx(3 / 5)
    assert m["recall"] == pytest.approx(1.0)
    # Gold set replaces the manifest for P@5 only.
    m = entry_points.score(ranked, manifest, gold={"a", "x"})
    assert m["p_at_5"] == pytest.approx(2 / 5)
    assert m["recall"] == pytest.approx(1.0)
    # Short list: divide by its length; recall catches the omission.
    m = entry_points.score(["a"], manifest)
    assert m == {"p_at_5": 1.0, "recall": 0.25}
    assert entry_points.score([], set()) == {"p_at_5": None, "recall": None}


# --- identity ----------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@t",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def mailmap_repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    for author in (
        "Alice <alice@corp.com>",
        "alice <12345+alice@users.noreply.github.com>",
        "Al <al@laptop.local>",
        "Bob <bob@corp.com>",
        "Bob Builder <BOB@corp.com>",
        "Carol <carol@x.org>",
    ):
        _git(repo, "commit", "-q", "--allow-empty", "-m", "c", f"--author={author}")
    (repo / ".mailmap").write_text(
        "Alice <alice@corp.com> <12345+alice@users.noreply.github.com>\n"
        "Alice <alice@corp.com> <al@laptop.local>\n"
    )
    return repo


def test_truth_clusters_follow_mailmap(mailmap_repo):
    truth = identity.truth_clusters(mailmap_repo)
    assert sorted(sorted(c) for c in truth) == [
        ["12345+alice@users.noreply.github.com", "al@laptop.local", "alice@corp.com"],
        ["bob@corp.com"],  # email case folds
        ["carol@x.org"],
    ]


def test_hidden_mailmap_exposes_raw_identities_and_restores(mailmap_repo):
    with identity.hidden_mailmap(mailmap_repo):
        assert not (mailmap_repo / ".mailmap").exists()
        raw = identity.truth_clusters(mailmap_repo)
        assert len(raw) == 5  # every email its own person without the mailmap
    assert (mailmap_repo / ".mailmap").is_file()
    assert len(identity.truth_clusters(mailmap_repo)) == 3


def test_alias_labels_union_into_truth(tmp_path, mailmap_repo):
    _write(
        tmp_path,
        "labels/r/identity.jsonl",
        json.dumps({"person": "Carol", "emails": ["Carol@x.org", "bob@corp.com"]})
        + "\n"
        + json.dumps({"emails": ["carol@x.org", "never-committed@x.org"]})
        + "\n",
    )
    aliases = identity.load_alias_sets("r", tmp_path / "labels")
    assert identity.load_alias_sets("missing", tmp_path / "labels") == []
    truth = identity.truth_clusters(mailmap_repo, aliases)
    assert {"bob@corp.com", "carol@x.org"} in truth
    assert all("never-committed@x.org" not in c for c in truth)


def test_b_cubed_and_person_count(mailmap_repo):
    truth = identity.truth_clusters(mailmap_repo)
    # Perfect prediction.
    perfect = [sorted(c) for c in truth]
    m = identity.score(perfect, truth)
    assert (m["b3_precision"], m["b3_recall"], m["person_count_error"]) == (1.0, 1.0, 0.0)
    # Tool misses the .local alias (split) and wrongly merges Bob with Carol.
    pred = [
        ["alice@corp.com", "12345+alice@users.noreply.github.com"],
        ["BOB@corp.com", "carol@x.org"],
        ["someone-outside@x.org"],  # outside the truth universe: ignored
    ]
    m = identity.score(pred, truth)
    # Items: alice, noreply, local, bob, carol.
    # precision: 1, 1, 1(local singleton), 1/2, 1/2 -> 4/5
    # recall:    2/3, 2/3, 1/3, 1, 1 -> (5/3 + 2)/5
    assert m["b3_precision"] == pytest.approx(4 / 5)
    assert m["b3_recall"] == pytest.approx((5 / 3 + 2) / 5)
    assert m["predicted_people"] == 3 and m["true_people"] == 3
    assert m["person_count_error"] == 0.0
    # All singletons: precision perfect, count off by 2/3.
    m = identity.score([], truth)
    assert m["b3_precision"] == 1.0
    assert m["person_count_error"] == pytest.approx(2 / 3)


def test_oracles_never_import_repowise():
    # Oracles grade repowise; importing it would grade it against itself.
    for path in (REPO_ROOT / "scripts/kg_validate/oracles").glob("*.py"):
        assert not re.search(r"^\s*(import|from)\s+repowise\b", path.read_text(), re.M), path
