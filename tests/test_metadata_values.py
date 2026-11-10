"""Metadata values: each value is checked against the type of its field.

An Int64 or Int256 value can be a string of decimal digits, because JSON keeps a big integer exact
only as text. Such a string is checked against the range of its field as a number is.

A Dynamic field can hold a value of any VM type, and the chain stores the type with the value. So
the value names its type: a MetadataDynamicValue, its value given as for a field of that type."""

from __future__ import annotations

from typing import cast

import pytest

from phantasma_py import BuilderError, MetadataDynamicValue, build_phantasma_nft_rom
from phantasma_py.carbon import (
    CarbonReader,
    VMDynamicStruct,
    VMDynamicVariable,
    VMNamedVariableSchema,
    VMStructSchema,
    VMType,
)

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


def test_a_dynamic_field_stores_its_value_with_the_type_it_names() -> None:
    root = "00112233445566778899AABBCCDDEEFF00112233445566778899AABBCCDDEEFF"
    schema = VMStructSchema(
        [
            VMNamedVariableSchema.make("extra", VMType.DYNAMIC),
            VMNamedVariableSchema.make("names", VMType.DYNAMIC),
            VMNamedVariableSchema.make("extras", VMType.ARRAY_DYNAMIC),
            VMNamedVariableSchema.make("note", VMType.STRING),
        ]
    )

    rom = build_phantasma_nft_rom(
        schema,
        [
            ("extra", MetadataDynamicValue(VMType.INT64, "9007199254740993")),
            ("names", MetadataDynamicValue(VMType.ARRAY_STRING, ["a", "b"])),
            ("extras", [MetadataDynamicValue(VMType.INT32, 5), MetadataDynamicValue(VMType.BYTES32, root)]),
            ("note", "end"),
        ],
    )

    # A Dynamic field is written as its value's type byte and then the value. An Array_Dynamic field
    # is a count and then each item in the same way.
    assert rom.hex().upper() == (
        "0C"  # Int64 2^53 + 1
        + "0100000000002000"
        + "17"  # Array_String a, b
        + "02000000"
        + "6100"
        + "6200"
        + "02000000"  # Int32 5, then Bytes32
        + "0A"
        + "05000000"
        + "12"
        + root
        + "656E6400"  # end
    )
    # Read back by its schema, a Dynamic field holds the stored value with the value's own type.
    read = VMDynamicStruct.read_with_schema(schema, CarbonReader(rom))
    extra = read.get("extra")
    assert extra is not None and extra.type == VMType.DYNAMIC
    assert extra.data == VMDynamicVariable(VMType.INT64, 9007199254740993)


def test_a_dynamic_field_takes_a_vm_dynamic_variable_as_it_is_given() -> None:
    raw = VMDynamicVariable(VMType.STRING, "raw")

    assert rom_of(("x", VMType.DYNAMIC, raw)) == rom_of(
        ("x", VMType.DYNAMIC, MetadataDynamicValue(VMType.STRING, "raw"))
    )
    # The two forms can stand side by side in an Array_Dynamic field.
    rom = rom_of(("x", VMType.ARRAY_DYNAMIC, [raw, MetadataDynamicValue(VMType.INT8, 1)]))
    assert rom.hex().upper() == "02000000" + "16" + "72617700" + "06" + "01"


def test_a_typed_value_inside_a_struct_field() -> None:
    schema = VMStructSchema(
        [
            VMNamedVariableSchema.make(
                "stats", VMType.STRUCT, VMStructSchema([VMNamedVariableSchema.make("extra", VMType.DYNAMIC)])
            )
        ]
    )

    rom = build_phantasma_nft_rom(schema, [("stats", {"extra": MetadataDynamicValue(VMType.INT64, "5")})])

    assert rom.hex().upper() == "0C" + "0500000000000000"


@pytest.mark.parametrize(
    ("vm_type", "value", "message"),
    [
        pytest.param(
            VMType.DYNAMIC,
            "text",
            'metadata field "x" is Dynamic and must be given as a MetadataDynamicValue',
            id="value-without-type",
        ),
        pytest.param(
            VMType.DYNAMIC,
            MetadataDynamicValue(cast(VMType, 99), 1),
            'metadata field "x" names an unknown type 99',
            id="unknown-type",
        ),
        pytest.param(
            VMType.DYNAMIC,
            MetadataDynamicValue(VMType.INT8, 300),
            'metadata field "x" must be between -128 and 127 or between 0 and 255',
            id="value-checked-by-its-type",
        ),
        pytest.param(
            VMType.ARRAY_DYNAMIC,
            [MetadataDynamicValue(VMType.INT8, 1), 5],
            'metadata field "x[1]" is Dynamic and must be given as a MetadataDynamicValue',
            id="item-without-type",
        ),
        pytest.param(
            VMType.ARRAY_DYNAMIC,
            [MetadataDynamicValue(VMType.BYTES32, "0011")],
            'metadata field "x[0]" must be exactly 32 bytes',
            id="item-checked-by-its-type",
        ),
    ],
)
def test_a_malformed_dynamic_value_is_refused(vm_type: VMType, value: object, message: str) -> None:
    assert refusal(vm_type, value) == message


# A MetadataDynamicValue names one type, so it cannot give the types of the fields of a struct. It
# takes no typed value inside a typed value either. The SDK names the type of Array_Dynamic, which is
# the plain Array type, "Array".
@pytest.mark.parametrize(
    ("vm_type", "type_name"),
    [
        (VMType.STRUCT, "Struct"),
        (VMType.ARRAY_STRUCT, "Array_Struct"),
        (VMType.DYNAMIC, "Dynamic"),
        (VMType.ARRAY_DYNAMIC, "Array"),
    ],
)
def test_a_dynamic_value_of_a_struct_type_or_a_dynamic_type_is_refused(vm_type: VMType, type_name: str) -> None:
    message = f'metadata field "x" cannot hold a Dynamic value of type {type_name}'
    assert refusal(VMType.DYNAMIC, MetadataDynamicValue(vm_type, {})) == message
