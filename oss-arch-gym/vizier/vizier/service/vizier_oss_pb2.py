# -*- coding: utf-8 -*-
# Generated by the protocol buffer compiler.  DO NOT EDIT!
# source: vizier_oss.proto
"""Generated protocol buffer code."""
from google.protobuf import descriptor as _descriptor
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import message as _message
from google.protobuf import reflection as _reflection
from google.protobuf import symbol_database as _symbol_database
# @@protoc_insertion_point(imports)

_sym_db = _symbol_database.Default()


from google.protobuf import timestamp_pb2 as google_dot_protobuf_dot_timestamp__pb2


DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(b'\n\x10vizier_oss.proto\x12\x06vizier\x1a\x1fgoogle/protobuf/timestamp.proto\"\xac\x02\n\x16\x45\x61rlyStoppingOperation\x12\x0c\n\x04name\x18\x01 \x01(\t\x12\x35\n\x06status\x18\x02 \x01(\x0e\x32%.vizier.EarlyStoppingOperation.Status\x12\x13\n\x0bshould_stop\x18\x03 \x01(\x08\x12\x17\n\x0f\x66\x61ilure_message\x18\x04 \x01(\t\x12\x31\n\rcreation_time\x18\x05 \x01(\x0b\x32\x1a.google.protobuf.Timestamp\x12\x33\n\x0f\x63ompletion_time\x18\x06 \x01(\x0b\x32\x1a.google.protobuf.Timestamp\"7\n\x06Status\x12\x0b\n\x07UNKNOWN\x10\x00\x12\n\n\x06\x41\x43TIVE\x10\x01\x12\x08\n\x04\x44ONE\x10\x02\x12\n\n\x06\x46\x41ILED\x10\x03\x62\x06proto3')



_EARLYSTOPPINGOPERATION = DESCRIPTOR.message_types_by_name['EarlyStoppingOperation']
_EARLYSTOPPINGOPERATION_STATUS = _EARLYSTOPPINGOPERATION.enum_types_by_name['Status']
EarlyStoppingOperation = _reflection.GeneratedProtocolMessageType('EarlyStoppingOperation', (_message.Message,), {
  'DESCRIPTOR' : _EARLYSTOPPINGOPERATION,
  '__module__' : 'vizier_oss_pb2'
  # @@protoc_insertion_point(class_scope:vizier.EarlyStoppingOperation)
  })
_sym_db.RegisterMessage(EarlyStoppingOperation)

if _descriptor._USE_C_DESCRIPTORS == False:

  DESCRIPTOR._options = None
  _EARLYSTOPPINGOPERATION._serialized_start=62
  _EARLYSTOPPINGOPERATION._serialized_end=362
  _EARLYSTOPPINGOPERATION_STATUS._serialized_start=307
  _EARLYSTOPPINGOPERATION_STATUS._serialized_end=362
# @@protoc_insertion_point(module_scope)
