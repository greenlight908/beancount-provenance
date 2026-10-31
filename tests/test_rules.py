"""Tests for the counting and ratchet rules.

Each test pins a decision that would otherwise change unseen: which keys count as
provenance, that pad-synthesised transactions are exempt, per-transaction counting,
blank values as gaps, and the ratchet that fails on growth AND on slack.
"""

from __future__ import annotations

from pathlib import Path

from beancount import loader

from beancount_provenance import count_gaps, evaluate, read_baselines

HEADER = """\
option "operating_currency" "GBP"

2010-01-01 open Assets:Cash GBP
2010-01-01 open Expenses:Food GBP
2010-01-01 open Equity:Opening GBP
"""


def load(tmp_path: Path, body: str):
    path = tmp_path / "main.beancount"
    path.write_text(HEADER + body, encoding="utf-8")
    loader.initialize(use_cache=False)
    entries, errors, _ = loader.load_file(str(path))
    assert not errors, errors
    return entries


def txn(date: str, payee: str | None, narration: str, meta: str = "") -> str:
    head = f'{date} * "{payee}" "{narration}"' if payee is not None else f'{date} * "{narration}"'
    return f"\n{head}\n{meta}  Assets:Cash  -1.00 GBP\n  Expenses:Food  1.00 GBP\n"


class TestProvenanceKeys:
    def test_source_counts(self, tmp_path: Path):
        entries = load(tmp_path, txn("2010-01-02", "Tesco", "Food", '  source: "statement"\n'))
        assert count_gaps(entries)["provenance"] == 0

    def test_script_counts(self, tmp_path: Path):
        entries = load(tmp_path, txn("2010-01-02", "Tesco", "Food", '  script: "importer"\n'))
        assert count_gaps(entries)["provenance"] == 0

    def test_created_at_alone_is_not_provenance(self, tmp_path: Path):
        """A timestamp says when, not from what (ADR 0010 in mike.dotfiles)."""
        entries = load(tmp_path, txn("2010-01-02", "Tesco", "Food", '  created_at: "2010-01-02"\n'))
        assert count_gaps(entries)["provenance"] == 1

    def test_an_importer_private_key_is_not_provenance(self, tmp_path: Path):
        entries = load(tmp_path, txn("2010-01-02", "Tesco", "Food", '  bank_id: "abc123"\n'))
        assert count_gaps(entries)["provenance"] == 1

    def test_an_empty_or_blank_value_is_not_provenance(self, tmp_path: Path):
        entries = load(
            tmp_path,
            txn("2010-01-02", "Tesco", "Food", '  source: ""\n') + txn("2010-01-03", "Tesco", "Food", '  script: "   "\n'),
        )
        assert count_gaps(entries)["provenance"] == 2


class TestCounting:
    def test_counts_per_transaction_not_per_posting(self, tmp_path: Path):
        body = '\n2010-01-02 * "Tesco" "Split"\n  Assets:Cash  -3.00 GBP\n  Expenses:Food  1.00 GBP\n  Expenses:Food  1.00 GBP\n  Expenses:Food  1.00 GBP\n'
        assert count_gaps(load(tmp_path, body))["provenance"] == 1

    def test_blank_payee_and_narration_are_gaps(self, tmp_path: Path):
        entries = load(
            tmp_path,
            txn("2010-01-02", " ", "Blank payee", '  source: "s"\n') + txn("2010-01-03", "Tesco", "   ", '  source: "s"\n') + txn("2010-01-04", None, "No payee", '  source: "s"\n'),
        )
        gaps = count_gaps(entries)
        assert (gaps["payee"], gaps["narration"]) == (2, 1)

    def test_pad_synthesised_transactions_are_exempt(self, tmp_path: Path):
        """A `Pad` becomes a `P` transaction at load time with no metadata of its own.

        Counting it made every booked meter statement raise the gap count
        (mike.dotfiles#3743: 261 of mike.accounts' gaps were pads).
        """
        body = '\n2010-01-02 pad Assets:Cash Equity:Opening\n2010-01-03 balance Assets:Cash 50.00 GBP\n  source: "statement"\n'
        entries = load(tmp_path, body)
        assert any(getattr(e, "flag", None) == "P" for e in entries), "control: the pad must have been expanded"
        assert count_gaps(entries) == {"provenance": 0, "payee": 0, "narration": 0, "legacy": 0, "balance": 0}


class TestBaselines:
    def test_baselines_are_read_from_the_ledger(self, tmp_path: Path):
        entries = load(
            tmp_path,
            '\n2010-01-05 custom "wnd-baseline" "provenance" "4"\n2010-01-05 custom "wnd-baseline" "payee" "0"\n',
        )
        assert read_baselines(entries) == {"provenance": 4, "payee": 0}

    def test_a_malformed_baseline_is_ignored_not_trusted(self, tmp_path: Path):
        entries = load(tmp_path, '\n2010-01-05 custom "wnd-baseline" "provenance" "lots"\n')
        assert read_baselines(entries) == {}


GAPS = {"provenance": 10, "payee": 3, "narration": 0, "legacy": 0, "balance": 0}
# Baselines as a ledger that never adopted `legacy` declares them.
DECLARED = {"provenance": 10, "payee": 3, "narration": 0}


class TestEvaluate:
    def test_holding_at_baseline_passes(self):
        verdict = evaluate(GAPS, dict(GAPS))
        assert verdict["ok"] is True
        assert (verdict["grown"], verdict["slack"], verdict["missing"]) == ([], [], [])

    def test_growth_fails(self):
        verdict = evaluate(GAPS, {**GAPS, "provenance": 9})
        assert verdict["ok"] is False
        assert [(r["metric"], r["delta"]) for r in verdict["grown"]] == [("provenance", 1)]

    def test_slack_fails_so_a_new_gap_cannot_spend_it(self):
        """A baseline above the count is room for a new gap to land unseen."""
        verdict = evaluate(GAPS, {**GAPS, "payee": 5})
        assert verdict["ok"] is False
        assert [(r["metric"], r["delta"]) for r in verdict["slack"]] == [("payee", -2)]

    def test_a_missing_baseline_fails_and_offers_the_line(self):
        verdict = evaluate(GAPS, {"provenance": 10, "payee": 3})
        assert verdict["ok"] is False
        assert verdict["missing"] == ["narration"]
        assert verdict["fix"] == ['custom "wnd-baseline" "narration" "0"']


class TestLegacy:
    """`source: "legacy"` is the explicit unknown (mike.dotfiles ADR 0014)."""

    def test_legacy_is_its_own_metric_not_provenance_and_not_a_gap(self, tmp_path: Path):
        entries = load(tmp_path, txn("2010-01-02", "Tesco", "Food", '  source: "legacy"\n'))
        gaps = count_gaps(entries)
        assert (gaps["provenance"], gaps["legacy"]) == (0, 1)

    def test_legacy_with_a_script_is_real_provenance(self, tmp_path: Path):
        entries = load(tmp_path, txn("2010-01-02", "Tesco", "Food", '  source: "legacy"\n  script: "importer.py"\n'))
        assert count_gaps(entries)["legacy"] == 0

    def test_an_undeclared_legacy_baseline_is_zero_so_any_legacy_fails(self):
        verdict = evaluate({**GAPS, "legacy": 1}, dict(DECLARED))
        assert verdict["ok"] is False
        assert verdict["missing"] == []
        assert [(r["metric"], r["delta"]) for r in verdict["grown"]] == [("legacy", 1)]

    def test_no_legacy_and_no_legacy_baseline_passes(self):
        """Ledgers that never adopted `legacy` must not start failing on upgrade."""
        assert evaluate(dict(GAPS), dict(DECLARED))["ok"] is True


class TestBalance:
    """P2: a `balance` assertion carries provenance too; pads stay exempt (ADR 0007)."""

    def test_an_unsourced_balance_is_a_balance_gap_not_a_transaction_gap(self, tmp_path: Path):
        entries = load(tmp_path, "\n2010-01-03 balance Assets:Cash 0.00 GBP\n")
        gaps = count_gaps(entries)
        assert (gaps["provenance"], gaps["balance"]) == (0, 1)

    def test_a_sourced_balance_is_not_a_gap(self, tmp_path: Path):
        entries = load(tmp_path, '\n2010-01-03 balance Assets:Cash 0.00 GBP\n  source: "statement"\n')
        assert count_gaps(entries)["balance"] == 0

    def test_a_legacy_balance_counts_as_legacy(self, tmp_path: Path):
        entries = load(tmp_path, '\n2010-01-03 balance Assets:Cash 0.00 GBP\n  source: "legacy"\n')
        gaps = count_gaps(entries)
        assert (gaps["balance"], gaps["legacy"]) == (0, 1)

    def test_a_pad_is_not_a_balance_gap(self, tmp_path: Path):
        entries = load(tmp_path, '\n2010-01-02 pad Assets:Cash Equity:Opening\n2010-01-03 balance Assets:Cash 5.00 GBP\n  source: "s"\n')
        assert count_gaps(entries)["balance"] == 0

    def test_an_undeclared_balance_baseline_is_zero(self):
        verdict = evaluate({**GAPS, "balance": 2}, dict(DECLARED))
        assert verdict["missing"] == []
        assert [(r["metric"], r["delta"]) for r in verdict["grown"]] == [("balance", 2)]
