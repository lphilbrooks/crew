---
name: crew
description: >-
  Hand a bounded task (code review, second opinion, implementation, web research, summarising)
  to a different vendor's coding-agent CLI: OpenAI Codex (`codex`), Anthropic Claude Code
  (`claude`) or Google Antigravity (`agy`). Works from any of them, so Claude can ask Codex for a
  review and Codex can ask Claude. Each task gets its own permission scope (file access, web,
  network) and every run is recorded on disk. Use only when the user explicitly asks to involve
  another agent, e.g. "/crew", "have codex review this", "get claude's opinion on this", "ask
  gemini to look this up", "delegate this to codex".
license: MIT (see LICENSE)
compatibility: >-
  Requires Python 3.9+ and git, plus at least one of the codex, claude or agy CLIs installed and
  signed in, with network access. Runs on Windows, macOS and Linux. Callable from Claude Code,
  Codex or any agent that loads SKILL.md skills and can run shell commands.
metadata:
  version: "1.0.0"
---

# crew

crew runs another vendor's agent on one task, with a permission scope you choose, and returns its
answer. You keep ownership of the plan, the judgment and every commit. Only use crew when the user
asks for it.

The script is `scripts/crew.py` in this skill's folder (Python 3.9+). Run it with `python3`, or
`python` on Windows. Below, `crew.py` means its full path.

## Before the first task

1. Always pass `--caller` with your own name (`codex`, `claude` or `agy`). crew then skips your
   vendor, so the user gets a second opinion from a different model family.
2. Run `crew.py roles --caller <you>` to see which agent each role will use. If one says
   `NOT INSTALLED` or `INVALID`, run `crew.py doctor` and show the user the output. Don't change
   the user's config unless they ask.
3. If your own shell runs in a sandbox (Codex does), ask the user to approve running crew outside
   it. crew writes to `~/.crew` and starts agents that need the network and the user's logins.

## Running a task

```sh
crew.py run --caller <you> --role <role> --cd <repo> --task "<what to do>" --wait
crew.py run --caller <you> --role <role> --cd <repo> --task-file <file> --wait   # longer tasks
```

| Role | Default scope | Use for |
|---|---|---|
| `review` | verify | reviewing a diff: `--target uncommitted` (default), `commit:<sha>` or `base:<branch>` |
| `check` | verify | a second opinion on a plan, claim or finding, backed by commands and tests |
| `implement` | write | a bounded implementation; never commits |
| `implement-light` | write | boilerplate, fixtures, scaffolding |
| `research` | read + web | web and documentation lookups, with sources |
| `light` | read | summarising files or logs, drafting text |

The user may have changed these. `crew.py roles` shows the real setup.

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
`verify`).
- Codex's sandbox is a hard limit.
- Claude's tool list is a hard limit, and so is its sandbox on macOS and Linux.
- agy's file limits, and Claude's shell on native Windows, are instructions plus a git check.

See [references/permissions.md](references/permissions.md).

Other options:

| Option | Does |
|---|---|
| `--agent <alias or spec>` | use a specific agent, e.g. `codex:gpt-6.1-sol@high` (always honoured) |
| `--model`, `--effort` | override the model or reasoning effort |
| `--view hidden\|tab` | hidden, or in a terminal tab |
| `--no-hold` | close the tab when the task finishes |
| `--slug` | name the run folder |
| `--max-minutes` | kill the agent after this long |

### Waiting for results

Quick lookups can run in the foreground with `--wait`. Reviews and implementation usually take
2 to 15 minutes, and so does bigger research. Run those in a background shell if you have one and
keep working. If a wait times out, `crew.py collect <run folder>` picks the task up again.

Independent tasks can run in parallel. Parallel `write` tasks in one repository must touch
different files, and each task must say which.

`--wait` prints the status, the scope, any violations or changed files, and the agent's answer.
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

## Follow-ups

For Codex and Claude, `crew.py run --resume <run folder> --task "<what to change>" --wait`
continues the same session with the same scope.

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
terms. Check the user is happy with that before sending private material. Never put secrets in a
task, because run folders under `~/.crew/runs/` keep a copy.
