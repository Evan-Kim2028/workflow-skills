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
 * State is per session: the first bash command that runs `cg`/`codegraph`, or
 * a call to a tool whose name contains `codegraph`, turns the gate off. After
 * three blocks the gate opens for the rest of the session (safety valve).
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { execFile } from "node:child_process";
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

/** True when `cg --where` exits 0 in `cwd`. Never throws. */
function probe(cwd: string): Promise<boolean> {
  return new Promise((resolve) => {
    try {
      execFile(
        cgBinary(),
        ["--where"],
        { cwd, timeout: PROBE_TIMEOUT_MS },
        (err: unknown) => resolve(!err),
      );
    } catch {
      resolve(false);
    }
  });
}

export default function (pi: ExtensionAPI) {
  let state: State = freshState();
  let ready: Promise<void> = Promise.resolve();

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
    await ready;
  });

  pi.on("tool_call", async (event: any) => {
    try {
      await ready;
      if (!state.active) return undefined;

      const name = typeof event?.toolName === "string" ? event.toolName : "";
      // The MCP tool (mcp__codegraph__*) counts as CodeGraph use.
      if (name.toLowerCase().includes("codegraph")) {
        state.hasRunCodeGraph = true;
        return undefined;
      }
      if (name !== "bash") return undefined;

      const command = typeof event?.input?.command === "string" ? event.input.command : "";
      if (!command) return undefined;

      if (runsCodeGraph(command)) {
        state.hasRunCodeGraph = true;
        return undefined;
      }

      // Safety valve: after MAX_BLOCKS blocks the gate opens for the session.
      if (state.blocks >= MAX_BLOCKS) return undefined;

      const decision = gateDecision(command, state.hasRunCodeGraph);
      if (!decision.block) return undefined;
      state.blocks += 1;
      return { block: true, reason: decision.reason };
    } catch {
      return undefined;
    }
  });

  pi.on("before_agent_start", async (event: any) => {
    try {
      await ready;
      if (!state.active) return undefined;
      const prompt = typeof event?.systemPrompt === "string" ? event.systemPrompt : "";
      if (prompt.includes(PROMPT_RULE)) return undefined;
      return { systemPrompt: prompt.length > 0 ? `${prompt}\n\n${PROMPT_RULE}` : PROMPT_RULE };
    } catch {
      return undefined;
    }
  });
}
