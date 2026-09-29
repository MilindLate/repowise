// TypeScript/JavaScript import-edge oracle (called by imports.py).
//
// Resolves every import of every TS/JS file with the TypeScript compiler's own
// ts.resolveModuleName, using the options of the file's nearest tsconfig.json /
// jsconfig.json (paths, baseUrl, extends, package.json "imports"). Workspace
// packages (pnpm-workspace.yaml, package.json "workspaces") are mapped to their
// source directory, because a checkout has no node_modules and a package's
// exports usually point at a build dir that does not exist.
//
// Usage: node ts_resolve.mjs --root <repo> [--ts-path <dir of the typescript package>]
//   stdin:  JSON array of repo-relative tracked file paths
//   stdout: JSON {"edges": [[src, dst], ...], "unresolved": {src: [spec, ...]}}
//
// Imported as a module (ts_calls.mjs), it runs nothing: the importer passes the
// same --root/--ts-path argv, calls init(files), then uses resolveOne and
// fileOptions.
//
// Finding `typescript`: --ts-path, else $REPOWISE_ORACLE_TS, else a normal
// require from this script's directory upward (a repo checkout with
// node_modules), else from the analysed repo. See imports.py for setup.

import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const args = process.argv.slice(2);
const argVal = (name) => {
  const i = args.indexOf(name);
  return i >= 0 ? args[i + 1] : undefined;
};
const ROOT = path.resolve(argVal("--root") || ".");

function loadTs() {
  const explicit = argVal("--ts-path") || process.env.REPOWISE_ORACLE_TS;
  const tries = explicit
    ? [explicit]
    : [
        "typescript",
        ...[import.meta.url, path.join(ROOT, "package.json")].map((base) => {
          try {
            return createRequire(base).resolve("typescript");
          } catch {
            return null;
          }
        }),
      ].filter(Boolean);
  const req = createRequire(import.meta.url);
  for (const t of tries) {
    try {
      return req(t);
    } catch {}
  }
  process.stderr.write(
    "ts_resolve: cannot load the `typescript` package; pass --ts-path or set REPOWISE_ORACLE_TS\n",
  );
  process.exit(2);
}
const ts = loadTs();

// The tracked file list, set by init().
let files = [];
let tracked = new Set();
const SRC_RE = /\.(ts|tsx|mts|cts|js|jsx|mjs|cjs)$/;
const TS_EXT = [".ts", ".tsx", ".mts", ".cts", ".d.ts", ".js", ".jsx", ".mjs", ".cjs", ".json"];
const rel = (abs) => path.relative(ROOT, abs).split(path.sep).join("/");
const readJson = (relPath) => {
  try {
    return JSON.parse(fs.readFileSync(path.join(ROOT, relPath), "utf8"));
  } catch {
    return null;
  }
};

// A repo path (posix, relative) → the tracked file an import of it loads, trying
// the resolution suffixes a bundler would (and .js → .ts source swaps).
function tryFile(p) {
  p = path.posix.normalize(p);
  if (p.startsWith("..")) return null;
  const cands = [p];
  const m = p.match(/^(.*)\.(m|c)?jsx?$/);
  if (m) cands.push(...[".ts", ".tsx", ".mts", ".cts"].map((e) => m[1] + e));
  cands.push(...TS_EXT.map((e) => p + e), ...TS_EXT.map((e) => `${p}/index${e}`));
  return cands.find((c) => tracked.has(c)) || null;
}

// ---- tsconfig per directory ------------------------------------------------
const parsedCache = new Map();
function parseConfig(relCfg) {
  if (!parsedCache.has(relCfg)) {
    const abs = path.join(ROOT, relCfg);
    const raw = ts.readConfigFile(abs, ts.sys.readFile);
    const parsed = raw.error
      ? null
      : ts.parseJsonConfigFileContent(raw.config, ts.sys, path.dirname(abs), undefined, abs);
    parsedCache.set(relCfg, parsed);
  }
  return parsedCache.get(relCfg);
}

const DEFAULT_OPTIONS = {
  moduleResolution: ts.ModuleResolutionKind.Bundler,
  module: ts.ModuleKind.ESNext,
};
function normaliseOptions(opts) {
  const o = { ...opts, allowJs: true, resolveJsonModule: true };
  // Classic resolution (TS's default for some `module` settings) knows no
  // node_modules or index files; no real toolchain resolves like that today.
  if (!o.moduleResolution || o.moduleResolution === ts.ModuleResolutionKind.Classic) {
    o.moduleResolution = ts.ModuleResolutionKind.Bundler;
    if (!o.module || o.module === ts.ModuleKind.None) o.module = ts.ModuleKind.ESNext;
  }
  // Bundler resolution requires an ES `module` setting.
  if (
    o.moduleResolution === ts.ModuleResolutionKind.Bundler &&
    ![ts.ModuleKind.ES2015, ts.ModuleKind.ES2020, ts.ModuleKind.ES2022, ts.ModuleKind.ESNext, ts.ModuleKind.Preserve].includes(o.module)
  ) {
    o.module = ts.ModuleKind.ESNext;
  }
  return o;
}

const dirCfgCache = new Map();
function nearestConfig(dir) {
  if (dirCfgCache.has(dir)) return dirCfgCache.get(dir);
  let found = null;
  for (const name of ["tsconfig.json", "jsconfig.json"]) {
    const c = dir === "." ? name : `${dir}/${name}`;
    if (tracked.has(c)) {
      found = c;
      break;
    }
  }
  const res = found ? found : dir === "." ? null : nearestConfig(path.posix.dirname(dir));
  dirCfgCache.set(dir, res);
  return res;
}

const refOptions = new Map();
// Options for one file: its nearest config, or — for a solution-style config
// (references only) — the referenced project whose file list contains it.
function optionsFor(file) {
  const cfg = nearestConfig(path.posix.dirname(file));
  if (!cfg) return normaliseOptions(DEFAULT_OPTIONS);
  const parsed = parseConfig(cfg);
  if (!parsed) return normaliseOptions(DEFAULT_OPTIONS);
  const abs = path.join(ROOT, file);
  for (const ref of parsed.projectReferences || []) {
    let refCfg = rel(ref.path);
    if (!refCfg.endsWith(".json")) refCfg += "/tsconfig.json";
    const refParsed = tracked.has(refCfg) ? parseConfig(refCfg) : null;
    if (refParsed && refParsed.fileNames.some((f) => path.resolve(f) === abs)) {
      if (!refOptions.has(refCfg)) refOptions.set(refCfg, normaliseOptions(refParsed.options));
      return refOptions.get(refCfg);
    }
  }
  return normaliseOptions(parsed.options);
}

// ---- package.json targets (exports / imports) --------------------------------
const CONDITION_ORDER = ["types", "import", "module", "node", "require", "default"];
function targetLeaves(t) {
  if (typeof t === "string") return [t];
  if (Array.isArray(t)) return t.flatMap(targetLeaves);
  if (t && typeof t === "object") {
    const keys = Object.keys(t).sort((a, b) => {
      const ia = CONDITION_ORDER.indexOf(a);
      const ib = CONDITION_ORDER.indexOf(b);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
    });
    return keys.flatMap((k) => targetLeaves(t[k]));
  }
  return [];
}

// Look `key` up in an exports/imports map, honouring one `*` wildcard.
function matchMap(map, key) {
  if (map == null) return [];
  if (typeof map === "string" || Array.isArray(map)) return key === "." ? targetLeaves(map) : [];
  const isSubpathMap = Object.keys(map).some((k) => k.startsWith(".") || k.startsWith("#"));
  if (!isSubpathMap) return key === "." ? targetLeaves(map) : [];
  if (key in map) return targetLeaves(map[key]);
  for (const [k, v] of Object.entries(map)) {
    const star = k.indexOf("*");
    if (star < 0) continue;
    const pre = k.slice(0, star);
    const post = k.slice(star + 1);
    if (key.startsWith(pre) && key.endsWith(post) && key.length >= pre.length + post.length) {
      const mid = key.slice(pre.length, key.length - post.length);
      return targetLeaves(v).map((t) => t.split("*").join(mid));
    }
  }
  return [];
}

// A package-relative target → a tracked source file. Build output is mapped
// back to the source tree: the package tsconfig's outDir → rootDir, else the
// conventional dist|lib|build|out → src.
function mapTarget(pkgDir, target) {
  const join = (p) => (pkgDir === "." ? path.posix.normalize(p) : path.posix.join(pkgDir, p));
  const direct = tryFile(join(target));
  if (direct) return direct;
  const noExt = target.replace(/\.d\.(m|c)?ts$/, "").replace(/\.(m|c)?jsx?$/, "");
  const swaps = [];
  const cfg = tracked.has(join("tsconfig.json")) ? parseConfig(join("tsconfig.json")) : null;
  if (cfg && cfg.options.outDir) {
    const out = rel(cfg.options.outDir);
    const src = rel(cfg.options.rootDir || path.join(ROOT, pkgDir, "src"));
    swaps.push([out, src]);
  }
  for (const d of ["dist", "lib", "build", "out"]) swaps.push([join(d), join("src")]);
  const full = join(noExt);
  for (const [from, to] of swaps) {
    if (full === from || full.startsWith(from + "/")) {
      const hit = tryFile(to + full.slice(from.length));
      if (hit) return hit;
    }
  }
  return tryFile(full);
}

// ---- workspace packages ------------------------------------------------------
function workspaceGlobs() {
  const globs = [];
  const pj = readJson("package.json");
  const ws = pj && pj.workspaces;
  if (Array.isArray(ws)) globs.push(...ws);
  else if (ws && Array.isArray(ws.packages)) globs.push(...ws.packages);
  if (tracked.has("pnpm-workspace.yaml")) {
    // Only the top-level `packages:` list matters; a full YAML parser is not needed.
    const lines = fs.readFileSync(path.join(ROOT, "pnpm-workspace.yaml"), "utf8").split("\n");
    let inPackages = false;
    for (const line of lines) {
      if (/^packages\s*:/.test(line)) {
        inPackages = true;
        continue;
      }
      if (inPackages) {
        const m = line.match(/^\s*-\s*['"]?([^'"#]+?)['"]?\s*(#.*)?$/);
        if (m) globs.push(m[1].trim());
        else if (/^\S/.test(line)) inPackages = false;
      }
    }
  }
  return globs;
}

function globToRegExp(g) {
  g = g.replace(/^\.\//, "").replace(/\/$/, "");
  let re = "";
  for (let i = 0; i < g.length; i++) {
    const c = g[i];
    if (c === "*" && g[i + 1] === "*") {
      re += ".*";
      i++;
      if (g[i + 1] === "/") i++;
    } else if (c === "*") re += "[^/]*";
    else if (c === "?") re += "[^/]";
    else re += c.replace(/[.+^${}()|[\]\\]/g, "\\$&");
  }
  return new RegExp(`^${re}$`);
}

const workspaces = new Map(); // package name → {dir, pj}
function init(fileList) {
  files = fileList;
  tracked = new Set(files);
  const globs = workspaceGlobs();
  const include = globs.filter((g) => !g.startsWith("!")).map(globToRegExp);
  const exclude = globs.filter((g) => g.startsWith("!")).map((g) => globToRegExp(g.slice(1)));
  for (const f of files) {
    if (!f.endsWith("/package.json") || f.includes("node_modules/")) continue;
    const dir = path.posix.dirname(f);
    if (!include.some((r) => r.test(dir)) || exclude.some((r) => r.test(dir))) continue;
    const pj = readJson(f);
    if (pj && pj.name && !workspaces.has(pj.name)) workspaces.set(pj.name, { dir, pj });
  }
}

function resolveWorkspace(spec) {
  let best = null;
  for (const name of workspaces.keys()) {
    if ((spec === name || spec.startsWith(name + "/")) && (!best || name.length > best.length)) best = name;
  }
  if (!best) return undefined;
  const { dir, pj } = workspaces.get(best);
  const sub = spec.slice(best.length).replace(/^\//, "");
  if (pj.exports) {
    for (const t of matchMap(pj.exports, sub ? `./${sub}` : ".")) {
      const hit = mapTarget(dir, t);
      if (hit) return hit;
    }
  }
  const fields = sub ? [sub] : [pj.types, pj.typings, pj.module, pj.main].filter(Boolean);
  for (const t of fields) {
    const hit = mapTarget(dir, t);
    if (hit) return hit;
  }
  return mapTarget(dir, sub ? `src/${sub}` : "src/index") || mapTarget(dir, sub || "index") || null;
}

// `#name` specifiers from the nearest package.json "imports" (when tsconfig
// paths did not already resolve them).
function nearestPackageJson(dir) {
  for (;;) {
    const p = dir === "." ? "package.json" : `${dir}/package.json`;
    if (tracked.has(p)) return p;
    if (dir === ".") return null;
    dir = path.posix.dirname(dir);
  }
}
function resolveSubpathImport(file, spec) {
  const pjPath = nearestPackageJson(path.posix.dirname(file));
  const pj = pjPath && readJson(pjPath);
  if (!pj || !pj.imports) return null;
  for (const t of matchMap(pj.imports, spec)) {
    if (!t.startsWith("./")) continue; // maps to an external package
    const hit = mapTarget(path.posix.dirname(pjPath), t);
    if (hit) return hit;
  }
  return null;
}

// ---- main ----------------------------------------------------------------------
const host = {
  fileExists: ts.sys.fileExists,
  readFile: ts.sys.readFile,
  directoryExists: ts.sys.directoryExists,
  getDirectories: ts.sys.getDirectories,
  realpath: ts.sys.realpath,
};
const caches = new Map(); // one module-resolution cache per options object

function resolveOne(file, spec, options) {
  const abs = path.join(ROOT, file);
  if (!caches.has(options)) caches.set(options, ts.createModuleResolutionCache(ROOT, (x) => x, options));
  const r = ts.resolveModuleName(spec, abs, options, host, caches.get(options)).resolvedModule;
  if (r && !r.isExternalLibraryImport) {
    const hit = rel(r.resolvedFileName);
    if (tracked.has(hit) && !hit.includes("node_modules/")) {
      // A declaration file beside its JS implementation types that file; the
      // import loads the implementation.
      const decl = hit.match(/^(.*)\.d\.(m|c)?ts$/);
      const impl = decl && [".js", ".jsx", ".mjs", ".cjs"].map((e) => decl[1] + e).find((c) => tracked.has(c));
      return impl || hit;
    }
  }
  if (spec.startsWith(".")) {
    // Non-code assets (./styles.css, ./logo.svg) and anything TS refused.
    return tryFile(path.posix.join(path.posix.dirname(file), spec));
  }
  if (spec.startsWith("#")) return resolveSubpathImport(file, spec);
  const ws = resolveWorkspace(spec);
  return ws === undefined ? undefined : ws; // undefined = external package
}

// Every module specifier in a file, from a full parse (ts.preProcessFile's
// scanner misses `export * as ns from` and some dynamic `import()` calls).
function moduleSpecifiers(file, text) {
  const kind = /\.(tsx|jsx)$/.test(file) ? ts.ScriptKind.TSX : /\.(m|c)?js$/.test(file) ? ts.ScriptKind.JS : ts.ScriptKind.TS;
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, false, kind);
  const specs = new Set();
  const lit = (n) => n && (ts.isStringLiteral(n) || ts.isNoSubstitutionTemplateLiteral(n)) && specs.add(n.text);
  const visit = (n) => {
    if (ts.isImportDeclaration(n) || ts.isExportDeclaration(n)) lit(n.moduleSpecifier);
    else if (ts.isImportEqualsDeclaration(n) && ts.isExternalModuleReference(n.moduleReference)) lit(n.moduleReference.expression);
    else if (ts.isImportTypeNode(n) && ts.isLiteralTypeNode(n.argument)) lit(n.argument.literal);
    else if (ts.isCallExpression(n) && n.arguments.length >= 1) {
      const callee = n.expression;
      if (callee.kind === ts.SyntaxKind.ImportKeyword || (ts.isIdentifier(callee) && callee.text === "require")) lit(n.arguments[0]);
    }
    ts.forEachChild(n, visit);
  };
  visit(sf);
  for (const ref of sf.referencedFiles) specs.add(ref.fileName.startsWith(".") ? ref.fileName : `./${ref.fileName}`);
  return specs;
}

// Solution-style configs pick options per file; plain configs share one
// options object (and so one resolution cache).
const optionsByConfig = new Map();
function fileOptions(file) {
  const cfgKey = nearestConfig(path.posix.dirname(file)) || "";
  const parsed = cfgKey ? parseConfig(cfgKey) : null;
  if (parsed && (parsed.projectReferences || []).length) return optionsFor(file);
  if (!optionsByConfig.has(cfgKey)) optionsByConfig.set(cfgKey, optionsFor(file));
  return optionsByConfig.get(cfgKey);
}

export { ROOT, ts, init, resolveOne, fileOptions };

function main() {
  init(JSON.parse(fs.readFileSync(0, "utf8")));
  const edges = [];
  const unresolved = {};
  for (const file of files) {
    if (!SRC_RE.test(file) || file.includes("node_modules/")) continue;
    let text;
    try {
      text = fs.readFileSync(path.join(ROOT, file), "utf8");
    } catch {
      continue;
    }
    const options = fileOptions(file);
    const specs = moduleSpecifiers(file, text);
    const seen = new Set();
    for (const spec of specs) {
      let dst;
      try {
        dst = resolveOne(file, spec, options);
      } catch {
        dst = null;
      }
      if (dst === undefined) continue; // external package
      if (dst === null) {
        (unresolved[file] ||= []).push(spec);
        continue;
      }
      if (dst !== file && !seen.has(dst)) {
        seen.add(dst);
        edges.push([file, dst]);
      }
    }
  }
  process.stdout.write(JSON.stringify({ edges, unresolved }));
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) main();
