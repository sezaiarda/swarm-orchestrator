"""``swarm web``: the read-only web board, reached over Tailscale.

One concept per module: :mod:`.rows` reads the ledger's rows and headings,
:mod:`.campaigns` says what each campaign is, :mod:`.board` places every phase in
a column, :mod:`.detail` opens one card, :mod:`.redact` strips credentials,
:mod:`.graph` picks the rows the Graph tab draws and :mod:`.layout` lays them
out, :mod:`.usagechart` charts the usage windows, :mod:`.reshist` folds the
resource sampler's history into chart buckets and :mod:`.resview` shapes the
Resources tab from it, :mod:`.feed` rebuilds the board
once per change, :mod:`.server` serves it and
:mod:`.lifecycle` starts, stops and finds it. Importing this package imports
none of them, so ``swarm done`` never pays for ``http.server``.
"""
