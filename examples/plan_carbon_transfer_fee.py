"""Plan the fee of a one-atom KCAL transfer against a node, without signing or sending anything.

The plan is what a wallet shows before asking for confirmation. Read-only - it costs nothing to
run. Usage: plan_carbon_transfer_fee.py [RPC_URL] [RECIPIENT_ADDRESS]
"""

from __future__ import annotations

import os
import sys

from phantasma_py import PhantasmaRPC, summarize_fee_plan
from phantasma_py.carbon import (
    build_transfer_fungible_tx,
    bytes32_from_phantasma_address_text,
    bytes32_from_public_key,
)
from phantasma_py.crypto import PhantasmaKeys


def main() -> None:
    endpoint = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PHANTASMA_RPC_URL", "http://localhost:5172/rpc")
    # The sender needs no funds to be planned for: only its address goes into the message.
    keys = PhantasmaKeys(bytes([7]) * 32)
    sender = bytes32_from_public_key(keys.public_key)
    recipient = bytes32_from_phantasma_address_text(sys.argv[2]) if len(sys.argv) > 2 else sender

    rpc = PhantasmaRPC(endpoint)
    config = rpc.fees.config()
    msg = build_transfer_fungible_tx(from_address=sender, to=recipient, token_id=config.gas_token_id, amount=1)
    # Every fact this plan needs is in the message: a gas-token transfer escrows no rows, so the
    # options stay empty. A plan of an ordinary token would state recipient_holds_token when known.
    plan = rpc.fees.plan(msg)
    summary = summarize_fee_plan(plan)
    operations = ", ".join(kind.name for kind in plan.kinds)
    print(f"Operations: {operations}, signed size {plan.envelope_bytes} bytes")
    print(f"Gas bill: {summary.gas_bill} KCAL (offer {summary.gas_offer} KCAL)")
    print(f"Storage deposit ceiling: {summary.storage_ceiling} SOUL ({plan.new_storage_quanta} new rows)")
    print("Nothing was signed or sent.")


if __name__ == "__main__":
    main()
