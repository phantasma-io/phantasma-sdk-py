# RPC Client

`phantasma_py.rpc` contains a small JSON-RPC 2.0 transport and typed response
models for the current Phantasma RPC surface used by the other SDKs.

## Client

```python
from phantasma_py.rpc import PhantasmaRPC

rpc = PhantasmaRPC("http://localhost:5172/rpc")
account = rpc.get_account("P...", extended=False)
```

The client sends positional parameters as JSON arrays and validates response ids.
Some endpoints echo ids as JSON strings even when the caller uses a numeric id;
the Python client compares ids by string value to stay compatible while still
rejecting mismatches.

## Fee Planning And Sending

Every client owns a fee planner, `rpc.fees`, which reads the chain's gas config
through the client (cached for a minute; `rpc.fees.invalidate()` forgets it) and
prices a Carbon message from the message itself:

```python
plan = rpc.fees.plan(msg)                  # FeePlan: max_gas, max_data, expected_gas_bill, rows
params = rpc.fees.chain_params()           # expiry window, block rate, gas model version
config = rpc.fees.config()                 # the GasConfig, for the pure plan_fees / estimate_native_fee
```

`rpc.send_transaction(msg, signers, options=None)` plans an unplanned message,
pre-flights a token creation, collects every witness through its `TxSigner`
and broadcasts the envelope in one step; `rpc.preflight_transaction(msg)`
reports the pre-flight verdict alone, and `rpc.infused_assets(token_id,
instance_id)` reads what an NFT holds at its own address, which a burn returns
and pays for. A pre-flight refusal raises `PreflightError` before anything is
signed. See the README for the whole build, plan, sign, send flow.

## Errors

Transport and RPC failures raise `RPCError`; a JSON-RPC error body reaches the
caller as an `RPCError` with the node's code and message whatever the HTTP
status carried it.

```python
from phantasma_py.errors import RPCError

try:
    rpc.lookup_name("missing-name")
except RPCError as exc:
    print(exc.code, exc.data)
```

## Result Decoding

RPC objects are decoded into dataclasses such as `AccountResult`,
`TokenResult`, `TokenSeriesResult`, `BlockResult`, and `TransactionResult`.
Nested arrays are decoded as typed dataclass instances, not raw dictionaries.

`ScriptResult.decode_result()` and `decode_results(index)` parse VM object bytes
returned by script invocation endpoints.

## VM Values In Metadata And Properties

`TokenPropertyResult.value` is a `VmValue`: a scalar, an array, or a struct, exactly
as the chain stores it. It covers token metadata, series metadata, organization
metadata and the `properties`/`infusion` rows of an NFT. Scalars are always strings
because chain numbers are big integers.

```python
for row in token.metadata:
    text = row.value.as_text()
    if text is not None:
        print(row.key, text)
        continue
    items = row.value.as_items()
    if items is not None:
        print(row.key, "array of", len(items), "first mul:", items[0].get("mul"))
```

## Typed Extended Events

`EventExResult.data` is typed by the event's `kind`, and inside a special resolution
the `module` plus `method` of each call decide the shape of its `arguments`
(43 pairs). Anything this SDK does not model keeps its JSON verbatim in
`UnknownEventData` or `UnrecognizedArguments`, so a node newer than the SDK never
fails or truncates a block answer.

```python
from phantasma_py import SpecialResolutionData, UnknownEventData
from phantasma_py.special_resolution_arguments import TransferFungibleArguments

for event in tx.extended_events:
    if isinstance(event.data, SpecialResolutionData):
        for call in event.data.calls:
            if isinstance(call.arguments, TransferFungibleArguments):
                print(call.arguments.token, call.arguments.amount, call.arguments.from_)
    elif isinstance(event.data, UnknownEventData):
        print("unmodeled extended event", event.kind, event.data.data)
```

`from` is a Python keyword, so the argument shapes that carry it spell the field
`from_` and declare the wire name; every other field is the camelCase wire name in
snake_case.
