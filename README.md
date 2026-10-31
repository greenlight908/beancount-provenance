# beancount-provenance

A pre-commit hook that fails if a **Beancount ledger gains a transaction nobody can trace**:
one with no `source` or `script` metadata, no payee, or no narration.

Most ledgers carry a historical backlog of such entries, so the gate does not demand zero.
The ledger declares its backlog and the count may only shrink:

```beancount
2026-10-31 custom "wnd-baseline" "provenance" "17"
2026-10-31 custom "wnd-baseline" "payee" "1"
2026-10-31 custom "wnd-baseline" "narration" "0"
```

## Rules

- **Provenance** is a non-empty `source` (the document an entry came from) or `script` (the
  importer, tool or agent that wrote it). `created_at` alone is a timestamp, not an origin.
- **Per transaction**, not per posting.
- **Pads are exempt.** The `P` transactions beancount generates from a `pad` carry no
  metadata of their own; the `balance` assertion behind them is what has a source.
- **The gate passes only when every count equals its baseline.** Growth fails (a new gap).
  Slack fails too: a baseline above the count is room a new gap could spend unseen, so lower
  it in the same change. A missing baseline fails, with the lines to paste in `fix`.
- **A ledger that does not load exits 2**, never 0: "I could not look" is not a pass.

Known limit: a change that fixes one old gap and adds one new one nets to zero and passes.

## Usage

```yaml
repos:
  - repo: https://github.com/greenlight908/beancount-provenance
    rev: <commit sha>  # v0.1.0
    hooks:
      - id: check-beancount-provenance
```

In CI, run the same pinned hook: `uvx pre-commit run check-beancount-provenance --all-files`.

By hand: `check-beancount-provenance [--ledger main.beancount]`. Output is JSON; exit 0 pass,
1 refused, 2 could not read the ledger.

The hook runs in pre-commit's own environment, so a ledger that loads a custom plugin must
list it under the hook's `additional_dependencies`.

## Prior art

W3C PROV (`wasDerivedFrom`, `wasGeneratedBy`); the bookkeeping audit-trail requirement that
each entry trace to a source document; and ratcheted lint baselines (pyright, rubocop's
`.rubocop_todo.yml`) for the backlog.
