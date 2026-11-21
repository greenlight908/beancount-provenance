"""Tests for `check-beancount-invariants`: exit codes and the JSON it prints.

Same contract as `check-beancount-provenance`: 0 pass, 1 the gate refuses, 2 the ledger
could not be read. "I could not look" must never look like a pass.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from beancount_provenance.invariants_cli import main

CLEAN = """\
option "operating_currency" "GBP"

2010-01-01 open Assets:Broker
  ignore-for-currency-constraint-check: "container"
2010-01-01 open Assets:Broker:Cash GBP
2010-01-01 open Assets:Broker:VWRP VWRP
2010-01-01 open Equity:Opening GBP

2010-01-02 * "Vanguard" "Buy"
  Assets:Broker:VWRP  10 VWRP {100 GBP}
  Assets:Broker:Cash  -1000 GBP
"""


def run(tmp_path: Path, text: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
    ledger = tmp_path / "main.beancount"
    ledger.write_text(text, encoding="utf-8")
    code = main(["--ledger", str(ledger)])
    return code, json.loads(capsys.readouterr().out)


def test_a_clean_ledger_exits_zero(tmp_path, capsys):
    code, out = run(tmp_path, CLEAN, capsys)
    assert (code, out["ok"]) == (0, True)
    assert out["balance_sheet_accounts"] == 3
    assert out["cost_basis"] == out["currency_constraint"] == out["one_commodity_per_leaf"] == []


@pytest.mark.parametrize(
    ("extra", "rule"),
    [
        ('\n2010-01-03 * "Vanguard" "Buy at price"\n  Assets:Broker:VWRP  1 VWRP @@ 100 GBP\n  Assets:Broker:Cash  -100 GBP\n', "cost_basis"),
        ("\n2010-01-03 open Assets:Wallet\n", "currency_constraint"),
        ("\n2010-01-03 open Assets:Mixed GBP,VWRP\n", "one_commodity_per_leaf"),
    ],
)
def test_each_rule_refuses_on_its_own(tmp_path, capsys, extra, rule):
    """Control for the clean case: one defect of each kind flips the gate, and is
    reported under its own rule."""
    code, out = run(tmp_path, CLEAN + extra, capsys)
    assert (code, out["ok"]) == (1, False)
    assert out[rule], f"{rule} reported nothing"


def test_a_missing_ledger_exits_two(tmp_path, capsys):
    code = main(["--ledger", str(tmp_path / "nope.beancount")])
    assert code == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_a_ledger_that_does_not_load_exits_two(tmp_path, capsys):
    code, out = run(tmp_path, CLEAN + '\n2010-01-04 * "x" "unbalanced"\n  Assets:Broker:Cash  -1 GBP\n  Equity:Opening  2 GBP\n', capsys)
    assert (code, out["ok"]) == (2, False)
    assert "validation error" in out["error"]


def test_a_ledger_with_no_balance_sheet_accounts_exits_two(tmp_path, capsys):
    """An empty subject set is a scan that saw nothing, not a clean ledger."""
    code, out = run(tmp_path, 'option "operating_currency" "GBP"\n2010-01-01 open Expenses:Food GBP\n', capsys)
    assert (code, out["ok"]) == (2, False)
