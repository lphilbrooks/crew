---
name: crew
description: >-
  Split work across coding agents from other vendors: break a request into pieces (code review,
  second opinions, implementation, web research, summarising) and send them, several at once if
  they're independent, to OpenAI Codex (`codex`), Anthropic Claude Code (`claude`), Google
  Antigravity (`agy`), OpenCode (`opencode`, any provider it supports) or a model API directly
  (`api`: OpenRouter, DeepSeek, Groq, Ollama and other OpenAI-compatible providers), then check and
  combine the results. Works from any of them, so Claude can ask Codex for a review and Codex can
  ask Claude. Each task gets its own permission scope (file access, web,
  network) and every run is recorded on disk. Use only when the user explicitly asks to involve
  another agent, e.g. "/crew", "have codex review this", "get claude's opinion on this", "ask
  gemini to look this up", "delegate this to codex".
license: MIT (see LICENSE)
compatibility: >-
  Requires Python 3.9+ and git, plus at least one of the codex, claude, agy or opencode CLIs
  installed and signed in, or an API key for a provider such as OpenRouter, with network access. Runs on Windows, macOS and Linux. Callable from Claude Code,
  Codex or any agent that loads SKILL.md skills and can run shell commands.
metadata:
  version: "1.6.0"
---

# crew

crew lets you act as a team lead. Split the user's request into pieces where that helps, send each
piece to an agent from another vendor with a permission scope you choose (independent pieces in
parallel), then check the answers, follow up where needed, and combine the results. Each
`crew.py run` is one piece. You keep ownership of the plan, the judgment and every commit. Only
use crew when the user asks for it.

The script is `scripts/crew.py` in this skill's folder (Python 3.9+). Run it with `python3`, or
`python` on Windows. Below, `crew.py` means its full path.

## Before the first task

1. Always pass `--caller` with your own name (`codex`, `claude` or `agy`). crew then skips your
   model family, so the user gets a second opinion from a different one. From OpenCode, pass your
   model too, e.g. `--caller opencode:openrouter/anthropic/claude-sonnet-5-5`, since OpenCode can
   run any family.
2. Each `run` prints the agent it chose, and `--wait` prints a `WARNING agent choice` line when
   that agent is from your own model family. `review` and `check` refuse to run on your own family
   unless you name the agent with `--agent`. If a run fails because nothing suitable is installed,
   run `crew.py doctor` and show the user the output. `crew.py roles --caller <you>` shows every
   role at once. Don't change the user's config unless they ask.
3. If your own shell runs in a sandbox (Codex does), ask the user to approve running crew outside
   it. crew writes to `~/.crew` and starts agents that need the network and the user's logins.

## Running a task

Every task is one command: a role says what kind of job it is, and `--agent` says which model
does it. Leave `--agent` out to use the role's default.

```sh
crew.py run --caller <you> --role <role> [--agent <model>[@effort]] --cd <repo> --task "<what to do>" --wait
crew.py run --caller <you> --role <role> --cd <repo> --task-file <file> --wait   # longer tasks
```

### Research, review, build

| Job | Command | Default model | Notes |
|---|---|---|---|
| Research | `--role research` | `hydrogen@medium` (Gemini 4 Hydrogen, Google Search built in; falls back to `gemini-flash`) | read + web. Run `crew.py setup-agy-web` once so agy can open pages, not only search |
| Review | `--role review` | `sol@high` (Codex), then Opus, then `argon` | verify: can run tests (Argon via agy can't, so it reviews read-only). `--target uncommitted` (default), `commit:<sha>` or `base:<branch>` |
| Build | `--role build` | `sol@medium` (Codex) | write; never commits. agy can edit files but not run commands, so give it work you will test yourself |

Other roles: `check` (second opinion on a plan or claim, can run tests), `build-light` (boilerplate,
fixtures), `light` (summaries, drafts, read-only). `build` is the same role as `implement`, and
`build-light` as `implement-light`. The user may have changed the defaults: `crew.py roles` shows
the real setup.

### Naming a model

When the user names a model ("use gemini flash, medium effort"), pass it with `--agent`:
`--agent gemini-flash@medium`, or `--agent "gemini flash" --effort medium`. Plain names work too
(`gemini` for Hydrogen, `flash`, `codex`, `gemini pro` for Argon, `opus via agy`). A named model is always used, even from your own vendor.

| Name | CLI | Efforts (default first) | Good for |
|---|---|---|---|
| `hydrogen` | agy | medium, low, high, xhigh | Gemini 4 Hydrogen (Nov 2026, succeeds 3.8 Flash): fast research with Google Search, light builds |
| `gemini-flash` | agy | medium, low, high | Gemini 3.8 Flash: cheapest; fallback when Hydrogen is limited |
| `argon` | agy | medium, low, high | Gemini 4 Argon (replaced Gemini Pro): hard reasoning and reviews; its own weekly quota |
| `carbon` | api | high, low, medium | Gemini 4 Carbon, Google's strongest: Gemini API only (paid `GEMINI_API_KEY`), read-only, no tools |
| `agy-opus`, `agy-sonnet` | agy | high, low, medium | Claude through agy: saves the Claude plan, but it is still Claude |
| `gpt-oss` | agy | (one setting) | OpenAI's open-weight 120B model |
| `sol` | codex | high, low, medium, xhigh, max | reviews and harder builds |
| `astra` | codex | high, low, medium, xhigh, max | frontier reasoning, slower and dearer |
| `luna` | codex | medium, none, low, high, xhigh, max | fast, cheap builds and lookups |
| `opus`, `sonnet`, `fable` | claude | high, low, medium, xhigh, max | Claude models |
| `haiku` | claude | medium, low, high, xhigh, max | cheap Claude research and summaries |

An effort the model doesn't take is refused with the list it does take. Any other model id works
as `backend:model[@effort]`, e.g. `agy:gemini-4-hydrogen-high`. `crew.py models` prints this
list, other names it understands, and which models the installed CLIs offer or lack.

### Choose the scope every time

Give each task the least it needs. Flags override the role's defaults for one call.

- `--access read`: may not change any file.
- `--access verify`: may build and run tests, and write only to its scratch folder
  (`$CREW_SCRATCH`). Any change to the repository is reported as a violation.
- `--access write`: may edit files. Every changed file is listed.
- `--web` or `--no-web`: web search and page reading.
- `--network` or `--no-network`: network for the agent's commands, e.g. installing test
  dependencies (Codex and Claude only).

crew prints how each limit is enforced, and refuses scopes a backend can't honour (agy can't
`verify`; `api` agents can't `write` or use the web).
- Codex's sandbox is a hard limit.
- Claude's tool list is a hard limit, and so is its sandbox on macOS and Linux.
- OpenCode's permission rules are a hard limit on its tools, but its shell has no sandbox: in
  `verify`, repository writes and network are instructions plus a git check.
- `api` agents have no tools at all: they see only the task text, so put everything they need in
  it (reviews embed the diff automatically).
- agy's file limits, and Claude's shell on native Windows, are instructions plus a git check.

See [references/permissions.md](references/permissions.md).

Other options:

| Option | Does |
|---|---|
| `--agent <model>` | a model name from the table above, an alias, or `backend[+provider]:model[@effort]`, e.g. `codex:gpt-6.1-sol@high`, `opencode:openrouter/<model>`, `api:openrouter/<model>` or `claude+openrouter:<model>` (always honoured) |
| `--effort` | override the reasoning effort |
| `--model` | override only the model id, keeping the role's CLI |
| `--view hidden\|tab` | hidden, or in a terminal tab |
| `--no-hold` | close the tab when the task finishes |
| `--slug` | name the run folder |
| `--max-minutes` | kill the agent after this long |
| `--panel [N]` | send the task to N agents (default 2) from different model families at once |
| `--brief` | with `--wait` or `collect`, print only the first 40 lines of the answer and its path |
| `--worktree` | run in a new git worktree from HEAD; kept with its branch if the agent changed files. Use it for parallel `write` tasks |
| `--stall-minutes`, `--max-cost` | stop a silent agent; cap (Claude) or flag (others) the cost of a run |

**Panels.** For research where being wrong is costly, `--panel` asks agents from two model
families the same question. Their mistakes rarely overlap, so read where they disagree and check
those claims yourself before using them. `crew.py collect <panel folder>` reprints all answers.

**Usage limits.** crew skips a vendor that is at its usage limit, and if an agent hits its limit
during a task, the task moves on to the next agent in the role automatically (`--wait` follows
it). An agent you named with `--agent` is never swapped. `crew.py quota` shows each vendor's
last known usage.

### Waiting for results

Quick lookups can run in the foreground with `--wait`. Reviews and implementation usually take
2 to 15 minutes, and so does bigger research. Run those in a background shell if you have one and
keep working. If your shell limits how long a command may run, start the task without `--wait`
and call `crew.py collect <run folder> --timeout <seconds>` with a timeout below that limit,
repeating until it finishes. The task keeps running between calls.

`crew.py stop <run folder or panel folder>` stops a task that has gone wrong. It is recorded as
`cancelled`, and any files it changed are left for you to inspect.

Use `--brief` when several answers will come back, or an answer may be long: it prints the first
40 lines and the path to `final.md`, so you can read the rest only where you need it.

Independent tasks can run in parallel. Parallel `write` tasks in one repository must touch
different files, and each task must say which, unless each runs with `--worktree`. A kept worktree
holds the change on branch `crew/<run>`: review it there, then bring it into the user's checkout.
A `read` or `verify` task that runs alongside a `write` task in the same repository doesn't count
that task's changes as violations once it has finished; if it is still running, collect the read
task again after it finishes.

`--wait` prints the status, the scope, any violations or changed files, and the agent's answer.
After a task with web access, it also lists any cited URL that failed and any package version
the registries don't confirm: check those before relying on the answer.
If the model that ran differs from the one asked for, it says so (`ran <model>`): Claude Code
resolves short names itself and can lag a release. Research defaults to `medium` effort; pass
`--effort high` for knowledge-heavy research.
It exits 0 for ok, 2 for any other status, 3 if the wait timed out and 1 for usage errors.

## Statuses

| Status | Means |
|---|---|
| `ok` | finished with an answer |
| `empty` | finished with no answer |
| `agent-error` | the agent failed; `note` has its error |
| `permission-denied` | it needed something outside its scope; see `denied_tools` |
| `scope-violation` | repository files changed that the access level forbids; your own edits during the run count too |
| `timeout` | killed at `--max-minutes` |
| `harness-error` | crew's runner failed or was closed |
| `limited` | the vendor's usage limit was hit; `note` says whether it was sent on to another agent |
| `cancelled` | stopped with `crew.py stop` |
| `stalled` | killed after printing nothing for `--stall-minutes` |

## Follow-ups

For Codex, Claude, OpenCode and `api` agents,
`crew.py run --resume <run folder> --task "<what to change>" --wait` continues the same session
with the same scope.

## Writing tasks and checking results

- The agent hasn't seen this conversation. Read
  [references/writing-tasks.md](references/writing-tasks.md) before your first task in a session.
- Check every result before acting on it:
  [references/checking-results.md](references/checking-results.md). Confirm review findings in the
  code, run the project's checks yourself after `implement`, and read the diff.
- Don't reset, checkout, stash or clean the working tree before you've looked at an
  implementation. It's the only copy of the work.
- Tell the user which findings you accepted and which you rejected, and why.

## Privacy

The task, and any files the agent reads, go to that vendor under the user's plan and its data
terms. With OpenRouter (or OpenCode set up with it) the task also goes to whichever company hosts
the model. Check the user is happy with that before sending private material. Never put secrets in a
task, because run folders under `~/.crew/runs/` keep a copy. When a review pastes the diff into
the prompt, untracked files whose names suggest secrets (`.env`, keys, credentials) and files over
100 KB are left out by name only.
