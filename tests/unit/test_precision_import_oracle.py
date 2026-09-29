"""Unit tests for the import-edge oracle (scripts/kg_validate/oracles/imports.py).

The TS cases need node and the `typescript` npm package (``npm ci`` at the repo
root, or REPOWISE_ORACLE_TS pointing at a typescript package dir); the Go case
needs the go toolchain.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "kg_validate" / "oracles"))

import imports as oracle  # noqa: E402


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def _edges(root: Path, lang: str) -> set[tuple[str, str]]:
    return oracle.oracle_edges(root, (lang,))["edges"]


# --------------------------------------------------------------------- Python

PY_REPO = {
    "pyproject.toml": '[project]\nname = "mypkg"\n',
    "src/mypkg/__init__.py": "VERSION = '1'\n",
    "src/mypkg/core.py": (
        "import logging\n"  # stdlib, although src/mypkg/logging.py exists
        "from types import SimpleNamespace\n"  # stdlib, although mypkg/types.py exists
        "from pydantic import BaseModel\n"  # third-party, although utils/pydantic.py exists
        "import requests\n"
        "import plugin_x.hooks\n"  # a second project root
        "from mypkg import helpers, VERSION\n"
        "import mypkg.core\n"  # itself: no self-edge
    ),
    "src/mypkg/helpers.py": "",
    "src/mypkg/logging.py": "",
    "src/mypkg/types.py": "",
    "src/mypkg/utils/__init__.py": "",
    "src/mypkg/utils/pydantic.py": "",
    "src/mypkg/sub/__init__.py": "",
    "src/mypkg/sub/a.py": (
        "from ..core import thing\n"
        "from . import b\n"
        "from .b import name\n"
        "from .. import helpers\n"
        "from .missing import nope\n"
    ),
    "src/mypkg/sub/b.py": "",
    "plugins/plugin_x/pyproject.toml": '[project]\nname = "plugin-x"\n',
    "plugins/plugin_x/plugin_x/__init__.py": "",
    "plugins/plugin_x/plugin_x/hooks.py": "",
    "tests/test_core.py": "import mypkg.core\nfrom mypkg.sub import a\nimport helper_mod\n",
    "tests/helper_mod.py": "",
    "scripts/run.py": "import json\n",  # a script's own dir shadows stdlib
    "scripts/json.py": "",
}


def test_python_src_layout_relative_and_multi_root(tmp_path):
    edges = _edges(_write(tmp_path, PY_REPO), "python")
    core = "src/mypkg/core.py"
    assert {d for s, d in edges if s == core} == {
        "plugins/plugin_x/plugin_x/hooks.py",
        "src/mypkg/helpers.py",
        "src/mypkg/__init__.py",
    }
    assert {d for s, d in edges if s == "src/mypkg/sub/a.py"} == {
        "src/mypkg/core.py",
        "src/mypkg/sub/b.py",
        "src/mypkg/helpers.py",
    }
    assert {d for s, d in edges if s == "tests/test_core.py"} == {
        "src/mypkg/core.py",
        "src/mypkg/sub/a.py",
        "tests/helper_mod.py",  # tests/ is not a package: script-dir semantics
    }


def test_python_stdlib_and_third_party_names_do_not_shadow(tmp_path):
    edges = _edges(_write(tmp_path, PY_REPO), "python")
    targets = {d for _, d in edges}
    assert "src/mypkg/logging.py" not in targets
    assert "src/mypkg/types.py" not in targets
    assert "src/mypkg/utils/pydantic.py" not in targets
    assert all(s != d for s, d in edges)
    # ...but a script's own directory is sys.path[0] and does shadow.
    assert ("scripts/run.py", "scripts/json.py") in edges


def test_python_unparseable_file_is_out_of_scope(tmp_path):
    root = _write(tmp_path, {"a.py": "import b\n", "b.py": "def f(:\n"})
    out = oracle.oracle_edges(root, ("python",))
    assert out["sources"] == {"a.py"}
    # A graph edge from the unparseable file is neither a TP nor an FP.
    result = oracle.compare(out, {("a.py", "b.py"), ("b.py", "a.py")}, {"a.py", "b.py"})
    assert (result["python"]["tp"], result["python"]["fp"]) == (1, 0)


def test_python_regular_package_owns_its_name(tmp_path):
    # `pkg` is a regular package at the first root; find_spec never falls
    # through to another root's pkg/extra.py.
    _write(
        tmp_path,
        {
            "a/pyproject.toml": "",
            "a/pkg/__init__.py": "",
            "b/pyproject.toml": "",
            "b/pkg/__init__.py": "",
            "b/pkg/extra.py": "",
            "a/pkg/user.py": "import pkg.extra\n",
        },
    )
    assert _edges(tmp_path, "python") == set()


# ------------------------------------------------------------------------- TS


def _typescript_dir() -> str | None:
    if os.environ.get("REPOWISE_ORACLE_TS"):
        return os.environ["REPOWISE_ORACLE_TS"]
    if shutil.which("node") is None:
        return None
    res = subprocess.run(
        ["node", "-p", "require.resolve('typescript/package.json')"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return str(Path(res.stdout.strip()).parent) if res.returncode == 0 else None


TS_DIR = _typescript_dir()
needs_ts = pytest.mark.skipif(
    TS_DIR is None or shutil.which("node") is None,
    reason="node + typescript package unavailable (npm ci, or set REPOWISE_ORACLE_TS)",
)

TS_REPO = {
    "package.json": json.dumps({"name": "root", "private": True}),
    "pnpm-workspace.yaml": "packages:\n  - 'packages/*'\n\ncatalog:\n  zod: ^4\n",
    "tsconfig.base.json": json.dumps(
        {"compilerOptions": {"baseUrl": ".", "paths": {"@app/*": ["packages/app/src/*"]}}}
    ),
    "packages/app/package.json": json.dumps({"name": "app"}),
    "packages/app/tsconfig.json": json.dumps(
        {
            "extends": "../../tsconfig.base.json",
            "compilerOptions": {"module": "esnext", "moduleResolution": "bundler"},
        }
    ),
    "packages/app/src/main.ts": (
        "import { a } from '@app/util';\n"
        "import { b } from './local.js';\n"
        "export * as ns from './ns';\n"
        "import lib from '@scope/lib';\n"
        "import { sub } from '@scope/lib/sub';\n"
        "import React from 'react';\n"
        "const lazy = () => import('./lazy');\n"
        "type T = import('./types').T;\n"
    ),
    "packages/app/src/util.ts": "export const a = 1;\n",
    "packages/app/src/local.ts": "export const b = 1;\n",
    "packages/app/src/ns.ts": "export const n = 1;\n",
    "packages/app/src/lazy.ts": "export {};\n",
    "packages/app/src/types.ts": "export type T = 1;\n",
    "packages/lib/package.json": json.dumps(
        {
            "name": "@scope/lib",
            "exports": {
                ".": {"types": "./dist/index.d.ts", "default": "./dist/index.js"},
                "./sub": {"types": "./dist/sub.d.ts", "default": "./dist/sub.js"},
            },
            "imports": {"#conf": {"node": "./dist/conf.node.js", "default": "./dist/conf.js"}},
        }
    ),
    "packages/lib/tsconfig.json": json.dumps(
        {
            "compilerOptions": {
                "outDir": "./dist",
                "rootDir": "./src",
                "module": "nodenext",
                "moduleResolution": "nodenext",
            },
        }
    ),
    "packages/lib/src/index.ts": "import { c } from '#conf';\nexport default c;\n",
    "packages/lib/src/sub.ts": "export const sub = 1;\n",
    "packages/lib/src/conf.node.ts": "export const c = 1;\n",
}


@needs_ts
def test_ts_tsconfig_paths_extends_and_relative(tmp_path):
    edges = oracle.oracle_edges(_write(tmp_path, TS_REPO), ("typescript",), TS_DIR)["edges"]
    main = "packages/app/src/main.ts"
    assert {d for s, d in edges if s == main} == {
        "packages/app/src/util.ts",  # tsconfig paths via extends
        "packages/app/src/local.ts",  # ./local.js → local.ts
        "packages/app/src/ns.ts",  # export * as ns
        "packages/app/src/lazy.ts",  # dynamic import()
        "packages/app/src/types.ts",  # import('./types').T
        "packages/lib/src/index.ts",  # workspace package, dist → src
        "packages/lib/src/sub.ts",  # workspace subpath export
    }


@needs_ts
def test_ts_package_imports_field(tmp_path):
    edges = oracle.oracle_edges(_write(tmp_path, TS_REPO), ("typescript",), TS_DIR)["edges"]
    assert ("packages/lib/src/index.ts", "packages/lib/src/conf.node.ts") in edges


# ------------------------------------------------------------------------- Go


@pytest.mark.skipif(shutil.which("go") is None, reason="go toolchain not installed")
def test_go_package_imports_fan_out_to_package_files(tmp_path):
    _write(
        tmp_path,
        {
            "go.mod": "module example.com/m\n\ngo 1.21\n",
            "main.go": 'package main\n\nimport (\n\t"fmt"\n\tb "example.com/m/b"\n)\n\n'
            "func main() { fmt.Println(b.B) }\n",
            "b/b.go": "package b\n\nconst B = 1\n",
            "b/b_other.go": "//go:build neverset\n\npackage b\n\nconst C = 2\n",
            "b/b_test.go": 'package b_test\n\nimport "example.com/m/b"\n\nvar _ = b.B\n',
            "b/gen.go": "//go:build ignore\n\npackage main\n\nfunc main() {}\n",
            # `./...` skips `_` dirs: no package data, so out of scope.
            "_examples/ex.go": 'package main\n\nimport "example.com/m/b"\n\nvar _ = b.B\n',
        },
    )
    out = oracle.oracle_edges(tmp_path, ("go",))
    assert "_examples/ex.go" not in out["sources"] and "main.go" in out["sources"]
    edges = out["edges"]
    assert edges == {
        ("main.go", "b/b.go"),
        ("main.go", "b/b_other.go"),  # build-tag-excluded files are still b's
        ("b/b_test.go", "b/b.go"),
        ("b/b_test.go", "b/b_other.go"),
    }


# ----------------------------------------------------------------- comparison


def test_compare_scores_per_language_and_keeps_file_set_gaps_apart(tmp_path):
    kg = {
        "nodes": [{"type": "file", "filePath": p} for p in ("a.py", "b.py", "c.py", "x.ts")],
        "edges": [
            {"source": "file:a.py", "target": "file:b.py", "type": "imports"},  # TP
            {"source": "file:a.py", "target": "file:c.py", "type": "imports"},  # FP
            {"source": "file:a.py", "target": "file:a.py", "type": "imports"},  # FP self-loop
            {"source": "file:a.py", "target": "file:external:os", "type": "imports"},
            {"source": "file:c.py", "target": "file:b.py", "type": "imports", "hint": "conv"},
            {"source": "file:c.py", "target": "file:b.py", "type": "contains"},
        ],
    }
    path = tmp_path / "kg.json"
    path.write_text(json.dumps(kg))
    g_edges, g_files = oracle.load_graph_edges(path)
    truth = {
        "sources": {"a.py", "b.py", "c.py", "x.ts"},
        "edges": {("a.py", "b.py"), ("c.py", "b.py"), ("c.py", "gone.py")},
    }
    res = oracle.compare(truth, g_edges, g_files)["python"]
    assert (res["tp"], res["fp"], res["fn"]) == (1, 2, 1)
    assert res["precision"] == pytest.approx(1 / 3, abs=1e-4)
    assert res["recall"] == 0.5
    assert res["dst_not_indexed"] == 1
    assert res["recall_all"] == pytest.approx(1 / 3, abs=1e-4)
