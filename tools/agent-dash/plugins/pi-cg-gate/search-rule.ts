/**
 * cg-gate rule: decide whether a bash command is a recursive or multi-file
 * search over source files, and whether the session has used CodeGraph yet.
 *
 * Pure functions only (no I/O, no globals) so `test-search-rule.mjs` can run
 * the same code the extension runs, without a model.
 */

const NONCODE_EXT = new Set([
  "md", "markdown", "mdx", "rst", "adoc", "txt", "text",
  "yaml", "yml", "json", "jsonl", "json5", "ndjson",
  "log", "csv", "tsv", "toml", "ini", "cfg", "conf", "lock",
  "xml", "html", "htm", "svg",
]);

const NONCODE_DIRS = new Set(["docs", "doc", "documentation", "notes", "logs", "man"]);

const PREFIX_WORDS = new Set(["sudo", "time", "command", "env", "nice", "nohup", "builtin", "exec"]);

const GREP_SHORT_VALUE = new Set(["e", "f", "m", "A", "B", "C", "d", "D"]);
const GREP_LONG_VALUE = new Set([
  "--regexp", "--file", "--max-count", "--after-context", "--before-context",
  "--context", "--exclude", "--exclude-dir", "--exclude-from", "--label",
  "--binary-files", "--devices", "--directories", "--group-separator",
]);
const RG_SHORT_VALUE = new Set(["e", "f", "g", "t", "T", "m", "A", "B", "C", "r", "j", "M"]);
const RG_LONG_VALUE = new Set([
  "--regexp", "--file", "--glob", "--iglob", "--type", "--type-not", "--type-add",
  "--max-count", "--after-context", "--before-context", "--context",
  "--max-depth", "--max-filesize", "--replace", "--threads", "--pre", "--pre-glob",
  "--encoding", "--engine", "--sort", "--sortr", "--path-separator",
]);

export type SearchIntent = {
  search: boolean;
  kind?: string;
  query?: string;
  why?: string;
};

type Segment = { tokens: string[]; pipedIn: boolean };

function stripQuotes(s: string): string {
  return s.replace(/^['"]+|['"]+$/g, "");
}

function baseName(token: string): string {
  // Strip subshell/command-substitution wrappers: "(cg", "$(cg", "`cg`".
  let clean = stripQuotes(token).replace(/^[({$`]+/, "").replace(/[)};`]+$/, "");
  const parts = clean.split("/");
  return parts[parts.length - 1];
}

/** Split a segment into shell words, honouring single and double quotes. */
export function shellTokens(segment: string): string[] {
  const tokens: string[] = [];
  let cur = "";
  let quote: string | null = null;
  let started = false;
  for (let i = 0; i < segment.length; i++) {
    const ch = segment[i];
    if (quote) {
      if (ch === quote) quote = null;
      else cur += ch;
      started = true;
      continue;
    }
    if (ch === "'" || ch === '"') {
      quote = ch;
      started = true;
      continue;
    }
    if (ch === "\\" && i + 1 < segment.length) {
      cur += segment[++i];
      started = true;
      continue;
    }
    if (ch === " " || ch === "\t" || ch === "\r") {
      if (started) {
        tokens.push(cur);
        cur = "";
        started = false;
      }
      continue;
    }
    cur += ch;
    started = true;
  }
  if (started) tokens.push(cur);
  return tokens;
}

/** Split a command on unquoted ; && || | and newlines. */
function splitCommand(command: string): Segment[] {
  const segments: Segment[] = [];
  let buf = "";
  let quote: string | null = null;
  let nextPiped = false;

  const flush = () => {
    const tokens = shellTokens(buf);
    buf = "";
    if (tokens.length) {
      segments.push({ tokens, pipedIn: nextPiped });
      nextPiped = false;
    }
  };

  for (let i = 0; i < command.length; i++) {
    const ch = command[i];
    if (quote) {
      buf += ch;
      if (ch === quote) quote = null;
      continue;
    }
    if (ch === "'" || ch === '"') {
      quote = ch;
      buf += ch;
      continue;
    }
    if (ch === "\\" && i + 1 < command.length) {
      buf += ch + command[++i];
      continue;
    }
    if (ch === "\n") {
      flush();
      continue;
    }
    if (ch === ";") {
      flush();
      nextPiped = false;
      continue;
    }
    if (ch === "|") {
      flush();
      if (command[i + 1] === "|") {
        i++;
        nextPiped = false;
      } else {
        nextPiped = true;
      }
      continue;
    }
    if (ch === "&") {
      flush();
      if (command[i + 1] === "&") i++;
      nextPiped = false;
      continue;
    }
    buf += ch;
  }
  flush();
  return segments;
}

/** Drop shell redirects and their targets: < file, > file, 2>&1, ... */
function stripRedirects(tokens: string[]): string[] {
  const out: string[] = [];
  for (let i = 0; i < tokens.length; i++) {
    const tok = tokens[i];
    if (/^\d*(<|>|>>|<<)/.test(tok)) {
      if (/^\d*(<|>|>>|<<)$/.test(tok)) i++; // skip the redirect target
      continue;
    }
    out.push(tok);
  }
  return out;
}

/** Drop env assignments and harmless prefixes (sudo, time, env, ...). */
function commandWords(rawTokens: string[]): string[] {
  const tokens = stripRedirects(rawTokens);
  let i = 0;
  while (i < tokens.length) {
    const tok = tokens[i];
    if (/^[A-Za-z_][A-Za-z0-9_]*=/.test(tok) || PREFIX_WORDS.has(tok)) {
      i++;
      continue;
    }
    break;
  }
  return tokens.slice(i);
}

function extOf(target: string): string {
  const t = stripQuotes(target).replace(/\/+$/, "");
  const b = t.split("/").pop() || "";
  if (!b.includes(".")) return "";
  return (b.split(".").pop() || "").toLowerCase();
}

function isGlob(target: string): boolean {
  return /[*?[\]]/.test(target);
}

export function isNonCodeTarget(target: string): boolean {
  const t = stripQuotes(target);
  if (!t) return false;
  if (/(^|\/)(docs?|documentation|notes|logs)(\/|$)/i.test(t)) return true;
  const ext = extOf(t);
  if (ext) return NONCODE_EXT.has(ext);
  const b = t.replace(/\/+$/, "").split("/").pop() || "";
  return NONCODE_DIRS.has(b.toLowerCase());
}

function allNonCode(targets: string[]): boolean {
  return targets.length > 0 && targets.every(isNonCodeTarget);
}

function globsAllNonCode(globs: string[]): boolean {
  return globs.length > 0 && globs.every((g) => isNonCodeTarget(g));
}

type Parsed = {
  recursive: boolean;
  listing: boolean;
  pattern?: string;
  targets: string[];
  globs: string[];
  docsOnly: boolean;
};

function parseGrep(args: string[]): Parsed {
  const p: Parsed = { recursive: false, listing: false, targets: [], globs: [], docsOnly: false };
  let patternSeen = false;
  let patternFlagged = false;
  let afterDashes = false;
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (!afterDashes && a === "--") {
      afterDashes = true;
      continue;
    }
    if (!afterDashes && a.length > 1 && a.startsWith("-")) {
      if (a.startsWith("--")) {
        const eq = a.indexOf("=");
        const name = eq >= 0 ? a.slice(0, eq) : a;
        const val = eq >= 0 ? a.slice(eq + 1) : undefined;
        if (name === "--recursive" || name === "--dereference-recursive") p.recursive = true;
        if (name === "--include" || name === "--glob" || name === "--iglob") {
          p.globs.push(val !== undefined ? val : (args[++i] ?? ""));
        } else if (name === "--files-with-matches" || name === "--files-without-match") {
          // still a search
        } else if (name === "--regexp" || name === "--file") {
          patternFlagged = true;
          if (val === undefined) i++;
        } else if (val === undefined && GREP_LONG_VALUE.has(name)) {
          i++;
        }
      } else {
        if (a.includes("r") || a.includes("R")) p.recursive = true;
        const last = a[a.length - 1];
        if (GREP_SHORT_VALUE.has(last)) {
          patternFlagged = true;
          i++;
        }
      }
      continue;
    }
    if (!patternSeen && !patternFlagged) {
      p.pattern = a;
      patternSeen = true;
      continue;
    }
    p.targets.push(a);
  }
  p.docsOnly = allNonCode(p.targets) || (p.targets.length > 0 && globsAllNonCode(p.globs));
  return p;
}

function parseRg(args: string[], tool: string): Parsed {
  const p: Parsed = { recursive: true, listing: false, targets: [], globs: [], docsOnly: false };
  let patternSeen = false;
  let patternFlagged = false;
  let afterDashes = false;
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (!afterDashes && a === "--") {
      afterDashes = true;
      continue;
    }
    if (!afterDashes && a.length > 1 && a.startsWith("-")) {
      if (a.startsWith("--")) {
        const eq = a.indexOf("=");
        const name = eq >= 0 ? a.slice(0, eq) : a;
        const val = eq >= 0 ? a.slice(eq + 1) : undefined;
        if (name === "--files" || name === "--type-list" || name === "--help") p.listing = true;
        if (name === "--glob" || name === "--iglob" || name === "--type" || name === "--type-not") {
          p.globs.push(val !== undefined ? val : (args[++i] ?? ""));
        } else if (name === "--regexp" || name === "--file") {
          patternFlagged = true;
          if (val === undefined) i++;
        } else if (val === undefined && RG_LONG_VALUE.has(name)) {
          i++;
        }
      } else {
        const last = a[a.length - 1];
        if (RG_SHORT_VALUE.has(last)) {
          // -e/--regexp, -f/--file, -g/--glob, -t/--type, -r/--replace, ...
          if (last === "e" || last === "f") patternFlagged = true;
          if (last === "g" || last === "t" || last === "T") p.globs.push(args[++i] ?? "");
          else i++;
        }
      }
      continue;
    }
    if (!patternSeen && !patternFlagged) {
      p.pattern = a;
      patternSeen = true;
      continue;
    }
    p.targets.push(a);
  }
  p.docsOnly = allNonCode(p.targets) || (p.targets.length > 0 && globsAllNonCode(p.globs));
  return p;
}

function parseGitGrep(args: string[]): Parsed {
  const p: Parsed = { recursive: true, listing: false, targets: [], globs: [], docsOnly: false };
  let patternSeen = false;
  let afterDashes = false;
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (!afterDashes && a === "--") {
      afterDashes = true;
      continue;
    }
    if (!afterDashes && a.length > 1 && a.startsWith("-")) {
      if (a.startsWith("--")) {
        const eq = a.indexOf("=");
        const name = eq >= 0 ? a.slice(0, eq) : a;
        if (name === "--and" || name === "--or" || name === "--not" || name === "--all-match") {
          // boolean word flags; no values
        } else if (eq < 0) {
          const valued = ["--max-depth", "--threads", "--encoding", "--color", "--color-words"];
          if (valued.includes(name)) i++;
        }
      } else if ("e f".includes(a[a.length - 1])) {
        i++; // -e pattern / -f file
        patternSeen = true;
      }
      continue;
    }
    if (!patternSeen) {
      p.pattern = a;
      patternSeen = true;
      continue;
    }
    p.targets.push(a);
  }
  p.docsOnly = allNonCode(p.targets);
  return p;
}

function parseFindNames(args: string[]): string[] {
  const names: string[] = [];
  for (let i = 0; i < args.length; i++) {
    if (["-name", "-iname", "-path", "-ipath", "-wholename"].includes(args[i])) {
      const v = args[i + 1];
      if (v) names.push(v);
      i++;
    }
  }
  return names;
}

function pipeSourceKind(prev: string[] | undefined): "listing" | "content" {
  if (!prev || prev.length === 0) return "content";
  const w = commandWords(prev);
  if (w.length === 0) return "content";
  const cmd = baseName(w[0]);
  const second = baseName(w[1] ?? "");
  if (cmd === "xargs") return "listing";
  if (cmd === "find" || cmd === "fd" || cmd === "ls" || cmd === "tree" || cmd === "rg") {
    if (cmd === "rg" && !w.some((t) => t === "--files")) return "content";
    return "listing";
  }
  if (cmd === "git" && second === "ls-files") return "listing";
  return "content";
}

function blocked(kind: string, pattern: string | undefined, why: string): SearchIntent {
  return { search: true, kind, query: pattern, why };
}

function analyze(
  words: string[],
  pipedIn: boolean,
  prev: string[] | undefined,
): SearchIntent {
  if (words.length === 0) return { search: false };
  let cmd = baseName(words[0]);
  let args = words.slice(1);
  let toolLabel = cmd;

  if (cmd === "xargs") {
    const inner = args.findIndex((a) => ["grep", "egrep", "fgrep", "rg", "ag", "ack"].includes(baseName(a)));
    if (inner >= 0) {
      cmd = baseName(args[inner]);
      toolLabel = `xargs ${cmd}`;
      args = args.slice(inner + 1);
    } else {
      return { search: false };
    }
  }

  if (cmd === "git" && baseName(args[0] ?? "") === "grep") {
    return analyzeParsed("git grep", parseGitGrep(args.slice(1)), pipedIn, prev);
  }
  if (cmd === "git") return { search: false };
  if (cmd === "find") return { search: false }; // only its grep side is a search
  if (cmd === "grep" || cmd === "egrep" || cmd === "fgrep") {
    return analyzeParsed(toolLabel, parseGrep(args), pipedIn, prev);
  }
  if (cmd === "rg" || cmd === "ag" || cmd === "ack") {
    return analyzeParsed(toolLabel, parseRg(args, cmd), pipedIn, prev);
  }
  return { search: false };
}

function analyzeParsed(
  label: string,
  p: Parsed,
  pipedIn: boolean,
  prev: string[] | undefined,
): SearchIntent {
  if (p.listing) return { search: false };

  const isRecursive = p.recursive;
  const noTargets = p.targets.length === 0;

  if (pipedIn && noTargets) {
    // A search over a pipe: content pipes are fine, file listings are not.
    if (pipeSourceKind(prev) === "listing") {
      const w = commandWords(prev ?? []);
      const names = baseName(w[0] ?? "") === "find" ? parseFindNames(w.slice(1)) : [];
      if (names.length > 0 && names.every(isNonCodeTarget)) return { search: false };
      return blocked(label, p.pattern, `${label} over a find/ls file listing`);
    }
    return { search: false };
  }

  if (noTargets) {
    // Non-recursive grep with no file reads stdin or a < redirect.
    if (!isRecursive) return { search: false };
    if (globsAllNonCode(p.globs)) return { search: false };
    return blocked(label, p.pattern, `recursive ${label} over the working tree`);
  }

  if (p.docsOnly) return { search: false };
  if (allNonCode(p.targets)) return { search: false };

  // grep on one named file is always fine, recursive flag or not.
  const single = p.targets.length === 1 ? p.targets[0] : undefined;
  if (single && !isGlob(single) && extOf(single) !== "") {
    return { search: false };
  }

  if (isRecursive) {
    return blocked(label, p.pattern, `recursive ${label} over source files`);
  }
  if (p.targets.length > 1) {
    return blocked(label, p.pattern, `multi-file ${label}`);
  }
  return blocked(label, p.pattern, `${label} over a directory or glob`);
}

/** True when the command runs cg or codegraph anywhere in it. */
export function runsCodeGraph(command: string): boolean {
  return splitCommand(command).some((seg) => {
    const words = commandWords(seg.tokens);
    if (words.length === 0) return false;
    const cmd = baseName(words[0]);
    return cmd === "cg" || cmd === "codegraph";
  });
}

/** The first recursive/multi-file source search in the command, if any. */
export function searchIntent(command: string): SearchIntent {
  const segs = splitCommand(command);
  for (let i = 0; i < segs.length; i++) {
    const words = commandWords(segs[i].tokens);
    const prevWords = i > 0 ? commandWords(segs[i - 1].tokens) : undefined;
    const verdict = analyze(words, segs[i].pipedIn, prevWords);
    if (verdict.search) return verdict;
  }
  return { search: false };
}

export function blockReason(intent: SearchIntent): string {
  const raw = (intent.query || "<your question>").replace(/["\\]/g, "").slice(0, 80);
  const query = raw.trim() || "<your question>";
  return [
    `Blocked: ${intent.why ?? "recursive or multi-file code search"} is not allowed until CodeGraph has run once in this session.`,
    `Run exactly: cg explore "${query}"`,
    "It returns the relevant symbols, their source and call paths from the main checkout's index.",
    "Then use cg node <symbol|file>, cg callers <symbol>, cg impact <symbol>.",
    "Grep is fine for one named file, or for docs, YAML, JSON, logs and Markdown. After one cg call every command is allowed.",
  ].join(" ");
}

export type GateDecision = { block: boolean; reason?: string };

export function gateDecision(command: string, hasRunCodeGraph: boolean): GateDecision {
  if (hasRunCodeGraph) return { block: false };
  if (runsCodeGraph(command)) return { block: false };
  const intent = searchIntent(command);
  if (!intent.search) return { block: false };
  return { block: true, reason: blockReason(intent) };
}
