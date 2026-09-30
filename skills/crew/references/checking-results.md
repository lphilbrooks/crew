# Checking what comes back

A delegated agent's answer is a claim. Its summary of what it did, and its report that tests
passed, can both be wrong. Check the result against the repository before you act on it or commit
it.

## Start with the status

| Status | What to do |
|---|---|
| `ok` | Check the result as below. |
| `empty` | No answer. Read `raw.log`; usually the task was unclear or the agent hit a limit. |
| `agent-error` | `note` has the agent's own error (usage limit, bad model name, crash). Fix and re-run. |
| `permission-denied` | The agent needed something outside its scope (`denied_tools`, or agy's missing allow rule). Widen the scope only if the task really needs it. |
| `scope-violation` | Files changed that the access level forbids. See below before doing anything else. |
| `timeout` | Killed at `--max-minutes`. Look at what it changed, then split the task or allow more time. |
| `harness-error` | crew's runner failed or its window was closed. `note` says which. Re-run. |

### Scope violations

`violations` lists repository files that changed during a `read` or `verify` task. The check
compares before and after, so **your own edits during the run are included**. First remove from
the list any file you changed yourself. Then look at each remaining file (`git diff <file>`, or open
it if it's new) and revert only those. Never reset the whole tree.

## Results from implement tasks

Work through these in order:

1. **See what changed.** `touched_files` in `result.json` is the list. Confirm it with
   `git status`, and open new (untracked) files directly, since a plain diff doesn't show them.
2. **Look at test changes first.** If existing tests were edited, deleted, skipped or had their
   assertions loosened, the passing checks mean less. Treat any test change the task didn't ask
   for as a problem to raise, not a fix to accept.
3. **Run the project's checks yourself.** Don't rely on the agent's report of them.
4. **Read the diff against the task.** Did it do all of it? Did it change things it was told to
   leave alone? Did it make design choices the task didn't settle? Those are for you or the user
   to decide.
5. **Look for problems checks miss:**
   - code that reports success without doing the work, or returns canned data;
   - errors caught and silently turned into a default value;
   - calls to functions, options or library features that don't exist in the installed version;
   - a new helper, client or pattern that duplicates one the codebase already has;
   - leftover debug output, unused code, or comments that narrate the change;
   - tests that only exercise mocks, or copy-paste variants of one test.
6. **Ask for fixes in the same session** where the backend supports it (codex, claude). Send only
   what needs to change:
   `crew.py run --resume <taskdir> --task "Use the existing retry helper in net.py; drop the new one." --wait`
   Then check the new result the same way.
7. **Commit it yourself** once the checks pass and the diff is right. Until then the uncommitted
   working tree is the only copy of the work, so don't reset, checkout, stash or clean it before
   you've looked at it.

## Results from review and check tasks

- Treat each finding separately. Open the cited code, or run the reproduction the agent gave,
  and sort findings into confirmed, rejected (say why) and unsure.
- A finding backed by a command and its output is stronger than one the agent labels as
  reasoning. With `--access verify` you can ask it to reproduce findings, so ask.
- Fix confirmed findings (add a test that fails first where the project works that way), then
  run the checks again.
- If a disputed point matters, get a view from a different model family: run a `check` task
  with `--agent` set to another vendor, give both positions, and ask it to decide with evidence.

## Tell the user

Report what the agent concluded or changed, which findings you accepted and which you rejected
(with reasons), anything you chose not to act on, and any decision the task left open. If
following up would widen the scope beyond what was asked, ask first.
