"""Wire-level differential tests for the schema-compiled ROS 2 decoder."""

import array
from io import BytesIO
import math
import os
from pathlib import Path

from mcap.records import Schema
from mcap_ros2._cdr import CdrWriter, EncapsulationKind
from mcap_ros2._dynamic import _for_each_msgdef, _write_complex_type
from mcap_ros2.decoder import DecoderFactory
import numpy as np
import pytest

from tianji_provider.decoder import CompiledDecoderFactory


SCHEMA_TEXT = """std_msgs/Header header
geometry_msgs/Wrench wrench
float64[] positions
float32[5] feedback
uint8[<=4] bounded
string[] names
geometry_msgs/Vector3[2] fixed_vectors
geometry_msgs/Vector3[] vectors
std_msgs/Empty empty
bool active
int8 i8
uint8 u8
int16 i16
uint16 u16
int32 i32
uint32 u32
int64 i64
uint64 u64
float32 value
string label
===
MSG: std_msgs/Header
builtin_interfaces/Time stamp
string frame_id
===
MSG: builtin_interfaces/Time
int32 sec
uint32 nanosec
===
MSG: geometry_msgs/Wrench
geometry_msgs/Vector3 force
geometry_msgs/Vector3 torque
===
MSG: geometry_msgs/Vector3
float64 x
float64 y
float64 z
===
MSG: std_msgs/Empty
"""


def schema(text=SCHEMA_TEXT, name="test_msgs/msg/Signals", sid=1):
    return Schema(id=sid, name=name, encoding="ros2msg", data=text.encode())


def payload(definition, values, endian):
    definitions = {}

    def collect(name, short, message):
        definitions[name] = definitions[short] = message

    _for_each_msgdef(definition.name, definition.data.decode(), collect)
    root = definitions[definition.name]
    output = BytesIO()
    writer = CdrWriter(output, endian)
    _write_complex_type(root.msg_name, root.fields, definitions, values, writer)
    return output.getvalue()


def values():
    return dict(
        header=dict(stamp=dict(sec=1800000000, nanosec=987654321), frame_id="flange_L"),
        wrench=dict(force=dict(x=1.25, y=-2.5, z=3.75), torque=dict(x=.1, y=.2, z=-.3)),
        positions=[.01, .02, .03, .04, .05, .06, .07],
        feedback=[1.25, 2.5, 3.75, -4.5, 5.5], bounded=[1, 2, 3],
        names=["Joint1_L", "Joint2_R", "名字"],
        fixed_vectors=[dict(x=1, y=2, z=3), dict(x=-1, y=-2, z=-3)],
        vectors=[dict(x=4, y=5, z=6)], empty={}, active=True,
        i8=-127, u8=255, i16=-32767, u16=65535, i32=-2147483647,
        u32=4294967295, i64=-9223372036854775807, u64=18446744073709551615,
        value=.123456, label="Tianji 标定",
    )


def assert_same(actual, expected):
    """Compare every field, including data that Tianji does not currently use."""
    if hasattr(expected, "__slots__"):
        assert actual.__slots__ == expected.__slots__
        assert actual._type == expected._type
        assert actual._full_text == expected._full_text
        for name in expected.__slots__:
            assert_same(getattr(actual, name), getattr(expected, name))
    elif isinstance(expected, (list, tuple, array.array)):
        assert type(actual) is type(expected)
        assert len(actual) == len(expected)
        for a, e in zip(actual, expected):
            assert_same(a, e)
    elif isinstance(expected, float) and math.isnan(expected):
        assert math.isnan(actual)
    else:
        assert type(actual) is type(expected)
        assert actual == expected


@pytest.mark.parametrize("endian", [EncapsulationKind.CDR_LE, EncapsulationKind.CDR_BE])
@pytest.mark.parametrize("empty_sequences", [False, True])
def test_all_nested_fields_arrays_and_endianness_match_stock(endian, empty_sequences):
    definition, message = schema(), values()
    if empty_sequences:
        for name in ("positions", "bounded", "names", "vectors"):
            message[name] = []
    raw = payload(definition, message, endian)
    old = DecoderFactory().decoder_for("cdr", definition)(raw)
    new = CompiledDecoderFactory().decoder_for("cdr", definition)(raw)
    assert_same(new, old)
    assert repr(new) == repr(old)


@pytest.mark.parametrize("number", [float("nan"), float("inf"), -float("inf"), -0.0])
def test_nonfinite_values_are_preserved_for_existing_numeric_validation(number):
    definition = schema("float64 data", "std_msgs/msg/Float64")
    raw = payload(definition, dict(data=number), EncapsulationKind.CDR_LE)
    old = DecoderFactory().decoder_for("cdr", definition)(raw)
    new = CompiledDecoderFactory().decoder_for("cdr", definition)(raw)
    assert_same(new, old)


def test_factory_reuses_compiled_nested_types_without_sharing_message_values():
    definition = schema()
    raw = payload(definition, values(), EncapsulationKind.CDR_LE)
    factory = CompiledDecoderFactory()
    decode = factory.decoder_for("cdr", definition)
    assert decode is factory.decoder_for("cdr", definition)
    a, b = decode(raw), decode(raw)
    assert type(a) is type(b)
    assert type(a.header.stamp) is type(b.header.stamp)
    assert type(a.wrench.force) is type(b.wrench.torque)
    assert a is not b and a.header is not b.header
    a.wrench.force.x = 99
    a.positions.append(99)
    assert b.wrench.force.x == 1.25 and len(b.positions) == 7


def test_same_schema_name_different_layouts_and_file_ids_do_not_share_plans():
    first = schema("float32 value", sid=1)
    second = schema("string value", sid=2)
    factory = CompiledDecoderFactory()
    for definition, value in ((first, .25), (second, "different layout")):
        raw = payload(definition, dict(value=value), EncapsulationKind.CDR_LE)
        assert factory.decoder_for("cdr", definition)(raw).value == value
    # Schema IDs are reused in other physical files, which get a new factory.
    second.id = 1
    raw = payload(second, dict(value="second file"), EncapsulationKind.CDR_LE)
    assert CompiledDecoderFactory().decoder_for("cdr", second)(raw).value == "second file"


@pytest.mark.parametrize("encoding,definition", [
    ("json", schema()), ("cdr", None),
    ("cdr", Schema(id=1, name="x", encoding="ros2idl", data=b"")),
])
def test_unsupported_encodings_return_no_decoder_like_stock(encoding, definition):
    assert DecoderFactory().decoder_for(encoding, definition) is None
    assert CompiledDecoderFactory().decoder_for(encoding, definition) is None


@pytest.mark.parametrize("length", [0, 1, 3, 4, 7, 11, 16, 36, 64, 100, 150, 200])
def test_truncated_payloads_still_fail(length):
    definition = schema()
    raw = payload(definition, values(), EncapsulationKind.CDR_LE)[:length]
    with pytest.raises(Exception) as original:
        DecoderFactory().decoder_for("cdr", definition)(raw)
    with pytest.raises(type(original.value)):
        CompiledDecoderFactory().decoder_for("cdr", definition)(raw)


def test_missing_nested_definition_still_fails():
    definition = schema("unknown_msgs/Missing value")
    with pytest.raises(ValueError, match="Message definition not found"):
        DecoderFactory().decoder_for("cdr", definition)(b"\x00\x01\x00\x00")
    with pytest.raises(ValueError, match="Message definition not found"):
        CompiledDecoderFactory().decoder_for("cdr", definition)


def test_recursive_message_arrays_and_builtin_time_defaults_match_stock():
    # Auxiliary topics are not restricted to the known Tianji layouts.
    definition = schema("builtin_interfaces/Time stamp\ntest_msgs/Signals[] children")
    raw = b"\x00\x01\x00\x00" + bytes(12)
    old = DecoderFactory().decoder_for("cdr", definition)(raw)
    new = CompiledDecoderFactory().decoder_for("cdr", definition)(raw)
    assert_same(new, old)


RECORDINGS = [Path(p) for p in os.environ.get("TIANJI_TEST_RECORDINGS", "").split(os.pathsep) if p]


@pytest.mark.parametrize("bag", RECORDINGS or [None], ids=lambda p: p.name if p else "set-TIANJI_TEST_RECORDINGS")
@pytest.mark.skipif(not RECORDINGS, reason="Real MCAP paths not provided")
def test_complete_real_numeric_result_matches_stock_factory(bag, monkeypatch):
    from tianji_provider import reader

    actual = reader.read_numeric(bag)
    monkeypatch.setattr(reader, "CompiledDecoderFactory", DecoderFactory)
    expected = reader.read_numeric(bag)
    assert actual[1:] == expected[1:]  # topic ranges, clocks and calibration
    assert actual[0].keys() == expected[0].keys()
    for topic in expected[0]:
        np.testing.assert_array_equal(actual[0][topic].times, expected[0][topic].times)
        np.testing.assert_array_equal(actual[0][topic].values, expected[0][topic].values)
