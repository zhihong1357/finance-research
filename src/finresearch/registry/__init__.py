"""The registry: the machine-readable part of governance (docs/architecture.md section 3).

* ``registry/hypotheses/*.toml``  hypothesis cards; the test plan is frozen at registration
* ``registry/trials.jsonl``        trial ledger; append-only, hash-chained, written by code
* ``registry/owner_decisions.jsonl`` the owner's decisions (vault access, gates, capital);
  append-only, hash-chained
* ``registry/relations/*.toml``    no-arbitrage relations
* ``registry/sources/*.toml``      data source catalogue

``finresearch registry check`` validates all of it, including the protocol
rules that span files; ``finresearch registry check-append-only`` compares the
append-only files with an earlier commit.
"""
