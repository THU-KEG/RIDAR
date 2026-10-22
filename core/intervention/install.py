"""Write what the audited agent needs to reach the Supervisor: a stdlib hook client and, per
framework, the command-line additions that install the hooks.

- Claude Code: a generated settings file containing ``Stop`` and ``PostToolBatch`` hooks, passed
  with ``--settings``.
- Codex: ``-c hooks.Stop=...`` and an asynchronous ``-c hooks.PostToolUse=...`` override plus
  ``--dangerously-bypass-hook-trust``; nothing is written into the workspace or the user's home.
  The PostToolUse hook must be ``async``: a synchronous one places its context before the tool
  output and Responses-API gateways reject the request; the asynchronous one is delivered before
  the next model request.
- OpenCode: a plugin file whose ``tool.execute.after`` asks the Supervisor and appends its answer to
  the tool output, made visible through ``OPENCODE_CONFIG_DIR``. There is no Stop equivalent:
  ``opencode run`` exits when its turn ends, so the barrier relaunches it with ``--session`` (see
  ``core.detector.online.run``).

All three reach the Supervisor through the run directory's file queue (``monitor/hook-spool``,
see ``core.intervention.supervisor``): a request is written under a temporary name and renamed
into place, the answer is polled for. The run directory is mounted into the agent container at the
same absolute path, so no network is involved.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HOOK_CLIENT = '''#!/usr/bin/env python3
"""Ask the Supervisor about one hook event (stdin JSON) through the run's file queue and print its
answer. Fails open: any error prints an empty decision and exits 0, so the agent is never blocked
by the monitor's fault."""
import json, os, sys, time, uuid
spool, token = sys.argv[1], sys.argv[2]
timeout = float(sys.argv[3]) if len(sys.argv) > 3 else 900.0
base = os.path.join(spool, uuid.uuid4().hex)
req, ans = base + ".req", base + ".ans"
try:
    event = json.loads(sys.stdin.read() or "{}")
    with open(req + ".tmp", "w") as handle:
        json.dump({"token": token, "event": event}, handle)
    os.replace(req + ".tmp", req)  # visible to the Supervisor only once complete
    deadline = time.monotonic() + timeout
    while not os.path.exists(ans) and time.monotonic() < deadline:
        time.sleep(0.05)
    with open(ans) as handle:
        answer = handle.read()
    json.loads(answer)
    sys.stdout.write(answer)
except Exception:
    sys.stdout.write("{}")
finally:
    for path in (req + ".tmp", req, ans):
        try:
            os.unlink(path)
        except OSError:
            pass
'''

PROCESS_HOOK_TIMEOUT = 30.0
# What the OpenCode plugin prepends to a repair request appended to a tool output; the feeder strips
# the tool result from this marker on.
SUPERVISOR_MARKER = "\n\n[supervisor] "

OPENCODE_PLUGIN = """// Written by ridar runtime --intervene. Asks the Supervisor through the run's file queue and, in the
// "context" channel, appends a due repair request to the tool output after each tool call; in the
// "deny" channel, refuses the next tool call once by throwing the repair request as the tool's error.
// Fails open on any error of its own.
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
const SPOOL = %(spool)s;
const TOKEN = %(token)s;
const MARKER = %(marker)s;
const TIMEOUT_MS = %(timeout_ms)d;
const CHANNEL = %(channel)s;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function ask(event) {
  const base = path.join(SPOOL, crypto.randomUUID().replace(/-/g, ""));
  const req = base + ".req", ans = base + ".ans";
  try {
    fs.writeFileSync(req + ".tmp", JSON.stringify({token: TOKEN, event}));
    fs.renameSync(req + ".tmp", req);
    const deadline = Date.now() + TIMEOUT_MS;
    while (!fs.existsSync(ans) && Date.now() < deadline) await sleep(50);
    return JSON.parse(fs.readFileSync(ans, "utf8"));
  } catch (error) {
    return {};
  } finally {
    for (const p of [req + ".tmp", req, ans]) { try { fs.unlinkSync(p); } catch (error) {} }
  }
}
export const Supervisor = async () => (CHANNEL === "deny" ? {
  "tool.execute.before": async (input, output) => {
    const answer = await ask({hook_event_name: "PreToolUse", tool_name: input.tool, session_id: input.sessionID});
    const out = answer && answer.hookSpecificOutput;
    if (out && out.permissionDecision === "deny" && out.permissionDecisionReason) throw new Error(out.permissionDecisionReason);
  },
} : {
  "tool.execute.after": async (input, output) => {
    const answer = await ask({hook_event_name: "PostToolUse", tool_name: input.tool, session_id: input.sessionID});
    const text = answer && answer.hookSpecificOutput && answer.hookSpecificOutput.additionalContext;
    if (text && output && typeof output.output === "string") output.output += MARKER + text;
  },
});
"""


@dataclass
class Installation:
    """Arguments and environment variables RIDAR adds to the agent command."""

    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


# How each framework's barrier is driven: a blocking Stop hook, or relaunching the exited process.
BARRIER_MODE = {"claude-code": "hook", "codex": "hook", "opencode": "relaunch"}


def write_hook_client(directory: Path) -> Path:
    path = directory / "hook_client.py"
    path.write_text(HOOK_CLIENT, encoding="utf-8")
    return path


def _client_command(hook_client: Path, spool: Path, token: str, timeout: float) -> str:
    # The client outlives the Supervisor's own wait so a decision made at the deadline still arrives.
    # ``python3`` from the agent's own PATH, not this process's interpreter: the agent may run inside
    # a container where only its own environment exists. Paths are quoted for spaces.
    return f"python3 {shlex.quote(str(hook_client))} {shlex.quote(str(spool))} {shlex.quote(token)} {timeout + 30:g}"


def claude_code_settings(*, hook_client: Path, spool: Path, token: str, timeout: float, process_channel: str = "context") -> dict[str, Any]:
    """Generated Supervisor hooks. ``Stop`` is the completion barrier. The process channel is either
    ``PostToolBatch`` (findings added as context after
    a tool batch) or, with ``process_channel="deny"``, ``PreToolUse`` on every tool (the next call is
    refused once with the findings as the reason); never both, so a finding is delivered one way."""
    settings: dict[str, Any] = {"hooks": {}}
    hooks = settings["hooks"]
    hooks["Stop"] = [{"hooks": [{"type": "command", "command": _client_command(hook_client, spool, token, timeout), "timeout": int(timeout) + 60}]}]
    process = [{"hooks": [{"type": "command", "command": _client_command(hook_client, spool, token, PROCESS_HOOK_TIMEOUT), "timeout": int(PROCESS_HOOK_TIMEOUT) + 30}]}]
    if process_channel == "deny":
        hooks["PreToolUse"] = process
        hooks.pop("PostToolBatch", None)
    else:
        hooks["PostToolBatch"] = process
        hooks.pop("PreToolUse", None)
    return settings


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def codex_overrides(*, hook_client: Path, spool: Path, token: str, timeout: float, process_channel: str = "context") -> list[str]:
    """Command-line additions for ``codex exec``: the Stop hook (barrier) and the process channel as
    config overrides: an asynchronous PostToolUse hook adding context (``context``), or a synchronous
    PreToolUse hook on every tool that refuses the next call once with the findings as the reason
    (``deny``); Codex's hook output schema for both is the same as Claude Code's."""
    stop = _toml_string(_client_command(hook_client, spool, token, timeout))
    post = _toml_string(_client_command(hook_client, spool, token, PROCESS_HOOK_TIMEOUT))
    if process_channel == "deny":
        process = f"hooks.PreToolUse=[{{hooks=[{{type=\"command\",command={post},timeout={int(PROCESS_HOOK_TIMEOUT) + 60}}}]}}]"
    else:
        process = f"hooks.PostToolUse=[{{matcher=\"Bash|apply_patch\",hooks=[{{type=\"command\",command={post},async=true,timeout={int(PROCESS_HOOK_TIMEOUT) + 60}}}]}}]"
    return [
        "--dangerously-bypass-hook-trust",
        "-c", f"hooks.Stop=[{{hooks=[{{type=\"command\",command={stop},timeout={int(timeout) + 60}}}]}}]",
        "-c", process,
    ]


def opencode_plugin(*, spool: Path, token: str, process_channel: str = "context") -> str:
    return OPENCODE_PLUGIN % {"spool": json.dumps(str(spool)), "token": json.dumps(token), "marker": json.dumps(SUPERVISOR_MARKER),
                              "timeout_ms": int(PROCESS_HOOK_TIMEOUT * 1000), "channel": json.dumps(process_channel)}


def install_agent_hooks(
    adapter: str,
    out_dir: Path,
    *,
    spool: Path,
    token: str,
    timeout: float,
    process_channel: str = "context",
) -> Installation:
    """Write the hook client and, per framework, the settings file or plugin under ``out_dir``;
    return the arguments and environment the runtime injects into the agent command.
    ``process_channel`` is ``context`` (findings added after a tool call) or ``deny`` (the next tool
    call is refused once with the findings as the reason) for all three frameworks."""
    if adapter not in BARRIER_MODE:
        raise ValueError(f"intervention is not implemented for adapter {adapter!r}")
    out_dir.mkdir(parents=True, exist_ok=True)
    if adapter == "opencode":
        plugins = out_dir / "opencode" / "plugins"
        plugins.mkdir(parents=True, exist_ok=True)
        (plugins / "supervisor.js").write_text(opencode_plugin(spool=spool, token=token, process_channel=process_channel), encoding="utf-8")
        return Installation(env={"OPENCODE_CONFIG_DIR": str(out_dir / "opencode")})
    client = write_hook_client(out_dir)
    if adapter == "codex":
        return Installation(args=codex_overrides(hook_client=client, spool=spool, token=token, timeout=timeout, process_channel=process_channel))
    settings = claude_code_settings(hook_client=client, spool=spool, token=token, timeout=timeout, process_channel=process_channel)
    path = out_dir / "agent-settings.json"
    path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return Installation(args=["--settings", str(path)])
