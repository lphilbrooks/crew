# crew

Let your coding agent hand work to another company's coding agent.

## TL;DR

- **What:** a skill for Claude Code, Codex and other agents that read `SKILL.md` skills. It sends
  one task at a time to **Codex**, **Claude Code** or **Gemini (via Antigravity)** and brings back
  the answer.
- **Why:** a second model family catches different bugs, and you get more out of the
  subscriptions you already pay for.
- **Safe by default:** every task gets its own permission scope (read, verify or write; web on or
  off; network on or off). Delegated agents never commit. Every run is logged.
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

crew is one Python script (`skills/crew/scripts/crew.py`, standard library only). Your agent runs
it as a normal shell command.

1. **Pick an agent.** Each role (review, check, implement, research…) lists agents in order of
   preference. crew skips the vendor you're calling from, so from Claude Code a review goes to
   Codex, and from Codex it goes to Claude.
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
   answer. It checks the answer, and does any committing itself.

Everything lands in a run folder, `~/.crew/runs/<time>-<role>-<name>/`:
- `task.md`: exactly what was sent;
- `cmd.json`: the exact command;
- `raw.log`: what the tab showed;
- `final.md`: the answer;
- `result.json`: status, time, tokens, cost and changed files.

A delegated agent can't start crew tasks of its own, and your agent's session details aren't
passed on to it.

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
crew.py status | stats | collect <run folder> | roles | models | doctor
```

Useful flags:

| Flag | Does |
|---|---|
| `--access read\|verify\|write` | file access for this task |
| `--web` / `--no-web` | allow or block web search |
| `--network` / `--no-network` | allow or block network for commands |
| `--agent <spec>` | use a specific agent, e.g. `codex:gpt-6.1-sol@high` |
| `--caller codex\|claude\|agy` | say who's calling, so crew skips that vendor |

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
| implement-light | Codex gpt-6-luna, Claude Haiku | write |
| research | Gemini Flash, then Codex, then Claude | read + web |
| light | same | read |

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
| `terminal` | use your own terminal, e.g. `["kitty", "@", "launch", "{argv}"]` |
| `backends.<name>.exe` | point to a CLI that isn't on PATH |

Tabs open automatically in Windows Terminal, Terminal.app (untested) or tmux.

## Privacy and cost

Tasks, and any files the agent reads, go to that vendor under your plan and its data terms.
Don't put secrets in a task, since run folders keep a copy. Every task uses your quota;
`crew.py stats` shows the split.

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
