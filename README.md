# crew

Turn your coding agent into a team lead that can hand work to agents from other companies.

## TL;DR

- **What:** a skill for Claude Code, Codex and other agents that read `SKILL.md` skills. Your
  agent breaks a request into pieces and farms them out, often several at once, to **Codex**,
  **Claude Code**, **Gemini (via Antigravity)**, **OpenCode** (with any provider it supports), or
  straight to a model API such as **OpenRouter**, DeepSeek, Groq or a local **Ollama**. It then
  checks what comes back, sends follow-ups where needed, and pulls it all together.
- **Why:** you get more done in parallel, a different model family catches different bugs, and
  you make use of the subscriptions you already pay for.
- **Spreads the load:** crew knows how much of each vendor's usage limit is left. It skips a
  vendor that's out, moves a task on if its agent hits a limit mid-run, and can send one question
  to several model families at once so you can compare answers.
- **Checks facts for free:** after web research, crew loads every cited URL and checks every
  claimed package version against crates.io, PyPI and npm, without using a model.
- **Nothing hidden:** each agent opens in its own terminal tab showing the exact prompt it was
  sent and its output as it works. The prompt and the answer are saved as Markdown, so you can
  review what every agent was asked and what it said.
- **Safe by default:** every task gets its own permission scope (read, verify or write; web on or
  off; network on or off). Delegated agents never commit.
- **Any model:** Claude Code and Codex can also run other providers' models (e.g. a GLM or
  DeepSeek model through OpenRouter), and any other command-line agent can be added in config.
- **Install:** copy `skills/crew` into your agent's skills folder and run `crew.py doctor`.
- **Use:** ask in plain words, e.g. *"/crew have codex review my changes"*.

## What it's for

| Job | Role | Goes to, when Claude Code is in charge |
|---|---|---|
| Review a diff (uncommitted, a commit, or a branch) | `review` | Codex gpt-6.1-sol, which can build and run tests |
| Second opinion on a plan, claim or finding | `check` | Codex gpt-6.1-sol |
| Bounded implementation; it edits files but never commits | `implement` | Codex gpt-6.1-sol |
| Boilerplate, fixtures, scaffolding | `implement-light` | Codex gpt-6-luna |
| Web research and version lookups, with sources | `research` | Codex gpt-6-luna, then Gemini Flash |
| Summaries of files or logs, drafting text | `light` | Gemini Flash |

When Codex is in charge, reviews go to Claude Opus and the light roles to Claude Haiku 5.5.
Independent pieces run in parallel, and a finished Codex or Claude task can be continued in the
same session with `--resume`.

## What it looks like in practice

These are real sessions, from Claude Code.

**Building a Rust desktop app**
> "Lets continue building lodoc, use /crew when needed, only review at major checkpoints.
> Delegate appropriate coding tasks to codex, and delegate any lookup or internet searching to
> gemini."

Over about 90 minutes, crew:
- had Gemini look up the current Rust release, crate versions and a file-format question;
- had Codex review the existing code, then re-check two findings from an earlier review;
- had Codex write a settings module and an archive reader, then review that work.

Claude read each result before acting on it.

**Planning the foundations of an imaging app**
> "look at the forums like image.sc and other sites, find common issues, look at known bugs in the
> apps we are trying to replace"

crew ran five agents at once:
- three Codex agents, one each on the ImageJ, CellProfiler and napari issue trackers;
- two Gemini agents, one on user forums and one on the published literature.

Claude drafted the app's foundations from their reports, then a further Codex task critiqued the
draft. Along the way crew flagged two of the Codex agents for writing files in the repository
when they were only meant to read.

**Researching a presentation**

crew started six Gemini agents in parallel, one per topic of the talk. Claude reviewed each
report, sent follow-up research where a report was thin or badly sourced, and built the talk from
the results.

## How it works

crew is one Python script (`skills/crew/scripts/crew.py`, standard library only). Your agent stays
in charge: it decides how to split your request, then calls crew once for each piece. Pieces that
don't depend on each other run at the same time, each as its own agent. For each piece:

1. **Pick an agent.** Each role (review, check, implement, research…) lists agents in order of
   preference. crew skips your own model family, so from Claude Code a review goes to Codex, and
   from Codex it goes to Claude. It goes by the model, not the CLI: agy can run Claude and GPT
   models too, and `agy:claude-opus-5-5-high` doesn't count as a second opinion for Claude. The
   same goes for OpenCode, OpenRouter and the other providers: crew reads the family from the
   model name (`openrouter/anthropic/...` is Anthropic, `deepseek-v4` is DeepSeek).
   crew also remembers each vendor's usage limits (Claude and Codex report them as they run):
   - a vendor at its limit is skipped until the limit resets;
   - if an agent hits its limit mid-task, crew sends the task on to the next agent in the role
     and links the two runs;
   - roles marked `balance` (by default `implement-light` and `light`) prefer whichever vendor
     has used least of its current limit. `crew.py quota` shows what crew knows.
2. **Set the scope.** The role gives defaults and your agent can override them per task:
   - **access:** `read`, `verify` (may build and run tests) or `write`;
   - **web:** on or off;
   - **network:** on or off.

   crew turns this into each tool's own controls: Codex's sandbox, Claude's tool list and
   settings, agy's edit mode. It tells you which limits are enforced and which are only
   instructions.
3. **Run it.** A small runner starts the agent in a terminal tab you can watch, or hidden if
   there isn't one. It snapshots `git status` before and after, so any file the agent changed
   without permission is reported.
4. **Report back.** Your agent gets a status, the list of changed files and the delegated agent's
   answer. After a task with web access it also gets crew's fact check: cited URLs that failed,
   and package versions the registries don't confirm.

When the pieces are back, your agent checks each one, sends follow-ups where something is wrong or
thin (continuing the same session, or as a new task), and combines the results. It does any
committing itself.

A delegated agent can't start crew tasks of its own, and your agent's session details aren't
passed on to it.

## Traceable by design

When agents hand work to other agents, it's easy to lose track of who was asked what. crew is
built so that a person can follow every step.

- **You can watch it happen.** Each agent opens in its own terminal tab. The tab shows the exact
  prompt that was sent, the permissions it has, and its output as it works. Tabs stay open until
  you close them.
- **Everything is saved.** Each run gets a folder, `~/.crew/runs/<time>-<role>-<name>/`:

  | File | Holds |
  |---|---|
  | `task.md` | the exact prompt sent, in Markdown |
  | `final.md` | the agent's answer, in Markdown |
  | `raw.log` | everything the tab showed |
  | `cmd.json` | the exact command, model, permissions, and the session that sent the task |
  | `result.json` | status, time, tokens, cost, the model that actually ran, and any changed files |
  | `events.jsonl` | the full event stream, for Codex and Claude |
  | `request.md` | the task as your agent wrote it, before crew's scope note (used for retries) |
  | `checks.json` | for web tasks: every cited URL and package version crew checked, and the result |

- **Easy to review across agents.** Prompts and answers sit side by side as readable Markdown, so
  you can check one agent's work against another's, or see why your agent made a decision.
  `crew.py status` lists recent runs, and `crew.py stats` shows usage and cost per model.
- **Linked both ways.** `cmd.json` records the calling agent's session id (`caller_session`), so a
  run can be traced to the conversation that sent it, and `resumed_from` links each follow-up to
  the run it continued.
- **Honest about models and cost.** Claude Code resolves short names like `haiku` itself, and
  older versions lag a release, so each run records `resolved_model`. Tools such as web search
  run on a helper model; `model_usage` lists every model a run used and what each cost. A
  follow-up's `cost_usd` covers that follow-up only (the session total is `session_cost_usd`).

- **Facts checked for free.** After a task with web access, crew fetches each URL the answer
  cites and looks up every package version it claims on crates.io, PyPI or npm. Dead links and
  versions that don't exist (or aren't the latest) are listed with the answer. This uses no
  model, only plain web requests. Turn it off with `"fact_check": false`.

Run folders are never deleted automatically, but build output is: when a run finishes, crew removes
build folders (`target`, `node_modules` and the like) from its scratch folder and keeps everything
else, including small evidence logs. `crew.py prune` does the same for older runs.

## Install

You need Python 3.9+, git, and at least one of `codex`, `claude`, `agy` or `opencode` besides the
agent you're using, or an API key for a provider such as OpenRouter.

Copy `skills/crew` into your agent's skills folder:

| Agent | Folder |
|---|---|
| Claude Code | `~/.claude/skills/` |
| Codex | `~/.agents/skills/` |
| Others | wherever your agent loads `SKILL.md` skills from |

```sh
git clone https://github.com/lphilbrooks/crew.git
cp -R crew/skills/crew ~/.claude/skills/
cp -R crew/skills/crew ~/.agents/skills/
python3 ~/.claude/skills/crew/scripts/crew.py doctor
```

On Windows use `Copy-Item -Recurse` and `python` instead of `python3`. `doctor` checks each CLI,
its login, your models and which terminal crew can open. Your own settings live in `~/.crew/`, so
updating is just copying the folder again.

## Setting up each agent

**Codex:** `npm install -g @openai/codex`, then `codex login`.
- On Windows, turn on Codex's sandbox once (`[windows] sandbox = "elevated"` in
  `~/.codex/config.toml`). Without it, Codex can't run commands.

**Claude Code:** install it ([docs](https://code.claude.com/docs/en/setup)), then
`claude auth login`.
- On Linux, Claude's sandbox needs `bubblewrap` and `socat`.
- Native Windows has no Claude sandbox, so there Claude's shell commands are only limited by
  instructions and crew's git check.

**Antigravity (Gemini):** install `agy` from [antigravity.google](https://antigravity.google) and
sign in once.
- agy only reads permissions from its own settings file. To let research read web pages (search
  already works), run `crew.py setup-agy-web`, which backs the file up first. Or add
  `"permissions": {"allow": ["read_url(*)"]}` to `~/.gemini/antigravity-cli/settings.json`
  yourself.

Gemini CLI isn't supported: Google has moved personal accounts to Antigravity. Gemini models are
also reachable through OpenCode, OpenRouter or the `gemini` API provider.

**OpenCode:** install `opencode` from [opencode.ai](https://opencode.ai) and connect a provider with
`opencode auth login` (OpenRouter, OpenCode Zen, Anthropic, OpenAI, a local Ollama and many more).
- Agents are written `opencode:<provider>/<model>`, e.g. `opencode:openrouter/<model>`.
  `crew.py models` lists what your opencode offers.
- crew sets each task's permissions itself (see [`permissions.md`](skills/crew/references/permissions.md#opencode)).
  OpenCode has no shell sandbox, so `verify` limits are partly instructions plus crew's git check.
- On OpenCode 2, crew runs each task on its own private server (`--standalone`), because the shared
  background server ignores a run's own permission settings. A known OpenCode bug
  ([#55076](https://github.com/anomalyco/opencode/issues/55076)) can let the background service
  resume a killed run without those permissions; until it is fixed, consider
  `opencode service set disabled true`, which makes private servers the default.
- `"backends": {"opencode": {"private_db": true}}` keeps crew's sessions in each run folder instead
  of your own OpenCode history.

**Model APIs (OpenRouter and others):** give crew a key and it can call the API directly.

```sh
crew.py set-key openrouter            # or export OPENROUTER_API_KEY=...
crew.py models --provider openrouter  # the model ids it offers
crew.py set-agent or-deepseek api:openrouter/<model id>
```

Built-in providers: `openrouter`, `openai`, `anthropic`, `gemini`, `deepseek`, `moonshot`, `kimi`
(Kimi's coding plan, for `claude+kimi` only), `zai`, `xai`, `mistral`, `groq`, `cerebras`,
`together`, `fireworks`, and local `ollama` and `lmstudio` (no key). Add any other OpenAI-compatible
endpoint (vLLM, a company gateway) under `providers` in `~/.crew/config.json`, and limit a provider
to certain models with `allowed_models`.

A local gateway such as [LiteLLM](https://docs.litellm.ai) or
[Claude Code Router](https://github.com/musistudio/claude-code-router) translates between API
styles, so pointing a provider's `anthropic_url` (or `base_url`) at it lets `claude+<provider>`
reach models whose own provider has no Anthropic-compatible endpoint.

There are three ways to use a provider:

| Agent | Example | Gets |
|---|---|---|
| `api:<provider>/<model>` | `api:openrouter/<model>` | no tools: answers from the task text alone. For `light`, `check` and reviews (the diff is embedded). |
| `claude+<provider>:<model>` | `claude+openrouter:<model>`, `claude+deepseek:<model>`, `claude+ollama:<model>` | Claude Code's tools and sandbox, running that model. Needs an Anthropic-compatible endpoint (OpenRouter, DeepSeek, Moonshot, Z.ai, Ollama). |
| `codex+<provider>:<model>` | `codex+ollama:<model>` | Codex's tools and sandbox, running that model. Needs a Responses API (OpenAI, OpenRouter, Ollama). |

Claude Code is built for Anthropic's models, and Codex for OpenAI's, so other models may use their
tools less reliably. Keys stored with `set-key` live in `~/.crew/keys.json`, readable only by you;
they reach the agent at run time and never appear in run folders.

**The same model through another provider.** When a vendor's usage limit is hit, crew moves the
task to the next agent in the role. Put the same model on another route next in line, and a Claude
limit sends the task to Claude through OpenRouter instead of to a different model:

```sh
crew.py set-agent opus claude:claude-opus-5-5@high claude+openrouter:<OpenRouter's id for it>@high
crew.py set-role check --agent opus codex-strong
```

**Any other command-line agent:** describe it under `backends` in your config and use it as
`<name>:<model>`. See [`permissions.md`](skills/crew/references/permissions.md#command-line-agents-from-the-config).

**Calling crew from Codex:** Codex runs commands in a sandbox, and crew has to run outside it.
Approve the request when Codex asks to run `crew.py` unsandboxed.

## Commands

```sh
crew.py run --role review --cd . --wait                      # review uncommitted changes
crew.py run --role review --target base:main --task "Focus on error handling" --wait
crew.py run --role check --task-file plan.md --wait          # second opinion
crew.py run --role implement --task-file todo.md --network --wait
crew.py run --resume <run folder> --task "Use the helper in util.py instead" --wait
crew.py run --role research --task "Latest stable <library> version, with sources" --wait
crew.py run --role research --panel --task "..." --wait     # same question to two model families
crew.py check <run folder>                                  # re-run the fact check
crew.py prune --dry-run                                     # build output older runs still hold
crew.py run --role light --agent api:openrouter/<model> --task-file notes.md --wait
crew.py set-key openrouter                                  # store a provider's API key
crew.py models --provider openrouter                        # list a provider's models
crew.py run --role implement --worktree --task-file todo.md --wait   # in its own git worktree
crew.py doctor --ask                                        # prove every configured agent answers
crew.py models --changes                                    # models added or removed since last time
crew.py status | stats | quota | collect <run folder> | roles | models | doctor
```

Useful flags:

| Flag | Does |
|---|---|
| `--access read\|verify\|write` | file access for this task |
| `--web` / `--no-web` | allow or block web search |
| `--network` / `--no-network` | allow or block network for commands |
| `--agent <spec>` | use a specific agent, e.g. `codex:gpt-6.1-sol@high` or `opencode:openrouter/<model>` |
| `--caller codex\|claude\|agy\|opencode:<model>` | say who's calling, so crew skips that model family |
| `--panel [N]` | send the task to N agents (default 2) from different model families, to compare |
| `--worktree` | run in a new git worktree from HEAD, on branch `crew/<run>`; kept if the agent changed files, removed if not. Parallel write tasks then can't collide |
| `--stall-minutes N` | stop an agent that prints nothing for N minutes (status `stalled`; not for agy, which prints only at the end) |
| `--max-cost USD` | cost cap: Claude enforces it (`--max-budget-usd`); for other agents crew flags a run that went over |

## How strictly scopes are enforced

| | Codex | Claude (Mac/Linux) | Claude (Windows) | agy | OpenCode | api |
|---|---|---|---|---|---|---|
| read | sandbox | tool list | tool list | instructions + git check | permissions | no tools |
| verify | sandbox + git check | sandbox | instructions + git check | not supported | permissions + instructions + git check | runs read-only |
| write | sandbox | sandbox | instructions + git check | edit mode | permissions | not supported |
| web | setting | tool list | tool list | search always on | permissions | not supported |
| network | sandbox | sandbox | not enforced | n/a | not enforced | n/a |

"Instructions + git check" means the agent is told not to, and crew reports anything it changed
afterwards. Details: [`permissions.md`](skills/crew/references/permissions.md).

## Choosing models

Models live in config, not code.
- The defaults are in [`assets/config.default.json`](skills/crew/assets/config.default.json).
- Your overrides go in `~/.crew/config.json`.
- An agent is written `backend[+provider]:model@effort`, e.g. `codex:gpt-6.1-sol@high`,
  `claude:claude-opus-5-5@high`, `agy:gemini-3.8-flash-medium`, `opencode:openrouter/<model>`,
  `api:groq/<model>` or `claude+openrouter:<model>`.
- The defaults use only Codex, Claude and agy. Add others to a role with `set-agent` and
  `set-role`; crew doesn't ship model ids for other providers, because their catalogues change
  often. `crew.py models` and `crew.py models --provider <name>` show the current ones.

| Role | Tries, in order | Default scope |
|---|---|---|
| review | Codex gpt-6.1-sol (high), Claude Opus | verify |
| check | same | verify |
| implement | Codex gpt-6.1-sol (medium), Claude Sonnet | write |
| implement-light | Claude Haiku 5.5, Codex gpt-6-luna | write |
| research | Claude Haiku 5.5, then Codex gpt-6-luna, then Gemini Flash | read + web |
| light | Claude Haiku 5.5, then Gemini Flash, then Codex gpt-6-luna | read |

crew skips the caller's own model family, so from Claude Code the last three roles go to Codex or
Gemini, and Haiku 5.5 runs them when Codex or agy is in charge. Claude agents use full model ids
(`claude:claude-haiku-5-5`) because Claude Code's aliases can lag a release.

Change them from the command line:

```sh
crew.py models                                          # what's available
crew.py set-agent codex-strong codex:gpt-6-astra@high   # change what an alias points to
crew.py set-role review --agent claude-strong codex-strong
crew.py set-agent cheap-research opencode:openrouter/<model> claude-fast
crew.py set-role research --agent cheap-research codex-fast google-fast
crew.py roles --caller codex                            # check what each role will use
```

Other settings:

| Setting | Controls |
|---|---|
| `view` | `auto`, `tab` or `hidden` terminal |
| `hold` | keep tabs open after a task finishes |
| `max_minutes` | kill runaway tasks (default 60) |
| `stall_minutes` | stop an agent that prints nothing for this long (off by default; also per role) |
| `max_cost_usd` | cost cap per run (see `--max-cost`; also per role) |
| `fact_check` | check cited URLs and package versions after web tasks (default on) |
| `prune_scratch` | remove build output from a run's scratch folder when it finishes (default on) |
| `roles.<name>.env` | environment for that role's agents; `{cache}` is a build folder kept between runs, e.g. `"CARGO_TARGET_DIR": "{cache}/target"` |
| `roles.<name>.balance` | prefer the vendor with the most usage left (on for `implement-light`, `light`) |
| `limit_cooldown_minutes` | how long to avoid a vendor that hit its limit without saying when it resets (60) |
| `terminal` | use your own terminal, e.g. `["kitty", "@", "launch", "{argv}"]` |
| `backends.<name>.exe` | point to a CLI that isn't on PATH |
| `backends.<name>.command` | add a command-line agent crew doesn't know |
| `providers.<name>` | a model API: `base_url`, `key_env`, and optionally `anthropic_url`, `auth_env`, `responses`, `allowed_models`, `effort_style`, `family`, `headers`, `body` |
| `backends.codex.ignore_user_config` | skip your `~/.codex/config.toml` entirely; otherwise crew disables its MCP servers by name for each run |
| `backends.opencode.private_db` | keep crew's OpenCode sessions in the run folder |

Tabs open automatically in Windows Terminal, Terminal.app (untested) or tmux.

## Privacy and cost

Tasks, and any files the agent reads, go to that vendor under your plan and its data terms.
OpenRouter (and OpenCode or Claude Code set up with it) passes them on to whichever company hosts
the model, under that company's terms; check your OpenRouter privacy settings if that matters.
Don't put secrets in a task, since run folders keep a copy. Every task uses your quota;
`crew.py stats` shows the split. The cost column is the price the agent or provider reports:
Claude Code's API list price, OpenCode's cost, and providers that return one such as OpenRouter
(Codex and agy report none). A `?` means your claude CLI had no price for that model (update it).
For OpenRouter keys with a credit limit, `crew.py quota` shows how much is left.

## When not to use crew

If the work doesn't need another vendor, your agent's own subagents are simpler: no extra
process, no task brief to write, and the result comes straight back. crew earns its place when a
different model family should check the work, when you want to spread work across subscriptions,
or when you want a per-task permission scope and a readable record of every prompt and answer.

## Troubleshooting

| Problem | Fix |
|---|---|
| a role says `NOT INSTALLED` | install that CLI, or set `backends.<name>.exe` |
| agy research returns `permission-denied` | run `crew.py setup-agy-web` |
| an agy tab stays blank for minutes | normal: agy prints only when it's done |
| unexpected `scope-violation` | your own edits during the run count too |
| Codex on Windows can't run any command | set up its sandbox. From a remote or service session (Windows session 0) it can't work at all; `doctor` warns about this |
| no tab opens | run `crew.py doctor`, or use `--view hidden` |
| status `limited` | the vendor's usage limit was hit; `note` says whether the task moved to another agent. `crew.py quota` shows when limits reset |
| cost shows `?` | your claude CLI has no price for that model and guessed; update the claude CLI |
| fact check says a site "could not be checked" | the site blocks automated requests; the link may be fine, so open it yourself |
| old tabs pile up | finished tabs wait for Enter while `hold` is on; set `"hold": false`, or pass `--no-hold` |
| `~/.crew/runs` is very large | runs from before 1.3 kept their build output; run `crew.py prune` |
| status `stalled` | the agent printed nothing for `stall_minutes`; raise it, or turn it off for that role |
| a codex run reports it "could not list your codex MCP servers" | your codex has no `mcp list --json`; update codex, or set `backends.codex.ignore_user_config` |
| old `crew/...` branches pile up | they belong to kept worktrees; merge what you want, then `git worktree remove <path>` and `git branch -D <branch>` |
| `no API key` for a provider | `crew.py set-key <provider>`, or export the variable `crew.py models` names. Tabs may not inherit your shell's variables, so `set-key` is the reliable way |
| an `api` task fails with `cannot edit files` or `cannot search the web` | `api` agents have no tools; use `--access read --no-web`, or an opencode, codex or claude agent |
| an opencode task stops at a denied tool | the scope denies it (see `denied_tools`); widen `--access` or `--web` if the task needs it |
| a `claude+<provider>` task fails with model errors | check the model id with `crew.py models --provider <name>`. OpenRouter's guide says a cached Anthropic login can cause model-not-found errors; signing out of Claude Code (`/logout`) clears it |

## Development

```sh
python -m unittest discover -s tests -v
```

The tests use stand-in agents and a stand-in API server, so they need no accounts. CI runs them on Linux, Windows and
macOS, and checks the skill against the
[Agent Skills specification](https://agentskills.io/specification).

## Changes

**1.4.0**
- New backends: **OpenCode** (`opencode:<provider>/<model>`, any provider OpenCode supports), and
  **`api`** for calling OpenAI-compatible model APIs directly (`api:<provider>/<model>`), read-only,
  with streaming, cost and `--resume`.
- Built-in providers: OpenRouter, OpenAI, Anthropic, Gemini, DeepSeek, Moonshot, Z.ai, xAI, Mistral,
  Groq, Cerebras, Together, Fireworks, Ollama and LM Studio. Add others in config.
- Claude Code and Codex can run other providers' models: `claude+openrouter:<model>`,
  `codex+ollama:<model>`.
- Any other command-line agent can be defined in config (`backends.<name>.command`).
- Model families come from the model name across all backends (Anthropic, OpenAI, Google,
  DeepSeek, Qwen, Kimi, GLM, Grok, Mistral, Llama and more), so second opinions and panels stay
  cross-family whichever CLI or provider serves the model.
- Usage limits are tracked per provider; OpenRouter credit limits are read from its key endpoint.
- `crew.py set-key` stores API keys outside run folders; `crew.py models --provider` lists a
  provider's models; `--caller opencode:<model>`.
- OpenCode 2: each run gets a private server (`--standalone`) so its permissions apply, and effort
  is sent as `provider/model#effort`. The prompt goes on stdin, and every agent gets `PWD` set to
  its working folder (OpenCode reads its project from it). Optional per-run OpenCode database.
- Codex runs with plugins off and your MCP servers disabled by name, since Codex runs MCP tools
  outside its sandbox. On Windows, npm `.cmd` launchers are run without cmd.exe.
- `--worktree` runs a task in its own git worktree; `--stall-minutes` stops a silent agent;
  `--max-cost` caps or flags spending; `crew.py status` shows how long a running agent has been quiet.
- `crew.py doctor --ask` sends every configured agent a one-word question; `crew.py models --changes`
  reports models added or removed since the last check.
- Providers: `auth_env` for endpoints that want the key as `ANTHROPIC_API_KEY` (Kimi's coding plan),
  and `allowed_models` to limit which models a provider may run.

**1.3.0**
- Build output is removed from a run's scratch folder when it finishes. Before this, every Rust
  review kept a full build (2-8 GB each). Small evidence logs and the run record are kept.
- `crew.py prune` cleans older runs; `--dry-run` reports first, `--cache` clears shared caches.
- `{cache}` placeholder: a build folder per repository, kept between runs, so reviews can reuse
  the last build.

**1.2.0**
- When an agent hits its vendor's usage limit, the task moves to the next agent in the role, and
  the vendor is skipped until its limit resets.
- crew records each vendor's usage windows (Claude and Codex report them). `light` and
  `implement-light` prefer the vendor with the most left. `crew.py quota` shows the state.
- crew picks a second opinion by model family, not CLI, so a Claude model run through agy no longer
  counts as a different vendor from Claude.
- `--panel` sends one task to several model families at once.
- Fact check after web tasks: cited URLs and package versions (crates.io, PyPI, npm). It refuses
  local and private addresses, including through redirects. `crew.py check` re-runs it.

**1.1.0**
- Claude Haiku 5.5 leads the fast roles. Default Claude agents use full model ids, because Claude
  Code's short aliases can lag a release.
- Each run records the model that actually ran, every helper model it used, and the session that
  sent it. A follow-up's cost no longer includes the earlier runs in its session.
- `stats` shows cost. Web tasks are told today's date, and write tasks must report the checks they ran.

## Credits

Inspired by [`amelnagdy/delegate-skills`](https://github.com/amelnagdy/delegate-skills). crew's
code and docs were written separately. MIT licensed.
