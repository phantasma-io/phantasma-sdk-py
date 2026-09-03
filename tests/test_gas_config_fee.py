"""Gas-model-v2 GasConfig wire format and getGasConfig conversion tests.

The chain serializes the 10 v2 config fields only for version >= 1; the version-0 image is
frozen forever (historical replay). The calculator itself is tested in
tests/test_native_fee_estimator.py.
"""

from __future__ import annotations

import dataclasses

import pytest

from phantasma_py import (
    CarbonReader,
    CarbonWriter,
    GasConfig,
    GasConfigDataResult,
    GasConfigResult,
    RPCError,
    SerializationError,
)


def live_v1_config() -> GasConfig:
    """The mainnet v1 values (feeMultiplier 10000, transfer 10 units, byte fee 250000, escrow 2)."""
    return GasConfig(
        version=0,
        max_name_length=32,
        max_token_symbol_length=10,
        fee_shift=0,
        max_structure_size=65536,
        fee_multiplier=10_000,
        gas_token_id=2,
        data_token_id=1,
        minimum_gas_offer=10,
        data_escrow_per_row=2,
        gas_fee_transfer=10,
        gas_fee_query=2,
        gas_fee_create_token_base=10_000_000_000,
        gas_fee_create_token_symbol=10_000_000_000,
        gas_fee_create_token_series=2_500_000_000,
        gas_fee_per_byte=250_000,
        gas_fee_register_name=10_000_000_000_000,
        gas_burn_ratio_mul=1,
    )


def v2_config() -> GasConfig:
    """The spec activation-package values for the v2 tail."""
    config = live_v1_config()
    config.version = 1
    config.data_escrow_per_row = 200_000
    config.minimum_gas_bill = 10_000_000
    config.policy_fee_create_token_base = 100_000_000_000_000
    config.policy_fee_create_token_symbol = 100_000_000_000_000
    config.policy_fee_create_token_series = 25_000_000_000_000
    config.policy_fee_register_name = 100_000_000_000_000_000
    config.legacy_data_escrow_per_row = 2
    return config


def serialize(config: GasConfig) -> bytes:
    writer = CarbonWriter()
    config.write_carbon(writer)
    return writer.bytes()


class TestGasConfigWireFormat:
    def test_v0_keeps_legacy_113_byte_layout(self) -> None:
        # Any growth of the version-0 image would corrupt every historical block image.
        assert len(serialize(live_v1_config())) == 113

    def test_v2_appends_66_byte_tail_after_unchanged_head(self) -> None:
        v2_bytes = serialize(v2_config())
        assert len(v2_bytes) == 179

        v0_twin = v2_config()
        v0_twin.version = 0  # same head values, version-0 layout
        v0_bytes = serialize(v0_twin)
        assert len(v0_bytes) == 113

        assert v2_bytes[0] == 1
        assert v0_bytes[0] == 0
        # The tail is a pure wire extension: the head encoding must be untouched.
        assert v2_bytes[1:113] == v0_bytes[1:113]

    def test_v2_roundtrip_preserves_all_fields(self) -> None:
        original = v2_config()
        decoded = GasConfig.read_carbon(CarbonReader(serialize(original)))

        assert dataclasses.asdict(decoded) == dataclasses.asdict(original)
        assert decoded.has_gas_model_v2

    def test_v0_read_zeroes_v2_fields(self) -> None:
        # Consumers must never see stale tail values on a v1 chain.
        decoded = GasConfig.read_carbon(CarbonReader(serialize(live_v1_config())))

        assert not decoded.has_gas_model_v2
        assert decoded.minimum_gas_bill == 0
        assert decoded.policy_fee_create_token_base == 0
        assert decoded.legacy_data_escrow_per_row == 0

    def test_truncated_v2_image_fails_to_parse(self) -> None:
        # Never silently produce a config with zeroed v2 prices (free product actions).
        truncated = serialize(v2_config())[:113]
        with pytest.raises(SerializationError):
            GasConfig.read_carbon(CarbonReader(truncated))


class TestGasConfigResultDecoding:
    def v2_result(self) -> GasConfigResult:
        return GasConfigResult(
            gas_model_version=2,
            block_rate_target=2000,
            expiry_window=90_000,
            units_per_block_data_byte=25,
            gas_config=GasConfigDataResult(
                version=1,
                max_name_length=32,
                max_token_symbol_length=10,
                fee_shift=0,
                max_structure_size=65536,
                fee_multiplier="10000",
                gas_token_id="2",
                data_token_id="1",
                minimum_gas_offer="10",
                data_escrow_per_row="200000",
                gas_fee_transfer="10",
                gas_fee_query="2",
                gas_fee_create_token_base="10000000000",
                gas_fee_create_token_symbol="10000000000",
                gas_fee_create_token_series="2500000000",
                gas_fee_per_byte="250000",
                gas_fee_register_name="10000000000000",
                gas_burn_ratio_mul="1",
                gas_burn_ratio_shift=0,
                minimum_gas_bill="10000000",
                gas_producer_ratio_mul="0",
                gas_producer_ratio_shift=0,
                gas_dapp_ratio_mul="0",
                gas_dapp_ratio_shift=0,
                policy_fee_create_token_base="100000000000000",
                policy_fee_create_token_symbol="100000000000000",
                policy_fee_create_token_series="25000000000000",
                # > 2^53: must survive exactly because it rides a string.
                policy_fee_register_name="100000000000000000",
                legacy_data_escrow_per_row="2",
            ),
        )

    def test_v2_response_maps_to_gas_config(self) -> None:
        config = self.v2_result().to_gas_config()

        assert config.has_gas_model_v2
        assert config.data_escrow_per_row == 200_000
        assert config.minimum_gas_bill == 10_000_000
        assert config.policy_fee_register_name == 100_000_000_000_000_000
        assert config.legacy_data_escrow_per_row == 2

    def test_v1_response_zeroes_absent_v2_fields(self) -> None:
        result = self.v2_result()
        assert result.gas_config is not None
        result.gas_config.version = 0

        config = result.to_gas_config()

        assert not config.has_gas_model_v2
        # v2 strings are still present in this synthetic fixture, but a version-0 config must
        # ignore them: v1 semantics never read the tail.
        assert config.minimum_gas_bill == 0
        assert config.policy_fee_register_name == 0

    def test_v2_response_with_missing_tail_field_raises(self) -> None:
        # Estimating fees from silently zeroed v2 prices would produce rejected transactions.
        result = self.v2_result()
        assert result.gas_config is not None
        result.gas_config.minimum_gas_bill = None

        with pytest.raises(RPCError):
            result.to_gas_config()

    def test_missing_gas_config_section_raises(self) -> None:
        with pytest.raises(RPCError):
            GasConfigResult(gas_model_version=1).to_gas_config()
