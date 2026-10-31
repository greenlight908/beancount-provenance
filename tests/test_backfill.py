"""Tests for the backfill engine (mike.dotfiles ADRs 0014/0016).

The safety property under test: a stamp is written only from evidence the entry itself
records, as named by the ledger's declared rules; every stamp names its rule; an entry
with no matching evidence is marked `legacy`, never given a guessed source.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from beancount import loader

from beancount_provenance import count_gaps
from beancount_provenance.backfill import Rule, apply, classify, load_rules, main, plan

HEADER = """\
option "operating_currency" "GBP"

2010-01-01 open Assets:Cash GBP
2010-01-01 open Expenses:Food GBP
2010-01-01 open Equity:Opening GBP
"""

RULES = [
    Rule("imported-file", when_any=("imported_from_file",), source="{imported_from_file}", script="{imported_with_file}"),
    Rule("monzo-api-id", when_any=("bank_id",), match=r"^tx_", source="monzo_api"),
    Rule("rt-ticket", when_any=("cbdrt_id",), match=r"^RT\d+$", source="RT ticket {cbdrt_id}"),
    Rule("amex-export", when_any=("transaction_appears_as", "transaction_extended_details"), source="american_express export"),
]


def _txn(meta: str = "", head: str = '2010-01-02 * "Tesco" "Food"') -> str:
    return f"\n{head}\n{meta}  Assets:Cash  -1.00 GBP\n  Expenses:Food  1.00 GBP\n"


def _ledger(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "main.beancount"
    path.write_text(HEADER + body, encoding="utf-8")
    return path


def _load(path: Path):
    loader.initialize(use_cache=False)
    entries, errors, _ = loader.load_file(str(path))
    assert not errors, errors
    return entries


class TestClassify:
    def test_templates_fill_from_the_entrys_own_metadata(self):
        stamp = classify({"imported_from_file": "/x/a.csv", "imported_with_file": "/x/imp.py"}, RULES)
        assert (stamp.rule, stamp.source, stamp.script) == ("imported-file", "/x/a.csv", "/x/imp.py")

    def test_a_script_template_whose_key_is_absent_is_dropped_not_guessed(self):
        stamp = classify({"imported_from_file": "/x/a.csv"}, RULES)
        assert (stamp.source, stamp.script) == ("/x/a.csv", None)

    def test_match_is_applied_to_the_value(self):
        assert classify({"bank_id": "tx_1"}, RULES).rule == "monzo-api-id"
        assert classify({"bank_id": "12345"}, RULES).rule == "no-evidence"
        assert classify({"cbdrt_id": "RT1371"}, RULES).source == "RT ticket RT1371"

    def test_a_present_but_empty_export_column_is_still_evidence(self):
        """An export writes every column, empty or not; the key is the evidence."""
        assert classify({"transaction_extended_details": ""}, RULES).rule == "amex-export"

    def test_a_blank_value_cannot_fill_a_source_template(self):
        assert classify({"cbdrt_id": ""}, RULES).rule == "no-evidence"

    def test_first_matching_rule_wins(self):
        stamp = classify({"imported_from_file": "/x/a.csv", "bank_id": "tx_1"}, RULES)
        assert stamp.rule == "imported-file"

    def test_no_evidence_is_legacy_not_a_guess(self):
        stamp = classify({"note": "paid in cash"}, RULES)
        assert (stamp.rule, stamp.source, stamp.script) == ("no-evidence", "legacy", None)

    def test_no_rules_marks_everything_legacy(self):
        assert classify({"imported_from_file": "/x/a.csv"}, []).source == "legacy"


class TestLoadRules:
    def test_rules_are_read_from_the_toml(self, tmp_path):
        cfg = tmp_path / ".beancount-provenance.toml"
        cfg.write_text(
            '[[backfill.rule]]\nid = "rt-ticket"\nwhen_any = ["cbdrt_id"]\nmatch = "^RT\\\\d+$"\nsource = "RT ticket {cbdrt_id}"\n',
            encoding="utf-8",
        )
        assert load_rules(cfg) == [Rule("rt-ticket", when_any=("cbdrt_id",), match=r"^RT\d+$", source="RT ticket {cbdrt_id}")]

    def test_a_missing_config_means_no_rules(self, tmp_path):
        assert load_rules(tmp_path / ".beancount-provenance.toml") == []

    def test_a_template_naming_an_unlisted_key_is_rejected(self, tmp_path):
        """`source` must be built only from keys the rule requires, or it could render blank."""
        cfg = tmp_path / ".beancount-provenance.toml"
        cfg.write_text('[[backfill.rule]]\nid = "x"\nwhen_any = ["a"]\nsource = "{b}"\n', encoding="utf-8")
        with pytest.raises(ValueError, match="b"):
            load_rules(cfg)

    def test_reserved_rule_id_is_rejected(self, tmp_path):
        cfg = tmp_path / ".beancount-provenance.toml"
        cfg.write_text('[[backfill.rule]]\nid = "no-evidence"\nwhen_any = ["a"]\nsource = "s"\n', encoding="utf-8")
        with pytest.raises(ValueError, match="no-evidence"):
            load_rules(cfg)


class TestPlanAndApply:
    def test_only_gaps_are_planned_and_pads_are_not(self, tmp_path):
        path = _ledger(
            tmp_path,
            _txn('  source: "statement"\n') + _txn(head='2010-01-03 * "Boots" "Shampoo"') + '\n2010-01-04 pad Assets:Cash Equity:Opening\n2010-01-05 balance Assets:Cash 5.00 GBP\n  source: "s"\n',
        )
        assert [(p.payee, p.stamp.rule) for p in plan(path, RULES, today="2026-10-31").stamps] == [("Boots", "no-evidence")]

    def test_a_blank_provenance_key_is_refused_not_duplicated(self, tmp_path):
        p = plan(_ledger(tmp_path, _txn('  source: ""\n')), RULES, today="2026-10-31")
        assert (p.stamps, len(p.refused)) == ([], 1)

    def test_apply_leaves_no_gaps_and_a_second_run_finds_nothing(self, tmp_path):
        path = _ledger(
            tmp_path,
            _txn('    cbdrt_id: "RT1371"\n') + _txn(head='2010-01-03 * "Boots" "Shampoo" #tag') + _txn('  imported_from_file: "C:\\\\a \\"b\\".csv"\n', head='2010-01-04 * "Argos" "Kettle"'),
        )
        apply(plan(path, RULES, today="2026-10-31"))
        text = path.read_text(encoding="utf-8")
        assert '    source: "RT ticket RT1371"\n    provenance: "inferred 2026-10-31 by beancount-provenance backfill rule rt-ticket"\n' in text
        entries = _load(path)
        gaps = count_gaps(entries)
        assert (gaps["provenance"], gaps["legacy"]) == (0, 1)
        argos = next(e for e in entries if getattr(e, "payee", None) == "Argos")
        assert argos.meta["source"] == argos.meta["imported_from_file"]
        assert plan(path, RULES, today="2026-10-31").stamps == []


class TestBalanceBackfill:
    def test_unsourced_balances_are_planned_and_stamped(self, tmp_path):
        path = _ledger(tmp_path, '\n2010-01-03 balance Assets:Cash 0.00 GBP\n2010-01-04 balance Assets:Cash 0.00 GBP\n  description: "x"\n')
        p = plan(path, RULES, today="2026-10-31")
        assert [s.stamp.rule for s in p.stamps] == ["no-evidence", "no-evidence"]
        apply(p)
        gaps = count_gaps(_load(path))
        assert (gaps["balance"], gaps["legacy"]) == (0, 2)
        assert plan(path, RULES, today="2026-10-31").stamps == []


class TestIndentation:
    def test_a_balance_followed_by_a_blank_line_gets_indented_metadata(self, tmp_path):
        """A blank next line has no indent to copy; "\n".lstrip() once made the indent a newline."""
        path = _ledger(tmp_path, "\n2010-01-03 balance Assets:Cash 0.00 GBP\n\n2010-01-04 close Assets:Cash\n")
        apply(plan(path, RULES, today="2026-10-31"))
        assert '2010-01-03 balance Assets:Cash 0.00 GBP\n  source: "legacy"\n  provenance: ' in path.read_text(encoding="utf-8")
        _load(path)

    def test_a_balance_followed_by_an_unindented_directive_gets_the_default_indent(self, tmp_path):
        path = _ledger(tmp_path, "\n2010-01-03 balance Assets:Cash 0.00 GBP\n2010-01-04 close Assets:Cash\n")
        apply(plan(path, RULES, today="2026-10-31"))
        assert '0.00 GBP\n  source: "legacy"\n' in path.read_text(encoding="utf-8")
        _load(path)


class TestCli:
    def test_plan_is_the_default_and_apply_writes(self, tmp_path, capsys):
        path = _ledger(tmp_path, _txn())
        (tmp_path / ".beancount-provenance.toml").write_text("", encoding="utf-8")
        assert main(["--ledger", str(path)]) == 0
        out = json.loads(capsys.readouterr().out)
        assert (out["applied"], out["stamps"], out["by_rule"]) == (False, 1, {"no-evidence": 1})
        assert "legacy" not in path.read_text(encoding="utf-8")
        assert main(["--ledger", str(path), "--apply"]) == 0
        assert count_gaps(_load(path))["legacy"] == 1

    def test_an_unloadable_ledger_exits_two(self, tmp_path, capsys):
        path = _ledger(tmp_path, '\n2010-01-02 * "X" "Y"\n  Assets:Nowhere  -1.00 GBP\n  Expenses:Food  1.00 GBP\n')
        assert main(["--ledger", str(path)]) == 2
        assert json.loads(capsys.readouterr().out)["ok"] is False
