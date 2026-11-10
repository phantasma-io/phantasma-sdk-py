"""Metadata values: each value is checked against the type of its field.

An Int64 or Int256 value can be a string of decimal digits, because JSON keeps a big integer exact
only as text. Such a string is checked against the range of its field as a number is."""

from __future__ import annotations

import pytest

from phantasma_py import BuilderError, build_phantasma_nft_rom
from phantasma_py.carbon import VMNamedVariableSchema, VMStructSchema, VMType

INT256_MIN = -(1 << 255)
UINT256_MAX = (1 << 256) - 1
INT64_RANGE = "between -9223372036854775808 and 9223372036854775807 or between 0 and 18446744073709551615"
INT256_RANGE = f"between {INT256_MIN} and {(1 << 255) - 1} or between 0 and {UINT256_MAX}"


def rom_of(*fields: tuple[str, VMType, object]) -> bytes:
    """Returns the NFT data of the given fields, each a name, the type of its schema and its value."""
    schema = VMStructSchema([VMNamedVariableSchema.make(name, vm_type) for name, vm_type, _value in fields])
    return build_phantasma_nft_rom(schema, [(name, value) for name, _vm_type, value in fields])


def refusal(vm_type: VMType, value: object) -> str:
    """Returns the message of the error that a field x of the given type raises for the value. The
    test fails if it raises none."""
    with pytest.raises(BuilderError) as refused:
        rom_of(("x", vm_type, value))
    return str(refused.value)


def test_digit_strings_at_the_ends_of_the_int64_and_int256_ranges() -> None:
    # The bytes the TypeScript SDK writes for these values. The writer stores the unsigned maximum of
    # a width as all ones, the same bits as -1.
    rom = rom_of(
        ("low64", VMType.INT64, "-9223372036854775808"),
        ("high64", VMType.INT64, "18446744073709551615"),
        ("low256", VMType.INT256, str(INT256_MIN)),
        ("high256", VMType.INT256, str(UINT256_MAX)),
        ("list64", VMType.ARRAY_INT64, ["-1", "18446744073709551615"]),
        ("list256", VMType.ARRAY_INT256, ["1", str(UINT256_MAX)]),
    )

    assert rom.hex().upper() == (
        "0000000000000080"  # -2^63
        + "FFFFFFFFFFFFFFFF"  # 2^64 - 1
        + "A0"  # -2^255: a header byte (negative, 32 bytes), then the bytes
        + "00" * 31
        + "80"
        + "80"  # 2^256 - 1: the header byte of -1
        + "02000000"  # -1 and 2^64 - 1
        + "FFFFFFFFFFFFFFFF" * 2
        + "02000000"  # 1 and 2^256 - 1
        + "0101"
        + "80"
    )


def test_a_digit_string_writes_the_bytes_of_its_number() -> None:
    for vm_type in (VMType.INT64, VMType.INT256):
        assert rom_of(("x", vm_type, "-12")) == rom_of(("x", vm_type, -12))
        # Leading zeros do not change the number, however many there are. int() alone refuses a string
        # of more than 4300 digits.
        assert rom_of(("x", vm_type, "0" * 5000 + "12")) == rom_of(("x", vm_type, 12))


# An item is named by its index.
@pytest.mark.parametrize(
    ("vm_type", "value", "message"),
    [
        pytest.param(VMType.INT64, "18446744073709551616", f'metadata field "x" must be {INT64_RANGE}', id="2^64"),
        pytest.param(VMType.INT64, "-9223372036854775809", f'metadata field "x" must be {INT64_RANGE}', id="-2^63-1"),
        pytest.param(VMType.INT256, str(1 << 256), f'metadata field "x" must be {INT256_RANGE}', id="2^256"),
        pytest.param(VMType.INT256, str(INT256_MIN - 1), f'metadata field "x" must be {INT256_RANGE}', id="-2^255-1"),
        # A number longer than int() converts is refused by its range too.
        pytest.param(VMType.INT64, "9" * 5000, f'metadata field "x" must be {INT64_RANGE}', id="Int64-5000-digits"),
        pytest.param(
            VMType.INT256, "-" + "9" * 5000, f'metadata field "x" must be {INT256_RANGE}', id="Int256-5000-digits"
        ),
        pytest.param(
            VMType.ARRAY_INT64,
            ["1", "18446744073709551616"],
            f'metadata field "x[1]" must be {INT64_RANGE}',
            id="Array_Int64-item",
        ),
    ],
)
def test_a_digit_string_outside_the_range_of_its_field_is_refused(vm_type: VMType, value: object, message: str) -> None:
    assert refusal(vm_type, value) == message


# Only a whole string of decimal digits with an optional minus sign is a number: no hex, no line
# break, no space, no plus sign, no exponent, no underscore, and no digits of another script, which
# int() would read.
@pytest.mark.parametrize("vm_type", [VMType.INT64, VMType.INT256])
@pytest.mark.parametrize("value", ["0x10", "12\n", " 12", "+12", "1e3", "1_000", "-", "", "١٢", 1.5, True])
def test_a_value_that_is_neither_an_integer_nor_decimal_digits_is_refused(vm_type: VMType, value: object) -> None:
    assert refusal(vm_type, value) == 'metadata field "x" must be an integer or a string of decimal digits'


def test_an_item_of_an_array_int256_in_hex_is_refused() -> None:
    message = 'metadata field "x[1]" must be an integer or a string of decimal digits'
    assert refusal(VMType.ARRAY_INT256, ["1", "0x10"]) == message


def test_int8_int16_and_int32_still_refuse_text() -> None:
    for vm_type in (VMType.INT8, VMType.INT16, VMType.INT32):
        assert refusal(vm_type, "5") == 'metadata field "x" must be an integer'
