"""Command-line gate: `check-beancount-provenance [--ledger main.beancount]`.

Designed for pre-commit (`pass_filenames: false`) and CI. Prints one JSON object and
exits 0 when every metric sits at its baseline, 1 when the gate refuses (growth, slack
or a missing baseline), and 2 when the ledger could not be read: "I could not look"
must never look like a pass.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .rules import count_gaps, evaluate, read_baselines

_EPILOG = """\
Every transaction carries a non-empty `source` or `script`, a payee and a narration.
The historical backlog is declared in the ledger and may only shrink:

  2026-10-31 custom "wnd-baseline" "provenance" "0"
  2026-10-31 custom "wnd-baseline" "payee" "0"
  2026-10-31 custom "wnd-baseline" "narration" "0"

A baseline above the count fails too: lower it in the same change.
"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check-beancount-provenance",
        description="Fail if a Beancount ledger gains a transaction with no provenance, payee or narration.",
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
    # never parsed.
    loader.initialize(use_cache=False)
    entries, errors, _ = loader.load_file(str(ledger))
    if errors:
        _emit({"ok": False, "ledger": str(ledger), "error": f"ledger has {len(errors)} validation error(s); run bean-check", "first_error": str(getattr(errors[0], "message", errors[0]))})
        return 2

    counts = count_gaps(entries)
    verdict = evaluate(counts, read_baselines(entries))
    today = datetime.date.today().isoformat()
    verdict["fix"] = [f"{today} {line}" for line in verdict["fix"]]
    _emit({"ledger": str(ledger), "counts": counts, **verdict})
    if not verdict["ok"]:
        print("beancount-provenance: gate refused; see the JSON on stdout (`fix` lists baseline lines to add or replace).", file=sys.stderr)
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
