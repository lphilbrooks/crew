# Permissions

Every task has three scope settings: `--access`, `--web` and `--network`. crew maps them onto
each backend's own controls, records how each one is enforced (`cmd.json` → `enforcement`), and
refuses a scope the backend can't honour.

Contents: [the table](#how-each-setting-is-enforced) · [git check](#the-git-check) · [fact check](#the-fact-check) ·
[scratch folder](#scratch-folder) · [isolation](#isolation) ·
[sandboxed callers](#calling-crew-from-a-sandbox) · [Codex](#codex) · [Claude](#claude) ·
[agy](#agy) · [OpenCode](#opencode) · [api agents](#api-agents) · [other providers](#claude-and-codex-on-other-providers) ·
[your own CLI](#command-line-agents-from-the-config)

## How each setting is enforced

| | Codex | Claude | agy | OpenCode | api |
|---|---|---|---|---|---|
| `read` | read-only sandbox (**hard**) | tools limited to Read, Glob, Grep (**hard**) | instructions + git check | edit and bash denied (**hard**) | no tools (**hard**) |
| `verify` | workspace-write sandbox, scratch folder writable, + git check | shell but no Edit/Write. Mac/Linux: sandbox with the repo read-only (**hard**). Windows: instructions + git check | refused: agy can't run commands headless | bash allowed, edit denied; shell writes: instructions + git check | runs read-only (it can't build or test) |
| `write` | workspace-write sandbox (**hard**) | Edit/Write + shell, sandboxed on Mac/Linux | accept-edits mode; commands stay denied | edit and bash allowed | refused |
| `--web` | `web_search` live or disabled (**hard**) | WebSearch/WebFetch in or out of the tool list (**hard**) | search always on; reading pages needs `read_url(*)` in agy's settings | webfetch/websearch allowed or denied (**hard**) | refused |
| `--network` | sandbox network on or off (**hard**) | sandbox domain allowlist on Mac/Linux; not enforced on Windows | n/a | not enforced: instructions only | n/a |

**Hard** means the tool itself blocks the action. Everything else relies on the agent following
instructions, with crew's git check catching changes afterwards.

## The git check

Inside a git repository, the runner records `git status` before and after the run, with a hash
of every dirty or untracked file.

- `read` and `verify`: any change is a violation, except under the role's `scratch_paths`
  (e.g. `"target/"`, relative to `--cd`).
- `write`: changes are listed in `touched_files`.

Gitignored files aren't seen, so ignored build output is always allowed. Outside git nothing is
checked. Your own edits during a run are indistinguishable from the agent's.

## Scratch folder

Each task gets `<run folder>/scratch`, exported as `CREW_SCRATCH`.
- For `verify` and `write`, `TMP`, `TEMP` and `TMPDIR` point there too.
- `verify` also sets `PYTHONDONTWRITEBYTECODE=1`, so `__pycache__` stays out of the repo.
- Codex and Claude may write to it.

When the run finishes, crew removes build output from the scratch folder: directories named
`target`, `node_modules`, `.venv`, `build`, `dist` and similar, and any directory carrying the
standard `CACHEDIR.TAG` marker that Cargo and other build tools write (so `probe-target` or `tgt`
count too), wherever they are inside it. Small
loose files (1 MB or less) at the top of a build folder are kept, because agents often save their
evidence logs there (e.g. `target/test-run.log`). Links and junctions are removed as links and never
followed. `result.json` records how much was freed (`scratch_pruned_bytes`). Set
`"prune_scratch": false` to keep everything, and run `crew.py prune` to clean older runs.

Roles can add environment variables with `{cwd}`, `{scratch}`, `{task}` and `{cache}` placeholders.
`{cache}` is a build folder kept between runs, one per repository (`~/.crew/cache/<repo>-<hash>`).
crew makes it writable for that role. For a Rust project, `"CARGO_TARGET_DIR": "{cache}/target"`
keeps the build out of the repository, and each review reuses the last build instead of compiling
from nothing (`{scratch}/target` works too, but every run then builds from scratch). Evidence that
should stay with a run belongs in `$CREW_SCRATCH`, not the cache. `crew.py prune --cache` deletes
the caches.

## Isolation

- Delegated agents get a `CREW_TASK_DIR` variable, and crew refuses to run when it sees one. A
  delegated agent can't start crew tasks of its own.
- The caller's session variables are removed before the agent starts: `CLAUDECODE`,
  `CLAUDE_PID`, `CLAUDE_EFFORT`, Claude Code's session and messaging variables, `CODEX_THREAD_ID`,
  `CODEX_SESSION_ID`, `CODEX_CI` and `CODEX_SANDBOX*`. Settings such as `CLAUDE_CODE_USE_BEDROCK`
  or `CODEX_HOME` are kept.
- A delegated Claude runs without MCP servers or skills. With `--restricted` (recent versions),
  it also ignores your personal Claude settings.

## The fact check

After a task with `--web`, crew itself (not the agent) requests every URL the answer cites, and
looks up claimed package versions on crates.io, PyPI and npm. These requests come from your
machine.
- Only tasks that already had web access are checked, and that agent could have fetched the same
  URLs itself, so the check gives a task no new way out.
- Addresses on your own network are never requested: loopback, private and link-local
  addresses, names that resolve to them, and redirects that lead to them are all refused.
- At most 30 URLs per answer, each with a 10-second timeout and a capped read.
- Turn it off with `"fact_check": false` in `~/.crew/config.json`.

## Calling crew from a sandbox

crew writes to `~/.crew`, and the agents it starts need the network and your logins. If the
calling agent sandboxes its commands, as Codex does, approve running `crew.py` outside the
sandbox.

On Windows, Codex's sandbox runs commands as a separate user that has none of your logins.

## Codex

- crew passes `--skip-git-repo-check`, so Codex works outside git repositories too.
- A review with no task text uses `codex exec review`. That takes no prompt, so crew's scope note
  isn't sent; the sandbox and git check still apply.
- `--resume` keeps the original session's sandbox.
- Codex loads your `~/.codex/config.toml` by default. Set
  `"backends": {"codex": {"ignore_user_config": true}}` to skip it. crew still passes your
  Windows sandbox setting.
- **Windows:**
  - The sandbox needs a one-time setup. Files it creates can belong to the sandbox user; clear
    them with `icacls <dir> /reset /T /Q` from an admin prompt.
  - From a remote or service session (session 0), Codex can't start commands at all.

## Claude

- Uses `--permission-mode dontAsk` (or `acceptEdits` for `write`) and `--permission-prompts none`,
  so nothing waits for approval. Blocked calls are listed in `denied_tools`.
- Sandbox settings are written to `<run folder>/claude-settings.json`. A role can limit network
  with `"network_domains": ["pypi.org"]`.
- Linux needs `bubblewrap` and `socat` for the sandbox. crew makes the task fail rather than run
  unsandboxed.
- Native Windows has no sandbox. Shell commands in `verify` and `write` tasks run with your normal
  rights.

## agy

- Permissions come only from its settings files: `~/.gemini/antigravity-cli/settings.json` and
  `~/.gemini/config/config.json`. crew edits them only when you run `crew.py setup-agy-web`,
  which adds `read_url(*)` and keeps a backup.
- Only `read_url(*)` works; `read_url` and `read_url(regex:.*)` don't match.
- Headless agy can't ask for permission, so it quietly refuses and exits 0. crew spots this and
  reports `permission-denied`.
- Your own agy settings still apply. If agy auto-approves tools on your machine, instructions are
  the only limit, so run agy tasks inside git repositories.
- agy prints nothing until it finishes. Long research can leave a blank tab for 15 minutes. Its
  logs are in `~/.gemini/antigravity-cli/log/`.

## OpenCode

- crew runs `opencode run --format json -m <provider>/<model>` and passes the scope as an inline
  config (`OPENCODE_CONFIG_CONTENT`, and the permission block as `OPENCODE_PERMISSION`). Inline
  config outranks your global and project `opencode.json`. A copy is kept in
  `<run folder>/opencode-config.json`.
- The rules start from `"*": "deny"`, so any tool crew doesn't name (MCP tools, subagents) is
  denied rather than left to an approval prompt a headless run can't answer. Read, search and
  todo tools are always allowed; `edit`, `bash`, `external_directory`, `webfetch` and `websearch`
  follow the scope. A denied call shows up in `denied_tools`.
- Session sharing is turned off for crew's runs (`"share": "disabled"`).
- OpenCode has no sandbox for its shell, so in `verify` and `write` a command can write anywhere
  and reach the network. The git check reports repository changes afterwards.
- Effort is passed with `--variant` when your opencode has that flag; otherwise crew says so and
  leaves it out. Long tasks are attached with `--file` when supported.
- OpenCode keeps its own provider logins (`opencode auth login`), so crew never sees those keys.

## api agents

- `api:<provider>/<model>` sends the task straight to an OpenAI-compatible
  `/chat/completions` endpoint from crew's own runner. The model gets no tools: it can't read
  files, run commands or browse, which makes every scope trivially enforced. `write` and `--web`
  are refused; `verify` runs read-only.
- Reviews embed the diff, as for agy. For other roles, put what the model needs in the task.
- The key comes from the provider's variable (e.g. `OPENROUTER_API_KEY`) or from
  `~/.crew/keys.json` (`crew.py set-key <provider>`; the file is readable by you only). It is
  added to the request at run time and never written to `cmd.json`.
- `--resume` replays the earlier conversation (`conversation.json` in the run folder).
- If a provider rejects crew's optional fields (`stream_options`, the effort setting), crew retries
  once with a minimal request and notes it.

## Claude and Codex on other providers

- `claude+<provider>:<model>` sets `ANTHROPIC_BASE_URL` to the provider's Anthropic-compatible
  endpoint, `ANTHROPIC_AUTH_TOKEN` to its key, and `ANTHROPIC_API_KEY` to empty. It also points
  Claude Code's helper and subagent models at the same model, so nothing is sent to Anthropic's
  model names. All of Claude's scope controls still apply.
- `codex+<provider>:<model>` adds a `model_providers` entry with `wire_api = "responses"`, so the
  provider needs a Responses API (`"responses": true` in its config). The Codex sandbox applies.
- The key is passed in the environment at run time, never written to `cmd.json`.
- Usage limits and credit are tracked per provider, not against your Claude or Codex plan.
- Claude Code is built for Anthropic's models; other models may handle its tools less well.

## Command-line agents from the config

A CLI crew doesn't know can be added under `backends.<name>`:

```json
"backends": {
  "mycli": {
    "command": ["mycli", "--model", "{model}", "-p", "{prompt}"],
    "effort_args": ["--effort", "{effort}"],
    "access": ["read", "write"],
    "access_args": {"write": ["--auto-approve-edits"]},
    "web_args": ["--web"],
    "family": "mycompany",
    "enforcement": {"write": "mycli's auto-approve-edits mode"}
  }
}
```

- `{prompt}` puts the task on the command line (long tasks become a pointer to `task.md`);
  without `{prompt}` or `{task_file}`, the task goes to stdin. `{cwd}` and `{scratch}` work too.
- `access` lists the levels the CLI may be given (default `read` only); others are refused.
- Output is shown and kept as plain text. Unless `enforcement` says otherwise, crew reports every
  limit as instructions plus the git check, because it can't know what the CLI enforces.
