---
name: crew
description: >-
  Hand a bounded task (code review, second opinion, implementation, web research, light
  summarising) to a different vendor's coding-agent CLI: OpenAI Codex (`codex`), Anthropic Claude
  Code (`claude`) or Google Antigravity (`agy`). Works from any of them, so Claude can ask Codex for
  a review and Codex can ask Claude. The caller sets each task's permission scope (file access,
  web, network), and every run is recorded on disk. Use only when the user explicitly asks to
  involve another agent, e.g. "/crew", "have codex review this", "get claude's opinion on this",
  "ask gemini to look this up", "delegate this to codex".
license: MIT (see LICENSE)
compatibility: >-
  Requires Python 3.9+ and git, plus at least one of the codex, claude or agy CLIs installed and
  signed in, with network access. Runs on Windows, macOS and Linux. Callable from Claude Code,
  Codex or any agent that loads SKILL.md skills and can run shell commands.
metadata:
  version: "1.0.0"
---

# crew: hand work to another vendor's agent

crew starts another vendor's agent in its own process, gives it one task and a permission scope
you choose, and brings back its answer. You stay responsible for the plan, for judging the result,
and for every commit. Use crew only when the user asks for it, never on your own initiative.

The script is `scripts/crew.py` in this skill's directory (Python 3.9+, standard library only).
Run it with `python3` (on Windows `python` or `py -3`). Below, `crew.py` means its full path.

## First, in each session

1. **Name yourself** with `--caller codex|claude|agy` on every `run`. Roles list agents from
   several vendors, and crew skips your own vendor so the user gets a second model family. It
   can often detect the caller, but naming it is reliable.
2. Run `crew.py roles --caller <you>` to see which agent and default scope each role resolves to
   on this machine. If something shows `NOT INSTALLED` or `INVALID`, run `crew.py doctor` and
   pass its output to the user. Don't change the user's crew config or agent settings unless they
   ask.
3. **If your own shell is sandboxed** (codex by default), crew must run outside that sandbox. It
   writes to `~/.crew` and starts agents that need the network and the user's logins. Request
   escalated or unsandboxed execution for crew commands; don't try to make it work inside.

## Dispatch

```sh
crew.py run --caller <you> --role <role> [scope flags] --cd <dir> --task "<what to do>" --wait
crew.py run --caller <you> --role <role> --task-file <file> --cd <dir> --wait   # for anything longer than a few lines
```

| Role (default config) | Default agents, in order | Default scope | Use for |
|---|---|---|---|
| `review` | codex strong, claude strong | verify | review a diff: `--target uncommitted` (default) \| `commit:<sha>` \| `base:<branch>`; with no task text, codex runs its built-in reviewer |
| `check` | codex strong, claude strong | verify | second opinion on a plan, claim, design or finding, backed by commands and tests |
| `implement` | codex balanced, claude balanced | write | bounded implementation; never commits |
| `implement-light` | codex fast, claude fast | write | mechanical work: boilerplate, fixtures, scaffolding |
| `research` | agy flash, codex fast, claude fast | read + web | web and documentation lookups, with sources |
| `light` | agy flash, codex fast, claude fast | read | summarise files or logs, draft text |

The user may have changed all of this. `crew.py roles` shows what's really configured.

### Scope: choose it every time

Give the smallest scope the task needs. Flags override the role's defaults for one call:

- `--access read`: may not change any file.
- `--access verify`: may build and run tests, writing only to its own scratch directory
  (`$CREW_SCRATCH`); any change to repository files is reported as a violation.
- `--access write`: may edit the working tree; every changed file is listed.
- `--web` / `--no-web`: web search and page reading.
- `--network` / `--no-network`: network for commands the agent runs, e.g. installing test
  dependencies (codex and claude, verify/write only).

How strictly each part is enforced depends on the backend and the OS. crew prints it at launch and
records it in `cmd.json`, and refuses scopes a backend can't honour (agy can't `verify`).
Summary:

- codex uses its OS sandbox, which is a hard limit.
- claude uses an exact tool list (hard). Its shell is sandboxed on macOS/Linux/WSL2, but not
  on native Windows, where shell commands are advisory plus crew's git check.
- agy's file limits are advisory plus the git check.

Details: [references/permissions.md](references/permissions.md).

Other options: `--agent <alias | codex:model@effort | claude:opus | agy:model>` (a named agent is
used even if it's your own vendor), `--model`, `--effort`, `--view hidden|tab`, `--no-hold` (close
the tab when done), `--slug` (names the run folder) and `--max-minutes` (kill a runaway agent).

### Waiting

Quick lookups can run in the foreground with `--wait`. Reviews, checks and implementation usually
take 2-15 minutes, and multi-question research 5-15. Run those with `--wait` in a background shell
if you have one, and carry on with other work. If a wait times out, `crew.py collect <taskdir>`
picks the task up again. Independent tasks can run in parallel, but parallel `write` tasks in one
repository must touch different files, and each task must say which.

`--wait` prints a status line, the scope, the run folder, any violations or changed files, and
the agent's final answer. Exit code: 0 ok, 2 finished but not ok, 3 wait timed out, 1 usage error.

## Statuses (`result.json`)

`ok` · `empty` (no answer) · `agent-error` (`note` has the agent's error) · `permission-denied`
(needed a tool outside its scope: see `denied_tools`, or agy's missing allow rule) ·
`scope-violation` (repository files changed that the access level forbids. `violations` lists
them, and your own edits during the run are included) · `timeout` (killed at `--max-minutes`) ·
`harness-error` (crew's runner failed or was closed; `note` says why).

## Follow-ups (codex and claude)

`crew.py run --resume <taskdir> --task "<only what to change>" --wait` continues the same session
with the same scope.

## Writing tasks and checking results

- The agent has none of this conversation, only the task text and the files it can read. Read
  [references/writing-tasks.md](references/writing-tasks.md) before the first dispatch in a
  session.
- Check results before acting on them or committing:
  [references/checking-results.md](references/checking-results.md). Verify review findings in the
  code, run the project's checks yourself after `implement`, and read the diff.
- Don't reset, checkout, stash or clean the working tree between an `implement` dispatch and
  checking its result. The uncommitted tree is the only copy of the work.
- Tell the user which findings you accepted and which you rejected, and why.

## Privacy

The task text, and any files the agent reads, go to that vendor under the user's own plan and
its data terms. Before sending private or unpublished material, make sure the user is happy for
that vendor to see it. Never put secrets (tokens, passwords, personal data) in a task. Tasks are
saved under `~/.crew/runs/`.
