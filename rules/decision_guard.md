
# Decision Guard: Behavior-Contract Changes Reach the User

A behavior decision can flip quietly, whether the user or the agent started it: a requirement
("Cupertino values must work") turns into code that changes pinned behavior and exposes a problem
elsewhere. Tests are where such flips leave a trace, so the trace is checked mechanically.

- **Commit gate (`[DECISION GUARD]` in `hooks/pre_tool_guard.py`)**: `git commit` in a product
  repo (not personal repos) is denied while the commit removes, renames, or adds tests, or
  changes asserts or numbers inside existing tests. Triage every listed test with
  `python3 ~/.gemini/config/hooks/decision_guard.py ack --repo <root> --hash <H> --conv <id>
  --root <id> --triage "<Test>=<class>: <why>"` (classes: `bugfix`, `refactor`, `rename`,
  `format`, `gating`, `feature`, `prune`, `policy`). `scan --repo <root>` previews the list.
- **Honest classification**: use `policy` whenever expected behavior changes, including changes
  that follow the user's own request; `bugfix` only moves code toward behavior that was already
  agreed. Deleting or thinning tests is `prune` and still needs a reason.
- **Policy = decision card + user reply**: `policy` needs `--card <file>` with all fields
  `Решение`, `Было → Стало`, `Критерий-победитель`, `Критерий-проигравший`, `Основание`
  (`измерено: <program/test>` or `допущение`), `Не измерено`, `Что обнажается`, `Противоречит`.
  The commit waits until the user answers after the ack. A subagent stops and reports the card
  to its parent, which asks the user.
- **Visibility (`stop_guard.py`)**: every test triaged in the turn (by the chat or its subagents)
  must appear in the final reply as one line (test, change, class, why); a policy ack also needs
  the card in the reply.
- **Before implementing a requirement** that contradicts behavior pinned by existing tests or an
  earlier decision, show the card first instead of waiting for the gate.
