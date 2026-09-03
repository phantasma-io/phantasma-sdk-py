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
