import pytest

from finresearch.data.csvtext import CsvError, read


def test_text_stays_text_and_integers_become_integers() -> None:
    t = read(b"a,b\n1,0.10\n2,\n", ["n", "x"], {"n"}, header=True)
    assert t.column("n").to_pylist() == [1, 2]
    assert t.column("x").to_pylist() == ["0.10", ""]  # exact text, and never a null


def test_without_a_header_the_first_line_is_data() -> None:
    assert read(b"1,a\n", ["n", "x"], {"n"}, header=False).num_rows == 1


def test_a_wrong_shape_is_an_error() -> None:
    with pytest.raises(CsvError):
        read(b"1,2,3\n", ["n", "x"], {"n"}, header=False)
    with pytest.raises(CsvError):
        read(b"x,a\n", ["n", "x"], {"n"}, header=False)
