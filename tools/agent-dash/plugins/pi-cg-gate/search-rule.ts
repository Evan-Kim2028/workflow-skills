/**
 * cg-gate rule: decide whether a bash command is a recursive or multi-file
 * search over source files, and whether a command actually runs CodeGraph.
 *
 * Pure functions only (no I/O, no globals) so the tests can run the same code
 * the extension runs, without a model.
 *
 * Design rule: the gate is a nudge, not a boundary. When the matcher is not
 * sure a command is a recursive or multi-file search over source code, it
 * allows it. A missed search only means the model greps as it did before; a
 * wrongly blocked command costs a worker a turn.
 *
 * Deliberately not caught (allowed), because guessing wrong is worse:
 *   - nested shells: `bash -lc 'rg foo'`
 *   - command substitution: `echo "$(rg foo)"`
 *   - anything the matcher cannot classify, including a lone directory target
 *     with no trailing slash (keep the slash to make it a directory search).
 */

const NONCODE_EXT = new Set([
  "md", "markdown", "mdx", "rst", "adoc", "txt", "text",
  "yaml", "yml", "json", "jsonl", "json5", "ndjson",
  "log", "csv", "tsv", "toml", "ini", "cfg", "conf", "lock",
  "xml", "html", "htm", "svg",
]);

/** ripgrep type names that only ever select non-code files. */
const NONCODE_TYPES = new Set([
  "md", "markdown", "mdx", "rst", "asciidoc", "txt", "text",
  "yaml", "yml", "json", "jsonl", "ndjson", "toml", "csv", "tsv",
  "log", "xml", "html", "svg",
]);

const NONCODE_DIRS = new Set(["docs", "doc", "documentation", "notes", "logs", "man"]);

const PREFIX_WORDS = new Set([
  "sudo", "time", "command", "env", "nice", "nohup", "builtin", "exec",
  "if", "then", "else", "elif", "while", "until", "for", "do", "done",
  "case", "esac", "fi", "!", "{", "(", "}", ")",
]);

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
const GIT_GREP_SHORT_VALUE = new Set(["e", "f", "A", "B", "C", "m", "O"]);
const GIT_GREP_LONG_VALUE = new Set([
  "--max-depth", "--threads", "--encoding", "--exclude", "--exclude-from",
  "--exclude-per-directory", "--open-files-in-pager", "--color", "--color-words",
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

/** Remove grouping punctuation and quoting from a path-like token. */
function cleanTarget(token: string): string {
  let t = stripQuotes(token.trim());
  t = t.replace(/^[({]+/, "").replace(/[)}]+$/, "");
  t = t.replace(/;+$/, "");
  return t;
}

function baseName(token: string): string {
  // Strip grouping/subshell wrappers, but not `$(` or backticks: command
  // substitution is deliberately out of scope.
  const clean = cleanTarget(token).replace(/^[({]+/, "").replace(/[)}]+$/, "");
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

/**
 * Drop heredoc bodies, keeping only the `<<'DELIM'` marker on the command
 * line. A heredoc body is file content, not commands. Handles quoted,
 * unquoted and `<<-` tab-indented delimiters.
 */
export function stripHeredocs(command: string): string {
  let out = "";
  let i = 0;
  let quote: string | null = null;
  while (i < command.length) {
    const ch = command[i];
    if (quote) {
      out += ch;
      if (ch === quote) quote = null;
      i++;
      continue;
    }
    if (ch === "'" || ch === '"') {
      quote = ch;
      out += ch;
      i++;
      continue;
    }
    if (ch === "\\" && i + 1 < command.length) {
      out += ch + command[i + 1];
      i += 2;
      continue;
    }
    if (ch === "<" && command[i + 1] === "<" && command[i + 2] !== "<") {
      let j = i + 2;
      let stripTabs = false;
      if (command[j] === "-") {
        stripTabs = true;
        j++;
      }
      while (j < command.length && (command[j] === " " || command[j] === "\t")) j++;
      let delim = "";
      let dq: string | null = null;
      while (j < command.length) {
        const c = command[j];
        if (dq) {
          if (c === dq) {
            dq = null;
            j++;
            continue;
          }
          delim += c;
          j++;
          continue;
        }
        if (c === "'" || c === '"') {
          dq = c;
          j++;
          continue;
        }
        if (c === "\\" && j + 1 < command.length) {
          delim += command[j + 1];
          j += 2;
          continue;
        }
        if (c === " " || c === "\t" || c === "\n" || c === ";" || c === "|" || c === "&") break;
        delim += c;
        j++;
      }
      if (!delim) {
        out += ch;
        i++;
        continue;
      }
      out += command.slice(i, j);
      let k = j;
      while (k < command.length && command[k] !== "\n") k++;
      if (k >= command.length) {
        i = k;
        continue;
      }
      k++; // past the marker line
      while (k <= command.length) {
        let lineEnd = command.indexOf("\n", k);
        if (lineEnd < 0) lineEnd = command.length;
        const line = command.slice(k, lineEnd);
        const cmp = stripTabs ? line.replace(/^\t+/, "") : line;
        k = lineEnd;
        if (cmp === delim) break;
        k = lineEnd + 1;
        if (k > command.length) break;
      }
      i = k;
      continue;
    }
    out += ch;
    i++;
  }
  return out;
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

function segmentsOf(command: string): Segment[] {
  return splitCommand(stripHeredocs(command));
}

/** Drop shell redirects and their targets: < file, > file, 2>&1, <<'X', ... */
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

/** Drop env assignments, grouping words and harmless prefixes. */
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
  const t = cleanTarget(target).replace(/\/+$/, "");
  const b = t.split("/").pop() || "";
  if (!b.includes(".")) return "";
  return (b.split(".").pop() || "").toLowerCase();
}

function isGlob(target: string): boolean {
  return /[*?[\]]/.test(cleanTarget(target));
}

export function isNonCodeTarget(target: string): boolean {
  const t = cleanTarget(target);
  if (!t) return false;
  if (t.startsWith("!")) return false;
  if (/(^|\/)(docs?|documentation|notes|logs)(\/|$)/i.test(t)) return true;
  const ext = extOf(t);
  if (ext) return NONCODE_EXT.has(ext);
  const b = t.replace(/\/+$/, "").split("/").pop() || "";
  return NONCODE_DIRS.has(b.toLowerCase());
}

function allNonCode(targets: string[]): boolean {
  return targets.length > 0 && targets.every(isNonCodeTarget);
}

/**
 * True when every include filter (glob or rg type) selects only non-code
 * files. A negated filter (`!*.md`, `-T py`) can never prove docs-only.
 */
function filtersAllNonCode(filters: string[]): boolean {
  if (filters.length === 0) return false;
  return filters.every((f) => {
    const t = cleanTarget(f);
    if (!t || t.startsWith("!")) return false;
    if (NONCODE_TYPES.has(t.toLowerCase())) return true;
    return isNonCodeTarget(t);
  });
}

/**
 * Walk a short-option cluster (`-rn`, `-C2`, `-tmd`). Value-taking letters
 * consume the rest of the token or the next argument. Returns the new index.
 */
function parseShortCluster(
  a: string,
  args: string[],
  i: number,
  valueSet: Set<string>,
  onValue: (letter: string, value: string) => void,
  onFlag: (letter: string) => void,
): number {
  for (let k = 1; k < a.length; k++) {
    const letter = a[k];
    if (valueSet.has(letter)) {
      const attached = a.slice(k + 1);
      if (attached.length > 0) {
        onValue(letter, attached);
        return i;
      }
      onValue(letter, args[i + 1] ?? "");
      return i + 1;
    }
    onFlag(letter);
  }
  return i;
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
        } else if (name === "--regexp" || name === "--file") {
          patternFlagged = true;
          if (val === undefined) i++;
        } else if (val === undefined && GREP_LONG_VALUE.has(name)) {
          i++;
        }
      } else {
        i = parseShortCluster(
          a,
          args,
          i,
          GREP_SHORT_VALUE,
          (letter) => {
            // Only -e/-f carry the pattern. -A/-B/-C/-m/-d/-D just take a value.
            if (letter === "e" || letter === "f") patternFlagged = true;
          },
          (letter) => {
            if (letter === "r" || letter === "R") p.recursive = true;
          },
        );
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
  p.docsOnly = allNonCode(p.targets) || (p.targets.length > 0 && filtersAllNonCode(p.globs));
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
        if (name === "--glob" || name === "--iglob") {
          p.globs.push(val !== undefined ? val : (args[++i] ?? ""));
        } else if (name === "--type") {
          p.globs.push(val !== undefined ? val : (args[++i] ?? ""));
        } else if (name === "--type-not") {
          p.globs.push("!" + (val !== undefined ? val : (args[++i] ?? "")));
        } else if (name === "--regexp" || name === "--file") {
          patternFlagged = true;
          if (val === undefined) i++;
        } else if (val === undefined && RG_LONG_VALUE.has(name)) {
          i++;
        }
      } else {
        i = parseShortCluster(
          a,
          args,
          i,
          RG_SHORT_VALUE,
          (letter, value) => {
            // -e/--regexp, -f/--file, -g/--glob, -t/--type, -T/--type-not, ...
            if (letter === "e" || letter === "f") patternFlagged = true;
            else if (letter === "g" || letter === "t") p.globs.push(value);
            else if (letter === "T") p.globs.push("!" + value);
          },
          () => {},
        );
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
  p.docsOnly = allNonCode(p.targets) || (p.targets.length > 0 && filtersAllNonCode(p.globs));
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
        const val = eq >= 0 ? a.slice(eq + 1) : undefined;
        if (eq < 0 && GIT_GREP_LONG_VALUE.has(name)) i++;
        void val;
      } else {
        i = parseShortCluster(
          a,
          args,
          i,
          GIT_GREP_SHORT_VALUE,
          (letter) => {
            if (letter === "e" || letter === "f") patternSeen = true;
          },
          () => {},
        );
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

const FIND_NAMES = new Set(["-name", "-iname", "-path", "-ipath", "-wholename"]);
const FIND_NEGATIONS = new Set(["!", "-not", "-o", "-or", "-prune"]);

function parseFindNames(args: string[]): string[] {
  const names: string[] = [];
  for (let i = 0; i < args.length; i++) {
    if (FIND_NAMES.has(args[i])) {
      const v = args[i + 1];
      if (v) names.push(v);
      i++;
    }
  }
  return names;
}

/** A find expression proves docs-only only when it has no negation/OR/prune. */
function findDocsOnly(words: string[]): boolean {
  const args = words.slice(1);
  if (args.some((a) => FIND_NEGATIONS.has(a))) return false;
  const names = parseFindNames(args);
  return names.length > 0 && names.every(isNonCodeTarget);
}

/** Pipe stages that pass their input through unchanged. */
const PASS_THROUGH = new Set([
  "sort", "uniq", "head", "tail", "cut", "tr", "tac", "rev", "nl", "wc",
  "column", "sed", "awk", "tee",
]);

/**
 * Walk back through a pipeline to its source. A `find | sort | xargs grep`
 * pipeline still searches a file listing.
 */
function pipeSource(chain: string[][]): { kind: "listing" | "content"; words: string[] } {
  for (let i = chain.length - 1; i >= 0; i--) {
    const w = commandWords(chain[i]);
    if (w.length === 0) continue;
    const cmd = baseName(w[0]);
    const second = baseName(w[1] ?? "");
    if (PASS_THROUGH.has(cmd)) continue;
    if (cmd === "xargs") return { kind: "listing", words: w };
    if (cmd === "find" || cmd === "fd" || cmd === "ls" || cmd === "tree") {
      return { kind: "listing", words: w };
    }
    if (cmd === "rg") {
      return { kind: w.some((t) => t === "--files") ? "listing" : "content", words: w };
    }
    if (cmd === "git" && second === "ls-files") return { kind: "listing", words: w };
    return { kind: "content", words: w };
  }
  return { kind: "content", words: [] };
}

function blocked(kind: string, pattern: string | undefined, why: string): SearchIntent {
  return { search: true, kind, query: pattern, why };
}

const GIT_GLOBAL_VALUE = new Set([
  "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
  "--config-env", "--super-prefix",
]);

/** Skip git's global options so `git -C src grep foo` is recognised. */
function gitSubcommand(args: string[]): { sub: string; rest: string[] } {
  let i = 0;
  while (i < args.length) {
    const a = args[i];
    if (GIT_GLOBAL_VALUE.has(a)) {
      i += 2;
      continue;
    }
    if (a.startsWith("-C") && a.length > 2) {
      i += 1;
      continue;
    }
    if (a.startsWith("-c") && a.length > 2) {
      i += 1;
      continue;
    }
    if (a.startsWith("-")) {
      i += 1;
      continue;
    }
    break;
  }
  return { sub: baseName(args[i] ?? ""), rest: args.slice(i + 1) };
}

function analyze(
  words: string[],
  pipedIn: boolean,
  prevChain: string[][],
): SearchIntent {
  if (words.length === 0) return { search: false };
  let cmd = baseName(words[0]);
  let args = words.slice(1);
  let toolLabel = cmd;

  if (cmd === "xargs") {
    const inner = args.findIndex((a) =>
      ["grep", "egrep", "fgrep", "rg", "ag", "ack"].includes(baseName(a)),
    );
    if (inner >= 0) {
      cmd = baseName(args[inner]);
      toolLabel = `xargs ${cmd}`;
      args = args.slice(inner + 1);
    } else {
      return { search: false };
    }
  }

  if (cmd === "git") {
    const g = gitSubcommand(args);
    if (g.sub === "grep") {
      return analyzeParsed("git grep", parseGitGrep(g.rest), pipedIn, prevChain);
    }
    return { search: false };
  }
  if (cmd === "find") return { search: false }; // only its grep side is a search
  if (cmd === "grep" || cmd === "egrep" || cmd === "fgrep") {
    return analyzeParsed(toolLabel, parseGrep(args), pipedIn, prevChain);
  }
  if (cmd === "rg" || cmd === "ag" || cmd === "ack") {
    return analyzeParsed(toolLabel, parseRg(args, cmd), pipedIn, prevChain);
  }
  return { search: false };
}

function analyzeParsed(
  label: string,
  p: Parsed,
  pipedIn: boolean,
  prevChain: string[][],
): SearchIntent {
  if (p.listing) return { search: false };

  const isRecursive = p.recursive;
  const noTargets = p.targets.length === 0;

  if (pipedIn && noTargets) {
    // A search over a pipe: content pipes are fine, file listings are not.
    const src = pipeSource(prevChain);
    if (src.kind === "listing") {
      if (baseName(commandWords(src.words)[0] ?? "") === "find" && findDocsOnly(src.words)) {
        return { search: false };
      }
      return blocked(label, p.pattern, `${label} over a find/ls file listing`);
    }
    return { search: false };
  }

  if (noTargets) {
    // Non-recursive grep with no file reads stdin or a < redirect.
    if (!isRecursive) return { search: false };
    if (filtersAllNonCode(p.globs)) return { search: false };
    return blocked(label, p.pattern, `recursive ${label} over the working tree`);
  }

  if (p.docsOnly) return { search: false };
  if (allNonCode(p.targets)) return { search: false };

  // One named file is fine, recursive flag or not. A trailing slash is a
  // directory; `.`/`..` and globs are directories too. An extensionless
  // lone target is treated as one file (fail-open): the matcher has no fs.
  const single = p.targets.length === 1 ? cleanTarget(p.targets[0]) : undefined;
  if (
    single &&
    !isGlob(single) &&
    !single.endsWith("/") &&
    single !== "." &&
    single !== ".."
  ) {
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

/** Subcommands that do not run a CodeGraph query. */
const CG_NONWORK = new Set(["help", "--help", "-h", "--version", "-V", "-v", "--where"]);

function codeGraphArgs(words: string[]): string[] | null {
  if (words.length === 0) return null;
  const cmd = baseName(words[0]);
  if (cmd !== "cg" && cmd !== "codegraph") return null;
  return words.slice(1);
}

function firstWord(args: string[]): string | undefined {
  return args.find((a) => !a.startsWith("-"));
}

/**
 * True when the command actually runs a CodeGraph query (not `--help`,
 * `--version` or `--where`, which exit 0 without touching the index).
 */
export function runsCodeGraph(command: string): boolean {
  return segmentsOf(command).some((seg) => {
    const args = codeGraphArgs(commandWords(seg.tokens));
    if (!args) return false;
    const first = firstWord(args);
    return first !== undefined && !CG_NONWORK.has(first);
  });
}

/** True when any segment of the command invokes cg/codegraph at all. */
export function mentionsCodeGraph(command: string): boolean {
  return segmentsOf(command).some((seg) => codeGraphArgs(commandWords(seg.tokens)) !== null);
}

/** The first recursive/multi-file source search in the command, if any. */
export function searchIntent(command: string): SearchIntent {
  const segs = segmentsOf(command);
  const chain: string[][] = [];
  for (const seg of segs) {
    const words = commandWords(seg.tokens);
    const verdict = analyze(words, seg.pipedIn, chain);
    if (verdict.search) return verdict;
    chain.push(seg.tokens);
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
  // Judge segments in shell order: a search before the first CodeGraph
  // segment is blocked; a CodeGraph segment that comes first excuses the rest.
  const segs = segmentsOf(command);
  const chain: string[][] = [];
  for (const seg of segs) {
    const words = commandWords(seg.tokens);
    const cgArgs = codeGraphArgs(words);
    if (cgArgs) {
      const first = firstWord(cgArgs);
      if (first !== undefined && !CG_NONWORK.has(first)) return { block: false };
    }
    const verdict = analyze(words, seg.pipedIn, chain);
    if (verdict.search) return { block: true, reason: blockReason(verdict) };
    chain.push(seg.tokens);
  }
  return { block: false };
}
