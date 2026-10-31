"""Tests for the command-line gate: exit codes and the JSON it prints.

Exit codes are the contract pre-commit and CI read: 0 pass, 1 the gate refuses, 2 the
ledger could not be read. The last must never look like a pass -- "I could not look" is
not "I looked and it is fine".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from beancount import loader

from beancount_provenance.cli import main

LEDGER = """\
option "operating_currency" "GBP"

2010-01-01 open Assets:Cash GBP
2010-01-01 open Expenses:Food GBP

2010-01-02 * "Tesco" "Food"
  source: "statement"
  Assets:Cash  -1.00 GBP
  Expenses:Food  1.00 GBP

2010-01-03 * "Boots" "Shampoo"
  Assets:Cash  -1.00 GBP
  Expenses:Food  1.00 GBP
"""

BASELINES = """
2010-01-05 custom "wnd-baseline" "provenance" "1"
2010-01-05 custom "wnd-baseline" "payee" "0"
2010-01-05 custom "wnd-baseline" "narration" "0"
"""


def run(tmp_path: Path, text: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
    ledger = tmp_path / "main.beancount"
    ledger.write_text(text, encoding="utf-8")
    code = main(["--ledger", str(ledger)])
    return code, json.loads(capsys.readouterr().out)


def test_holding_at_baseline_exits_zero(tmp_path, capsys):
    code, out = run(tmp_path, LEDGER + BASELINES, capsys)
    assert code == 0
    assert out["ok"] is True
    assert out["counts"] == {"provenance": 1, "payee": 0, "narration": 0}


def test_a_new_unstamped_transaction_exits_one(tmp_path, capsys):
    """Control for the test above: one more untraceable entry must flip it."""
    extra = '\n2010-01-04 * "Argos" "Kettle"\n  Assets:Cash  -1.00 GBP\n  Expenses:Food  1.00 GBP\n'
    code, out = run(tmp_path, LEDGER + extra + BASELINES, capsys)
    assert code == 1
    assert out["grown"][0]["metric"] == "provenance"


def test_a_missing_baseline_exits_one_with_lines_that_parse(tmp_path, capsys):
    """The offered lines are pasted back in; the ledger must then load and pass."""
    code, out = run(tmp_path, LEDGER, capsys)
    assert code == 1
    assert out["missing"] == ["provenance", "payee", "narration"]

    pasted = LEDGER + "\n" + "\n".join(out["fix"]) + "\n"
    loader.initialize(use_cache=False)
    path = tmp_path / "pasted.beancount"
    path.write_text(pasted, encoding="utf-8")
    _, errors, _ = loader.load_file(str(path))
    assert not errors, errors
    assert main(["--ledger", str(path)]) == 0


def test_a_ledger_with_errors_exits_two_not_zero(tmp_path, capsys):
    broken = LEDGER + BASELINES + '\n2010-01-06 * "X" "Y"\n  Assets:Nowhere  -1.00 GBP\n  Expenses:Food  1.00 GBP\n'
    code, out = run(tmp_path, broken, capsys)
    assert code == 2
    assert out["ok"] is False
    assert out["error"]


def test_a_missing_ledger_exits_two(tmp_path, capsys):
    code = main(["--ledger", str(tmp_path / "absent.beancount")])
    assert code == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_output_carries_the_version(tmp_path, capsys):
    from beancount_provenance import __version__

    _, out = run(tmp_path, LEDGER + BASELINES, capsys)
    assert out["version"] == __version__
