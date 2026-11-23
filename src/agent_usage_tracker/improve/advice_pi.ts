// Managed by AUT session advice.
import { execFileSync } from "node:child_process";

const executable = __AUT_EXECUTABLE__;
const mode = __AUT_MODE__;

export default function (pi: any) {
  pi.on("tool_result", async (event: any, ctx: any) => {
    if (event.toolName !== "bash" || event.parentToolCallId ||
        typeof event.input?.command !== "string" ||
        !/\bpytest\b/.test(event.input.command)) return;
    try {
      const payload = JSON.stringify({
        hook_event_name: "tool_result",
        session_id: ctx.sessionManager.getSessionId(),
        cwd: ctx.cwd,
        tool_name: "Bash",
        tool_use_id: event.toolCallId,
        tool_input: { command: event.input.command },
        tool_response: event.content,
      });
      if (Buffer.byteLength(payload) > 512 * 1024) return;
      const output = execFileSync(executable, ["advise", "hook", "--client", "pi", "--mode", mode], {
        input: payload,
        encoding: "utf-8",
        timeout: 5000,
        stdio: ["pipe", "pipe", "ignore"],
      });
      const advice = JSON.parse(output).additional_context;
      if (typeof advice === "string" && advice) {
        await pi.sendMessage(
          { customType: "aut-session-advice", content: advice, display: true },
          { deliverAs: "steer", triggerTurn: false },
        );
      }
    } catch {
      // Advice is optional. Leave tool execution and its result alone.
    }
  });
}
