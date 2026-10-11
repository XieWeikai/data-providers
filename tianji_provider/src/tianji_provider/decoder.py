"""Compile embedded ROS schemas once instead of rebuilding a class per value.

The wire format is still read by mcap-ros2-support's CdrReader. This adapter
only moves schema interpretation, type construction and field dispatch out of
the message loop; it does not project fields or replace recorded schemas with
hard-coded Tianji layouts. Unknown auxiliary topics therefore keep their full
header/clock and decoding checks in reader.read_numeric.

The schema/parser helpers are private mcap-ros2-support APIs. Keep differential
tests against its public DecoderFactory when upgrading that dependency.
"""

from types import SimpleNamespace

from mcap.decoder import DecoderFactory as McapDecoderFactory
from mcap.well_known import MessageEncoding, SchemaEncoding
from mcap_ros2._cdr import CdrReader
from mcap_ros2._dynamic import (
    ARRAY_PARSERS,
    FIELD_PARSERS,
    TimeDefinition,
    __eq__ as message_eq,
    __ne__ as message_ne,
    __repr__ as message_repr,
    _for_each_msgdef,
)
from mcap_ros2.decoder import McapROS2DecodeError


def _array_reader(parser, size, *, primitive):
    """Bind array semantics once, including the upstream primitive length guard."""
    if primitive:
        if size is not None:
            return lambda reader: parser(reader, size)
        return lambda reader: parser(reader, reader.sequence_length())
    if size is not None:
        return lambda reader: [parser(reader) for _ in range(size)]
    return lambda reader: [parser(reader) for _ in range(reader.uint32())]


def compile_schema(schema_name, schema_text):
    """Build a reader from the same parsed definitions as the upstream decoder."""
    definitions = {
        "builtin_interfaces/Time": TimeDefinition,
        "builtin_interfaces/Duration": TimeDefinition,
    }

    def collect(name, short_name, definition):
        definitions[name] = definitions[short_name] = definition

    _for_each_msgdef(schema_name, schema_text, collect)
    if schema_name not in definitions:
        raise McapROS2DecodeError(f'schema parsing failed for "{schema_name}"')
    compiled = {}

    def compile_message(definition):
        # Aliases can point to one definition; recursive arrays can refer back
        # to a decoder that is already being compiled.
        key = id(definition)
        if key in compiled:
            return compiled[key]
        fields = []
        message_type = type(definition.msg_name, (SimpleNamespace,), {
            "__name__": definition.msg_name,
            "__slots__": [field.name for field in definition.fields],
            "__repr__": message_repr,
            "__str__": message_repr,
            "__eq__": message_eq,
            "__ne__": message_ne,
            "_type": str(definition.base_type),
            "_full_text": str(definition),
        })

        def read(reader):
            message = message_type()
            if not fields:
                # ROS 2 serializes a dummy byte for an empty message.
                reader.uint8()
            for name, parser in fields:
                setattr(message, name, parser(reader))
            return message

        compiled[key] = read
        for field in definition.fields:
            ftype = field.type
            primitive = ftype.is_primitive_type()
            if primitive:
                parsers = ARRAY_PARSERS if ftype.is_array else FIELD_PARSERS
                parser = parsers.get(ftype.type)
                if parser is None:
                    suffix = "[]" if ftype.is_array else ""
                    raise NotImplementedError(f"Parsing for type {ftype.type}{suffix} is not implemented")
            else:
                nested = definitions.get(f"{ftype.pkg_name}/{ftype.type}")
                if nested is None:
                    raise ValueError(f'Message definition not found for field "{field.name}" with type "{ftype.type}"')
                parser = compile_message(nested)
            if ftype.is_array:
                size = ftype.array_size if ftype.is_fixed_size_array() else None
                parser = _array_reader(parser, size, primitive=primitive)
            fields.append((field.name, parser))
        return read

    read = compile_message(definitions[schema_name])
    return lambda data: read(CdrReader(data))


class CompiledDecoderFactory(McapDecoderFactory):
    """A per-file factory, like the stock ROS 2 factory, with compiled schemas."""

    def __init__(self):
        self._decoders = {}

    def decoder_for(self, message_encoding, schema):
        if message_encoding != MessageEncoding.CDR or schema is None or schema.encoding != SchemaEncoding.ROS2:
            return None
        # IDs are local to the MCAP file; reader.messages creates one factory
        # per file. Never share an ID-only cache across different recordings.
        decoder = self._decoders.get(schema.id)
        if decoder is None:
            decoder = compile_schema(schema.name, schema.data.decode())
            self._decoders[schema.id] = decoder
        return decoder
