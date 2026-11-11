/**
 * cg-gate: block recursive/multi-file code search until CodeGraph has run once.
 *
 * A pi extension. The matching rule is the pure function `gateDecision` in
 * ./search-rule.ts, table-tested by tests/test_pi_cg_gate.py.
 *
 * The gate is active only where `cg --where` finds an index (exit 0). It is
 * inactive when the binary is missing, the probe times out or exits non-zero,
 * and when PI_CG_GATE=off. Every handler fails open: an exception never blocks
 * a tool call and never breaks the session.
 *
 * The probe is bounded: it is killed with SIGKILL and raced against its own
 * timer, so a child that ignores SIGTERM cannot keep a handler waiting. If the
 * probe has not settled by the bound, the gate is treated as inactive.
 *
 * CodeGraph counts as used only after a successful tool result: a bash result
 * that is not an error and whose command runs a cg query, or a successful
 * CodeGraph MCP result. `cg --help`, `cg --where` and a failed call do not
 * open the gate. If the result event is unavailable or throws, the call itself
 * marks use (fail-open).
 *
 * State is per extension runtime, not persisted: a resume or reload starts
 * fresh, so the three-block valve and the CodeGraph-used flag reset. That is
 * deliberate; the gate is a nudge, not a boundary.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn } from "node:child_process";
import { gateDecision, runsCodeGraph } from "./search-rule.ts";

/**
 * Appended to the system prompt when the gate is active. Byte-identical on
 * every call of every session (no paths, dates or counters) so it does not
 * break prompt caching.
 */
export const PROMPT_RULE = [
  'For code search, call `cg` first: `cg explore "<question>"` returns the relevant symbols, their source and call paths in one call; `cg node <symbol|file>`, `cg callers <symbol>` and `cg impact <symbol>` drill in. cg works from a worktree too: it reads the main checkout\'s index.',
  "Use grep only for non-code text (docs, YAML, JSON, logs, Markdown) or to confirm one detail in one named file.",
].join("\n");

const MAX_BLOCKS = 3;
const PROBE_TIMEOUT_MS = 5000;
/** Handlers never wait on the probe longer than this. */
const WAIT_BOUND_MS = PROBE_TIMEOUT_MS + 1500;

type State = {
  active: boolean;
  hasRunCodeGraph: boolean;
  blocks: number;
};

function freshState(): State {
  return { active: false, hasRunCodeGraph: false, blocks: 0 };
}

function cgBinary(): string {
  const fromEnv = process.env.PI_CG_GATE_CG;
  return fromEnv && fromEnv.length > 0 ? fromEnv : "cg";
}

/**
 * True when `cg --where` exits 0 in `cwd`. Never throws and always settles
 * within PROBE_TIMEOUT_MS: the child is killed with SIGKILL (its whole process
 * group) and the promise is raced against its own timer, so a child that
 * ignores SIGTERM cannot keep it pending.
 */
export function probe(cwd: string): Promise<boolean> {
  return new Promise((resolve) => {
    let settled = false;
    let child: ReturnType<typeof spawn> | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const finish = (ok: boolean) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      if (child?.pid) {
        try {
          process.kill(-child.pid, "SIGKILL");
        } catch {
          /* group already gone */
        }
        try {
          child.kill("SIGKILL");
        } catch {
          /* already gone */
        }
      }
      resolve(ok);
    };
    timer = setTimeout(() => finish(false), PROBE_TIMEOUT_MS);
    try {
      child = spawn(cgBinary(), ["--where"], {
        cwd,
        stdio: "ignore",
        detached: true,
      });
      child.on("error", () => finish(false));
      child.on("exit", (code) => finish(code === 0));
    } catch {
      finish(false);
    }
  });
}

/** Resolve `p`, or `fallback` after `ms`. Never rejects. */
function withBound<T>(p: Promise<T>, fallback: T, ms: number): Promise<T> {
  return new Promise((resolve) => {
    let done = false;
    const timer = setTimeout(() => {
      if (done) return;
      done = true;
      resolve(fallback);
    }, ms);
    p.then(
      (value) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        resolve(value);
      },
      () => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        resolve(fallback);
      },
    );
  });
}

export default function (pi: ExtensionAPI) {
  let state: State = freshState();
  let ready: Promise<void> = Promise.resolve();
  let resultHook = true;

  /** True when the probe has settled and the gate is active. */
  const gateReady = async (): Promise<boolean> => {
    const settled = await withBound(ready.then(() => true), false, WAIT_BOUND_MS);
    return settled && state.active;
  };

  pi.on("session_start", async (_event: any, ctx: any) => {
    state = freshState();
    ready = (async () => {
      try {
        if (process.env.PI_CG_GATE === "off") return;
        const cwd =
          typeof ctx?.cwd === "string" && ctx.cwd.length > 0 ? ctx.cwd : process.cwd();
        state.active = await probe(cwd);
      } catch {
        state.active = false;
      }
    })();
    await withBound(ready.then(() => true), false, WAIT_BOUND_MS);
  });

  // Mark CodeGraph as used from the result, not the request: a failed call or
  // `cg --help` must not open the gate.
  try {
    pi.on("tool_result", async (event: any) => {
      try {
        const name = typeof event?.toolName === "string" ? event.toolName : "";
        if (event?.isError) return undefined;
        if (name.toLowerCase().includes("codegraph")) {
          state.hasRunCodeGraph = true;
          return undefined;
        }
        if (name === "bash") {
          const command =
            typeof event?.input?.command === "string" ? event.input.command : "";
          if (command && runsCodeGraph(command)) state.hasRunCodeGraph = true;
        }
        return undefined;
      } catch {
        // Fail-open: a CodeGraph call whose result we cannot read still counts.
        state.hasRunCodeGraph = true;
        return undefined;
      }
    });
  } catch {
    // No tool_result event in this runtime: fall back to marking on the call.
    resultHook = false;
  }

  pi.on("tool_call", async (event: any) => {
    try {
      if (!(await gateReady())) return undefined;

      const name = typeof event?.toolName === "string" ? event.toolName : "";
      const isMcpCodeGraph = name.toLowerCase().includes("codegraph");
      if (isMcpCodeGraph) {
        if (!resultHook) state.hasRunCodeGraph = true;
        return undefined;
      }
      if (name !== "bash") return undefined;

      const command = typeof event?.input?.command === "string" ? event.input.command : "";
      if (!command) return undefined;

      if (state.hasRunCodeGraph) return undefined;

      // Safety valve: after MAX_BLOCKS blocks the gate opens for the runtime.
      if (state.blocks >= MAX_BLOCKS) return undefined;

      const decision = gateDecision(command, state.hasRunCodeGraph);
      if (!decision.block) {
        if (!resultHook && runsCodeGraph(command)) state.hasRunCodeGraph = true;
        return undefined;
      }
      state.blocks += 1;
      return { block: true, reason: decision.reason };
    } catch {
      return undefined;
    }
  });

  pi.on("before_agent_start", async (event: any) => {
    try {
      if (!(await gateReady())) return undefined;
      const prompt = typeof event?.systemPrompt === "string" ? event.systemPrompt : "";
      if (prompt.includes(PROMPT_RULE)) return undefined;
      return { systemPrompt: prompt.length > 0 ? `${prompt}\n\n${PROMPT_RULE}` : PROMPT_RULE };
    } catch {
      return undefined;
    }
  });
}
