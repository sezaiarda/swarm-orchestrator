"""Resource tracking: what the host, each worker and each gated build cost.

The build gate (``[build].max_concurrent``, ``[build].jobs``) and the worker
count (``[swarm].max_workers``) are sized by guesswork unless something records
what a build and a worker actually take. This package is that record:

- :mod:`.host` reads the host from ``/proc`` (CPU, load, pressure, memory split
  into anon and page cache, swap, disk throughput);
- :mod:`.ptree` snapshots the process table and sums CPU seconds, resident
  anon memory and IO bytes over a process tree;
- :mod:`.disk` measures real free disk (WSL-aware) and directory sizes, rarely;
- :mod:`.builds` finds the build holding each slot (the gate's
  ``buildsem/events.jsonl``, or the slot's ``flock`` holder) and summarises it;
- :mod:`.store` keeps the samples (full resolution for a day, minute aggregates
  for a month, both bounded);
- :mod:`.sampler` is the thread the supervisor owns that ties them together;
- :mod:`.capacity` and :mod:`.view` turn the history into ``swarm resources``.

Everything reads ``/proc`` and the state dir only. Nothing here ever signals or
throttles a process: the idle-holder warning says what it saw and stops there.
"""
