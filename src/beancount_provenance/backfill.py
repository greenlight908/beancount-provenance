"""Backfill provenance onto untraceable transactions, from evidence they record.

mike.dotfiles ADRs 0014/0015. Old importers often wrote their own keys (an import file,
an API id, a ticket number) but no `source`/`script`. A ledger declares which of its
keys are evidence, in `.beancount-provenance.toml` beside `main.beancount`:

    [[backfill.rule]]
    id = "rt-ticket"
    when_any = ["cbdrt_id"]          # the rule applies if any of these keys is present
    match = "^RT\\d+$"               # optional: regex on that key's value
    source = "RT ticket {cbdrt_id}"  # template over the rule's own `when_any` keys
    script = "{imported_with_file}"  # optional template; dropped if its key is absent

First matching rule wins. An entry no rule matches gets `source: "legacy"`, the explicit
unknown, never a guess; no config file means every gap is `legacy`. The file an entry
lives in is never evidence: it says where the entry is kept, not where it came from.

Every stamp carries `provenance: "inferred <date> by beancount-provenance backfill rule
<id>"`, so an inferred stamp is never mistaken for one written at entry time, and a rule
found wrong can be fixed and its entries found again.

    backfill-beancount-provenance            # plan only (JSON)
    backfill-beancount-provenance --apply    # write the stamps

A gap whose `source` or `script` key is present but blank is REFUSED, not stamped:
inserting a second key would be a duplicate. Those are listed for a human.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import string
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from beancount.core import data, flags

from .rules import LEGACY_SOURCE, PROVENANCE_KEYS

TOOL = "beancount-provenance backfill"
CONFIG_NAME = ".beancount-provenance.toml"
NO_EVIDENCE = "no-evidence"


@dataclass(frozen=True)
class Rule:
    id: str
    when_any: tuple[str, ...]
    source: str
    match: str | None = None
    script: str | None = None


@dataclass(frozen=True)
class Stamp:
    rule: str
    source: str
    script: str | None = None


def _blank(value: object) -> bool:
    return not str(value or "").strip()


def _fields(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


def _render(template: str, meta: dict[str, Any]) -> str | None:
    """The template filled from `meta`, or None if any key it names is blank."""
    if any(_blank(meta.get(name)) for name in _fields(template)):
        return None
    return template.format_map({name: str(meta[name]).strip() for name in _fields(template)})


def classify(meta: dict[str, Any], rules: list[Rule]) -> Stamp:
    """The stamp an untraceable entry's own metadata supports. First match wins."""
    for rule in rules:
        # Presence, not value: an export writes every column, so an empty one is still
        # evidence of the export. A `source` template still needs its value (see _render).
        present = next((k for k in rule.when_any if k in meta), None)
        if present is None:
            continue
        if rule.match and not re.search(rule.match, str(meta[present])):
            continue
        source = _render(rule.source, meta)
        if source is None:
            continue
        script = _render(rule.script, meta) if rule.script else None
        return Stamp(rule.id, source, script)
    return Stamp(NO_EVIDENCE, LEGACY_SOURCE)


def load_rules(path: Path) -> list[Rule]:
    """The ledger's declared rules; a missing file means none (everything is `legacy`)."""
    if not path.exists():
        return []
    raw = tomllib.loads(path.read_text(encoding="utf-8")).get("backfill", {}).get("rule", [])
    rules = []
    for item in raw:
        rule = Rule(item["id"], tuple(item["when_any"]), item["source"], item.get("match"), item.get("script"))
        if rule.id == NO_EVIDENCE:
            raise ValueError(f"rule id {NO_EVIDENCE!r} is reserved for the legacy fallback")
        # `source` may name only the rule's own evidence keys, so it cannot render from
        # something the rule never checked for.
        stray = _fields(rule.source) - set(rule.when_any)
        if stray:
            raise ValueError(f"rule {rule.id!r}: source names keys outside when_any: {sorted(stray)}")
        rules.append(rule)
    return rules


@dataclass(frozen=True)
class Planned:
    path: Path
    lineno: int
    payee: str | None
    stamp: Stamp


@dataclass
class Plan:
    today: str
    stamps: list[Planned] = field(default_factory=list)
    refused: list[dict[str, Any]] = field(default_factory=list)


class LedgerError(Exception):
    pass


def plan(ledger: Path, rules: list[Rule], today: str) -> Plan:
    from beancount import loader

    loader.initialize(use_cache=False)
    entries, errors, _ = loader.load_file(str(ledger))
    if errors:
        raise LedgerError(f"ledger has {len(errors)} validation error(s); run bean-check")
    out = Plan(today)
    for entry in entries:
        if not isinstance(entry, data.Transaction) or entry.flag == flags.FLAG_PADDING:
            continue
        meta = entry.meta or {}
        if not all(_blank(meta.get(k)) for k in PROVENANCE_KEYS):
            continue
        if any(k in meta for k in PROVENANCE_KEYS):
            out.refused.append({"file": meta["filename"], "lineno": meta["lineno"], "reason": "blank provenance key present"})
            continue
        out.stamps.append(Planned(Path(meta["filename"]), meta["lineno"], entry.payee, classify(meta, rules)))
    return out


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _lines_for(stamp: Stamp, indent: str, today: str) -> list[str]:
    lines = [f"{indent}source: {_quote(stamp.source)}\n"]
    if stamp.script:
        lines.append(f"{indent}script: {_quote(stamp.script)}\n")
    lines.append(f"{indent}provenance: {_quote(f'inferred {today} by {TOOL} rule {stamp.rule}')}\n")
    return lines


def apply(p: Plan) -> None:
    """Insert each stamp directly under its transaction's header line."""
    by_file: dict[Path, list[Planned]] = {}
    for planned in p.stamps:
        by_file.setdefault(planned.path, []).append(planned)
    for path, items in by_file.items():
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        for planned in sorted(items, key=lambda x: x.lineno, reverse=True):
            header = planned.lineno - 1
            following = lines[header + 1] if header + 1 < len(lines) else ""
            indent = following[: len(following) - len(following.lstrip())] or "  "
            lines[header + 1 : header + 1] = _lines_for(planned.stamp, indent, p.today)
        path.write_text("".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    from . import __version__

    parser = argparse.ArgumentParser(prog="backfill-beancount-provenance", description="Stamp provenance onto transactions that have none, from evidence they record.")
    parser.add_argument("--ledger", type=Path, default=Path("main.beancount"), help="Root ledger file (default: main.beancount).")
    parser.add_argument("--config", type=Path, help=f"Rules file (default: {CONFIG_NAME} beside the ledger).")
    parser.add_argument("--apply", action="store_true", help="Write the stamps (default: plan only).")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    config = args.config or args.ledger.parent / CONFIG_NAME
    try:
        rules = load_rules(config)
        p = plan(args.ledger, rules, datetime.date.today().isoformat())
    except (LedgerError, ValueError, OSError) as exc:
        print(json.dumps({"version": __version__, "ok": False, "error": str(exc)}))
        return 2
    if args.apply:
        apply(p)
    root = args.ledger.resolve().parent
    print(
        json.dumps(
            {
                "version": __version__,
                "ok": True,
                "applied": args.apply,
                "config": str(config) if config.exists() else None,
                "stamps": len(p.stamps),
                "by_rule": dict(Counter(s.stamp.rule for s in p.stamps)),
                "by_file": dict(Counter(str(s.path.resolve().relative_to(root)) if s.path.resolve().is_relative_to(root) else str(s.path) for s in p.stamps).most_common()),
                "refused": p.refused,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
