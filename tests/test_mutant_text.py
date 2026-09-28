"""`halter.mutant_text`: the static classes the tier-2 shortlist sets aside.

Every class has a known-good instance that lands in it and a known-bad
instance that does not.
"""

from __future__ import annotations

import pytest

from halter.mutant_text import PHRASES, classify, function_of, parse_show

KIND_CASES = [
    # (status, function, before, after, expected kind)
    ("no tests", "Account.__eq__", "return a == b", "return a != b", "untested"),
    ("survived", "Account.__eq__", "return a == b", "return a != b", "behaviour"),
    (
        "survived",
        "quantize",
        "exponent = Decimal(1).scaleb(-_CURRENCY_DECIMALS[code])",
        "exponent = Decimal(2).scaleb(-_CURRENCY_DECIMALS[code])",
        "equivalent",
    ),
    (
        "survived",
        "q",
        "cents = int(scaled.quantize(Decimal(1), rounding=ROUND_HALF_UP))",
        "cents = int(scaled.quantize(Decimal(2), rounding=ROUND_HALF_UP))",
        "equivalent",
    ),
    # Bad: the value is returned, not used as an exponent.
    (
        "survived",
        "unit",
        "return Decimal(1).scaleb(-places)",
        "return Decimal(2).scaleb(-places)",
        "behaviour",
    ),
    # Bad: a different number, not the known pattern.
    ("survived", "q", "x = v.quantize(Decimal(1))", "x = v.quantize(Decimal(3))", "behaviour"),
    (
        "survived",
        "t",
        'raise ValueError("amount must be positive")',
        "raise ValueError(None)",
        "text",
    ),
    ("survived", "t", "raise KeyError(sku)", "raise KeyError()", "text"),
    (
        "survived",
        "t",
        'raise ValueError("bad: %r" % (value,)) from exc',
        "raise ValueError(None) from exc",
        "text",
    ),
    (
        "survived",
        "t",
        'raise ValueError("invalid decimal amount: %r" % (value,))',
        'raise ValueError("invalid decimal amount: %r" / (value,))',
        "text",
    ),
    (
        "survived",
        "t",
        'raise TypeError("got %r" % (type(value).__name__,))',
        'raise TypeError("got %r" % (type(None).__name__,))',
        "text",
    ),
    ("survived", "t", '"amount %s is below the fee %s" % (value, fee)', "None", "text"),
    ("survived", "t", '_check_int(percent, "percent")', "_check_int(percent, None)", "text"),
    # Bad: the raise changes exception type, not its message.
    ("survived", "t", 'raise ValueError("x")', 'raise TypeError("x")', "behaviour"),
    # Bad: a string-led line whose format argument changes may be report output.
    (
        "survived",
        "r",
        '"(%(count)d lines)" % dict(summary, currency=currency)',
        '"(%(count)d lines)" % dict(summary, currency=None)',
        "behaviour",
    ),
    # Bad: a check whose checked value, not its field name, is nulled.
    ("survived", "t", '_check_int(percent, "percent")', '_check_int(None, "percent")', "behaviour"),
    ("survived", "t", "if debit > current:", "if debit >= current:", "behaviour"),
]


@pytest.mark.parametrize(("status", "func", "before", "after", "kind"), KIND_CASES)
def test_classify(status: str, func: str, before: str, after: str, kind: str) -> None:
    assert classify(status, func, before, after) == kind


def test_every_kind_has_a_case_and_every_set_aside_kind_a_phrase() -> None:
    for kind in ("untested", "equivalent", "text", "behaviour"):
        assert any(k == kind for *_, k in KIND_CASES)
    assert set(PHRASES) == {"equivalent", "text"}


# -- parsing ---------------------------------------------------------------------


def test_function_of() -> None:
    assert function_of("accounts.xǁAccountǁwithdraw__mutmut_18") == "Account.withdraw"
    assert function_of("pkg.report.x_monthly_summary__mutmut_53") == "monthly_summary"
    assert function_of("fees.x__check_amount__mutmut_8") == "_check_amount"
    assert function_of("not a mutant name") == "not a mutant name"


def test_parse_show_joins_a_multi_line_hunk() -> None:
    body = (
        "# a.x_f__mutmut_2: survived\n--- a.py\n+++ a.py\n@@ -8,6 +8,5 @@\n"
        "     raise ValueError(\n"
        '-        "one of %s, got %r"\n'
        "-        % (codes, currency)\n"
        "+        None\n"
        "     )\n"
    )
    assert parse_show(body) == ("a.py", '"one of %s, got %r" % (codes, currency)', "None")
    assert classify("survived", "f", *parse_show(body)[1:]) == "text"
