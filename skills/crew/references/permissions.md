# Permission scopes and how they are enforced

Contents: [the enforcement table](#the-enforcement-table) ·
[the git before/after check](#the-git-beforeafter-check) · [scratch directory](#scratch-directory) ·
[isolation between caller and delegate](#isolation-between-caller-and-delegate) ·
[calling crew from inside a sandbox](#calling-crew-from-inside-a-sandbox) ·
[codex](#codex-notes) · [claude](#claude-notes) · [agy](#agy-notes)

## The enforcement table

Each task has a scope with three dimensions. The caller sets them per call (`--access`, `--web`,
`--network`), falling back to the role's defaults in the config. crew maps each dimension onto the
backend's own controls. It prints and records (`cmd.json` → `enforcement`) how each one is
enforced, and refuses a scope a backend cannot honour.

| Dimension | codex (`codex exec`) | claude (`claude -p`) | agy (`agy -p`) |
|---|---|---|---|
| `--access read` | `-s read-only` sandbox: **hard** | tools limited to Read, Glob, Grep: **hard** | **advisory**: instructions + git check (agy has no read-only switch; its plan mode was seen writing files) |
| `--access verify` | `-s workspace-write`, scratch dir as an extra writable root, + instructions + git check | shell but no Edit/Write tools. macOS/Linux/WSL2: claude's sandbox with the repository write-denied and scratch writable (**hard**). Native Windows: no sandbox, so the shell is **advisory** + git check | **refused**: agy can't run commands headless |
| `--access write` | `-s workspace-write`: **hard** outside the working dir | Edit/Write tools + shell (sandboxed on macOS/Linux/WSL2; **advisory** on native Windows) | `--mode accept-edits` (terminal commands stay denied) |
| `--web` / `--no-web` | `-c web_search="live"` / `"disabled"`: **hard** | WebSearch/WebFetch tools included or removed: **hard** | search always available (`--no-web` is **advisory**); page fetching needs a `read_url(*)` allow rule in agy's settings, and without it the task text says "search only" |
| `--network` / `--no-network` | `sandbox_workspace_write.network_access`: **hard**; only meaningful with verify/write | sandbox `allowedDomains` (`["*"]` or none) on macOS/Linux/WSL2; **not enforced** on native Windows | n/a (agy runs no commands) |

**Hard** means the vendor's sandbox or tool list blocks the action. **Advisory** means the task
text tells the agent not to, and crew's git check reports changes afterwards. Nothing prevents the
change up front.

## The git before/after check

When the working directory is inside a git repository, the runner snapshots `git status`, with a
content hash per dirty or untracked file, before and after the run:

- `read` / `verify`: any change is a **violation** → status `scope-violation`, listed in
  `violations`. Exception: paths in the role's `scratch_paths` (relative to the working
  directory, e.g. `"target/"`).
- `write`: changes are listed in `touched_files`, the starting point for checking the result.

Limits: gitignored files are invisible to it, so ignored build output is always allowed. Outside
a git repository nothing is detected. The check can't tell the agent's changes from yours, so if
you edit the repository while a task runs, your edits are reported too.

## Scratch directory

Every task gets `<task dir>/scratch`, exported as `CREW_SCRATCH`. For verify/write tasks, `TMP`,
`TEMP` and `TMPDIR` also point there. It is a writable sandbox root for codex, and allowed for
writes and added with `--add-dir` for claude. Verify tasks also get `PYTHONDONTWRITEBYTECODE=1`,
so `__pycache__` next to sources doesn't count as a violation.

Roles can add environment variables (`"env": {...}`, with `{cwd}`, `{scratch}` and `{task}`
substituted). Example: `"CARGO_TARGET_DIR": "{scratch}/target"` keeps a delegated Rust build out of
the repository. It then compiles from scratch each time; `"{cwd}/target/crew"` plus
`"scratch_paths": ["target/"]` reuses the build cache.

## Isolation between caller and delegate

- The runner sets `CREW_TASK_DIR` in every delegated agent's environment. `crew.py run` refuses
  to dispatch when it sees it, so a delegated agent can't start delegations of its own.
- The caller's per-session variables are removed from the delegated agent's environment. That
  covers `CLAUDECODE`, `CLAUDE_PID`, `CLAUDE_EFFORT`, `CLAUDE_CODE_*` session, messaging and
  bridge variables, `CODEX_SESSION_ID`, `CODEX_THREAD_ID`, `CODEX_CI` and `CODEX_SANDBOX*`.
  A claude started from Claude Code therefore doesn't think it is nested, and the caller's
  session tokens never reach another vendor's process. Configuration variables such as
  `CLAUDE_CODE_USE_BEDROCK` or `CODEX_HOME` are kept.
- A delegated claude also gets `--disable-slash-commands` (no skills, so it can't load crew),
  `--strict-mcp-config` (no MCP servers) and, where the installed claude supports it,
  `--restricted` (your own claude settings files are ignored, so the tool list and settings
  above are the whole policy).

## Calling crew from inside a sandbox

If the calling agent runs its own shell commands in a sandbox (codex does by default), crew must
be allowed to run outside it. It writes to `~/.crew`, and the agents it starts need the network
and your login credentials. In an interactive codex session, approve running the command
unsandboxed (escalated). On Windows this matters twice over: codex's elevated sandbox runs
commands as a separate sandbox user, which has none of your CLI logins.

## codex notes

- crew passes `--skip-git-repo-check`, so codex also runs outside git repositories.
- `codex exec review` (the built-in reviewer, used when a review has no task text) takes no
  prompt, so crew's scope text can't reach it. The sandbox and the git check still apply. A review
  *with* task text is a normal `codex exec` told which git command shows the diff.
- `--resume` continues the original session, which keeps its original sandbox.
- By default codex loads your `~/.codex/config.toml` (MCP servers, notifier, profiles). Set
  `"backends": {"codex": {"ignore_user_config": true}}` to skip it for delegated runs (faster and
  quieter). On Windows crew then restates your `[windows] sandbox` setting, because without it
  command execution is blocked.
- **Windows:** codex's sandbox needs a one-time setup (see the codex docs on Windows). Files the
  sandbox creates can be owned by the sandbox user. If you can't delete something a verify run
  left behind, run `icacls <dir> /reset /T /Q` from an elevated prompt.

## claude notes

- Headless runs use `--permission-mode dontAsk` (anything not pre-approved is denied) or
  `acceptEdits` for write, plus `--permission-prompts none`, so nothing waits for an answer.
  Denied tool calls are listed in `denied_tools`. When they left the task without an answer,
  the status is `permission-denied`.
- `--resume` continues the same claude session (`--resume <session id>`).
- The sandbox settings are written to `<task dir>/claude-settings.json` and passed with
  `--settings`. Roles can narrow network access with `"network_domains": ["pypi.org", ...]`
  instead of all domains.
- The claude sandbox needs its platform dependencies (on Linux: `bubblewrap`, `socat`). crew
  sets `failIfUnavailable`, so a missing sandbox makes the task fail instead of silently running
  unsandboxed.
- On native Windows there is no claude sandbox. Shell commands in verify/write tasks run with
  your full user rights. Use `--access read`, or run the caller in WSL2, when that matters.

## agy notes

- agy's permissions come from its settings files, not command-line flags:
  `~/.gemini/antigravity-cli/settings.json` (`permissions.allow`) and the shared
  `~/.gemini/config/config.json`. crew never edits them unless you run `crew.py setup-agy-web`,
  which adds `read_url(*)` after taking a timestamped backup.
- Rule syntax (agy 1.2.x): `read_url(*)` matches all URLs; `read_url` and `read_url(regex:.*)`
  do not. Web search needs no rule.
- In print mode agy can't ask for permission. An unapproved tool is auto-denied, the process
  exits 0, and the "answer" is the denial notice. crew detects this and reports
  `permission-denied`.
- Your own agy settings still apply to delegated runs (default mode, auto-approval, trusted
  workspaces). If agy auto-approves tools on your machine, the advisory limits are all that stand
  between the agent and your files. Run agy tasks inside a git repository so the check can catch
  changes.
- agy prints nothing until it has finished. Long research can show an empty tab for 15 minutes;
  that is not a hang. Its own logs are in `~/.gemini/antigravity-cli/log/`.
