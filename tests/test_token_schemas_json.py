"""Token schemas JSON: the fields of a struct field.

A Struct or Array_Struct field carries the schema of its struct, and the chain writes the field's
values by it. The JSON of token schemas names that struct's fields under "fields", nested to any
depth."""

from __future__ import annotations

import pytest

from phantasma_py import (
    BuilderError,
    TokenSchemaField,
    build_phantasma_nft_rom,
    build_token_schemas_from_fields,
    parse_token_schemas_json,
    serialize_token_schemas_hex,
    token_schemas_from_json,
)
from phantasma_py.carbon import VMType

# The fields every NFT token schema needs, so that token_schemas_from_json accepts the schemas.
STANDARD_ROM_FIELDS_JSON = """
    {"name": "name", "type": "String"},
    {"name": "description", "type": "String"},
    {"name": "imageURL", "type": "String"},
    {"name": "infoURL", "type": "String"},
    {"name": "royalties", "type": "Int32"}"""
STANDARD_ROM_FIELDS = [
    TokenSchemaField("name", VMType.STRING),
    TokenSchemaField("description", VMType.STRING),
    TokenSchemaField("imageURL", VMType.STRING),
    TokenSchemaField("infoURL", VMType.STRING),
    TokenSchemaField("royalties", VMType.INT32),
]

STATS_FIELD_JSON = """{"name": "stats", "type": "Struct", "fields": [
    {"name": "level", "type": "Int32"},
    {"name": "class", "type": "String"}]}"""
ITEMS_FIELD_JSON = """{"name": "items", "type": "Array_Struct", "fields": [{"name": "id", "type": "Int64"}]}"""
OUTER_FIELD_JSON = """{"name": "outer", "type": "Struct", "fields": [""" + STATS_FIELD_JSON + "]}"

# The schemas as the TypeScript SDK writes them for the JSON of these tests. A field is a
# length-prefixed name and a type byte (Struct is 0x04, Array_Struct is 0x05). A struct field then
# has the schema of its struct: a u32 field count, the fields and a flags byte.
STATS_SCHEMA_HEX = "057374617473" + "04" + "02000000" + "056C6576656C0A" + "05636C61737316" + "00"
ITEMS_SCHEMA_HEX = "056974656D73" + "05" + "01000000" + "0269640C" + "00"
OUTER_SCHEMA_HEX = "056F75746572" + "04" + "01000000" + STATS_SCHEMA_HEX + "00"
SCHEMAS_HEX = (
    # series metadata: _i, mode and rom, which the SDK adds
    "03000000"
    + "025F690E"
    + "046D6F646506"
    + "03726F6D02"
    + "00"
    # rom: _i and rom, which the SDK adds, the standard fields, then stats, items and outer
    + "0A000000"
    + "025F690E"
    + "03726F6D02"
    + "046E616D6516"
    + "0B6465736372697074696F6E16"
    + "08696D61676555524C16"
    + "07696E666F55524C16"
    + "09726F79616C746965730A"
    + STATS_SCHEMA_HEX
    + ITEMS_SCHEMA_HEX
    + OUTER_SCHEMA_HEX
    + "00"
    # ram: no fields, dynamic extras
    + "00000000"
    + "01"
)


def rom_schemas_json(*extra_fields: str) -> str:
    rom = ", ".join([STANDARD_ROM_FIELDS_JSON, *extra_fields])
    return '{"seriesMetadata": [], "rom": [' + rom + '], "ram": []}'


def string_z_hex(text: str) -> str:
    """Returns the hex of text as the chain stores a string: its UTF-8 bytes and a zero byte."""
    return (text.encode() + b"\x00").hex().upper()


def test_a_struct_field_gets_the_schema_of_its_struct() -> None:
    schemas = token_schemas_from_json(rom_schemas_json(STATS_FIELD_JSON, ITEMS_FIELD_JSON, OUTER_FIELD_JSON))

    assert serialize_token_schemas_hex(schemas) == SCHEMAS_HEX


def test_parse_token_schemas_json_returns_the_fields_of_a_struct() -> None:
    parsed = parse_token_schemas_json(rom_schemas_json(OUTER_FIELD_JSON))

    stats = TokenSchemaField(
        "stats", VMType.STRUCT, (TokenSchemaField("level", VMType.INT32), TokenSchemaField("class", VMType.STRING))
    )
    assert parsed.rom[-1] == TokenSchemaField("outer", VMType.STRUCT, (stats,))
    assert parsed.rom[0] == TokenSchemaField("name", VMType.STRING)


def test_the_field_builders_take_the_fields_of_a_struct() -> None:
    # A TokenSchemaField and a mapping in the JSON form build the same schemas as the JSON.
    level = TokenSchemaField("level", VMType.INT32)
    stats = TokenSchemaField("stats", VMType.STRUCT, (level, TokenSchemaField("class", VMType.STRING)))
    items = TokenSchemaField("items", VMType.ARRAY_STRUCT, (TokenSchemaField("id", VMType.INT64),))
    outer = {
        "name": "outer",
        "type": "Struct",
        "fields": [
            {
                "name": "stats",
                "type": VMType.STRUCT,
                "fields": [
                    {"name": "level", "type": "Int32"},
                    {"name": "class", "type": "String"},
                ],
            }
        ],
    }

    schemas = build_token_schemas_from_fields([], [*STANDARD_ROM_FIELDS, stats, items, outer], [])

    assert serialize_token_schemas_hex(schemas) == SCHEMAS_HEX
    with pytest.raises(BuilderError) as refused:
        build_token_schemas_from_fields(
            [], [*STANDARD_ROM_FIELDS, {"name": "stats", "type": "Struct", "fields": "level"}], []
        )
    assert str(refused.value) == "stats fields must be a list"


# Each refusal names the field by its path: the list, then the name of each struct field above it.
@pytest.mark.parametrize(
    ("field", "message"),
    [
        ('{"name": "stats", "type": "Struct"}', "rom.stats has the type Struct and needs a fields array"),
        # JSON null and an object are no array either.
        (
            '{"name": "stats", "type": "Struct", "fields": null}',
            "rom.stats has the type Struct and needs a fields array",
        ),
        ('{"name": "stats", "type": "Struct", "fields": {}}', "rom.stats has the type Struct and needs a fields array"),
        (
            '{"name": "items", "type": "Array_Struct", "fields": []}',
            "rom.items has the type Array_Struct and needs at least one field",
        ),
        ('{"name": "title", "type": "String", "fields": []}', "rom.title has the type String and cannot have fields"),
        # The SDK names the type of Array_Dynamic, which is the plain Array type, "Array".
        (
            '{"name": "extras", "type": "Array_Dynamic", "fields": []}',
            "rom.extras has the type Array and cannot have fields",
        ),
        (
            '{"name": "stats", "type": "Struct", "fields": [{"name": "level", "type": 5}]}',
            "rom.stats field type must be string",
        ),
        (
            '{"name": "stats", "type": "Struct", "fields": [{"name": 5, "type": "Int32"}]}',
            "rom.stats field name must be string",
        ),
        ('{"name": "stats", "type": "Struct", "fields": ["level"]}', "rom.stats field must be an object"),
        (
            '{"name": "outer", "type": "Struct", "fields": [{"name": "stats", "type": "Struct"}]}',
            "rom.outer.stats has the type Struct and needs a fields array",
        ),
        ('"stats"', "rom field must be an object"),
    ],
)
def test_parse_token_schemas_json_refuses_a_malformed_field(field: str, message: str) -> None:
    with pytest.raises(BuilderError) as refused:
        parse_token_schemas_json(rom_schemas_json(field))
    assert str(refused.value) == message


def test_nft_data_of_a_struct_field_is_written_by_the_schema_from_json() -> None:
    # Only the values are written, in the order of the struct's schema. A string is its UTF-8 bytes
    # and a zero byte, an Int32 four little-endian bytes.
    schemas = token_schemas_from_json(rom_schemas_json(STATS_FIELD_JSON))

    rom = build_phantasma_nft_rom(
        schemas.rom,
        [
            ("name", "Hero"),
            ("description", "An NFT with a struct field"),
            ("imageURL", "https://example.invalid/hero.png"),
            ("infoURL", "https://example.invalid/hero"),
            ("royalties", 0),
            # The keys of a struct value can come in any order.
            ("stats", {"class": "mage", "level": 3}),
        ],
    )

    assert rom.hex().upper() == (
        string_z_hex("Hero")
        + string_z_hex("An NFT with a struct field")
        + string_z_hex("https://example.invalid/hero.png")
        + string_z_hex("https://example.invalid/hero")
        + "00000000"  # royalties
        + "03000000"  # stats: level, then class
        + string_z_hex("mage")
    )
