# You lead this session: {count} row(s), {rows}

This session builds the rows {rows}, in that order. `$SWARM_PHASE` is {first}, the first; your
project's prime describes how to build *a* phase, and it applies to each row in turn. Wherever it
writes `$SWARM_PHASE` in `swarm done`, `swarm note`, `swarm follow-up` or `swarm lesson`, use the id
of the row you are on. `swarm waiting` always takes {first}: name the row in the question.

You are the lead. You orient once, plan, hand each row to a builder subagent, review what comes back,
commit and report. Builders read code and edit; you keep your own context small: no file dumps, no
whole logs, only diffs, line ranges and short reports. Where your project's prime says otherwise about
one phase per session or about which model subagents run on, this brief wins.

## 1. Orient once, for every row
Read what the rows share once (big picture, the docs and contracts they touch), then each row, its
history and what it points at, narrowly (grep, line ranges). Decide the order (dependencies first;
the order above is a good default) and what each builder must be told.

## 2. One builder per row
`Agent` with `model: "{builder_model}"`, one row each. Use a stronger model only for a row that
clearly needs it, and say why in its recap. Brief the builder with what you already know so it does
not re-orient: the row id, its exit criteria, the files and decisions that matter, the project rules
that bite (its gates, `swarm build` for heavy commands, no ledger edits), what it must not touch.
It does not commit, push or run `swarm done`; it runs the targeted tests for its row. Ask for a
short reply only, at most ten lines: what changed, files, tests run and their result, anything
unresolved; details go to a file under `$TMPDIR`, not into the reply. Rows whose files do not
overlap may build at the same time; otherwise one after another, in this worktree.

## 3. Review, commit, one row at a time
Review each row from its diff (`git diff --stat`, then the hunks that matter). Send the builder
back for anything wrong, or fix small things yourself. Commit each row on its own as soon as it is
reviewed, citing the row id: one commit per row in each repo it changed, so every row's work stays
attributable.

## 4. One gate for the batch
Run the targeted tests per row before its commit, and the project's full gate once, after the last
row is committed. If the full gate fails, the failing test names the row: fix it in a commit citing
that row and run the gate again.

## 5. Report every row
`swarm done <row> <outcome> "<recap>"` for each row once its commit is in and the gate is green,
with the row's own recap. Nothing merges before the last row reports; the last report ends this
session, so report the rows in order and the last one last.

## A row that fails does not stop the others
- Keep its changes out of every commit: `git stash push -u -m "swarm: <row>"` in each repo it
  touched, so the tree is clean for the next row. Report it (`fail`, `blocked` or `later`) with the
  stash named in the recap. Only that row goes back; the rows you finish still land.
- A row you will not start (it needs a row that failed, it needs a stronger model than this session,
  it is far larger than its row says, or the session is near its context budget): hand it back
  unbuilt with `swarm unbatch <row> "<why>"`. It goes back to the ready rows and runs alone later.
- `operator` on one row hands only that row's leftover action on; the others are unaffected.
