"""Coverage of the planner's input space.

The closure is taken over what the planner ACCEPTS, not over what the chain models: every message
type, every fee kind and every modelled call method needs a decision here or the tables fail.

The live matrix runs against one chain and one build; these tables are what fail on a laptop the
moment a branch appears with no decision behind it.

Three closures, because no two of them can see the third. A message type says nothing about which
call method a Call carries, and a call method that maps to an existing fee kind adds nothing to the
kinds a run has seen. A SCRIPT entry in either table is a decision that had to be written down,
never a default nobody chose.
"""

from __future__ import annotations

import pytest
from test_fee_plan import CREATOR, ICON, RECEIVER, base_tx, config

from phantasma_py import (
    BuilderError,
    BurnedInstance,
    FeePlanOptions,
    InfusedAsset,
    NativeFeeKind,
    burned_instances,
    plan_fees,
)
from phantasma_py.carbon import (
    BurnFungibleArgs,
    BurnNonFungibleArgs,
    CallArgSection,
    GovernanceContractMethod,
    IntX,
    MintFungibleArgs,
    MintPhantasmaNonFungibleArgs,
    ModuleID,
    MsgCallArgSections,
    PhantasmaNFTMintInfo,
    RegisterNameArgs,
    SeriesInfo,
    SmallString,
    TokenContractMethod,
    TransferFungibleArgs,
    TransferNonFungibleArgs,
    TxMsg,
    TxMsgBurnFungible,
    TxMsgBurnFungibleGasPayer,
    TxMsgBurnNonFungible,
    TxMsgBurnNonFungibleGasPayer,
    TxMsgCall,
    TxMsgCallMulti,
    TxMsgMintFungible,
    TxMsgMintNonFungible,
    TxMsgPhantasma,
    TxMsgPhantasmaRaw,
    TxMsgTrade,
    TxMsgTransferFungible,
    TxMsgTransferFungibleGasPayer,
    TxMsgTransferNonFungibleMulti,
    TxMsgTransferNonFungibleMultiGasPayer,
    TxMsgTransferNonFungibleSingle,
    TxMsgTransferNonFungibleSingleGasPayer,
    TxType,
    build_token_info,
    build_token_metadata,
    required_witnesses,
    serialize,
)

#: Arbitrary bytes. A method the planner does not model reads none of its arguments, so these are
#: enough for it; a modelled method would fail to read them, which is what makes the distinction
#: visible.
PROBE_ARGS = bytes([1, 2, 3, 4, 5, 6, 7, 8])


def burn_call(token_id: int, instance_ids: list[int]) -> TxMsgCall:
    args = BurnNonFungibleArgs(token_id, CREATOR, instance_ids)
    return TxMsgCall(ModuleID.TOKEN, TokenContractMethod.BURN_NON_FUNGIBLE, serialize(args))


def burn_fungible_call() -> TxMsgCall:
    args = BurnFungibleArgs(9, CREATOR, IntX(1))
    return TxMsgCall(ModuleID.TOKEN, TokenContractMethod.BURN_FUNGIBLE, serialize(args))


def type_cases() -> list[tuple[TxType, TxMsg, tuple[NativeFeeKind, ...] | None]]:
    """One row per message type: a message, and the operations the planner makes of it. None says
    the planner refuses the type."""
    return [
        (TxType.CALL, base_tx(TxType.CALL, CREATOR, burn_fungible_call()), (NativeFeeKind.BURN_FUNGIBLE,)),
        (
            TxType.CALL_MULTI,
            base_tx(TxType.CALL_MULTI, CREATOR, TxMsgCallMulti([burn_fungible_call(), burn_fungible_call()])),
            (NativeFeeKind.BURN_FUNGIBLE, NativeFeeKind.BURN_FUNGIBLE),
        ),
        # A Trade packs its operations into named arrays and not into calls. Nothing reads them yet,
        # so it takes the script budget, and that is a decision rather than an omission.
        (TxType.TRADE, base_tx(TxType.TRADE, CREATOR, TxMsgTrade()), (NativeFeeKind.SCRIPT,)),
        (
            TxType.TRANSFER_FUNGIBLE,
            base_tx(TxType.TRANSFER_FUNGIBLE, CREATOR, TxMsgTransferFungible(RECEIVER, 9, 1)),
            (NativeFeeKind.TRANSFER_FUNGIBLE,),
        ),
        (
            TxType.TRANSFER_FUNGIBLE_GAS_PAYER,
            base_tx(
                TxType.TRANSFER_FUNGIBLE_GAS_PAYER,
                CREATOR,
                TxMsgTransferFungibleGasPayer(RECEIVER, RECEIVER, 9, 1),
            ),
            (NativeFeeKind.TRANSFER_FUNGIBLE,),
        ),
        (
            TxType.TRANSFER_NON_FUNGIBLE_SINGLE,
            base_tx(
                TxType.TRANSFER_NON_FUNGIBLE_SINGLE,
                CREATOR,
                TxMsgTransferNonFungibleSingle(RECEIVER, 9, 1),
            ),
            (NativeFeeKind.TRANSFER_NON_FUNGIBLE,),
        ),
        (
            TxType.TRANSFER_NON_FUNGIBLE_SINGLE_GAS_PAYER,
            base_tx(
                TxType.TRANSFER_NON_FUNGIBLE_SINGLE_GAS_PAYER,
                CREATOR,
                TxMsgTransferNonFungibleSingleGasPayer(RECEIVER, RECEIVER, 9, 1),
            ),
            (NativeFeeKind.TRANSFER_NON_FUNGIBLE,),
        ),
        (
            TxType.TRANSFER_NON_FUNGIBLE_MULTI,
            base_tx(
                TxType.TRANSFER_NON_FUNGIBLE_MULTI,
                CREATOR,
                TxMsgTransferNonFungibleMulti(RECEIVER, 9, [1, 2]),
            ),
            (NativeFeeKind.TRANSFER_NON_FUNGIBLE,),
        ),
        (
            TxType.TRANSFER_NON_FUNGIBLE_MULTI_GAS_PAYER,
            base_tx(
                TxType.TRANSFER_NON_FUNGIBLE_MULTI_GAS_PAYER,
                CREATOR,
                TxMsgTransferNonFungibleMultiGasPayer(RECEIVER, RECEIVER, 9, [1, 2]),
            ),
            (NativeFeeKind.TRANSFER_NON_FUNGIBLE,),
        ),
        (
            TxType.MINT_FUNGIBLE,
            base_tx(TxType.MINT_FUNGIBLE, CREATOR, TxMsgMintFungible(9, RECEIVER, IntX(1))),
            (NativeFeeKind.MINT_FUNGIBLE,),
        ),
        (
            TxType.BURN_FUNGIBLE,
            base_tx(TxType.BURN_FUNGIBLE, CREATOR, TxMsgBurnFungible(9, IntX(1))),
            (NativeFeeKind.BURN_FUNGIBLE,),
        ),
        (
            TxType.BURN_FUNGIBLE_GAS_PAYER,
            base_tx(TxType.BURN_FUNGIBLE_GAS_PAYER, CREATOR, TxMsgBurnFungibleGasPayer(9, RECEIVER, IntX(1))),
            (NativeFeeKind.BURN_FUNGIBLE,),
        ),
        (
            TxType.MINT_NON_FUNGIBLE,
            base_tx(TxType.MINT_NON_FUNGIBLE, CREATOR, TxMsgMintNonFungible(9, RECEIVER, 1, bytes(8), b"")),
            (NativeFeeKind.MINT_NON_FUNGIBLE,),
        ),
        (
            TxType.BURN_NON_FUNGIBLE,
            base_tx(TxType.BURN_NON_FUNGIBLE, CREATOR, TxMsgBurnNonFungible(9, 1)),
            (NativeFeeKind.BURN_NON_FUNGIBLE,),
        ),
        (
            TxType.BURN_NON_FUNGIBLE_GAS_PAYER,
            base_tx(
                TxType.BURN_NON_FUNGIBLE_GAS_PAYER,
                CREATOR,
                TxMsgBurnNonFungibleGasPayer(9, RECEIVER, 1),
            ),
            (NativeFeeKind.BURN_NON_FUNGIBLE,),
        ),
        (
            TxType.PHANTASMA,
            base_tx(
                TxType.PHANTASMA,
                CREATOR,
                TxMsgPhantasma(SmallString("main"), SmallString("main"), bytes([1, 2, 3])),
            ),
            (NativeFeeKind.SCRIPT,),
        ),
        # A raw Phantasma transaction carries a foreign envelope the planner cannot size or read, so
        # it is refused instead of budgeted.
        (
            TxType.PHANTASMA_RAW,
            base_tx(TxType.PHANTASMA_RAW, CREATOR, TxMsgPhantasmaRaw(bytes([1, 2, 3]))),
            None,
        ),
    ]


def test_plans_every_transaction_type_as_declared() -> None:
    """Every TxType the SDK carries and what the planner makes of it. A new transaction type fails
    this test until someone states its fee."""
    seen: set[TxType] = set()
    for tx_type, msg, expected in type_cases():
        assert tx_type not in seen, f"{tx_type.name} is in the table twice"
        seen.add(tx_type)
        # The row must plan the type it declares, or the exhaustiveness check below means nothing.
        assert msg.type is tx_type, f"{tx_type.name} row carries a {msg.type.name} message"
        # Only the witness-array types take a count from the caller. Every other type fixes its own
        # witness set in the message, and a count that disagrees with it is refused.
        options = FeePlanOptions(
            witness_count=1 if required_witnesses(msg) is None else None,
            infusions=[],
        )
        if expected is None:
            with pytest.raises(BuilderError):
                plan_fees(msg, config(), options)
            continue
        plan = plan_fees(msg, config(), options)
        assert plan.kinds == expected, tx_type.name
    for tx_type in TxType:
        assert tx_type in seen, f"{tx_type.name} has no row: state what the planner makes of it"


def token_method_cases() -> list[tuple[int, bytes, NativeFeeKind]]:
    metadata = build_token_metadata(
        {"name": "Plan probe", "icon": ICON, "url": "https://example.invalid/p", "description": "x"}
    )
    info = build_token_info("GPX", IntX(0), is_nft=False, decimals=2, owner=CREATOR, metadata=metadata)
    modelled: list[tuple[int, bytes, NativeFeeKind]] = [
        (
            TokenContractMethod.TRANSFER_FUNGIBLE,
            serialize(TransferFungibleArgs(RECEIVER, CREATOR, 9, IntX(1))),
            NativeFeeKind.TRANSFER_FUNGIBLE,
        ),
        (
            TokenContractMethod.TRANSFER_NON_FUNGIBLE,
            serialize(TransferNonFungibleArgs(RECEIVER, CREATOR, 9, [1])),
            NativeFeeKind.TRANSFER_NON_FUNGIBLE,
        ),
        (TokenContractMethod.CREATE_TOKEN, serialize(info), NativeFeeKind.CREATE_TOKEN),
        (
            TokenContractMethod.MINT_FUNGIBLE,
            serialize(MintFungibleArgs(9, RECEIVER, IntX(1))),
            NativeFeeKind.MINT_FUNGIBLE,
        ),
        (
            TokenContractMethod.BURN_FUNGIBLE,
            serialize(BurnFungibleArgs(9, CREATOR, IntX(1))),
            NativeFeeKind.BURN_FUNGIBLE,
        ),
        (
            TokenContractMethod.CREATE_TOKEN_SERIES,
            (9).to_bytes(8, "little") + serialize(SeriesInfo(1, 10, CREATOR, b"")),
            NativeFeeKind.CREATE_TOKEN_SERIES,
        ),
        (
            TokenContractMethod.BURN_NON_FUNGIBLE,
            serialize(BurnNonFungibleArgs(9, CREATOR, [1])),
            NativeFeeKind.BURN_NON_FUNGIBLE,
        ),
        (
            TokenContractMethod.MINT_PHANTASMA_NON_FUNGIBLE,
            serialize(MintPhantasmaNonFungibleArgs(9, RECEIVER, [PhantasmaNFTMintInfo(IntX(1), bytes(8), b"")])),
            NativeFeeKind.MINT_PHANTASMA_NON_FUNGIBLE,
        ),
    ]
    # Every other method of the module is budgeted. MINT_NON_FUNGIBLE is among them on purpose: the
    # chain refuses an explicit NFT mint where governance has not allowed caller-supplied ROM ids,
    # whichever way it arrives, so there is nothing to price.
    priced = {method for method, _, _ in modelled}
    return modelled + [
        (int(method), PROBE_ARGS, NativeFeeKind.SCRIPT) for method in TokenContractMethod if method not in priced
    ]


def test_plans_every_token_contract_method_as_declared() -> None:
    """Every method of the token module and what the planner makes of a call to it. A method that
    grows a model, or loses one, fails here."""
    seen: set[int] = set()
    for method, args, kind in token_method_cases():
        assert method not in seen, f"token method {method} is in the table twice"
        seen.add(method)
        msg = base_tx(TxType.CALL, CREATOR, TxMsgCall(ModuleID.TOKEN, method, args))
        plan = plan_fees(msg, config(), FeePlanOptions(witness_count=1, infusions=[]))
        assert plan.kinds == (kind,), f"token method {method}"
    for method in TokenContractMethod:
        assert int(method) in seen, f"token method {method.name} has no row"


def test_plans_every_governance_contract_method_as_declared() -> None:
    """The same for the governance module, plus a module the planner does not dispatch on at all."""
    options = FeePlanOptions(witness_count=1)
    cases = [
        (
            GovernanceContractMethod.REGISTER_NAME,
            serialize(RegisterNameArgs(CREATOR, SmallString("probe"))),
            NativeFeeKind.REGISTER_NAME,
        ),
        (GovernanceContractMethod.SET_GAS_CONFIG, PROBE_ARGS, NativeFeeKind.SCRIPT),
    ]
    seen: set[int] = set()
    for method, args, kind in cases:
        seen.add(int(method))
        msg = base_tx(TxType.CALL, CREATOR, TxMsgCall(ModuleID.GOVERNANCE, method, args))
        assert plan_fees(msg, config(), options).kinds == (kind,), f"governance method {method}"
    for method in GovernanceContractMethod:
        assert int(method) in seen, f"governance method {method.name} has no row"
    # A module the planner does not dispatch on at all takes the script budget.
    market = base_tx(TxType.CALL, CREATOR, TxMsgCall(ModuleID.MARKET, 1, PROBE_ARGS))
    assert plan_fees(market, config(), options).kinds == (NativeFeeKind.SCRIPT,)


def test_prices_a_batch_as_the_sum_of_its_calls_with_one_envelope() -> None:
    """The chain runs a CALL_MULTI as a loop over one VM environment and one result buffer, so the
    bill is the sum of the parts with the envelope counted once, and never a flat script budget."""
    options = FeePlanOptions(witness_count=1, infusions=[])
    single = base_tx(TxType.CALL, CREATOR, burn_call(9, [1]))
    batch = base_tx(TxType.CALL_MULTI, CREATOR, TxMsgCallMulti([burn_call(9, [1]), burn_call(9, [2])]))
    one = plan_fees(single, config(), options)
    two = plan_fees(batch, config(), options)
    assert two.kinds == (NativeFeeKind.BURN_NON_FUNGIBLE, NativeFeeKind.BURN_NON_FUNGIBLE)
    # Each burn charges one transfer and two queries, which is 30 work units at these prices. The
    # rest of the difference is the envelope, which grows by the second call and is billed once.
    envelope_growth = two.envelope_bytes - one.envelope_bytes
    assert two.expected_gas_bill == one.expected_gas_bill + (30 + 25 * envelope_growth) * 10_000


def test_prices_what_the_batch_gives_back_once() -> None:
    """The returns cost the same wherever they sit, so the first burn takes the whole list and the
    burns after it take none; counting them per burn would multiply the return of one NFT by the
    batch size."""
    batch = base_tx(TxType.CALL_MULTI, CREATOR, TxMsgCallMulti([burn_call(9, [1]), burn_call(9, [2])]))
    empty = plan_fees(batch, config(), FeePlanOptions(witness_count=1, infusions=[]))
    returned = plan_fees(batch, config(), FeePlanOptions(witness_count=1, infusions=[InfusedAsset(token_id=7)]))
    # One returned fungible token costs one transfer plus one owner-lookup query, which is 20 work
    # units at these prices. Counting it per burn would double it.
    assert returned.expected_gas_bill - empty.expected_gas_bill == 20 * 10_000
    assert returned.max_data - empty.max_data == config().data_escrow_per_row


def test_names_every_instance_a_message_burns() -> None:
    """The helper a planner with a chain uses to find the addresses it must read. It answers for the
    native burns, for a burn call, and for every burn inside a batch."""
    native = base_tx(TxType.BURN_NON_FUNGIBLE, CREATOR, TxMsgBurnNonFungible(9, 4))
    assert burned_instances(native) == [BurnedInstance(9, 4)]
    batch = base_tx(
        TxType.CALL_MULTI,
        CREATOR,
        TxMsgCallMulti([burn_call(9, [1, 2]), burn_fungible_call(), burn_call(5, [3])]),
    )
    assert burned_instances(batch) == [BurnedInstance(9, 1), BurnedInstance(9, 2), BurnedInstance(5, 3)]
    transfer = base_tx(TxType.TRANSFER_FUNGIBLE, CREATOR, TxMsgTransferFungible(RECEIVER, 9, 1))
    assert burned_instances(transfer) == []


def test_says_whether_the_number_is_a_prediction_or_a_ceiling() -> None:
    """Whether the number a plan carries is a prediction of the settlement or an upper bound on it."""
    gas_token = base_tx(TxType.TRANSFER_FUNGIBLE, CREATOR, TxMsgTransferFungible(RECEIVER, config().gas_token_id, 1))
    assert plan_fees(gas_token, config(), FeePlanOptions()).exact, (
        "a gas-token transfer reads no state fact, so its plan is a prediction"
    )

    ordinary = base_tx(TxType.TRANSFER_FUNGIBLE, CREATOR, TxMsgTransferFungible(RECEIVER, 9, 1))
    assert not plan_fees(ordinary, config(), FeePlanOptions()).exact, (
        "an unstated recipient row decides the price, so the plan is a ceiling"
    )
    assert plan_fees(ordinary, config(), FeePlanOptions(recipient_holds_token=True)).exact, (
        "with the recipient row stated the plan is a prediction"
    )

    # A budgeted part makes the whole plan a ceiling, whatever the facts say.
    script = base_tx(TxType.PHANTASMA, CREATOR, TxMsgPhantasma(SmallString("main"), SmallString("main"), bytes([1])))
    assert not plan_fees(script, config(), FeePlanOptions(witness_count=1)).exact, (
        "a budgeted script is never a prediction"
    )


def test_budgets_a_call_that_builds_its_arguments_at_execution_time() -> None:
    """The one call shape that cannot be priced from the message: its arguments are assembled from
    the results of earlier calls, so there is nothing to read them from yet."""
    sectioned = base_tx(
        TxType.CALL,
        CREATOR,
        TxMsgCall(
            ModuleID.TOKEN,
            TokenContractMethod.BURN_FUNGIBLE,
            b"",
            MsgCallArgSections([CallArgSection(-1, b"")]),
        ),
    )
    assert plan_fees(sectioned, config(), FeePlanOptions(witness_count=1)).kinds == (NativeFeeKind.SCRIPT,)
