# Writing a task for a delegated agent

The delegated agent starts from nothing. It never saw your conversation, and it only knows:

1. the text crew sends: a scope block that crew writes, the role's own text if the config has
   any, then your task;
2. files it can read in the working directory (`--cd`);
3. the instruction files its own CLI loads: codex reads `AGENTS.md`, claude reads `CLAUDE.md`.
   Anything that lives only in the other one, or only in your head, must go in the task.

So a good task can be understood by a capable engineer who just walked in. It names things
concretely: file paths, function names, commands, versions, URLs.

## A template

Plain Markdown headings work with every backend. Leave out a section that doesn't apply.

```markdown
## Goal
One or two sentences: what should be true when this is finished.

## Context
What exists now and why this is needed. Paths, names, the error message, the relevant
decision already made. Say which files to change and which to leave alone.

## Constraints
Only the rules that matter for this task: no new dependencies, keep the public API, match
the existing error-handling style, target Python 3.9, ...

## Done when
The exact commands that must pass, e.g. `pytest tests/parser -q` and `ruff check src/`.

## Report
What to put in the final answer and in what shape (see below).
```

crew's scope block already says what the agent may touch (read / verify / write, web, network)
and that it must not commit. Don't repeat it; add only the task-specific limits.

## Fit the task to the role

**review / check.** Ask for evidence with every finding: `file:line`, plus the command and output
that shows the problem, since these roles can run tests. Ask it to mark anything it could not
confirm as unverified. Name what matters most ("focus on concurrency in `worker.py`") so a long
diff doesn't get a shallow pass.

**implement.** Name the files it may change. Give the real check commands from the project's
README, CI config or `AGENTS.md`/`CLAUDE.md`, never "run the tests". Say what "done" means in
behaviour, not effort. Ask for a report of files changed, commands run with pass/fail counts, and
open questions.

**research.** List the questions numbered, and say the answer shape (a table, one line each, a
short list). Ask for a source URL per claim and the date or version the answer applies to. If
recency matters, say "as of today".

**light.** Say exactly what to produce (a summary under 200 words, a list of functions with
one-line descriptions). If it should not explore, say "use only the files named here".

## Things that make delegated tasks fail

- **Pointing at the conversation.** "Fix the bug we discussed" means nothing to the agent. Paste
  the error, name the file.
- **Several jobs in one task.** One task should produce one result you can check on its own.
  Dispatch the rest separately, in parallel if they're independent.
- **Overlapping parallel writes.** Two `write` tasks in the same repository must touch different
  files, and each task must say which.
- **A wrong starting assumption.** You can't talk to the agent while it runs. If you realise the
  task was based on something false, stop it (close its tab or kill the process), look at what
  it already changed, and dispatch a corrected task.
- **Tool-free work without saying so.** For pure reasoning (critique this plan, compare these
  options) write "answer from the text above; don't read files or run commands", otherwise
  coding agents go exploring.
- **Secrets.** Never put tokens, passwords or personal data in a task. It is saved under
  `~/.crew/runs/` and sent to the vendor.
