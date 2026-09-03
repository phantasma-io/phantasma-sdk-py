# Phantasma Python SDK

Typed Python SDK for the Phantasma blockchain with support for the Phoenix chain
update.

The package provides JSON-RPC access, transaction building and signing, VM script
helpers, Ed25519 keys/signatures, and Carbon wire-format support.

The public API uses Python naming conventions, dataclasses, exceptions, and type
hints. Fixture tests lock shared VM and Carbon wire formats against reference SDK
vectors.

## Requirements

- Python 3.11 or newer
- `cryptography`
- `requests`

Development uses `uv`, `ruff`, `mypy`, and `pytest`.

## Install

```sh
pip install phantasma-sdk-py
```

For local development:

```sh
uv sync --extra dev
just check
```

## Modules

- `phantasma_py.crypto`: addresses, hashes, WIF keys, Ed25519 signatures
- `phantasma_py.vm`: VM objects, opcodes, and `ScriptBuilder`
- `phantasma_py.transaction`: VM script transaction serialization/signing
- `phantasma_py.rpc`: JSON-RPC client and typed response dataclasses
- `phantasma_py.carbon`: Carbon primitives, VM schemas, module call args, token builders, and transaction messages

## RPC

```python
from phantasma_py.rpc import PhantasmaRPC

rpc = PhantasmaRPC.mainnet()
account = rpc.get_account("P...")
balance = account.get_token_balance("SOUL", decimals=8)

print(balance.decimal_amount())
```

`JsonRpcClient` validates JSON-RPC response ids, propagates RPC errors as
`RPCError`, and accepts endpoints that echo numeric ids as strings.

## Keys And Signatures

```python
from phantasma_py.crypto import PhantasmaKeys

keys = PhantasmaKeys.from_wif("...")
signature = keys.sign(b"message")

assert signature.verify(b"message", [keys.address])
```

Address parsing rejects malformed Base58/checksum data. `Address.from_text("NULL")`
and `Address.null()` produce the system null address.

## VM Scripts

```python
from phantasma_py.crypto import Address, PhantasmaKeys
from phantasma_py.vm import ScriptBuilder

keys = PhantasmaKeys.from_wif("...")

script = (
    ScriptBuilder.begin()
    .allow_gas(keys.address, Address.null(), gas_price=10_000, gas_limit=210_000)
    .call_contract("stake", "GetStake", keys.address)
    .spend_gas(keys.address)
    .end_script()
)
```

`end_script()` raises `BuilderError` if labels or user input are invalid.
`end_script_with_error()` returns `(script, error)` for callers that prefer an
explicit checked path.

## VM Script Transactions

```python
from phantasma_py.crypto import PhantasmaKeys
from phantasma_py.transaction import Transaction
from phantasma_py.vm import ScriptBuilder

keys = PhantasmaKeys.from_wif("...")
script = ScriptBuilder.begin().call_interop("Runtime.Time").end_script()

tx = Transaction("mainnet", "main", script, expiration=1_754_000_000)
tx.sign(keys)

raw_hex = tx.to_bytes().hex()
```

Broadcasting is intentionally separate from signing:

```python
tx_hash = rpc.send_raw_transaction(raw_hex)
```

Do not run broadcasting examples without explicit credentials, funds, and an
endpoint you intend to use.

## Carbon

Carbon serialization uses fixed-width little-endian integers, zero-terminated
strings, fixed byte types, compact signed Int256 values, and typed transaction
payloads. Use `serialize()` and `deserialize()` for stable wire round-trips.

```python
from phantasma_py.carbon import (
    Bytes32,
    IntX,
    build_token_info,
    build_token_metadata,
    prepare_standard_token_schemas,
    serialize,
)

owner = Bytes32()
schemas = prepare_standard_token_schemas(shared_metadata=False)
token = build_token_info(
    symbol="ART",
    max_supply=IntX(0),
    is_nft=True,
    decimals=0,
    owner=owner,
    metadata=build_token_metadata(
        {
            "name": "Art Token",
            "icon": "data:image/png;base64,AA==",
            "url": "https://example.invalid/art",
            "description": "Example token metadata",
        }
    ),
    token_schemas=serialize(schemas),
)

payload = serialize(token)
```

Carbon token and NFT helpers validate required metadata, token symbol casing,
standard schema fields, Carbon NFT address packing, and result parsing. Token
symbols follow the Carbon token-module rule of uppercase ASCII letters `A-Z`.

## Fee planning: build, plan, sign, send

Under gas model v2 every byte a transaction puts in the block is billed and every
new storage row is escrowed, so the fee of a native operation is a function of
the message and the chain's prices - and the SDK computes it from the message.
Builders carry no prices: a message built without a `max_gas` has a zero offer,
which marks it as unplanned and refuses to sign. The steps are:

1. **Build** the message with a builder (`build_transfer_fungible_tx`,
   `build_create_token_tx`, `build_mint_phantasma_non_fungible_tx`, ...). The
   builders write only the limits you pass (`TxLimits`).
2. **Plan** it against the chain: `rpc.fees.plan(msg)` reads the chain's gas
   config through the client (cached for a minute), recognises the operation and
   prices it. For every operation the SDK models the bill is exact for the facts
   it was given; a chain-state fact the message does not carry - whether the
   recipient already holds the token, whether the series is duplicated - defaults
   to the reading that costs more, so an unstated plan is an upper bound the
   settlement can only undercut, and the unused part of the offer is refunded.
   State what you know in `PlanRequestOptions` to get the exact quote. A burn of
   an NFT is planned for what the NFT holds: the client reads the NFT's address
   for you; the pure `plan_fees` demands the list instead.
3. **Sign** with every witness the message needs: `sign_tx_msg(planned, *keys)`
   for in-memory keys, `sign_tx_msg_with(planned, *signers)` for a `TxSigner`
   such as a hardware wallet. A gas-payer transfer takes the payer's and the
   owner's keys; the SDK puts them in the order the chain reads.
4. **Send** the envelope with `rpc.send_carbon_transaction(raw)`.

`rpc.send_transaction(msg, signers)` does all four in one step, plus a
pre-flight: a token creation pays its policy fee before the chain looks at the
symbol, so the client asks whether the symbol is taken and refuses to send
unless the chain answered that it is free. `rpc.preflight_transaction(msg)`
reports that verdict to callers who want to decide for themselves.

```python
from phantasma_py import PhantasmaRPC, build_transfer_fungible_tx, summarize_fee_plan
from phantasma_py.carbon import bytes32_from_public_key
from phantasma_py.crypto import PhantasmaKeys

rpc = PhantasmaRPC("http://localhost:5172/rpc")
keys = PhantasmaKeys.from_wif("...")
msg = build_transfer_fungible_tx(
    from_address=bytes32_from_public_key(keys.public_key),
    to=receiver,
    token_id=1,  # KCAL
    amount=100_000_000,
)

plan = rpc.fees.plan(msg)
summary = summarize_fee_plan(plan)
print(f"gas {summary.gas_bill} KCAL, storage deposit up to {summary.storage_ceiling} SOUL")

# The wallet shows the summary and asks; then:
tx_hash = rpc.send_transaction(msg, [keys])
```

Notes:

- `summarize_fee_plan` renders a plan in KCAL and SOUL; `plan.apply(msg)` returns
  the message with the plan written in when you sign yourself.
- A message keeps a default lifetime of 45 seconds. When a person sits between
  building and signing, set `TxLimits.expiry` from the chain's own window:
  `expiry_within(rpc.fees.chain_params().expiry_window_ms)`.
- Calls whose witness set the caller chooses (token creation, series creation,
  Phantasma mints, name registration) need `witness_count` in the plan options;
  `send_transaction` and the `build_*_tx_and_sign` helpers fill it in from the
  signers they are given.
- Scripts and calls the SDK does not model are planned as a budget
  (`NativeFeeKind.SCRIPT`), not a formula; the node's `estimate_transaction`
  gives their exact bill.
- A node that refuses a request with an HTTP error and a JSON-RPC body surfaces
  the body as an `RPCError` with the node's code and message, so the node's
  refusal is told from a transport failure.

`examples/plan_carbon_transfer_fee.py` plans a one-atom KCAL transfer against a
node and prints the summary without signing or sending anything.

Carbon transaction signing is available without going through RPC. Without a
chain to plan against, state the offer yourself; a 170-byte KCAL transfer bills
0.00426 KCAL on mainnet, and the unused part of the offer is refunded:

```python
from phantasma_py.carbon import TxLimits, build_transfer_fungible_tx, sign_and_serialize_tx_msg_hex
from phantasma_py.crypto import PhantasmaKeys

keys = PhantasmaKeys.from_wif("...")
msg = build_transfer_fungible_tx(
    from_address=sender,
    to=receiver,
    token_id=1,
    amount=1_000_000,
    limits=TxLimits(max_gas=50_000_000),  # 0.005 KCAL
)
raw_hex = sign_and_serialize_tx_msg_hex(msg, keys)
```

## Development

```sh
just f          # format and autofix
just f-check    # verify formatting and lint
just typecheck  # strict mypy
just test       # pytest
just build      # package build
just check      # all checks above
```

The shared Carbon vector fixture in `tests/fixtures/carbon_vectors.tsv` is
copied from the Go SDK and should stay byte-for-byte compatible across SDKs.
