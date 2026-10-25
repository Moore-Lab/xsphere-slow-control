# xsphere AI operator profile (template)

A Claude Code profile for a session that should operate the cryostat **only**
through the AI control service (`aicontrol/`, port 8089 on xbox-pi). Copy the
folder out of the repo, open Claude Code in the copy, run the acceptance check.

| File | Purpose |
|---|---|
| `.mcp.json` | the `xsphere` MCP server, `http://xbox-pi:8089/mcp`, loaded up front, 60 s per tool call, sending `Authorization: Bearer ${XSPHERE_AI_TOKEN}` (the operator's write token, read from the environment, never stored in the file) |
| `.claude/settings.json` | permission mode, allow / deny rules, MCP and memory restrictions (below) |
| `CLAUDE.md` | the operating rules the model reads at session start |

## Why a copy outside the repo

- Claude Code reads `.claude/settings.json` only from the session's working
  directory, never from a parent. A session opened on the repo, or in one of its
  worktrees, ignores this folder's rules completely.
- Auto memory is per git repository. Inside the repo the session would load the
  repo's memory, which explains how to SSH to the Pi and restart services.
- The working directory is also what the session may read (see
  `blockReadsOutsideWorkingDirectories` below), so it should hold nothing but
  this profile.

## Install

On the DAQ PC, from the repo root:

```powershell
robocopy aicontrol\operator C:\Users\xbox\xsphere-ai-operator /E
```

(Robocopy creates the folder the first time and overwrites changed files after
that. Its exit code 1 means "files copied", not an error.)

### The write token

Every write tool refuses a client without a write token (reads still work). The
operator's token is a secret only two places hold: the operator user's
environment on this PC, and — as a SHA-256 hash only — the Pi's
`/etc/xsphere/aicontrol.env` (`XSPHERE_AICONTROL_CLIENTS=operator:<hash>`).
Make it yourself, in your own terminal, never through a Claude session (the
token would end up in its transcript):

```powershell
python -m aicontrol.enroll token operator
```

It prints the token and the `operator:<hash>` entry for the Pi (see
`aicontrol/README.md`, "Enrolling"). Then, in a PowerShell window of the user
that runs the operator:

```powershell
setx XSPHERE_AI_TOKEN "<the token>"
```

and open a new terminal (setx only reaches new processes). `.mcp.json` expands
`${XSPHERE_AI_TOKEN}` when Claude Code starts; if the variable is missing,
Claude Code reports it and the `xsphere` server does not load. To revoke the
token, remove the `operator:` entry on the Pi and restart `xsphere-aicontrol`.

Then open Claude Code in `C:\Users\xbox\xsphere-ai-operator`:

- **Terminal (preferred)**: `cd C:\Users\xbox\xsphere-ai-operator` and run
  `claude`. For the tightest session, use
  `claude --strict-mcp-config --mcp-config .mcp.json`, which ignores every MCP
  server not in this folder's `.mcp.json`.
- **Desktop app, Code tab**: start a new local session on that folder, with no
  worktree. The desktop app adds tools of its own (browser pane, terminal,
  session control, connectors). Only the deny list keeps them out, so do the
  acceptance check there too.

The first time, accept the trust dialog for the folder. Until you do, the
`allow` rule and the `.mcp.json` approval in the settings are ignored, and the
`xsphere` tools are refused. The `deny` rules apply even before you trust it.

When the template changes, run the same robocopy again. It overwrites
`.claude/settings.json`, so add back any deny rules you added by hand.

## What the settings do

| Key | Effect |
|---|---|
| `permissions.defaultMode: "dontAsk"` | anything not pre-approved is refused without a prompt; reads inside this folder still run |
| `permissions.allow: ["mcp__xsphere__*"]` | the AI control tools are the only pre-approved tools |
| `permissions.deny` | removes shells (`Bash`, `PowerShell`, `Monitor`), file writes, web access, subagents and workflows, messaging, listing or stopping other agents (`SendMessage`, `ListAgents`, `TaskStop`), scheduling and self-paced loops (`CronCreate`, `CronDelete`, `ScheduleWakeup`, `RemoteTrigger`), skills (`Skill`), artifacts (and their comments and databases: `ArtifactComments`, `ArtifactData`) and design sync, worktrees and `/cd`, and the desktop app's browser, terminal, computer-use, widget, session, window and connector tools (each listed by its `mcp__<server>__*` prefix). `mcp__claude_ai_*` covers connectors the CLI fetches. `mcp__*-*` covers every MCP server whose name contains a hyphen, which includes the desktop app's UUID-named claude.ai connectors (Notion, Calendar, Docs); the `xsphere` tool names never contain one |
| `permissions.disableBypassPermissionsMode`, `disableAutoMode` | bypass and auto mode cannot be selected in this session |
| `permissions.blockReadsOutsideWorkingDirectories: true` | Read / Glob / Grep refuse paths outside this folder, so the repo, its `.env` and anything else on the PC stay out of reach |
| `allowedMcpServers` | only an MCP server at `http://xbox-pi:8089/…` (or `xbox-pi.local` / `192.168.8.116`) may load; user, plugin and claude.ai servers are blocked. It does not reach the desktop app's built-in servers or its connectors, which is why the deny list names them |
| `enabledMcpjsonServers: ["xsphere"]` | approves the `.mcp.json` server without a prompt (needs the folder trusted) |
| `disableClaudeAiConnectors: true` | the claude.ai connectors the CLI fetches itself are not loaded |
| `disableAllHooks: true` | your hooks and installed mods do not run. Without managed settings or a Team / Enterprise login, a mod can approve a call that a deny rule refuses |
| `crossSessionInbound: "refuse"` | messages from other Claude sessions (the desktop app's session tools, `SendMessage`) are dropped instead of arriving as a user turn. From project settings it outranks every other source. Only your own chat messages reach the operator |
| `autoMemoryEnabled: false` | no auto memory is read or written |

`Read`, `Glob` and `Grep` are not in `allow` on purpose: in `dontAsk` mode they
already work inside this folder, and an allow rule would extend them to every path.

## Acceptance check

Do this after copying the profile, after a Claude Code update, and in each
surface you use (terminal, desktop app).

1. `/status`: **Setting sources** lists Project settings.
2. `/mcp`: `xsphere` is connected. In the terminal it is the only server.
3. Ask: *run hostname*. It must be refused.
4. Ask: *open http://xbox-pi:8088/control*. It must be refused.
5. Ask: *list the exact names of every tool you can call*. Expected:
   `mcp__xsphere__*` plus harmless built-ins such as `Read`, `Glob`, `Grep`,
   `ToolSearch`, `CronList`, `PushNotification`, `ExitWorktree` (and, in some
   hosts, `ReportFindings`). `Skill`, `ListAgents` and `ScheduleWakeup` must
   not be there. If any tool not in this list appears, MCP or built-in, add it
   to `deny` in the copy (for a server, `"mcp__<server>__*"`), or disconnect
   that connector, before relying on the profile.
6. Ask for `get_ai_control`: `you_are` must read `AI client operator` (the
   token arrived). Then, with nothing armed, ask for a heater setpoint. The tool
   must refuse it: "Heaters are not armed for AI control". Without
   `XSPHERE_AI_TOKEN` set, the same request is refused with "no valid write
   token".
7. From another Claude session (a dev session in the desktop app), send this
   session a message. It must not arrive.

Verified on 2026-10-22 with Claude Code 2.1.299 in the terminal: the session
started in `dontAsk`; the built-in tool list matched step 5; a server not in
`allowedMcpServers` was never contacted; and the tools of a hyphen-named server
and of a server named `visualize` were removed. Step 6 (a real `tools/call`
against the service) was not reached then: the test session's login had
expired — run it at the first deploy and note the date here. The desktop app
was not checked; run steps 1–6 there before relying on it. The deny list
already names its cross-session tools (`SendMessage`, `Artifact`,
`ArtifactComments`, `ArtifactData`, the `ccd_session*` servers).

## What this does not do

- It confines **this** session only. Your other sessions (bypass mode, SSH,
  browser) can still do anything a user on this PC can, and so can anyone on
  the lab network: the MQTT broker is anonymous, and the Control page's
  `/api/cmd` relays commands. What they can no longer do is arm the AI or save
  rails (both need your one-time code) or make AI writes (they need a write
  token, and an arm for that client). A session running as the same Windows
  user can still read `XSPHERE_AI_TOKEN` from the environment; see "Safety
  model" in `aicontrol/README.md`.
- You can still change the permission mode from the session's UI. Deny rules
  hold in every mode, and bypass and auto are disabled.
- Arming, disarming and the rails are yours, on the Control page. The operator
  has no tool for them.
