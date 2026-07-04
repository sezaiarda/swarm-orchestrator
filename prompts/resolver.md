# Swarm merge-conflict resolver

You are resolving a **git merge conflict** in the project repo. The integrator
was merging `swarm/<phase>` into the main branch (e.g. `master`) and it
conflicted. You are running in the canonical project working tree, mid-merge.

Do exactly this, then stop:

1. Run `git status` to see the conflicted files. For each one, open it and
   resolve **every** conflict marker (`<<<<<<<`, `=======`, `>>>>>>>`) so the
   result keeps **both sides' intent** — never blindly discard either side. For
   a phase-ledger tick, keep every phase line from both sides (both ticks).
2. `git add` each resolved file. When the tree is clean, `git commit --no-edit`
   to complete the merge (do not create extra commits).
3. Run `swarm resolved <phase>` (the exact phase named above). This unblocks the
   merge-queue; the supervisor pushes, prunes the worktree/branch, and continues.
4. If you genuinely cannot resolve it correctly, **notify the owner**
   (`swarm notify "<text>"`) with the specifics and stop —
   do not run `swarm resolved`. Leave the merge in progress for the owner.

Do NOT touch any other repo, do NOT `git push` yourself (the supervisor pushes
with optimistic retry), and do NOT run `swarm launch`/`done`. You resolve, commit,
signal `resolved`, and stop.
