// TypeScript/JavaScript call-edge oracle (called by calls.py).
//
// Two jobs, both driven by one JSON request on stdin:
// - "sites": parse each listed file and return its call sites (call and `new`
//   expressions, decorators, JSX component tags whose callee is a plain or
//   dotted name) plus every name the file declares.
// - "queries": go to the definition of each callee name with the TypeScript
//   LanguageService (getDefinitionAtPosition; for a member of an interface or
//   a .d.ts declaration, getImplementationAtPosition too). Each file gets a
//   service over its nearest tsconfig's options, with module resolution
//   delegated to ts_resolve.mjs so tsconfig paths and workspace packages
//   resolve to their sources.
//
// Usage: node ts_calls.mjs --root <repo> [--ts-path <dir of the typescript package>]
//   stdin:  {"files": [tracked paths], "sites": [paths], "queries": [[file, line, col], ...]}
//           (line 1-based, col 0-based UTF-16 column of the callee name)
//   stdout: {"sites": {file: [[line, start_line, col, name], ...]}, "names": [...],
//            "defs": [{"s": "defs"|"external"|"unknown", "d": [[file, line, name], ...]}, ...]}
//
// A definition outside the repo (node_modules, TS lib files) is "external"; an
// unresolved import, a parameter, or no definition at all is "unknown".

import fs from "node:fs";
import path from "node:path";
import { ROOT, ts, init, resolveOne, fileOptions } from "./ts_resolve.mjs";

const req = JSON.parse(fs.readFileSync(0, "utf8"));
init(req.files);
const tracked = new Set(req.files);
const rel = (abs) => path.relative(ROOT, abs).split(path.sep).join("/");

function scriptKind(file) {
  if (/\.(tsx|jsx)$/.test(file)) return ts.ScriptKind.TSX;
  return /\.(m|c)?js$/.test(file) ? ts.ScriptKind.JS : ts.ScriptKind.TS;
}

// ---- sites ---------------------------------------------------------------------
const DECLS = new Set([
  ts.SyntaxKind.FunctionDeclaration,
  ts.SyntaxKind.ClassDeclaration,
  ts.SyntaxKind.MethodDeclaration,
  ts.SyntaxKind.MethodSignature,
  ts.SyntaxKind.PropertyDeclaration,
  ts.SyntaxKind.PropertySignature,
  ts.SyntaxKind.PropertyAssignment,
  ts.SyntaxKind.VariableDeclaration,
  ts.SyntaxKind.GetAccessor,
]);

function calleeName(expr) {
  if (ts.isIdentifier(expr)) return expr;
  if (ts.isPropertyAccessExpression(expr) && ts.isIdentifier(expr.name)) return expr.name;
  return null;
}

function fileSites(file, names) {
  const text = fs.readFileSync(path.join(ROOT, file), "utf8");
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, scriptKind(file));
  const out = [];
  const line = (pos) => sf.getLineAndCharacterOfPosition(pos);
  const add = (node, nameNode) => {
    if (!nameNode) return;
    const at = line(nameNode.getStart(sf));
    out.push([at.line + 1, line(node.getStart(sf)).line + 1, at.character, nameNode.text]);
  };
  const visit = (n) => {
    if (ts.isCallExpression(n) || ts.isNewExpression(n)) add(n, calleeName(n.expression));
    else if (ts.isDecorator(n) && !ts.isCallExpression(n.expression)) add(n, calleeName(n.expression));
    else if (ts.isJsxOpeningElement(n) || ts.isJsxSelfClosingElement(n)) {
      const tag = calleeName(n.tagName);
      if (tag && (tag !== n.tagName || /^[A-Z]/.test(tag.text))) add(n, tag);
    }
    if (DECLS.has(n.kind) && n.name && ts.isIdentifier(n.name)) names.add(n.name.text);
    ts.forEachChild(n, visit);
  };
  visit(sf);
  return out;
}

// ---- definitions ---------------------------------------------------------------
const services = new Map(); // options object → LanguageService
function serviceFor(file) {
  const options = fileOptions(file);
  if (services.has(options)) {
    const s = services.get(options);
    s.roots.add(path.join(ROOT, file));
    return s.ls;
  }
  const roots = new Set([path.join(ROOT, file)]);
  const settings = { ...options, noEmit: true };
  const host = {
    getScriptFileNames: () => [...roots],
    getScriptVersion: () => "0",
    getScriptSnapshot: (f) => {
      const t = ts.sys.readFile(f);
      return t === undefined ? undefined : ts.ScriptSnapshot.fromString(t);
    },
    getCurrentDirectory: () => ROOT,
    getCompilationSettings: () => settings,
    getDefaultLibFileName: (o) => ts.getDefaultLibFilePath(o),
    fileExists: ts.sys.fileExists,
    readFile: ts.sys.readFile,
    readDirectory: ts.sys.readDirectory,
    directoryExists: ts.sys.directoryExists,
    getDirectories: ts.sys.getDirectories,
    realpath: ts.sys.realpath,
    resolveModuleNames: (specs, containing) =>
      specs.map((spec) => {
        let dst;
        try {
          dst = resolveOne(rel(containing), spec, options);
        } catch {
          dst = null;
        }
        if (dst === undefined) {
          // An external package: whatever TS finds (node_modules, if installed).
          return ts.resolveModuleName(spec, containing, settings, ts.sys).resolvedModule;
        }
        if (!dst) return undefined;
        const abs = path.join(ROOT, dst);
        try {
          return { resolvedFileName: abs, extension: ts.extensionFromPath(abs), isExternalLibraryImport: false };
        } catch {
          return undefined; // a non-code asset (./styles.css)
        }
      }),
  };
  const ls = ts.createLanguageService(host, ts.createDocumentRegistry());
  services.set(options, { ls, roots });
  return ls;
}

function define(ls, file, line, col) {
  const abs = path.join(ROOT, file);
  const sf = ls.getProgram().getSourceFile(abs);
  const pos = sf.getPositionOfLineAndCharacter(line - 1, col);
  let found = ls.getDefinitionAtPosition(abs, pos) || [];
  // A call through an in-repo interface member or ambient declaration runs an
  // implementation: accept those too.
  const abstract = (d) => d.containerKind === "interface" || d.fileName.endsWith(".d.ts");
  if (found.some((d) => abstract(d) && tracked.has(rel(d.fileName)))) {
    found = found.concat(ls.getImplementationAtPosition(abs, pos) || []);
  }
  const defs = [];
  let unknown = found.length === 0;
  let external = false;
  for (const d of found) {
    const r = rel(d.fileName);
    if (d.kind === "alias" || d.kind === "parameter") unknown = true;
    else if (r.startsWith("..") || r.includes("node_modules/") || !tracked.has(r)) external = true;
    else {
      const dsf = ls.getProgram().getSourceFile(d.fileName);
      const at = dsf.getLineAndCharacterOfPosition(d.textSpan.start);
      const span = dsf.text.slice(d.textSpan.start, d.textSpan.start + d.textSpan.length);
      defs.push([r, at.line + 1, d.name || span]);
    }
  }
  if (defs.length) return { s: "defs", d: defs };
  return { s: unknown || !external ? "unknown" : "external", d: [] };
}

// ---- main ------------------------------------------------------------------------
const sites = {};
const names = new Set();
for (const file of req.sites || []) {
  try {
    sites[file] = fileSites(file, names);
  } catch {
    sites[file] = [];
  }
}
// Register every queried file before the first query, so each service builds
// one program instead of one per new root.
const queries = req.queries || [];
for (const [file] of queries) serviceFor(file);
const defs = queries.map(([file, line, col]) => {
  try {
    return define(serviceFor(file), file, line, col);
  } catch {
    return { s: "unknown", d: [] };
  }
});
process.stdout.write(JSON.stringify({ sites, names: [...names].sort(), defs }));
