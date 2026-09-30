"""When will each campaign finish: a simulated forecast, not a rate.

The dashboard used to time the rest of the ledger on one number, the swarm's
recent finishes per hour. That is right while many rows are ready at once and
wrong exactly when the owner asks: a campaign whose last rows are one chain in
one repo, rows waiting for an ``after:`` date, rows behind an owner-run row, and
campaigns that share the same workers. So the forecast is made the way the swarm
actually works through the ledger:

* :mod:`.record` — what past phases took, read from this machine's supervisor
  log and the ledger's git history;
* :mod:`.model` — a log-normal work time per row, pooled from the whole history
  down to the row's kind, repo and campaign where there is enough of each;
* :mod:`.hazards` — the rarer things a phase runs into: an owner question, a
  failed attempt, follow-up rows filed while a campaign runs;
* :mod:`.plan` — the open ledger as the swarm will schedule it, and what is
  left out of the forecast because it waits on the owner;
* :mod:`.holds` — when the swarm may not launch: a scheduled pause, a usage cap
  now, and the caps its own burn will reach;
* :mod:`.sim` — a seeded Monte Carlo replay that picks rows with the
  supervisor's own :func:`ledger.ready`;
* :mod:`.forecast` — the per-campaign and overall finish ranges it produces;
* :mod:`.engine` — the background worker the dashboard, the web board and
  ``swarm status`` all read, so every view says the same thing.

The method behind every constant is in ``docs/research/eta-engine-literature.md``;
the constants were fitted offline against recorded phase histories.
"""
