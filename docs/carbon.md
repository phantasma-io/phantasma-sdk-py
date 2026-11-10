# Carbon

`phantasma_py.carbon` implements the Carbon wire formats and helper structures
shared with the C#, TypeScript, C++, and Go SDKs.

## Wire Format

Carbon uses fixed-width little-endian integers, zero-terminated UTF-8 strings,
fixed byte values (`Bytes16`, `Bytes32`, `Bytes64`), length-prefixed arrays, and
a compact signed Int256 representation. `IntX` uses the eight-byte form when the
value fits in signed int64 and falls back to the full Int256 encoding otherwise.

```python
from phantasma_py.carbon import CarbonReader, CarbonWriter, IntX

writer = CarbonWriter()
IntX(100).write_carbon(writer)

assert IntX.read_carbon(CarbonReader(writer.bytes())).value == 100
```

## VM Schemas

VM schemas and dynamic structs are used by token metadata and NFT ROM/RAM
builders. The SDK validates required metadata fields and schema types before
serializing.

```python
from phantasma_py.carbon import build_nft_rom, prepare_standard_token_schemas

schemas = prepare_standard_token_schemas(shared_metadata=False)
rom = build_nft_rom(
    schemas.rom,
    1,
    [
        ("name", "Example NFT"),
        ("description", "Example NFT metadata"),
        ("imageURL", "https://example.invalid/image.png"),
        ("infoURL", "https://example.invalid/nft"),
        ("royalties", 10_000_000),
    ],
)
```

## Token Builders

Token helper functions create Carbon payloads in the chain's wire order:

- `build_token_info`
- `build_series_info`
- `build_create_token_tx`
- `build_create_token_tx_and_sign`
- `build_create_token_series_tx`
- `build_create_token_series_tx_and_sign`
- `build_update_token_metadata_tx`
- `build_update_token_metadata_tx_and_sign`
- `build_mint_phantasma_non_fungible_tx`
- `build_mint_phantasma_non_fungible_tx_and_sign`

The native transaction types have builders too: `build_transfer_fungible_tx`,
`build_transfer_non_fungible_tx`, `build_mint_fungible_tx`,
`build_burn_fungible_tx` and `build_burn_non_fungible_tx`; naming a `gas_payer`
selects the gas-payer form, where a second account pays the gas and both sign.
Phantasma NFTs are minted through `build_mint_phantasma_non_fungible_tx` only:
the native `MintNonFungible` message exists for the chain's own use.

Builders carry no prices. They write only the `TxLimits` you pass; a message
built without `max_gas` has a zero offer, which marks it as unplanned and
refuses to sign - plan it from the message (see `docs/rpc.md`) or state the
offer. The `*_and_sign` helpers plan the message against a `GasConfig` before
signing unless `PlanAndSignOptions.limits` fixes the offer.

They are safe constructors: malformed symbols, missing metadata, invalid icon
data URIs, and wrong schema field names raise explicit SDK errors.

Token symbols follow the current Carbon token-module rule: one or more uppercase
ASCII letters `A-Z`.

## NFT Token Schemas And Metadata Values

An NFT token is created with three schemas: `seriesMetadata` (the data a series
shares), `rom` (the data an NFT gets at its mint and keeps) and `ram` (the data
of an NFT that can change). The schemas of a token cannot change after it is
created. `token_schemas_from_json` builds them from JSON. A field is
`{"name": ..., "type": ...}`, and the type is a VM type name such as `String`,
`Int32`, `Bytes32` or `Array_Int64`. A `Struct` or `Array_Struct` field also
lists the fields of its struct, in the same form:

```json
{
  "seriesMetadata": [],
  "rom": [
    { "name": "name", "type": "String" },
    { "name": "description", "type": "String" },
    { "name": "imageURL", "type": "String" },
    { "name": "infoURL", "type": "String" },
    { "name": "royalties", "type": "Int32" },
    {
      "name": "stats",
      "type": "Struct",
      "fields": [
        { "name": "level", "type": "Int32" },
        { "name": "class", "type": "String" }
      ]
    }
  ],
  "ram": []
}
```

The SDK adds its own fields: `_i` and `rom` to the `rom` schema, and `_i`,
`mode` and `rom` to the `seriesMetadata` schema. The fields `name`,
`description`, `imageURL`, `infoURL` and `royalties` must be in `rom` or in
`seriesMetadata`. `build_token_schemas_from_fields` takes the fields as
`TokenSchemaField` values, `(name, type)` pairs or mappings in the JSON form.
The `fields` of a `TokenSchemaField` hold the fields of its struct.

The builders of NFT data and series metadata (`build_phantasma_nft_rom`,
`build_nft_rom`, `build_token_series_metadata`) take the values as
`(name, value)` pairs and check each value against the field's type:

| Type | Value |
|---|---|
| `String` | a `str` that is not blank |
| `Int8`, `Int16`, `Int32` | an `int` in the signed or the unsigned range of the type |
| `Int64`, `Int256` | the same, or a string of decimal digits such as `"-12"` or `"9007199254740993"` |
| `Bytes` | `bytes`, a `bytearray` or a hex string |
| `Bytes16`, `Bytes32`, `Bytes64` | the same, of exactly 16, 32 or 64 bytes, or a `Bytes16`, `Bytes32` or `Bytes64` |
| `Struct` | a mapping or a list of `(name, value)` pairs with a value for every field of the struct, or a `VMDynamicStruct` that holds every field |
| `Array_Struct` | a list of such structs, or a `VMStructArray` |
| `Array_Dynamic` | a list of `VMDynamicVariable` values |
| other `Array_...` types | a list of values of the item type |

```python
from phantasma_py.carbon import build_phantasma_nft_rom, token_schemas_from_json

schemas = token_schemas_from_json(schemas_json)
rom = build_phantasma_nft_rom(
    schemas.rom,
    [
        ("name", "Hero"),
        ("description", "An NFT with a struct field"),
        ("imageURL", "https://example.invalid/hero.png"),
        ("infoURL", "https://example.invalid/hero"),
        ("royalties", 0),
        ("stats", {"level": 3, "class": "mage"}),
    ],
)
```

`schemas_json` is the JSON above.

## Changing Token Metadata

`build_update_token_metadata_tx` builds `Token.UpdateTokenMetadata`, signed by
the token owner. The chain merges an update into the stored metadata field by
field, so a field the update does not name keeps its value. A field is removed
by sending it as an empty `Array_Dynamic`. This is the only value the chain
reads as a removal:

```python
from phantasma_py.carbon import (
    VMDynamicStruct,
    VMNamedDynamicVariable,
    VMType,
    build_update_token_metadata_tx,
    bytes32_from_public_key,
)
from phantasma_py.crypto import PhantasmaKeys

keys = PhantasmaKeys.from_wif("...")
update = VMDynamicStruct(
    [
        VMNamedDynamicVariable.make("description", VMType.STRING, "A new description"),
        VMNamedDynamicVariable.make("website", VMType.ARRAY_DYNAMIC, []),
    ]
)
msg = build_update_token_metadata_tx(token_id, update, bytes32_from_public_key(keys.public_key))
tx_hash = rpc.send_transaction(msg, [keys])
```

`rpc` is a `PhantasmaRPC` client (see `docs/rpc.md`). The planner prices
`Token.UpdateTokenMetadata` with the script budget, so its plan is an upper
bound (`plan.exact` is `False`). The chain charges the transfer fee and the
storage the update adds.

## Sealing A Token Against Contracts

A sealed token never gets a VM contract, from its owner or from governance. Only
a token without a contract can be sealed, and the seal cannot be removed.

The seal blocks VM contracts only. A sealed token can be minted, transferred and
burned, and it can get new NFT series and NFT mints. Its other metadata fields
can be changed.

The seal is the metadata field `_sealed_c` holding the Int64 value 1
(`STANDARD_META_TOKEN_SEALED_CONTRACT`).

Seal a token when it is created:

```python
metadata = build_token_metadata(fields, seal_contract=True)
```

Seal a token that already exists:

```python
owner = bytes32_from_public_key(keys.public_key)
msg = build_update_token_metadata_tx(token_id, build_contract_seal_update(), owner)
tx_hash = rpc.send_transaction(msg, [keys])
```

## Reserved Metadata Keys

Token metadata keys that start with `_` belong to the chain: the staking
settings, the inflation settings, the contract seal and `_brn`, the burned
supply that genesis recorded for a token. The chain refuses any other key that
starts with `_`. It refuses `_brn` in an update, because only genesis sets it.

The SDK refuses these keys before anything is signed. `build_token_metadata`
takes no key that starts with `_` among its string fields, because the chain's
own keys are typed values. `build_update_token_metadata_tx` lets an update set
the chain's own keys. It lets an update name any other `_` key only to remove
it.

## Module Call Args

The SDK exposes the Carbon token and market module argument structures used by
the other SDKs, including `MintFungibleArgs`, `MintNonFungibleArgs`,
`MintPhantasmaNonFungibleArgs`, `TransferFungibleArgs`,
`TransferNonFungibleArgs`, `BurnFungibleArgs`, `BurnNonFungibleArgs`,
`UpdateTokenMetadataArgs`, `UpdateSeriesMetadataArgs`, `TokenListing`,
`MarketConfig`, and the market sell/buy/cancel/listing-info argument types.

`ChainConfig`, `GasConfig`, and `TokensConfig` are also serializable through the
same `serialize()` / `deserialize()` contract.

## Transactions

`TxMsg` and `SignedTxMsg` support the Carbon transaction payload families from
the current Go SDK: calls, multi-calls, trades, fungible transfers, NFT
transfers, mint/burn payloads, and Phantasma VM wrappers.

Use `serialize()` and `deserialize()` for stable round-trips:

```python
from phantasma_py.carbon import SignedTxMsg, deserialize, serialize

decoded = deserialize(raw_bytes, SignedTxMsg)
assert serialize(decoded) == raw_bytes
```

Use `sign_tx_msg()`, `sign_and_serialize_tx_msg()`, or
`sign_and_serialize_tx_msg_hex()` for Carbon transaction signing with in-memory
keys - one key per witness the message needs, in any order - and
`sign_tx_msg_with()` for any `TxSigner` (a hardware wallet, a signing service).
These helpers sign the serialized `TxMsg` and create the same witness shape as
the Go, C#, Rust and TypeScript SDKs; a message whose `max_gas` is zero is
refused as unplanned. `required_witnesses()` names the witnesses a message's
type fixes and `envelope_bytes()` the size the signed message will have, which
gas model v2 bills.

```python
from phantasma_py.carbon import sign_and_serialize_tx_msg_hex
from phantasma_py.crypto import PhantasmaKeys

keys = PhantasmaKeys.from_wif("...")
raw_hex = sign_and_serialize_tx_msg_hex(tx_msg, keys)
```

The test suite keeps a shared `carbon_vectors.tsv` fixture copied from the Go SDK
to catch byte-order, field-order, and Int256 drift.
