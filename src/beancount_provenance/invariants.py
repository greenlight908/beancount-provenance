"""Three structural invariants of a Beancount ledger, as pure functions.

Each judges only what is written in the ledger, so its verdict can only change with a
commit: that is why they run as a commit-time gate (pre-commit and PR CI) rather than
a periodic sweep. Ported from the mike.dotfiles WND checks 08-cost-basis,
10-currency-constraint and 11-one-commodity-per-leaf (mike.dotfiles#4236).

* **Cost basis.** An ``Assets:`` posting holding a *security* bought at a price with no
  cost (``10 VWRL @@ 1000 GBP``) records what was paid but not the lot's cost, so
  Beancount cannot compute a realised gain on sale. The fix is ``{}``.
* **Currency constraint.** Every ``Assets:``/``Liabilities:`` ``open`` pins the
  commodities it may hold, so ``bean-check`` rejects a wrong-commodity posting.
* **One commodity per open leaf.** An open balance-sheet account holds, and declares,
  at most one commodity; several make it a container with a leaf per commodity
  (the Beancount Cookbook's ``Assets:US:ETrade:Cash USD`` beside ``...:FB FB``).

Every rule has a reviewed escape hatch: metadata carrying a free-text reason.
"""

from __future__ import annotations

import datetime
import re
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from beancount.core import data

# Only balance-holding account types. Income/Expenses/Equity are flow accounts where
# mixed commodities are normal (a meter-reading counterweight in Equity).
BALANCE_SHEET_PREFIXES = ("Assets:", "Liabilities:")

IGNORE_COST_BASIS = "ignore-for-cost-basis-check"
IGNORE_CURRENCY_CONSTRAINT = "ignore-for-currency-constraint-check"
IGNORE_ONE_COMMODITY = "ignore-for-one-commodity-check"

# A 3-letter all-uppercase commodity is an ISO-4217-style currency, held at an FX rate.
# KNOWN FALSE-NEGATIVE: a 3-letter security ticker (VOD) is read as a currency. Widening
# this would flag every legitimate `USD @ 1.27 GBP` cash posting instead.
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

# Ubiquitous USD stablecoins, held at their peg. Deliberately small so it cannot rot:
# a ledger-specific token is marked in the ledger with `asset-class` instead.
_STABLECOIN_TICKERS = frozenset({"USDT", "USDC", "GUSD", "CUSD", "DAI", "BUSD", "TUSD", "USDP", "PYUSD", "FDUSD"})

# A `commodity` directive whose `asset-class` names it crypto, stablecoin or fiat is
# currency-like: the classification lives in the ledger, so no ticker is baked in here.
_CURRENCYLIKE_ASSET_CLASS_RE = re.compile(r"crypto|stablecoin|fiat", re.IGNORECASE)


def _where(entry: Any) -> dict[str, Any]:
    return {"file": Path(entry.meta.get("filename", "")).name, "line": entry.meta.get("lineno", 0)}


def _currency_like(entries: list, options_map: dict) -> set[str]:
    marked = set(options_map.get("operating_currency", []) or []) | _STABLECOIN_TICKERS
    for entry in entries:
        if isinstance(entry, data.Commodity) and _CURRENCYLIKE_ASSET_CLASS_RE.search(entry.meta.get("asset-class", "") or ""):
            marked.add(entry.currency)
    return marked


def find_price_only_holdings(entries: list, options_map: dict) -> list[dict]:
    """``Assets:`` postings holding a security with a price (``@``/``@@``) and no cost."""
    currencies = _currency_like(entries, options_map)
    flagged: list[dict] = []
    for entry in entries:
        if not isinstance(entry, data.Transaction) or entry.meta.get(IGNORE_COST_BASIS):
            continue
        for posting in entry.postings:
            if not posting.account.startswith("Assets:") or posting.units is None:
                continue
            commodity = posting.units.currency
            if commodity in currencies or _CURRENCY_RE.match(commodity):
                continue
            if posting.price is not None and posting.cost is None:
                flagged.append({"account": posting.account, "commodity": commodity, "date": str(entry.date), "payee": entry.payee or "", "narration": entry.narration or "", **_where(entry)})
    return flagged


def find_unconstrained_opens(entries: list) -> list[dict]:
    """Balance-sheet ``open`` directives that pin no commodity and carry no exemption."""
    return [{"account": entry.account, "date": str(entry.date), **_where(entry)} for entry in entries if isinstance(entry, data.Open) and entry.account.startswith(BALANCE_SHEET_PREFIXES) and not entry.currencies and not entry.meta.get(IGNORE_CURRENCY_CONSTRAINT)]


def find_mixed_leaves(
    holdings: dict[str, dict[str, Decimal]],
    declared: dict[str, list[str]],
    closed_on: dict[str, datetime.date],
    exempt: dict[str, str],
    as_of: datetime.date,
) -> list[dict]:
    """Balance-sheet accounts, still open at ``as_of``, that hold or permit >1 commodity.

    **Open accounts only** (a scope, not an exemption): a closed account cannot take a
    new posting, so finished books are left alone. A ``close`` dated after ``as_of``
    leaves the account open. **Both the balance and the declaration are judged**:
    ``open Assets:Broker VWRP,GBP`` holding only VWRP still permits a GBP posting
    tomorrow, and that permission is what let ``Liabilities:Octopus`` hold kWh meter
    readings beside money (mike.accounts#787). **No dust threshold**: a commodity is
    held when its balance is not exactly zero, since no unitless cutoff spans
    incomparable units (0.001 BTC is about GBP60).

    Each finding is ``{"account", "held", "declared"}``, worst first.
    """
    flagged: list[dict] = []
    for account in sorted(set(holdings) | set(declared)):
        if not account.startswith(BALANCE_SHEET_PREFIXES) or account in exempt:
            continue
        closed_date = closed_on.get(account)
        if closed_date is not None and closed_date <= as_of:
            continue
        held = sorted(c for c, n in holdings.get(account, {}).items() if n != 0)
        permitted = sorted(declared.get(account, []))
        if len(held) > 1 or len(permitted) > 1:
            flagged.append({"account": account, "held": held, "declared": permitted})
    flagged.sort(key=lambda f: (-max(len(f["held"]), len(f["declared"])), f["account"]))
    return flagged


def collect_holdings(entries: list) -> tuple[dict[str, dict[str, Decimal]], dict[str, list[str]], dict[str, datetime.date], dict[str, str], int]:
    """Holdings, declared commodities, close dates, exemptions, and the count of
    balance-sheet accounts opened (zero means the scan saw nothing)."""
    holdings: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    declared: dict[str, list[str]] = {}
    closed_on: dict[str, datetime.date] = {}
    exempt: dict[str, str] = {}
    for entry in entries:
        if isinstance(entry, data.Open):
            if entry.account.startswith(BALANCE_SHEET_PREFIXES):
                declared[entry.account] = list(entry.currencies or [])
            reason = entry.meta.get(IGNORE_ONE_COMMODITY)
            if reason:
                exempt[entry.account] = str(reason)
        elif isinstance(entry, data.Close):
            prior = closed_on.get(entry.account)
            if prior is None or entry.date < prior:
                closed_on[entry.account] = entry.date
        elif isinstance(entry, data.Transaction):
            for posting in entry.postings:
                if posting.units is not None and posting.units.number is not None:
                    holdings[posting.account][posting.units.currency] += posting.units.number
    return holdings, declared, closed_on, exempt, len(declared)
