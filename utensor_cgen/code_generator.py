# -*- coding:utf8 -*-
import os
from tempfile import NamedTemporaryFile
import logging

import numpy as np
import tensorflow as tf
from tensorflow.core.framework.graph_pb2 import GraphDef
from tensorflow.tools.graph_transforms import TransformGraph

from .operators import OperatorFactory
from .transformer.pipline import TransformerPipeline
from .ir import uTensorGraph
from .snippets import (CommentSnippet, ContextHeaderSnippet,
                       ContextSnippetsContainer, CreateTensorIdxSnippet)
from .snippets.composer import Composer

__all__ = ["CodeGenerator"]
_logger = logging.getLogger('utensor-cli')

class CodeGenerator(object):
  def __init__(self, model_file,
               idx_dir,
               embed_data_dir,
               trans_methods,
               output_nodes,
               debug_cmt=False,
               **trans_kwargs):
    self.model_file = model_file
    if not os.path.exists(idx_dir):
      os.makedirs(idx_dir)
    self.idx_dir = idx_dir
    self.embed_data_dir = embed_data_dir.rstrip("/")
    self.trans_methods = trans_methods
    self.output_nodes = output_nodes
    self.debug_cmt = debug_cmt
    self.trans_kwargs = trans_kwargs

  def generate(self, src_fname):
    _, ext = os.path.splitext(self.model_file)
    if ext == '.pb':
      self._generate_from_pb(src_fname)
    else:
      raise ValueError('Support only pb file')

  def _generate_from_pb(self, src_fname):
    """Generate source and header files
    """
    fname, _ = os.path.splitext(src_fname)
    graph_name, _ = os.path.splitext(os.path.basename(self.model_file))
    guard_name = fname.replace('/', '_')
    header_snippet = ContextHeaderSnippet(guard_name, graph_name)

    composer = Composer()
    header_fname = '{}.hpp'.format(fname)
    header_name = os.path.basename(header_fname)
    container = ContextSnippetsContainer(graph_name, header_name)

    opFactory = OperatorFactory()

    graph_def = self._tf_load_graph_def(self.model_file)
    self._expect_non_quantized(graph_def)
    ugraph = uTensorGraph(graph_def, self.output_nodes)
    _logger.info("Transforming graph: %s", self.model_file)
    quant_ugraph = self._transform_graph(ugraph,
                                         self.trans_methods,
                                         self.trans_kwargs)
    _logger.info('Graph transormation done')

    for op_id, op_name in enumerate(quant_ugraph.topo_order):
      op_info = quant_ugraph.ops_info[op_name]
      op_type = op_info.op_type
      if op_type == "Placeholder":
        out_tname = op_info.output_tensors[0].name
        ref_count = 0 #ref_counts[0]
        container.template_vars["placeholders"].append(out_tname)
        container.template_vars["ref_counts"].append(ref_count)
        header_snippet.template_vars["placeholders"].append(out_tname)
      else:
        snippet = opFactory.createOperatorSnippet(op_info,
                                                  idx_dir=self.idx_dir,
                                                  embeded_data_dir=self.embed_data_dir)
        container.add_snippet(snippet)

      if self.debug_cmt:
        comments = ["<<< Operation id {}: {}".format(op_id, op_name),
                    ">>> Operation id {}: {}".format(op_id + 1, op_name)]
        cmt_snippet = CommentSnippet(comments)
        container.add_snippet(cmt_snippet)
    composer.add_snippet(container)

    _logger.info("Generate header file: %s", header_fname)
    with open(header_fname, "w") as wf:
      wf.write('// Auto generated by utensor-cli\n\n')
      wf.write(header_snippet.render())
    _logger.info("Generate source file: %s", src_fname)
    with open(src_fname, "w") as wf:
      wf.write('// Auto generated by utensor-cli\n\n')
      wf.write(composer.compose())
  
  @classmethod
  def _expect_non_quantized(cls, graph_def):
    is_quantized = False
    for node in graph_def.node:
      if node.op in ["Dequantize", "QuantizedMaxPool",
                     "QuantizeV2", "QuantizedMatMul",
                     "QuantizedRelu", "QuantizedAdd",
                     "RequantizationRange",
                     "Requantize",
                     "QuantizedReshape",
                     "QuantizedConv2D"]:
        is_quantized = True
        break
    if is_quantized:
      _logger.warning(("Expecting non-quantized graph, "
                        "graph transformation/optimization might not work properly"))

  def _transform_graph(self, ugraph, methods, trans_kwargs):
    pipeline = TransformerPipeline(methods, trans_kwargs)
    return pipeline.transform(ugraph)

  def _tf_load_graph_def(self, pb_fname):
    with tf.gfile.FastGFile(pb_fname, 'rb') as fid:
      graph_def = tf.GraphDef()
      graph_def.ParseFromString(fid.read())
    return graph_def
