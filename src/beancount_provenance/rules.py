"""The counting rules and the ratchet.

Pure functions over loaded entries, so each rule is testable without a filesystem.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from beancount.core import data, flags

# Either ONE of these, non-empty, marks a transaction as traceable. `source` names the
# document it came from, `script` the code or agent that wrote it. `created_at` is a
# timestamp, not an origin, so it does not count on its own (W3C PROV: generatedAtTime
# is an attribute of provenance, not a substitute for wasDerivedFrom/wasGeneratedBy).
PROVENANCE_KEYS = ("source", "script")

METRICS = ("provenance", "payee", "narration")

# `<date> custom "wnd-baseline" "<metric>" "<count>"` in the ledger.
BASELINE_DIRECTIVE = "wnd-baseline"


def _blank(value: object) -> bool:
    return not str(value or "").strip()


def count_gaps(entries: Iterable[Any]) -> dict[str, int]:
    """Count transactions missing provenance, payee and narration.

    Per transaction, not per posting: provenance describes the entry's origin. The
    transactions beancount synthesises from a `Pad` at load time carry no metadata of
    their own and are exempt with their pad; the assertion that drives them is the
    thing with a source.
    """
    gaps: dict[str, int] = dict.fromkeys(METRICS, 0)
    for entry in entries:
        if not isinstance(entry, data.Transaction) or entry.flag == flags.FLAG_PADDING:
            continue
        meta = entry.meta or {}
        if all(_blank(meta.get(key)) for key in PROVENANCE_KEYS):
            gaps["provenance"] += 1
        if _blank(entry.payee):
            gaps["payee"] += 1
        if _blank(entry.narration):
            gaps["narration"] += 1
    return gaps


def read_baselines(entries: Iterable[Any]) -> dict[str, int]:
    """The ledger's declared baselines, for the metrics we know.

    A malformed or unknown declaration is ignored and so surfaces as missing, which
    fails with the correct line to paste -- better than trusting a number nobody can
    parse.
    """
    out: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, data.Custom) or entry.type != BASELINE_DIRECTIVE:
            continue
        values = [getattr(v, "value", v) for v in (entry.values or [])]
        if len(values) != 2 or str(values[0]) not in METRICS:
            continue
        try:
            out[str(values[0])] = int(str(values[1]))
        except ValueError:
            continue
    return out


def evaluate(gaps: dict[str, int], baselines: dict[str, int]) -> dict[str, Any]:
    """The gate's verdict: pass only when every metric sits exactly at its baseline.

    Growth is a new gap. Slack (a baseline above the count) fails too: it is room a new
    gap could spend unseen, so the baseline is ratcheted down in the same change that
    burned the backlog. A missing baseline is unmeasured, never clean.
    """
    grown: list[dict[str, Any]] = []
    slack: list[dict[str, Any]] = []
    missing: list[str] = []
    for metric in METRICS:
        if metric not in baselines:
            missing.append(metric)
            continue
        row = {"metric": metric, "count": gaps[metric], "baseline": baselines[metric], "delta": gaps[metric] - baselines[metric]}
        if row["delta"] > 0:
            grown.append(row)
        elif row["delta"] < 0:
            slack.append(row)
    fix = [f'custom "{BASELINE_DIRECTIVE}" "{m}" "{gaps[m]}"' for m in missing] + [f'custom "{BASELINE_DIRECTIVE}" "{r["metric"]}" "{r["count"]}"' for r in slack]
    return {"ok": not (grown or slack or missing), "grown": grown, "slack": slack, "missing": missing, "fix": fix}
