# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
# pyarrow is only partly typed; its calls stay inside this module, behind typed functions.
"""Exact decimals as fixed-point integers (decision 0010).

Exchanges publish prices and quantities as decimal text: ``73762.90``,
``0.000012``. Floats cannot hold all of these exactly (a quote volume with 18
significant digits loses its last ones), so the canonical layer keeps every
digit: a column of decimal text becomes integer *mantissas* and one *scale*,
the number of decimal places, with ``value = mantissa / 10**scale``.

Integers that change little from one row to the next also compress far better
than floats under delta encoding: measured at under half the size
(docs/architecture.md 7.3).

The work is done by Arrow's vectorised string functions, not a Python loop:
hundreds of millions of values go through here.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal
from typing import Any, cast

import pyarrow as pa
import pyarrow.compute as pc

type Column = pa.Array[Any]

MANTISSA_TYPE = pa.int64()
SCALE_TYPE = pa.int8()
# A plain decimal: optional minus sign, digits, optionally a point and more digits.
_DECIMAL = r"^(?P<i>-?\d+)(?:\.(?P<f>\d*))?$"
# The same with a power of ten, as a float printed by Python looks: 1.0985e+06.
_SCIENTIFIC = r"^-?\d+(?:\.\d*)?[eE][+-]?\d+$"
# A 64-bit float carries 15 significant decimal digits for certain: every decimal of up to
# 15 digits survives a trip through a float unchanged. Text with more digits than that,
# from a source that prints floats, is a shorter decimal plus the float's rounding error.
_FLOAT_DIGITS = 15
# The rounded value must be shorter than this to count as a decimal with rounding error:
# at least three of the float's digits must have come out as zeros.
_CLEAN_DIGITS = 12
_NOT_DIGITS = r"^-?0*\.?0*|\."  # sign, leading zeros and the point: what is left are digits


class FixedPointError(ValueError):
    """Text that is not a plain decimal, or a value too large for 64-bit integers."""


@dataclass(frozen=True)
class Fixed:
    """A column of exact decimals: ``mantissa[i] / 10**scale``."""

    mantissa: Column
    scale: int

    def __len__(self) -> int:
        return len(self.mantissa)

    def decimals(self) -> list[Decimal]:
        """The values as ``Decimal`` (for checks and tests; slow on large columns)."""
        values = cast(list[int], self.mantissa.to_pylist())  # mantissas are never null
        return [Decimal(m).scaleb(-self.scale) for m in values]


def _expand(arr: Column) -> Column:
    """Write numbers in scientific notation out in full (exactly: ``1.0985e+06`` is ``1098500``)."""
    sci = pc.match_substring_regex(arr, _SCIENTIFIC)
    if not pc.any(sci).as_py():
        return arr
    full = [format(Decimal(str(v)), "f") for v in pc.filter(arr, sci).to_pylist()]
    return cast(Column, pc.replace_with_mask(arr, sci, pa.array(full, type=pa.string())))


def _unfloat(arr: Column) -> Column:
    """Replace float rounding error by the decimal it surrounds.

    ``12.309999999999999`` becomes ``12.31``.
    Only values written with more than 15 significant digits are touched. Each is
    rounded to 15 digits; if what remains is not a short decimal, the value was
    not rounding error and is refused.
    """
    # The stubs type the result for expressions only; with an array it is an array.
    bare: Any = pc.replace_substring_regex(arr, _NOT_DIGITS, "")
    long = pc.greater(pc.utf8_length(bare), pa.scalar(_FLOAT_DIGITS))
    if not pc.any(long).as_py():
        return arr
    context = Context(prec=_FLOAT_DIGITS, rounding=ROUND_HALF_EVEN)
    short: list[str] = []
    for v in pc.filter(arr, long).to_pylist():
        try:
            d = context.plus(Decimal(str(v))).normalize()
        except ArithmeticError as exc:
            raise FixedPointError(f"not a plain decimal: {v!r}") from exc
        if not d.is_finite() or len(d.as_tuple().digits) > _CLEAN_DIGITS:
            raise FixedPointError(f"{v!r} is not a short decimal with float rounding error")
        short.append(format(d, "f"))
    return cast(Column, pc.replace_with_mask(arr, long, pa.array(short, type=pa.string())))


def parse(text: Column | Sequence[str], *, exponents: bool = False, floats: bool = False) -> Fixed:
    """Decimal text to mantissas at the largest number of decimal places in the column.

    Every value keeps all of its digits: ``["1.5", "2", "0.25"]`` becomes
    ``[150, 200, 25]`` with scale 2. Raises ``FixedPointError`` on empty, null or
    non-decimal text (``NaN``; ``1e5`` too unless ``exponents``) and on mantissas
    beyond 64 bits.

    Two options for sources that print numbers as Python prints floats.
    ``exponents`` accepts scientific notation; the text is read exactly as
    written. ``floats`` reads a value written with more than 15 significant
    digits as the short decimal it is the rounding error of
    (``12.309999999999999`` as ``12.31``), and refuses it if it is not near one.
    Never set ``floats`` for a source that publishes true decimals: a quote
    volume can have 18 digits, all of them real.
    """
    arr = text if isinstance(text, pa.Array) else pa.array(list(text), type=pa.string())
    if len(arr) == 0:
        return Fixed(pa.array([], type=MANTISSA_TYPE), 0)
    arr = arr.cast(pa.string())
    if exponents and arr.null_count == 0:
        arr = _expand(arr)
    if floats and arr.null_count == 0:
        arr = _unfloat(arr)
    parts = pc.extract_regex(arr, _DECIMAL)
    bad = pc.is_null(parts)
    if pc.any(bad).as_py():
        first = pc.filter(arr, bad)[0].as_py()
        raise FixedPointError(f"not a plain decimal: {first!r}")
    whole, frac = pc.struct_field(parts, "i"), pc.struct_field(parts, "f")
    scale = int(pc.max(pc.utf8_length(frac)).as_py())
    padded = pc.utf8_rpad(frac, width=scale, padding="0")
    # The stubs cannot tell that array inputs give an array back.
    digits = cast(Column, pc.binary_join_element_wise(whole, padded, pa.scalar("")))
    try:
        mantissa = digits.cast(MANTISSA_TYPE)
    except pa.ArrowInvalid as exc:
        raise FixedPointError(f"a value does not fit in 64 bits at scale {scale}") from exc
    return Fixed(mantissa, scale)


def rescale(f: Fixed, scale: int) -> Column:
    """The mantissas at a larger scale (exact): ``(150, scale 2)`` at 4 is ``15000``."""
    if scale < f.scale:
        raise FixedPointError(f"cannot lower the scale from {f.scale} to {scale} exactly")
    factor = pa.scalar(10 ** (scale - f.scale), type=MANTISSA_TYPE)
    try:
        return pc.multiply_checked(f.mantissa, factor)
    except pa.ArrowInvalid as exc:
        raise FixedPointError(f"a value does not fit in 64 bits at scale {scale}") from exc


def to_float(mantissa: Column, scale: Column | int) -> Column:
    """Floats for computation: ``mantissa / 10**scale`` (``scale`` per row or one for all)."""
    scales = scale if isinstance(scale, pa.Array) else pa.scalar(scale, type=SCALE_TYPE)
    power = pc.power(pa.scalar(10.0), pc.cast(scales, pa.float64()))
    return pc.divide(pc.cast(mantissa, pa.float64()), power)
