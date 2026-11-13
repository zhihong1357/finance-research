from decimal import Decimal

import pyarrow as pa
import pytest
from hypothesis import given
from hypothesis import strategies as st

from finresearch.data.fixed import FixedPointError, parse, rescale, to_float


def test_every_digit_is_kept_at_the_largest_scale() -> None:
    f = parse(["1.5", "2", "0.25", "-0.05", "73762.90"])
    assert f.scale == 2
    assert f.mantissa.to_pylist() == [150, 200, 25, -5, 7376290]
    assert f.decimals() == [
        Decimal("1.50"),
        Decimal("2"),
        Decimal("0.25"),
        Decimal("-0.05"),
        Decimal("73762.90"),
    ]


def test_digits_beyond_a_float_survive() -> None:
    # 18 significant digits: a float would round the last ones.
    f = parse(["123456789.123456789"])
    assert (f.mantissa.to_pylist(), f.scale) == ([123456789123456789], 9)


def test_an_empty_column() -> None:
    f = parse([])
    assert (len(f), f.scale) == (0, 0)


@pytest.mark.parametrize("text", ["1e5", "NaN", "", " 1", "1.2.3", "+1", "0x10"])
def test_anything_but_a_plain_decimal_is_refused(text: str) -> None:
    with pytest.raises(FixedPointError, match="not a plain decimal"):
        parse(["1", text])


def test_a_null_is_refused() -> None:
    with pytest.raises(FixedPointError, match="not a plain decimal"):
        parse(pa.array(["1", None], type=pa.string()))


def test_values_beyond_64_bits_are_refused() -> None:
    with pytest.raises(FixedPointError, match="64 bits"):
        parse(["9223372036854775808"])  # 2**63
    with pytest.raises(FixedPointError, match="64 bits"):
        parse(["92233720368547758.07", "0.001"])  # fits at scale 2, not at scale 3


def test_rescaling_is_exact_and_only_goes_up() -> None:
    f = parse(["1.5", "-2.25"])
    assert rescale(f, 4).to_pylist() == [15000, -22500]
    with pytest.raises(FixedPointError, match="cannot lower"):
        rescale(f, 1)
    with pytest.raises(FixedPointError, match="64 bits"):
        rescale(parse(["9000000000000000000"]), 1)


def test_floats_for_computation() -> None:
    m = pa.array([150, 25], pa.int64())
    assert to_float(m, 2).to_pylist() == [1.5, 0.25]
    assert to_float(m, pa.array([1, 2], pa.int8())).to_pylist() == [15.0, 0.25]


@given(
    st.lists(
        st.decimals(min_value=-(10**9), max_value=10**9, places=8, allow_nan=False), min_size=1
    )
)
def test_text_round_trips_exactly(values: list[Decimal]) -> None:
    text = [format(v, "f") for v in values]
    f = parse(text)
    assert f.decimals() == [Decimal(t) for t in text]


def test_scientific_notation_only_when_asked_and_exactly() -> None:
    with pytest.raises(FixedPointError, match="not a plain decimal"):
        parse(["1.0985e+06"])
    f = parse(["1.0985e+06", "1.2e-05", "2.5", "3E2"], exponents=True)
    assert f.decimals() == [Decimal("1098500"), Decimal("0.000012"), Decimal("2.5"), Decimal("300")]
    assert f.scale == 6
    with pytest.raises(FixedPointError, match="not a plain decimal"):
        parse(["1e", "2"], exponents=True)
