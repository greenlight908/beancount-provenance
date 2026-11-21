"""Tests for the three ledger-invariant rules: cost basis, currency constraint, and
one commodity per open balance-sheet leaf.

Ported from the mike.dotfiles WND checks they replace (08, 10 and 11), so every
decision those tests pinned is still pinned: the closed-account scope, the exemptions,
the absence of a dust threshold, the currency-like classes and the container shape.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from beancount import loader

from beancount_provenance.invariants import _STABLECOIN_TICKERS, find_mixed_leaves, find_price_only_holdings, find_unconstrained_opens

# --- cost basis ---------------------------------------------------------------------

COST_LEDGER = """\
option "operating_currency" "GBP"

2010-01-01 commodity CELO
  name: "Celo"
  asset-class: "cryptocurrency"

2010-01-01 open Assets:Crypto
2020-01-01 open Assets:Broker
2020-01-01 open Assets:Cash
2020-01-01 open Assets:Cash:USD
2020-01-01 open Equity:Opening

2020-01-01 * "seed"
  Assets:Cash  10000 GBP
  Equity:Opening

2020-01-02 * "buy security at total price (BAD)"
  Assets:Broker  10 BADFUND @@ 1000 GBP
  Assets:Cash   -1000 GBP

2020-01-03 * "buy security at per-unit price (BAD)"
  Assets:Broker  5 BADFUND @ 100 GBP
  Assets:Cash   -500 GBP

2020-01-04 * "buy security at cost (GOOD)"
  Assets:Broker  10 GOODFUND {100 GBP}
  Assets:Cash   -1000 GBP

2020-01-05 * "buy foreign currency at FX rate (OK, not a security)"
  Assets:Cash:USD  100 USD @ 0.79 GBP
  Assets:Cash      -79 GBP

2020-01-06 * "buy security at price but reviewed-exempt (IGNORED)"
  ignore-for-cost-basis-check: "closed pension, historical lot"
  Assets:Broker  10 EXEMPTFUND @@ 1000 GBP
  Assets:Cash   -1000 GBP

2021-01-07 * "swap crypto for a curated stablecoin (OK, currency-like)"
  Assets:Crypto  100 USDT @ 0.79 GBP
  Assets:Cash    -79 GBP

2021-01-08 * "crypto-to-crypto swap, directive-marked (OK, currency-like)"
  Assets:Crypto   50 CELO @ 0.62 USDT
  Assets:Crypto  -31 USDT
"""


def _price_only() -> list[dict]:
    entries, _errors, options_map = loader.load_string(COST_LEDGER)
    return find_price_only_holdings(entries, options_map)


def test_price_only_security_is_flagged_in_both_price_forms():
    badfund = [f for f in _price_only() if f["commodity"] == "BADFUND"]
    assert {f["commodity"] for f in _price_only()} == {"BADFUND"}
    assert len(badfund) == 2  # the @@ (total) and the @ (per-unit) buys


def test_flagged_cost_basis_items_carry_their_location():
    item = next(f for f in _price_only() if f["commodity"] == "BADFUND")
    assert item["account"] == "Assets:Broker"
    assert item["line"] > 0


def test_currency_like_commodities_are_not_securities():
    """USD (ISO code), USDT (curated stablecoin) and CELO (asset-class marker) are
    held at a price by design; GOODFUND is held at cost; EXEMPTFUND is reviewed."""
    flagged = {f["commodity"] for f in _price_only()}
    assert flagged.isdisjoint({"USD", "USDT", "CELO", "GOODFUND", "EXEMPTFUND"})


def test_stablecoin_set_stays_curated():
    """Ledger-specific tokens use the asset-class marker, never this hardcoded set."""
    assert {"USDT", "USDC"} <= _STABLECOIN_TICKERS
    assert not {"BORG", "CHSB"} & _STABLECOIN_TICKERS


# --- currency constraint ------------------------------------------------------------

CONSTRAINT_LEDGER = """\
option "operating_currency" "GBP"

2020-01-01 open Assets:Cash:Unpinned
2020-01-01 open Assets:Cash:Pinned            GBP
2020-01-01 open Assets:Broker:Pinned          VWRP,GBP
2020-01-01 open Liabilities:Card:Unpinned
2020-01-01 open Liabilities:Card:Pinned       GBP
2020-01-01 open Income:Salary
2020-01-01 open Expenses:Groceries
2020-01-01 open Equity:Opening

2020-01-01 open Assets:Cash:ExemptUnpinned
  ignore-for-currency-constraint-check: "multi-currency travel wallet"
"""


def _unconstrained() -> list[dict]:
    entries, _errors, _options_map = loader.load_string(CONSTRAINT_LEDGER)
    return find_unconstrained_opens(entries)


def test_unconstrained_balance_sheet_opens_are_flagged_and_nothing_else():
    """Flow and equity accounts carry mixed commodities legitimately; pinned and
    reviewed-exempt opens are fine."""
    assert {f["account"] for f in _unconstrained()} == {"Assets:Cash:Unpinned", "Liabilities:Card:Unpinned"}


def test_flagged_open_carries_its_location():
    item = next(f for f in _unconstrained() if f["account"] == "Assets:Cash:Unpinned")
    assert item["line"] > 0
    assert item["date"] == "2020-01-01"


# --- one commodity per open leaf ----------------------------------------------------

TODAY = datetime.date(2026, 9, 10)


def _accounts(findings: list[dict]) -> list[str]:
    return [f["account"] for f in findings]


def test_open_mixed_account_is_flagged():
    holdings = {"Assets:ISA:Vanguard:Mike": {"GBP": Decimal("133.86"), "VWRP": Decimal("1397")}}
    found = find_mixed_leaves(holdings, {}, {}, {}, TODAY)
    assert _accounts(found) == ["Assets:ISA:Vanguard:Mike"]
    assert found[0]["held"] == ["GBP", "VWRP"]


def test_container_plus_leaves_is_clean():
    holdings = {
        "Assets:ISA:Vanguard:Mike": {},
        "Assets:ISA:Vanguard:Mike:Cash": {"GBP": Decimal("133.86")},
        "Assets:ISA:Vanguard:Mike:VWRP": {"VWRP": Decimal("1397")},
    }
    declared = {"Assets:ISA:Vanguard:Mike": [], "Assets:ISA:Vanguard:Mike:Cash": ["GBP"], "Assets:ISA:Vanguard:Mike:VWRP": ["VWRP"]}
    assert find_mixed_leaves(holdings, declared, {}, {}, TODAY) == []


def test_closed_account_is_out_of_scope():
    """cbdrt's Assets:Fireworks2022:HelmTickets: ticket units beside cash, books finished."""
    holdings = {"Assets:Fireworks2022:HelmTickets": {"GBP": Decimal("-2305"), "TICKET": Decimal("461")}}
    closed_on = {"Assets:Fireworks2022:HelmTickets": datetime.date(2022, 11, 10)}
    assert find_mixed_leaves(holdings, {}, closed_on, {}, TODAY) == []
    # Control: the same holdings with no close DO flag.
    assert _accounts(find_mixed_leaves(holdings, {}, {}, {}, TODAY)) == ["Assets:Fireworks2022:HelmTickets"]


def test_future_dated_close_is_still_open_until_it_arrives():
    holdings = {"Assets:Brokerage": {"GBP": Decimal("10"), "VWRP": Decimal("5")}}
    tomorrow = TODAY + datetime.timedelta(days=1)
    assert _accounts(find_mixed_leaves(holdings, {}, {"Assets:Brokerage": tomorrow}, {}, TODAY)) == ["Assets:Brokerage"]
    assert find_mixed_leaves(holdings, {}, {"Assets:Brokerage": tomorrow}, {}, tomorrow) == []


def test_exemption_suppresses_a_flag():
    holdings = {"Assets:Odd": {"GBP": Decimal("1"), "XYZ": Decimal("2")}}
    assert find_mixed_leaves(holdings, {}, {}, {"Assets:Odd": "reviewed"}, TODAY) == []


def test_small_but_real_holding_is_not_dust():
    """0.001 BTC is roughly GBP60: no unitless cutoff can span incomparable units."""
    holdings = {"Assets:Exchange": {"GBP": Decimal("100"), "BTC": Decimal("0.001")}}
    assert _accounts(find_mixed_leaves(holdings, {}, {}, {}, TODAY)) == ["Assets:Exchange"]


def test_netted_out_commodity_is_not_held():
    holdings = {"Assets:Exchange": {"GBP": Decimal("100"), "ETH2": Decimal("0")}}
    assert find_mixed_leaves(holdings, {}, {}, {}, TODAY) == []


def test_flow_accounts_are_never_flagged():
    holdings = {
        "Income:Fireworks:PreSales": {"GBP": Decimal("1"), "TICKET": Decimal("2")},
        "Equity:MeterReadings": {"ELECTRICITY": Decimal("-26436.2"), "GAS": Decimal("-34252.2")},
    }
    assert find_mixed_leaves(holdings, {}, {}, {}, TODAY) == []


def test_worst_tangled_account_is_reported_first():
    holdings = {
        "Assets:Two": {"GBP": Decimal("1"), "A": Decimal("1")},
        "Assets:Three": {"GBP": Decimal("1"), "A": Decimal("1"), "B": Decimal("1")},
    }
    assert _accounts(find_mixed_leaves(holdings, {}, {}, {}, TODAY)) == ["Assets:Three", "Assets:Two"]


def test_multi_commodity_declaration_is_flagged_even_with_one_live_balance():
    """Liabilities:Octopus declared ELECTRICITY, GAS, GBP: the permission is the defect."""
    holdings = {"Assets:Broker": {"VWRP": Decimal("100"), "GBP": Decimal("0")}}
    found = find_mixed_leaves(holdings, {"Assets:Broker": ["VWRP", "GBP"]}, {}, {}, TODAY)
    assert _accounts(found) == ["Assets:Broker"]
    assert found[0]["held"] == ["VWRP"]
    assert found[0]["declared"] == ["GBP", "VWRP"]
    # Control: narrow the declaration and it goes quiet with the same holdings.
    assert find_mixed_leaves(holdings, {"Assets:Broker": ["VWRP"]}, {}, {}, TODAY) == []


def test_account_with_no_postings_is_judged_on_its_declaration():
    assert _accounts(find_mixed_leaves({}, {"Assets:NewBroker": ["VWRP", "GBP"]}, {}, {}, TODAY)) == ["Assets:NewBroker"]
