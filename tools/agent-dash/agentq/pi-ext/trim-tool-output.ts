/**
 * agentq pi extension: keep long bash output out of the conversation.
 *
 * Every tool result stays in the context and is re-sent on every later turn, and bash output
 * is about 69% of a worker's conversation. A result longer than AGENTQ_TOOL_OUTPUT_CAP
 * characters (default 8000) is replaced by its first 30 lines and its tail, with a note naming
 * the file that holds the full output so the worker can grep or tail it.
 * AGENTQ_TOOL_OUTPUT_CAP=0 turns the trim off.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const HEAD_LINES = 30;
const NOTE_ROOM = 400;

function cap(): number {
  const raw = Number(process.env.AGENTQ_TOOL_OUTPUT_CAP ?? "8000");
  return Number.isFinite(raw) && raw >= 0 ? raw : 8000;
}

function saveFull(id: string, text: string): string {
  try {
    const dir = join(process.env.AGENT_SCRATCH || tmpdir(), "tool-output");
    mkdirSync(dir, { recursive: true });
    const path = join(dir, `${id.replace(/[^A-Za-z0-9_-]/g, "_")}.txt`);
    writeFileSync(path, text);
    return path;
  } catch {
    return "";
  }
}

export function trimText(full: string, limit: number, path: string): string {
  const lines = full.split("\n");
  const head = lines.slice(0, HEAD_LINES).join("\n");
  const room = Math.max(0, limit - head.length - NOTE_ROOM);
  let tail = room > 0 ? full.slice(full.length - room) : "";
  const cut = tail.indexOf("\n");
  if (cut >= 0 && cut < 200) tail = tail.slice(cut + 1);
  const where = path ? ` Full output: ${path} (grep or tail it).` : "";
  return `${head}\n\n[agentq: output trimmed from ${full.length} to about ${limit} characters, ${lines.length} lines, to save context.${where}]\n\n${tail}`;
}

export default function (pi: ExtensionAPI) {
  pi.on("tool_result", async (event) => {
    const limit = cap();
    if (event.toolName !== "bash" || limit === 0) return undefined;
    const texts = event.content.filter((part) => part.type === "text") as { type: "text"; text: string }[];
    const full = texts.map((part) => part.text).join("");
    if (full.length <= limit) return undefined;
    const path = saveFull(event.toolCallId, full);
    const others = event.content.filter((part) => part.type !== "text");
    return {
      content: [{ type: "text", text: trimText(full, limit, path) }, ...others],
      details: event.details,
      isError: event.isError,
    };
  });
}
