# crew

Turn your coding agent into a team lead that can hand work to agents from other companies.

## TL;DR

- **What:** a skill for Claude Code, Codex and other agents that read `SKILL.md` skills. Your
  agent breaks a request into pieces and farms them out, often several at once, to **Codex**,
  **Claude Code** or **Gemini (via Antigravity)**. It then checks what comes back, sends
  follow-ups where needed, and pulls it all together.
- **Why:** you get more done in parallel, a different model family catches different bugs, and
  you make use of the subscriptions you already pay for.
- **Nothing hidden:** each agent opens in its own terminal tab showing the exact prompt it was
  sent and its output as it works. The prompt and the answer are saved as Markdown, so you can
  review what every agent was asked and what it said.
- **Safe by default:** every task gets its own permission scope (read, verify or write; web on or
  off; network on or off). Delegated agents never commit.
- **Install:** copy `skills/crew` into your agent's skills folder and run `crew.py doctor`.
- **Use:** ask in plain words, e.g. *"/crew have codex review my changes"*.

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
   models too, and `agy:claude-opus-5-5-high` doesn't count as a second opinion for Claude.
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
   answer.

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

Run folders are never deleted automatically. Remove old ones whenever you like.

## Install

You need Python 3.9+, git, and at least one of `codex`, `claude` or `agy` besides the agent you're
using.

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

Gemini CLI isn't supported: Google has moved personal accounts to Antigravity.

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
crew.py status | stats | quota | collect <run folder> | roles | models | doctor
```

Useful flags:

| Flag | Does |
|---|---|
| `--access read\|verify\|write` | file access for this task |
| `--web` / `--no-web` | allow or block web search |
| `--network` / `--no-network` | allow or block network for commands |
| `--agent <spec>` | use a specific agent, e.g. `codex:gpt-6.1-sol@high` |
| `--caller codex\|claude\|agy` | say who's calling, so crew skips that model family |
| `--panel [N]` | send the task to N agents (default 2) from different model families, to compare |

## How strictly scopes are enforced

| | Codex | Claude (Mac/Linux) | Claude (Windows) | agy |
|---|---|---|---|---|
| read | sandbox | tool list | tool list | instructions + git check |
| verify | sandbox + git check | sandbox | instructions + git check | not supported |
| write | sandbox | sandbox | instructions + git check | edit mode |
| web | setting | tool list | tool list | search always on |
| network | sandbox | sandbox | not enforced | n/a |

"Instructions + git check" means the agent is told not to, and crew reports anything it changed
afterwards. Details: [`permissions.md`](skills/crew/references/permissions.md).

## Choosing models

Models live in config, not code.
- The defaults are in [`assets/config.default.json`](skills/crew/assets/config.default.json).
- Your overrides go in `~/.crew/config.json`.
- An agent is written `backend:model@effort`, e.g. `codex:gpt-6.1-sol@high`, `claude:opus`,
  `agy:gemini-3.8-flash-medium`.

| Role | Tries, in order | Default scope |
|---|---|---|
| review | Codex gpt-6.1-sol (high), Claude Opus | verify |
| check | same | verify |
| implement | Codex gpt-6.1-sol (medium), Claude Sonnet | write |
| implement-light | Claude Haiku 5.5, Codex gpt-6-luna | write |
| research | Claude Haiku 5.5, then Codex gpt-6-luna, then Gemini Flash | read + web |
| light | Claude Haiku 5.5, then Gemini Flash, then Codex gpt-6-luna | read |

crew skips the caller's own vendor, so from Claude Code these roles go to Codex or Gemini, and
Haiku 5.5 runs them when Codex or agy is in charge. Claude agents use full model ids
(`claude:claude-haiku-5-5`) because Claude Code's aliases can lag a release.

Change them from the command line:

```sh
crew.py models                                          # what's available
crew.py set-agent codex-strong codex:gpt-6-astra@high   # change what an alias points to
crew.py set-role review --agent claude-strong codex-strong
crew.py roles --caller codex                            # check what each role will use
```

Other settings:

| Setting | Controls |
|---|---|
| `view` | `auto`, `tab` or `hidden` terminal |
| `hold` | keep tabs open after a task finishes |
| `max_minutes` | kill runaway tasks (default 60) |
| `fact_check` | check cited URLs and package versions after web tasks (default on) |
| `roles.<name>.balance` | prefer the vendor with the most usage left (on for `implement-light`, `light`) |
| `limit_cooldown_minutes` | how long to avoid a vendor that hit its limit without saying when it resets (60) |
| `terminal` | use your own terminal, e.g. `["kitty", "@", "launch", "{argv}"]` |
| `backends.<name>.exe` | point to a CLI that isn't on PATH |

Tabs open automatically in Windows Terminal, Terminal.app (untested) or tmux.

## Privacy and cost

Tasks, and any files the agent reads, go to that vendor under your plan and its data terms.
Don't put secrets in a task, since run folders keep a copy. Every task uses your quota;
`crew.py stats` shows the split. The cost column is the API list price Claude Code reports
(Codex and agy report none). A `?` means your claude CLI had no price for that model (update it).

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

## Development

```sh
python -m unittest discover -s tests -v
```

The tests use stand-in agents, so they need no accounts. CI runs them on Linux, Windows and
macOS, and checks the skill against the
[Agent Skills specification](https://agentskills.io/specification).

## Credits

Inspired by [`amelnagdy/delegate-skills`](https://github.com/amelnagdy/delegate-skills). crew's
code and docs were written separately. MIT licensed.
