# Permissions

Every task has three scope settings: `--access`, `--web` and `--network`. crew maps them onto
each backend's own controls, records how each one is enforced (`cmd.json` → `enforcement`), and
refuses a scope the backend can't honour.

Contents: [the table](#how-each-setting-is-enforced) · [git check](#the-git-check) · [fact check](#the-fact-check) ·
[scratch folder](#scratch-folder) · [isolation](#isolation) ·
[sandboxed callers](#calling-crew-from-a-sandbox) · [Codex](#codex) · [Claude](#claude) ·
[agy](#agy)

## How each setting is enforced

| | Codex | Claude | agy |
|---|---|---|---|
| `read` | read-only sandbox (**hard**) | tools limited to Read, Glob, Grep (**hard**) | instructions + git check |
| `verify` | workspace-write sandbox, scratch folder writable, + git check | shell but no Edit/Write. Mac/Linux: sandbox with the repo read-only (**hard**). Windows: instructions + git check | refused: agy can't run commands headless |
| `write` | workspace-write sandbox (**hard**) | Edit/Write + shell, sandboxed on Mac/Linux | accept-edits mode; commands stay denied |
| `--web` | `web_search` live or disabled (**hard**) | WebSearch/WebFetch in or out of the tool list (**hard**) | search always on; reading pages needs `read_url(*)` in agy's settings |
| `--network` | sandbox network on or off (**hard**) | sandbox domain allowlist on Mac/Linux; not enforced on Windows | n/a |

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
