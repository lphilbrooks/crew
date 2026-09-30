# Checking results

Treat every answer as a claim. The agent's summary of what it did, and its report that the tests
passed, can both be wrong. Check against the repository before you act on an answer or commit
anything.

## Start with the status

| Status | What to do |
|---|---|
| `ok` | Check the result as below. |
| `empty` | Read `raw.log`. Usually the task was unclear or the agent hit a limit. |
| `agent-error` | `note` has the agent's own error (usage limit, wrong model name, crash). Fix it and rerun. |
| `permission-denied` | It needed something outside its scope (`denied_tools`). Widen the scope only if the task really needs it. |
| `scope-violation` | See below before doing anything else. |
| `timeout` | Look at what it changed, then split the task or allow more time. |
| `harness-error` | crew's runner failed or its window was closed. Rerun. |

**Scope violations.** `violations` lists repository files that changed during a `read` or
`verify` task. Your own edits during the run are included, so remove those from the list first.
Then check each remaining file and revert only those. Never reset the whole tree.

## After an implement task

1. **See what changed.** Start from `touched_files`, confirm with `git status`, and open any new
   files, since a plain diff won't show them.
2. **Check test changes first.** Edited, deleted or skipped tests, or looser assertions, make
   passing checks mean less. Raise any test change the task didn't ask for.
3. **Run the project's checks yourself.**
4. **Compare the diff with the task.** Did it do all of it? Did it touch things it was told to
   leave alone? Did it make design choices the task didn't settle? Those are for you or the user.
5. **Look for what checks miss:**
   - success reported without the work being done;
   - errors caught and turned into defaults;
   - calls to functions or options that don't exist in the installed version;
   - a new helper that duplicates an existing one;
   - leftover debug code or comments narrating the change;
   - tests that only exercise mocks.
6. **Ask for fixes in the same session** (Codex and Claude):
   `crew.py run --resume <run folder> --task "Use the retry helper in net.py; remove the new one." --wait`.
   Then check the new result the same way.
7. **Commit it yourself** once the checks pass and the diff looks right. Until then the working
   tree is the only copy of the work, so don't reset, checkout, stash or clean it.

## After a review or check task

- Look at each finding separately: open the cited code or run the reproduction. Sort findings
  into confirmed, rejected (with a reason) and unsure.
- A finding backed by command output beats one the agent reasoned its way to. With
  `--access verify` you can ask for reproductions, so do.
- Fix confirmed findings, with a failing test first if the project works that way, then run the
  checks again.
- If a disputed point matters, run a `check` task on a different vendor (`--agent`). Give it both
  positions and ask for evidence.

## Tell the user

Say what the agent found or changed, which findings you accepted or rejected and why, what you
chose not to act on, and any decision still open. Ask before widening the scope of the work.
