import pytest

from harnessdoc.pricing import fmt_usd


@pytest.mark.parametrize("x,digits,expected", [
    (24.3, 2, "$24.30"),  # ordinary amounts print exactly as before
    (0.0366, 4, "$0.0366"),
    (0.97, 2, "$0.97"),
    (1234.5, 2, "$1,234.50"),
    (0.0, 4, "$0.0000"),
    (0.000038, 4, "$0.0000380"),  # the MiniMax run: a real $0.000038 used to print as $0.0000
    (0.0023, 4, "$0.0023"),
    (0.0123456, 2, "$0.0123"),  # 2 digits would show $0.01 (19% off)
    (4e-9, 4, "$0.0000000040"),  # tiny values are capped at 10 places
])
def test_fmt_usd(x, digits, expected):
    assert fmt_usd(x, digits) == expected


def test_fmt_usd_sign_none_and_nan():
    assert fmt_usd(None) == "—" and fmt_usd(float("nan")) == "—"
    assert fmt_usd(-0.000007, 4, signed=True) == "-$0.00000700"
    assert fmt_usd(0.0117, 4, signed=True) == "+$0.0117"
    assert fmt_usd(-24.3) == "-$24.30"
    assert fmt_usd(0.0, 4, signed=True) == "$0.0000"
