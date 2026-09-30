# crew

An agent skill that lets the coding agent you're working with hand a bounded task to a different
vendor's CLI agent:

- **OpenAI Codex CLI** (`codex`)
- **Anthropic Claude Code** (`claude`)
- **Google Antigravity CLI** (`agy`, Gemini models)

It works from any of them. Claude Code can ask Codex to review a diff, Codex can ask Claude, and
either can send web research to Gemini.

Why: a reviewer from a different model family catches different bugs, and spreading work across
the subscriptions you already pay for stretches each one further.

Each task:

- gets a **permission scope chosen per call** (file access, web, network). crew turns it into
  each backend's own controls (sandbox modes, tool lists, settings) and records how strictly each
  part is enforced;
- runs in its **own process**, optionally in a visible terminal tab so you can watch;
- is **recorded on disk**: the exact task text, the command line, the output, the final answer,
  changed files, and a usage ledger.

The skill only acts when you ask ("/crew", "have codex review this", "get claude's opinion on
this", "ask gemini to look this up"). It never dispatches on its own.

## How it works

crew is one Python script that your agent runs as an ordinary shell command. It starts another
vendor's CLI agent with a single task and a chosen permission scope, waits for it, and hands back
the answer. There is no server or background service.

### The parts

| Part | What it is |
|---|---|
| `skills/crew/SKILL.md` | What your agent reads when you ask to delegate: when to use crew, which command to run, how to choose a scope, how to check results. |
| `skills/crew/scripts/crew.py` | The whole program: the command-line tool your agent calls, and the runner that looks after each delegated agent. |
| `skills/crew/assets/config.default.json` | Built-in agents and roles. Your overrides go in `~/.crew/config.json`. |
| `skills/crew/references/` | Extra guidance your agent reads only when needed: writing tasks, checking results, permission details. |
| `~/.crew/` | Your config, one folder per run, and a usage ledger. |

### One request, start to finish

Example: in Claude Code you type *"/crew have codex review my changes"*.

1. **Your agent writes the command.** Following SKILL.md it runs something like
   `crew.py run --caller claude --role review --cd . --wait`.
2. **crew picks the agent.**
   - The `review` role lists `["codex-strong", "claude-strong"]` with scope `verify`.
   - crew knows who's calling from `--caller`, or from environment clues like `CLAUDECODE` or
     `CODEX_THREAD_ID`, and skips that vendor, so here it picks `codex:gpt-6.1-sol@high`.
   - If Codex weren't installed, the next installed candidate would be used. If only the caller's
     own vendor were left, crew would use it and say so.
   - `--agent` overrides all of this.
3. **crew works out the scope**: access (`read` / `verify` / `write`), web (on/off), network
   (on/off). The role gives defaults and flags override them for this call. crew translates the
   scope into the chosen CLI's own controls:
   - **codex**: sandbox mode (`read-only` / `workspace-write`), web search on/off, sandbox network
     on/off, and the task's scratch folder as an extra writable folder.
   - **claude**: an exact tool list (read: `Read, Glob, Grep`; verify adds the shell; write adds
     `Edit, Write`; web adds `WebSearch, WebFetch`), a permission mode that denies anything not
     pre-approved, and a per-task settings file (sandbox rules on macOS/Linux). MCP servers,
     skills and your personal Claude settings are switched off.
   - **agy**: accept-edits mode for write. agy has no per-call permission switches, so its
     read-only mode is instructions only, and crew refuses `verify` because agy can't run commands
     headless.

   crew records whether each part is enforced **hard** (sandbox or tool list) or is **advisory**
   (instructions plus a check afterwards), and prints this at launch.
4. **crew writes the run folder**, `~/.crew/runs/<time>-<role>-<name>/`:
   - `task.md`: exactly what will be sent. That's a scope block written by crew ("read-only: do
     not create…", "do not use web search…"), then any role text, then your task.
   - `cmd.json`: the exact command line, working directory, environment additions, scope and
     enforcement.
   - an empty `scratch/` folder the agent may write to.

   For reviews: with no task text, codex's built-in reviewer runs. Otherwise the agent is told
   which git command shows the diff, and agents that can't run git get the diff pasted in.
5. **crew starts a runner and returns.** The runner (`crew.py _run`) opens in a visible terminal
   tab if one is available, otherwise as a hidden background process. It is detached from your
   agent's shell, so the task survives if your agent's command is cut off. If a tab doesn't
   actually start within 15 seconds, crew runs the task hidden instead, and a lock file stops a
   late tab from running it twice.
6. **The runner does the work:**
   1. writes a heartbeat file every 5 seconds;
   2. snapshots `git status`, with a content hash of every dirty or untracked file;
   3. starts the vendor CLI with a cleaned environment. The caller's session variables are removed,
      and a marker is added that stops the delegated agent from starting crew tasks of its own;
   4. feeds it the task and shows a compact live view (one line per command or tool call), saving
      the full event stream;
   5. kills the agent if it runs past `max_minutes` (default 60);
   6. snapshots git again. For `read`/`verify`, any changed repository file is a violation. For
      `write`, the changed files are listed for checking;
   7. writes `final.md` (the answer), `result.json` (status, time, tokens, cost, session id,
      changed files, violations, denied tools) and a line in `~/.crew/ledger.jsonl`.
7. **`--wait` reports back.** It watches for `result.json`, and uses the heartbeat to notice a
   runner that died. Then it prints the status, scope, run folder, any violations, and the
   agent's final answer. That is what your agent reads.
8. **Your agent checks the result.** SKILL.md tells it to treat the answer as a claim: verify
   findings against the code, run the project's checks itself after an implementation, tell you
   what it accepted or rejected, and commit itself (the delegated agent never commits). Fixes go
   back with `--resume <run folder>`, which continues the same Codex or Claude session.

### What it deliberately doesn't do

- It doesn't run by itself; your agent calls it only when you ask.
- It doesn't commit, push or touch any remote.
- It doesn't let a delegated agent delegate further.
- It doesn't claim protection it can't enforce. agy's read-only mode, and Claude's shell on native
  Windows, are only watched by the git check, and crew labels them advisory wherever they apply.

## Requirements

- Python 3.9+ (standard library only) and git.
- At least one backend CLI other than the agent you're using, installed and signed in (see
  [Backend setup](#backend-setup)).
- Optional: a terminal crew can open tabs in (see [Visible terminals](#visible-terminals)).
  Without one, tasks run hidden and you read their logs.

Platform status: developed and used on **Windows 11**. The test suite, which uses stand-in
agents, passes on **Windows and Linux**. **macOS** (Terminal.app tabs, claude's sandbox) is
written but untested; reports welcome.

## Install

Copy `skills/crew` into the skills directory of each agent you want to call it from. Copy it
rather than symlink, since some agents don't follow links.

| Agent | Skills directory |
|---|---|
| Claude Code | `~/.claude/skills/` (all projects) or `<project>/.claude/skills/` |
| Codex CLI | `~/.agents/skills/` (all projects) or `<project>/.agents/skills/` |
| Others | see your agent's docs for where it loads `SKILL.md` skills from |

macOS / Linux:

```sh
git clone https://github.com/lphilbrooks/crew.git
mkdir -p ~/.claude/skills ~/.agents/skills
cp -R crew/skills/crew ~/.claude/skills/
cp -R crew/skills/crew ~/.agents/skills/
```

Windows (PowerShell):

```powershell
git clone https://github.com/lphilbrooks/crew.git
foreach ($d in "$HOME\.claude\skills", "$HOME\.agents\skills") {
  New-Item -ItemType Directory -Force $d | Out-Null
  Copy-Item -Recurse crew\skills\crew "$d\"
}
```

If a `crew` folder already exists there, look at it and back it up before replacing it. To update,
pull and copy again. Your settings live in `~/.crew/`, not in the skill folder.

Then check everything:

```sh
python3 ~/.claude/skills/crew/scripts/crew.py doctor     # Windows: python ... or py -3 ...
```

`doctor` checks:

- Python and git;
- each backend: installed, version, signed in;
- claude's `--restricted` support and sandbox availability;
- agy's web permission;
- whether every role's model exists;
- which terminal crew can open.

## Backend setup

Install whichever backends you want to delegate to. Roles list several agents in order and use
the first installed one that isn't the vendor you're calling from.

### Codex CLI (OpenAI)

1. Install: `npm install -g @openai/codex` (other methods:
   [github.com/openai/codex](https://github.com/openai/codex)).
2. Sign in: `codex login` (ChatGPT plan or API key).
3. **Windows only:** enable codex's Windows sandbox once (see the codex docs on Windows). The
   setting lives in `~/.codex/config.toml`; the elevated mode needs a one-time admin approval:

   ```toml
   [windows]
   sandbox = "elevated"
   ```

   Without a working sandbox, commands in `verify`/`write` tasks are blocked.

**Permissions:** nothing to configure. crew passes the sandbox mode, network and web-search
settings on every call.

### Claude Code (Anthropic)

1. Install: see [the Claude Code docs](https://code.claude.com/docs/en/setup)
   (native installer, or `npm install -g @anthropic-ai/claude-code`).
2. Sign in: `claude auth login` (Claude plan or API key). Check with `claude auth status`.
3. **macOS / Linux / WSL2:** make sure Claude Code's shell sandbox works. On Linux it needs
   `bubblewrap` and `socat`. crew requires the sandbox for `verify`/`write` tasks and fails the
   task rather than run commands unsandboxed.
4. **Native Windows:** Claude Code has no shell sandbox there. crew still limits Claude's tools
   exactly, but shell commands in `verify`/`write` tasks run with your normal rights, backed only
   by instructions and crew's git check. `doctor` warns about this.

**Permissions:** nothing to configure. On each call crew passes an exact tool list, a permission
mode and a per-task settings file (sandbox and allow rules). With `--restricted` (recent Claude
Code versions) your own Claude settings files are ignored for delegated runs. MCP servers and
skills are always switched off for them.

### Antigravity CLI (Google)

1. Install the Antigravity CLI from [antigravity.google](https://antigravity.google) and make
   sure `agy` is on your PATH (`agy --version`).
2. Sign in: run `agy` once interactively and complete the login.
3. **Permissions:** agy reads permissions from its settings file, not from command-line flags,
   and in headless mode it silently denies anything not pre-approved. Web *search* works out of
   the box, but *reading a web page* needs an allow rule. Either let crew add it (it backs the file
   up first):

   ```sh
   python3 crew.py setup-agy-web
   ```

   or add it yourself to `~/.gemini/antigravity-cli/settings.json`:

   ```json
   { "permissions": { "allow": ["read_url(*)"] } }
   ```

   Only exactly `read_url(*)` works; `read_url` and `read_url(regex:.*)` don't match. Without the
   rule, research still runs but is told to work from search results only. Leave terminal commands
   un-allowed: crew never asks agy to run commands.

> Gemini CLI (`gemini`) is not supported. Google has moved individual users to Antigravity, and
> personal Google sign-in no longer works with Gemini CLI.

### Calling crew from Codex (or any sandboxed agent)

Codex runs its shell commands in a sandbox. crew has to run outside it: it writes to `~/.crew`,
and the agents it starts need the network and your logins. On Windows, codex's sandbox also runs
commands as a separate user that has none of your logins. When Codex asks to run `crew.py`
unsandboxed (escalated), approve it. SKILL.md tells the agent to ask for this.

## Use

Ask your agent in plain words, or run `/crew`:

```text
/crew have codex review my uncommitted changes          (from Claude Code)
/crew get claude to review this branch against main      (from Codex)
/crew get a second opinion on this migration plan
/crew ask gemini for the current stable version of <library>, with sources
/crew delegate the test boilerplate for parser.py, then check it before I commit
```

The agent turns that into a command like these. You can also run them yourself:

```sh
crew.py run --caller claude --role review --cd . --wait            # codex's built-in review of uncommitted changes
crew.py run --caller codex --role review --target base:main --task "Focus on error handling" --wait   # claude reviews
crew.py run --role check --task-file plan.md --cd . --wait
crew.py run --role implement --task-file todo.md --cd . --network --wait
crew.py run --resume <taskdir> --task "Use the existing helper in util.py" --wait   # follow-up, same session
crew.py run --role research --task "Latest stable <library> version and its changelog URL" --wait
crew.py status        # recent tasks
crew.py stats         # usage per backend/model
crew.py collect <dir> # wait for / reprint a task
```

`--caller` names the agent running crew, so a role skips that vendor. crew detects Claude Code and
(usually) Codex by itself; the flag makes it certain. `--agent` picks an exact agent and always
wins.

## Permissions

The calling agent sets the scope on every call. Role defaults fill in anything it leaves out.

| Flag | Meaning |
|---|---|
| `--access read` | may not change any file |
| `--access verify` | may build and run tests; scratch writes go to a per-task scratch dir; any repository change is reported as a violation |
| `--access write` | may edit the working tree (never commits); every changed file is listed |
| `--web` / `--no-web` | web search and page reading |
| `--network` / `--no-network` | network for commands the agent runs (installing test deps); codex/claude, verify/write only |

How each is enforced:

| | codex | claude (macOS / Linux / WSL2) | claude (native Windows) | agy |
|---|---|---|---|---|
| read | read-only OS sandbox | tool list: Read, Glob, Grep | same | advisory + git check |
| verify | OS sandbox + git check | no Edit/Write; shell sandboxed, repository write-denied | no Edit/Write; shell advisory + git check | refused |
| write | OS sandbox | Edit/Write; shell sandboxed | Edit/Write; shell unsandboxed | accept-edits mode |
| web | `web_search` setting | WebSearch/WebFetch in or out of the tool list | same | search always on; page reading needs the allow rule |
| network | sandbox setting | sandbox domain allowlist | not enforced | n/a |

"Advisory + git check" means the agent is told not to, and crew compares `git status` before and
after the run and reports any change as `scope-violation`. It detects changes; it doesn't prevent
them. Details:
[`skills/crew/references/permissions.md`](skills/crew/references/permissions.md).

A delegated agent can't start crew tasks of its own. crew marks its environment and refuses to
dispatch from inside a task.

## Choosing models

Models are set in configuration, never in code. The built-in defaults are in
[`skills/crew/assets/config.default.json`](skills/crew/assets/config.default.json). Your overrides go in
`~/.crew/config.json` (or wherever `$CREW_CONFIG` points), merged key by key over the defaults.

- **Agent spec:** `backend:model[@effort]`, e.g. `codex:gpt-6.1-sol@high`, `claude:opus@high`,
  `agy:gemini-3.8-flash-medium`. For claude, the aliases `fable`, `opus`, `sonnet` and `haiku`
  follow Anthropic's latest release; full model names work too.
- **Aliases** name a spec, or a list of specs tried in order.
- **Roles** map a kind of work to an agent (or a fallback list) and a default scope. From a
  list, crew uses the first installed agent whose vendor isn't the caller's. If the list only
  has the caller's vendor, it uses that and says so.

Default aliases (September 2026):

| Alias | Spec |
|---|---|
| `codex-strong` / `codex-balanced` | `codex:gpt-6.1-sol@high` / `@medium` |
| `codex-fast` | `codex:gpt-6-luna@medium` |
| `codex-frontier` | `codex:gpt-6-astra@high` (not used by any role by default: expensive) |
| `claude-strong` / `claude-balanced` | `claude:opus@high` / `claude:sonnet@high` |
| `claude-fast` | `claude:haiku` |
| `claude-frontier` | `claude:fable@high` (not used by any role by default) |
| `google-fast` / `google-strong` | `agy:gemini-3.8-flash-medium` / `agy:gemini-3.1-pro-high` |

Default roles:

| Role | Agents, in order | Default scope |
|---|---|---|
| `review` | codex-strong, claude-strong | verify, no web, no network |
| `check` | codex-strong, claude-strong | verify |
| `implement` | codex-balanced, claude-balanced | write |
| `implement-light` | codex-fast, claude-fast | write |
| `research` | google-fast, codex-fast, claude-fast | read + web |
| `light` | google-fast, codex-fast, claude-fast | read |

Change them from the command line:

```sh
crew.py models                                          # what each installed backend offers
crew.py set-agent codex-strong codex:gpt-6-astra@high
crew.py set-role review --agent claude-frontier codex-strong   # a fallback list
crew.py set-role research --agent google-strong
crew.py set-role docs --agent codex-fast --access write --about "Write docs from code"   # new role
crew.py set-role review --reset                         # back to the built-in default
crew.py roles --caller codex                            # what each role resolves to from Codex
```

Or edit `~/.crew/config.json` directly:

```json
{
  "agents": {
    "codex-strong": "codex:gpt-6.1-sol@xhigh",
    "google-fast": ["agy:gemini-3.8-flash-low", "codex:gpt-6-luna@low"]
  },
  "roles": {
    "review": { "network": true },
    "rust-check": {
      "agent": ["codex-strong", "claude-strong"], "access": "verify",
      "env": { "CARGO_TARGET_DIR": "{cwd}/target/crew" }, "scratch_paths": ["target/"]
    }
  },
  "backends": { "codex": { "ignore_user_config": true } },
  "view": "hidden"
}
```

For a single call, `--agent`, `--model` and `--effort` override the role.

Other config keys:

| Key | Meaning |
|---|---|
| `view` | `auto` \| `tab` \| `hidden` |
| `hold` | keep tabs open until you press Enter |
| `max_minutes` | kill a runaway agent (default 60) |
| `terminal` | a custom terminal command (see below) |
| `backends.<name>.exe` | a path or argv list, if the CLI isn't on PATH under its usual name |
| `backends.agy.settings` | agy's settings file, used by `setup-agy-web` and `doctor` |
| `roles.<role>.network_domains` | limit claude's sandbox network to these domains |

## Visible terminals

With `view: auto` (the default) crew opens each task in a terminal tab when it can, and otherwise
runs it hidden. If a tab hasn't started within 15 seconds (some launchers report success without
opening anything), crew runs the task hidden instead, and a late tab exits without running it
twice:

| Platform | Default |
|---|---|
| Windows | Windows Terminal (`wt`), tabs in a window named `crew` |
| macOS | Terminal.app (untested) |
| Linux | a new tmux window, if crew is called from inside tmux |

Anything else: set `terminal` to an argv template. `{argv}` expands to the runner command,
`{cmd}` to it as one shell-quoted string, and `{title}` to the tab title:

```json
{ "terminal": ["gnome-terminal", "--title", "{title}", "--", "{argv}"] }
{ "terminal": ["kitty", "@", "launch", "--type=tab", "--tab-title", "{title}", "{argv}"] }
{ "terminal": ["wezterm", "cli", "spawn", "--", "{argv}"] }
```

Hidden tasks behave the same way. Follow them in `raw.log` in the run folder.

## Run records

`~/.crew/runs/<yyyyMMdd-HHmmss>-<role>-<slug>/`:

```
cmd.json             backend, model, caller, scope, how it is enforced, exact argv, cwd, env
task.md              exactly what was sent (scope block + role text + your task)
claude-settings.json claude only: the per-task permissions and sandbox settings
run.json             runner pid + heartbeat (lets --wait/status tell a dead runner from a slow one)
raw.log              what the tab showed (compact: one line per command or tool call)
events.jsonl         codex/claude: the complete JSON event stream
final.md             the agent's final answer
result.json          status, exit code, seconds, tokens, cost (claude), session id, changed files, violations, denied tools
scratch/             the task's scratch directory
```

`~/.crew/ledger.jsonl` has one line per task, and `crew.py stats` summarises it. Nothing is
deleted automatically. Remove old run folders whenever you like; they hold copies of your tasks
and of any code the agent quoted.

Statuses: `ok`, `empty`, `agent-error`, `permission-denied`, `scope-violation`, `timeout`,
`harness-error`. See [SKILL.md](skills/crew/SKILL.md#statuses-resultjson).

## Privacy and cost

- The task text, and any files the delegated agent reads, go to that vendor under **your** plan
  and its data-use terms. Check those terms before delegating private or unpublished code, and
  never put secrets in a task.
- Every task uses your quota with that vendor. `crew.py stats` shows the spread, and claude
  runs also record their cost. Usage-limit errors show up as `agent-error` with the vendor's
  message in `note`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `NOT INSTALLED` in `roles` / `doctor` | install the backend, or set `backends.<name>.exe` |
| From Codex, crew fails to write `~/.crew` or agents can't connect | crew ran inside Codex's sandbox; approve running it unsandboxed |
| codex: every command "blocked by policy" (Windows) | set up the Windows sandbox (see Codex setup); if you use `ignore_user_config`, make sure `[windows] sandbox` is in your config.toml |
| claude verify/write task fails at start on Linux | claude's sandbox is missing dependencies (`bubblewrap`, `socat`) |
| model not found | `crew.py models`, then `crew.py set-agent ...` |
| agy research returns `permission-denied` | `crew.py setup-agy-web`, or add the rule by hand |
| agy tab stays empty for minutes | normal: agy prints only when it has finished |
| `scope-violation` you didn't expect | check whether you edited the repository during the run; the check sees your edits too |
| `harness-error: runner exited without writing result.json` | the tab was closed or the process killed; run again |
| no tab opens | `crew.py doctor` → "visible terminal"; set `terminal`, or use `--view hidden` |
| codex (Windows): "timed out connecting runner pipe", so no command runs | your agent runs in session 0 (service or remote session); codex's elevated sandbox can't start commands there. Run from a normal desktop session. `doctor` warns about this |
| Windows: tasks always run hidden although Windows Terminal is installed | your agent runs in session 0 (as a service, or remotely), where no window can reach the desktop; `doctor` reports this. Nothing to fix, just watch `raw.log` |

## Development

```sh
python -m unittest discover -s tests -v     # stand-in agents; needs git, no accounts
uvx --from "git+https://github.com/agentskills/agentskills#subdirectory=skills-ref" skills-ref validate skills/crew
```

CI (`.github/workflows/ci.yml`) runs both on Linux, Windows and macOS with Python 3.9 and 3.13.

The skill follows the [Agent Skills specification](https://agentskills.io/specification).
`skills/crew/` holds:

- `SKILL.md`: what the agent reads;
- `scripts/crew.py`: the CLI and the per-task runner;
- `references/`: detail loaded only when needed;
- `assets/config.default.json`: the built-in defaults;
- `LICENSE`.

## Acknowledgements

The idea of pairing a coding agent with other vendors' CLIs, with the calling agent owning review
and commits, was inspired by
[`amelnagdy/delegate-skills`](https://github.com/amelnagdy/delegate-skills). crew's code and
documentation are written independently.

## License

MIT
