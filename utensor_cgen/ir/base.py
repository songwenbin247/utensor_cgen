# -*- coding: utf8 -*-
import re
from collections import defaultdict
from copy import deepcopy
import six

import attr
import numpy as np
import tensorflow as tf
from attr.validators import instance_of
from tensorflow.core.framework.attr_value_pb2 import AttrValue as _AttrValue
from tensorflow.core.framework.attr_value_pb2 import NameAttrList as _NameAttrList
from tensorflow.core.framework.tensor_pb2 import TensorProto as _TensorProto
from tensorflow.core.framework.tensor_shape_pb2 import TensorShapeProto as _TensorShapeProto
from tensorflow.core.framework.types_pb2 import DataType as _DataType

from .converter import AttrValueConverter, ConverterFactory
from utensor_cgen.utils import topologic_order_graph

__all__ = ['TensorInfo', 'OperationInfo', 'uTensorGraph']


class _NoShallowCopyMixin(object):

  def __copy__(self):
    raise RuntimeError('shallow copy is not allowed for type %s' % type(self))


class IRBase(object):

  @property
  def all_supported_backends(self):
    return ['tensorflow']


@attr.s
class TensorInfo(IRBase, _NoShallowCopyMixin):
  """
  name : str
  dtype : numpy.dtype
  shape : list
  """
  name = attr.ib(validator=instance_of(six.text_type))
  op_name = attr.ib(validator=instance_of(six.text_type))
  dtype = attr.ib(validator=instance_of(np.dtype))
  shape = attr.ib(validator=instance_of((list, type(None))))
  @shape.validator
  def check(self, attrib, shape_values):
    if shape_values is not None:
      for v in shape_values:
        assert isinstance(v, (int, type(None))), \
          "shape should be a list of integers"
  ugraph = attr.ib(repr=False)
  @ugraph.validator
  def check(self, attrib, value):
    if not isinstance(value, uTensorGraph):
      raise ValueError('Expecting a uTensorGraph, get {}'.format(type(value)))

  @property
  def op(self):
    return self.ugraph.ops_info.get(self.op_name, None)

  @property
  def backend(self):
    return self.ugraph.backend

  @property
  def is_dangling(self):
    op = self.op
    if not op:
      return True
    return op.is_dangling
  
  @property
  def n_th_output(self):
    if self.is_dangling:
      raise ValueError(
        "dangling tensor: {}".format(self.name)
      )
    op = self.op
    out_tnames = [t_info.name for t_info in op.output_tensors]
    return out_tnames.index(self.name)

  def semantic_signature(self):
    return "<{n_th}>{op_sig}".format(
      n_th=self.n_th_output,
      op_sig=self.op.semantic_signature
    )

  def __deepcopy__(self, memo):
    new_tensor = TensorInfo(name=self.name,
                            ugraph=memo['ugraph'],
                            op_name=self.op_name,
                            dtype=self.dtype,
                            shape=deepcopy(self.shape, memo))
    return new_tensor


@attr.s
class OperationInfo(IRBase, _NoShallowCopyMixin):
  """
  name : str
  input_tensors : List[TensorInfo]
  output_tensors : List[TensorInfo]
  input_nodes : Set[OperationInfo]
  output_nodes : Set[OperationInfo]
  op_type : str
  backend : str {"tensorflow", 'pytorch'(future work)}
  op_attr : dict

  Note
  ====
  - `op_attr` will be a dictionary with key as str and value as generic
    types defined in `converter.ConverterFactor.all_generic_types`. The
    only exception is the key which match regex pattern r'_[^_]*'. The 
    values of such keys will be saved as-is without any type conversion.
  """
  name = attr.ib(type=str)
  _backend = attr.ib(type=str)
  ugraph = attr.ib(repr=False)
  @ugraph.validator
  def check(self, attrib, value):
    if not isinstance(value, uTensorGraph):
      raise ValueError(('Expecting a uTensorGraph, '
                        'get {}'.format(type(value))))

  input_tensors = attr.ib(validator=instance_of(list))
  @input_tensors.validator
  def check(self, attribute, value):
    if not all([isinstance(v, TensorInfo) for v in value]):
      raise ValueError('Expecting a list of TensorInfo for input_tensors')

  output_tensors = attr.ib(validator=instance_of(list))
  @output_tensors.validator
  def check(self, attribute, value):
    if not all([isinstance(v, TensorInfo) for v in value]):
      raise ValueError('Expecting a list of TensorInfo for output_tensors')

  op_type = attr.ib(type=str)
  op_attr = attr.ib(factory=dict, converter=dict)

  @property
  def backend(self):
    return self._backend

  @property
  def input_nodes(self):
    in_ops = []
    for tensor in self.input_tensors:
      if tensor.op_name not in in_ops:
        in_ops.append(tensor.op_name)
    return [self.ugraph.ops_info.get(name, None) for name in in_ops]
  
  @property
  def output_nodes(self):
    out_ops = []
    for op in self.ugraph.ops:
      for in_tensor in op.input_tensors:
        if in_tensor.op_name == self.name and op.name not in out_ops:
          out_ops.append(op.name)
          break
    return [self.ugraph.ops_info[name] for name in out_ops]
  
  @property
  def is_dangling(self):
    """
    True: the op is dangling in the graph
    False: otherwise
    """
    return None in self.input_nodes

  @property
  def n_inputs(self):
    return len(self.input_tensors)

  @property
  def n_outputs(self):
    return len(self.output_tensors)

  def semantic_signature(self):
    queue = [(self, self.input_nodes)]
    sig = "{}<:".format(self.op_type)
    while queue:
      current_node, input_nodes = queue.pop(0)
      if any([node.is_dangling for node in input_nodes]):
        raise ValueError(
          "Dangling node detected: {}".format(current_node.name)
        )
      queue.extend([
        (node, node.input_nodes) for node in input_nodes
      ])
      sig += "{}<:".format(tuple(node.op_type for node in input_nodes))
    return sig

  def __attrs_post_init__(self):
    skip_pattern = re.compile(r'_utensor_[^_]*')
    if self.op_attr:
      op_attr = {}
      for k, v in self.op_attr.items():
        match = skip_pattern.match(k)
        if match:
          op_attr[k] = v
        else:
          op_attr[k] = ConverterFactory.get_generic_value(v)
      self.op_attr = op_attr
    self.ugraph.ops_info[self.name] = self

  def __deepcopy__(self, memo):
    op_info = OperationInfo(name=self.name,
                            input_tensors=deepcopy(self.input_tensors, memo),
                            output_tensors=deepcopy(self.output_tensors, memo),
                            op_type=self.op_type,
                            backend=self.backend,
                            op_attr=deepcopy(self.op_attr, memo),
                            ugraph=memo['ugraph'])
    return op_info

  def copy_into_graph(self, ugraph):
    return deepcopy(self, {'ugraph': ugraph})


@attr.s
class uTensorGraph(IRBase, _NoShallowCopyMixin):
  """
  Attributes
  ==========
  ops_info : dict
  topo_order : list
  output_nodes : list
  backend : str {"tensorflow", 'pytorch'(future work)}
  """
  KWPARSER_PATTERN = re.compile(r'^([^\d\W][\w\d_]*)__([^\d\W][\w\d_]*)')

  output_nodes = attr.ib(type=list)
  _backend = attr.ib(default='', type=str)
  ops_info = attr.ib(factory=dict)
  # non-init
  topo_order = attr.ib(factory=list, init=False)
  _type_to_op_map = attr.ib(factory=dict, init=False, repr=False)

  def __attrs_post_init__(self):
    if not self.output_nodes:
      raise ValueError('No output_nodes given')
  
  def get_ops_by_type(self, op_type):
    if not self._type_to_op_map:
      for op_info in self.ops_info.values():
        op_type = op_info.op_type
        ops = self._type_to_op_map.get(
          op_type,
          []
        ) + [op_info]
        self._type_to_op_map.update(
          [(op_type, ops),]
        )
    return self._type_to_op_map.get(op_type, [])
  
  @property
  def output_ops(self):
    return [self.ops_info[name] for name in self.output_nodes]
  
  @property
  def backend(self):
    return self._backend

  @property
  def graph_def(self):
    assert self._backend == 'tensorflow', \
      'Convert a uTensorGraph to tf.GraphDef from a non-tf backend'
    graph_def = tf.GraphDef()
    for node_name in self.topo_order:
      op_info = self.ops_info[node_name]
      attr = {}
      for key, obj in op_info.op_attr.items():
        if self.KWPARSER_PATTERN.match(key):
          continue
        value_name = obj.value_name
        tf_value = ConverterFactory.get_tf_value(obj.value)
        attr_value = _AttrValue(**{value_name: tf_value})
        attr[key] = attr_value
      graph_def.node.add(name=op_info.name,
                         op=op_info.op_type,
                         input=[in_tensor.name for in_tensor in op_info.input_tensors],
                         device=op_info.op_attr.get('tensorflow__device', ''),
                         attr=attr)
    return graph_def
  
  @property
  def ops(self):
    return [self.ops_info[name] for name in self.topo_order]

  def add_op(self, op):
    if not isinstance(op, OperationInfo):
      raise ValueError('expecting OperationInfo, get {}'.format(type(op)))
    if op.name in self.ops_info:
      raise ValueError('duplicate op detected, {}'.format(op.name))
    self.ops_info[op.name] = op
    topologic_order_graph(self)

  def drop_op(self, op_name):
    if op_name not in self.ops_info:
      raise ValueError('op not found in the graph: {}'.format(op_name))
    del self.ops_info[op_name]
    self.topo_order.remove(op_name)

  def __deepcopy__(self, memo):
    new_graph = uTensorGraph(output_nodes=self.output_nodes)
    memo['ugraph'] = new_graph
    new_ops_info = dict((k, deepcopy(v, memo)) for k, v in self.ops_info.items())
    new_topo_order = [name for name in self.topo_order]

    new_graph.ops_info = new_ops_info
    new_graph.topo_order = new_topo_order
    new_graph._backend = self._backend
    return new_graph
