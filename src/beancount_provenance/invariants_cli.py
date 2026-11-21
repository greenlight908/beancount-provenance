"""Command-line gate: `check-beancount-invariants [--ledger main.beancount]`.

Runs the cost-basis, currency-constraint and one-commodity-per-leaf rules. Designed for
pre-commit (`pass_filenames: false`) and CI. Prints one JSON object and exits 0 when
the ledger satisfies all three, 1 when any rule finds a defect, and 2 when the ledger
could not be read or yielded no balance-sheet accounts: "I could not look" must never
look like a pass.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .invariants import IGNORE_COST_BASIS, IGNORE_CURRENCY_CONSTRAINT, IGNORE_ONE_COMMODITY, collect_holdings, find_mixed_leaves, find_price_only_holdings, find_unconstrained_opens

_EPILOG = f"""\
cost_basis              an Assets: security bought at a price needs a {{}} cost
currency_constraint     an Assets:/Liabilities: open pins its commodities
one_commodity_per_leaf  an open Assets:/Liabilities: account holds and permits one commodity

A reviewed exemption is metadata carrying a free-text reason:
  {IGNORE_COST_BASIS} (on the transaction)
  {IGNORE_CURRENCY_CONSTRAINT} (on the open)
  {IGNORE_ONE_COMMODITY} (on the open)
"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check-beancount-invariants",
        description="Fail if a Beancount ledger breaks the cost-basis, currency-constraint or one-commodity-per-leaf rule.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--ledger", type=Path, default=Path("main.beancount"), help="Root ledger file (default: main.beancount).")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps({"version": __version__, **payload}, indent=2))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    ledger: Path = args.ledger
    if not ledger.is_file():
        _emit({"ok": False, "ledger": str(ledger), "error": "ledger not found"})
        return 2

    from beancount import loader

    # Never trust beancount's pickle cache: a cached load reports on a ledger the gate
    # never parsed. (This also deletes the cache file; the hook runs alone, so nothing
    # races it.)
    loader.initialize(use_cache=False)
    entries, errors, options_map = loader.load_file(str(ledger))
    if errors:
        _emit({"ok": False, "ledger": str(ledger), "error": f"ledger has {len(errors)} validation error(s); run bean-check", "first_error": str(getattr(errors[0], "message", errors[0]))})
        return 2

    holdings, declared, closed_on, exempt, accounts = collect_holdings(entries)
    if accounts == 0:
        _emit({"ok": False, "ledger": str(ledger), "error": "no Assets:/Liabilities: accounts found; the scan saw nothing, which is not a clean ledger"})
        return 2

    findings = {
        "cost_basis": find_price_only_holdings(entries, options_map),
        "currency_constraint": find_unconstrained_opens(entries),
        # "Can this account be posted to NOW?" is the question, so today is the as-of.
        "one_commodity_per_leaf": find_mixed_leaves(holdings, declared, closed_on, exempt, datetime.date.today()),
    }
    ok = not any(findings.values())
    _emit({"ok": ok, "ledger": str(ledger), "balance_sheet_accounts": accounts, **findings})
    if not ok:
        failing = ", ".join(f"{rule} ({len(items)})" for rule, items in findings.items() if items)
        print(f"beancount-invariants: gate refused: {failing}; see the JSON on stdout.", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
