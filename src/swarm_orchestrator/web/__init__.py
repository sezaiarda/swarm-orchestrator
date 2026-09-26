"""``swarm web``: the read-only Kanban board, reached over Tailscale.

One concept per module: :mod:`.rows` reads the ledger's rows and headings,
:mod:`.campaigns` says what each campaign is, :mod:`.board` places every phase in
a column, :mod:`.detail` opens one card, :mod:`.redact` strips credentials,
:mod:`.feed` rebuilds the board once per change, :mod:`.server` serves it and
:mod:`.lifecycle` starts, stops and finds it. Importing this package imports
none of them, so ``swarm done`` never pays for ``http.server``.
"""
