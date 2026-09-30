# Writing a task

The delegated agent hasn't seen your conversation. It knows only:

1. the text crew sends: crew's scope note, any role text, then your task;
2. the files it can read in `--cd`;
3. its own instruction file: Codex reads `AGENTS.md`, Claude reads `CLAUDE.md`.

Anything else has to be in the task. Write it for a capable engineer who just walked in: name
the files, functions, commands, versions and URLs.

## Template

Drop any section you don't need.

```markdown
## Goal
What should be true when this is done, in one or two sentences.

## Context
What exists now and why this matters. Paths, names, the error message, decisions already made.
Which files to change and which to leave alone.

## Constraints
Only the rules that matter here: no new dependencies, keep the public API, target Python 3.9...

## Done when
The exact commands that must pass, e.g. `pytest tests/parser -q`.

## Report
What the answer should contain, and in what shape.
```

crew already tells the agent its access level and not to commit. Add only the limits specific to
this task.

## By role

- **review / check:** ask for `file:line` and the command output behind each finding. Ask it to
  mark anything it couldn't confirm. Say what to focus on, so a large diff gets a proper look.
- **implement:** say which files it may change. Give the project's real check commands, not "run
  the tests". Ask for a report of files changed, commands run with pass/fail counts, and open
  questions.
- **research:** number the questions and say what shape the answer should take. Ask for a source
  per claim, and the date or version it applies to.
- **light:** say exactly what to produce, e.g. "a summary under 200 words". If it shouldn't
  explore, say "use only the files named here".

## Common mistakes

- **Referring to the conversation.** "Fix the bug we discussed" means nothing to the agent. Paste
  the error and name the file.
- **Several jobs in one task.** Split them, and run them in parallel if they're independent.
- **Parallel writes to the same files.** Give each `write` task its own files, and say which.
- **A wrong assumption.** You can't talk to the agent mid-run. Stop it, look at what it changed,
  and send a corrected task.
- **Unwanted exploring.** For pure reasoning, say "answer from the text above; don't read files or
  run commands".
- **Secrets.** Never include tokens, passwords or personal data. They'd be saved in the run
  folder and sent to the vendor.
