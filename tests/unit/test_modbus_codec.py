from __future__ import annotations

import pytest

from xedge.drivers.modbus import codec


def test_mbap_roundtrip() -> None:
    pdu = b"\x03\x02\x00\x2a"
    frame = codec.encode_mbap(transaction_id=42, unit_id=1, pdu=pdu)
    transaction_id, unit_id, decoded_pdu = codec.decode_mbap(frame)
    assert transaction_id == 42
    assert unit_id == 1
    assert decoded_pdu == pdu


def test_frame_remainder_length_matches_decode() -> None:
    pdu = b"\x03\x04\x00\x01\x00\x02"
    frame = codec.encode_mbap(transaction_id=1, unit_id=5, pdu=pdu)
    header, rest = frame[: codec.MBAP_HEADER_LENGTH], frame[codec.MBAP_HEADER_LENGTH :]
    assert codec.frame_remainder_length(header) == len(rest)


def test_decode_mbap_rejects_wrong_protocol_id() -> None:
    bad_frame = b"\x00\x01\x00\x01\x00\x02\x01\x03"
    with pytest.raises(codec.ModbusFramingError):
        codec.decode_mbap(bad_frame)


def test_decode_mbap_rejects_truncated_frame() -> None:
    with pytest.raises(codec.ModbusFramingError):
        codec.decode_mbap(b"\x00\x01\x00\x00\x00\x05\x01")


def test_encode_read_request_holding_registers() -> None:
    pdu = codec.encode_read_request(
        codec.FunctionCode.READ_HOLDING_REGISTERS, address=40001, quantity=2
    )
    assert pdu == b"\x03\x9c\x41\x00\x02"


@pytest.mark.parametrize("address", [-1, 0x10000])
def test_encode_read_request_rejects_bad_address(address: int) -> None:
    with pytest.raises(ValueError, match="address"):
        codec.encode_read_request(
            codec.FunctionCode.READ_HOLDING_REGISTERS, address=address, quantity=1
        )


@pytest.mark.parametrize("quantity", [0, 2001])
def test_encode_read_request_rejects_bad_quantity(quantity: int) -> None:
    with pytest.raises(ValueError, match="quantity"):
        codec.encode_read_request(
            codec.FunctionCode.READ_HOLDING_REGISTERS, address=0, quantity=quantity
        )


def test_decode_registers_response() -> None:
    # FC03, byte count 4, two registers: 0x0064 (100) and 0x00C8 (200)
    pdu = b"\x03\x04\x00\x64\x00\xc8"
    assert codec.decode_registers_response(pdu) == [100, 200]


def test_decode_bits_response_single_byte() -> None:
    # FC01, byte count 1, bits (LSB first): 1,0,1,1 -> value 0x0D, request quantity=4
    pdu = b"\x01\x01\x0d"
    assert codec.decode_bits_response(pdu, quantity=4) == [True, False, True, True]


def test_decode_bits_response_spans_multiple_bytes() -> None:
    # 10 bits requested -> 2 data bytes. First byte all 1s, second byte only bit0 set.
    pdu = b"\x01\x02\xff\x01"
    bits = codec.decode_bits_response(pdu, quantity=10)
    assert bits == [True] * 9 + [False]


def test_decode_registers_response_raises_modbus_exception() -> None:
    # FC03 with exception flag set (0x83), exception code 2 (illegal data address)
    pdu = b"\x83\x02"
    with pytest.raises(codec.ModbusException) as exc_info:
        codec.decode_registers_response(pdu)
    assert exc_info.value.function_code == codec.FunctionCode.READ_HOLDING_REGISTERS
    assert exc_info.value.exception_code == codec.ExceptionCode.ILLEGAL_DATA_ADDRESS


def test_decode_bits_response_raises_modbus_exception() -> None:
    pdu = b"\x81\x01"  # FC01 exception, illegal function
    with pytest.raises(codec.ModbusException) as exc_info:
        codec.decode_bits_response(pdu, quantity=1)
    assert exc_info.value.exception_code == codec.ExceptionCode.ILLEGAL_FUNCTION


def test_is_bit_and_register_function_classification() -> None:
    assert codec.is_bit_function(codec.FunctionCode.READ_COILS)
    assert codec.is_bit_function(codec.FunctionCode.READ_DISCRETE_INPUTS)
    assert not codec.is_bit_function(codec.FunctionCode.READ_HOLDING_REGISTERS)
    assert codec.is_register_function(codec.FunctionCode.READ_HOLDING_REGISTERS)
    assert codec.is_register_function(codec.FunctionCode.READ_INPUT_REGISTERS)
    assert not codec.is_register_function(codec.FunctionCode.READ_COILS)


def test_encode_write_single_coil_matches_pymodbus_oracle() -> None:
    from pymodbus.pdu.bit_message import WriteSingleCoilRequest

    pdu = codec.encode_write_single_coil(address=10, value=True)
    assert pdu[0] == codec.FunctionCode.WRITE_SINGLE_COIL
    assert pdu[1:] == WriteSingleCoilRequest(address=10, bits=[True]).encode()


def test_encode_write_single_coil_off_uses_0x0000() -> None:
    pdu = codec.encode_write_single_coil(address=10, value=False)
    assert pdu == b"\x05\x00\x0a\x00\x00"


def test_encode_write_single_register_matches_pymodbus_oracle() -> None:
    from pymodbus.pdu.register_message import WriteSingleRegisterRequest

    pdu = codec.encode_write_single_register(address=20, value=1234)
    assert pdu[0] == codec.FunctionCode.WRITE_SINGLE_REGISTER
    assert pdu[1:] == WriteSingleRegisterRequest(address=20, registers=[1234]).encode()


def test_encode_write_multiple_registers_matches_pymodbus_oracle() -> None:
    from pymodbus.pdu.register_message import WriteMultipleRegistersRequest

    pdu = codec.encode_write_multiple_registers(address=5, values=[1, 2, 3])
    assert pdu[0] == codec.FunctionCode.WRITE_MULTIPLE_REGISTERS
    assert pdu[1:] == WriteMultipleRegistersRequest(address=5, registers=[1, 2, 3]).encode()


def test_decode_write_single_response_returns_echoed_address_and_value() -> None:
    pdu = codec.encode_write_single_register(address=20, value=1234)
    address, value = codec.decode_write_single_response(pdu)
    assert (address, value) == (20, 1234)


def test_decode_write_single_response_raises_modbus_exception() -> None:
    pdu = b"\x86\x03"  # FC06 exception, illegal data value
    with pytest.raises(codec.ModbusException) as exc_info:
        codec.decode_write_single_response(pdu)
    assert exc_info.value.exception_code == codec.ExceptionCode.ILLEGAL_DATA_VALUE


def test_decode_write_multiple_response_returns_address_and_quantity() -> None:
    pdu = b"\x10\x00\x05\x00\x03"
    address, quantity = codec.decode_write_multiple_response(pdu)
    assert (address, quantity) == (5, 3)


def test_encode_write_multiple_coils_matches_pymodbus_oracle() -> None:
    from pymodbus.pdu.bit_message import WriteMultipleCoilsRequest

    pdu = codec.encode_write_multiple_coils(address=5, values=[True, False, True])
    assert pdu[0] == codec.FunctionCode.WRITE_MULTIPLE_COILS
    assert pdu[1:] == WriteMultipleCoilsRequest(address=5, bits=[True, False, True]).encode()


def test_encode_write_multiple_coils_packs_lsb_first() -> None:
    # bit 0 -> byte 0's LSB, matching decode_bits_response's own bit order.
    pdu = codec.encode_write_multiple_coils(address=0, values=[True, False, False, True])
    assert pdu == b"\x0f\x00\x00\x00\x04\x01\x09"


def test_encode_write_multiple_coils_spans_multiple_bytes() -> None:
    values = [True] * 9  # 9 coils needs 2 bytes (ceil(9/8))
    pdu = codec.encode_write_multiple_coils(address=0, values=values)
    byte_count = pdu[5]
    assert byte_count == 2
    assert len(pdu) == 6 + byte_count


def test_encode_write_multiple_coils_rejects_empty() -> None:
    with pytest.raises(ValueError, match="quantity out of range"):
        codec.encode_write_multiple_coils(address=0, values=[])


def test_encode_write_multiple_coils_rejects_over_max() -> None:
    with pytest.raises(ValueError, match="quantity out of range"):
        codec.encode_write_multiple_coils(address=0, values=[True] * (codec.MAX_WRITE_COILS + 1))


def test_encode_write_multiple_coils_accepts_the_max() -> None:
    pdu = codec.encode_write_multiple_coils(address=0, values=[True] * codec.MAX_WRITE_COILS)
    assert pdu[3:5] == codec.MAX_WRITE_COILS.to_bytes(2, "big")


def test_encode_write_multiple_coils_rejects_bad_address() -> None:
    with pytest.raises(ValueError, match="address out of range"):
        codec.encode_write_multiple_coils(address=-1, values=[True])


def test_decode_write_multiple_response_serves_fc15_and_fc16_identically() -> None:
    """Both function codes share the same (address, quantity) response
    shape per the spec, so one decoder is correct for both. A real device's
    response echoes address+quantity with FC15's code, not the request
    PDU itself — simulate that shape directly."""
    response = b"\x0f\x00\x05\x00\x03"
    assert codec.decode_write_multiple_response(response) == (5, 3)
