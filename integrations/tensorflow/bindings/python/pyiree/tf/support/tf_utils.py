# Lint as: python3
# Copyright 2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Utilities interop with TensorFlow."""

# pylint: disable=protected-access

import collections
import os
import random
import re
import tempfile

from absl import flags
from absl import logging
import numpy as np
from pyiree import rt
from pyiree.tf import compiler
import tensorflow.compat.v2 as tf

FLAGS = flags.FLAGS


def set_random_seed(seed=0):
  """Set random seed for tf, np and random."""
  tf.random.set_seed(seed)
  random.seed(seed)
  np.random.seed(seed)


def backends_to_str(target_backends):
  """Creates a flattened and normalized string representing target_backends."""
  normalized_backends = []
  for backend in target_backends:
    # Remove unusual characters and ensure names don't end or start in "_".
    backend = re.sub("[^0-9a-zA-Z_]+", "_", backend)
    normalized_backends.append(backend.strip("_"))
  return "__".join(normalized_backends)


def to_mlir_type(dtype):
  """Returns a string that denotes the type `dtype` in MLIR style."""
  bits = dtype.itemsize * 8
  if np.issubdtype(dtype, np.integer):
    return f"i{bits}"
  elif np.issubdtype(dtype, np.floating):
    return f"f{bits}"
  else:
    raise TypeError(f"Expected integer or floating type, but got {dtype}")


def save_input_values(inputs, artifacts_dir=None):
  """Saves input values with IREE tools format if `artifacts_dir` is set."""
  result = []
  for array in inputs:
    shape = [str(dim) for dim in list(array.shape)]
    shape.append(to_mlir_type(array.dtype))
    shape = "x".join(shape)
    values = " ".join([str(x) for x in array.flatten()])
    result.append(f"{shape}={values}")
  result = "\n".join(result)
  if artifacts_dir is not None:
    inputs_path = os.path.join(artifacts_dir, "inputs.txt")
    logging.info("Saving IREE input values to: %s", inputs_path)
    with open(inputs_path, "w") as f:
      f.write(result)
      f.write("\n")
  return result


def compile_tf_module(tf_module,
                      target_backends=(),
                      exported_names=(),
                      artifacts_dir=None):
  """Compiles a TensorFlow tf.Module and optionally saves compilation artifacts.

  The artifact this creates is not callable. See IreeCompiledModule for an API
  that returns a module that can be called without any further steps.

  If artifacts_dir is provided then the following artifacts will be saved:
    saved_model:
      A TF SavedModel directory containing the files used translate the
      tf.Module into an IREE module.
    tf_input.mlir:
      MLIR for the module in TF's input dialect.
    iree_input.mlir:
      The MLIR above translated to IREE via compiler.TF_IMPORT_PASS_PIPELINE.
    compiled__backends.vmfb:
      A VM FlatBuffer compiled to the target backends from the IREE MLIR above.
  Here 'backends' is a '__' delimited list of iree backends (e.g. vmla__llvm_ir)

  Args:
    tf_module: A tf.Module.
    target_backends: Iterable of string backend names to compile for.
    exported_names: Iterable of dotted function names to consider for
      compilation.
    artifacts_dir: An optional string pointing to where compilation artifacts
      should be saved.

  Returns:
    A compiled IREE module blob.
  """

  def _compile_from_path(sm_path):
    """Helper function for compile_tf_module."""
    # We break up the compilation here so we can save intermediary artifacts.
    compiler_context = compiler.Context()

    # Convert the tf_module into raw TF input MLIR.
    compiler_module = compiler.tf_load_saved_model(
        sm_path,
        exported_names=exported_names,
        compiler_context=compiler_context,
        pass_pipeline=())

    if artifacts_dir is not None:
      tf_mlir_path = os.path.join(artifacts_dir, "tf_input.mlir")
      logging.info("Saving raw TF input MLIR to: %s", tf_mlir_path)
      with open(tf_mlir_path, "w") as f:
        f.write(compiler_module.to_asm())

    # Now run the passes manually that tf_load_saved_model would usually do.
    compiler_module.run_pass_pipeline(compiler.TF_IMPORT_PASS_PIPELINE)

    if artifacts_dir is not None:
      iree_mlir_path = os.path.join(artifacts_dir, "iree_input.mlir")
      logging.info("Saving IREE input MLIR to: %s", iree_mlir_path)
      with open(iree_mlir_path, "w") as f:
        f.write(compiler_module.to_asm())

    compiled_module = compiler_module.compile(target_backends=target_backends)
    if artifacts_dir is not None:
      compiled_name = f"compiled__{backends_to_str(target_backends)}.vmfb"
      compiled_path = os.path.join(artifacts_dir, compiled_name)
      logging.info("Saving compiled IREE module to: %s", compiled_path)
      with open(compiled_path, "wb") as f:
        f.write(compiled_module)

    return compiled_module

  options = tf.saved_model.SaveOptions(save_debug_info=True)
  if artifacts_dir is not None:
    # Save the saved model alongside the other compilation artifacts.
    sm_path = os.path.join(artifacts_dir, "saved_model")
    tf.saved_model.save(tf_module, sm_path, options=options)
    return _compile_from_path(sm_path)
  else:
    # Round-trip the saved model through a temporary directory.
    with tempfile.TemporaryDirectory() as sm_path:
      tf.saved_model.save(tf_module, sm_path, options=options)
      return _compile_from_path(sm_path)


class CompiledModule(object):
  """Base class for the TF and IREE compiled modules."""

  def __init__(self, module_class, backend_info, exported_names, artifacts_dir):
    """Shared base constructor – not useful on its own."""
    self._module_class = module_class
    self._backend_info = backend_info
    self._exported_names = exported_names
    self._artifacts_dir = artifacts_dir

  def create_reinitialized(self):
    """Duplicates this module with its initial state without recompiling."""
    raise NotImplementedError()


class IreeCompiledModule(CompiledModule):
  """Iree compiled module."""

  def __init__(self,
               module_class,
               backend_info,
               exported_names=[],
               artifacts_dir=None,
               _create_reinitialized_args=None):
    """Compile a tf.Module to the target backend in backend_info.

    Args:
      module_class: the tf.Module subclass to compile.
      backend_info: an element of BackendInfo corresponding to the IREE backend
        to compile to.
      exported_names: an optional iterable of strings representing which of the
        module_class's functions to compile. If exported_names is empty all
        functions will be compiled.
      artifacts_dir: an optional path to save compilation artifacts to.
    """
    super().__init__(module_class, backend_info, exported_names, artifacts_dir)

    if _create_reinitialized_args is None:
      self._module_blob = compile_tf_module(
          tf_module=module_class(),
          target_backends=backend_info.iree_compiler_targets,
          exported_names=exported_names,
          artifacts_dir=artifacts_dir)
      self._module = rt.VmModule.from_flatbuffer(self._module_blob)
      self._config = rt.Config(driver_name=backend_info.iree_driver)
    else:
      # Called from self.create_reinitialized()
      self._module_blob, self._module, self._config = _create_reinitialized_args

    # Holds all of the module's mutable state.
    self._context = rt.SystemContext(
        modules=[self._module], config=self._config)

  def create_reinitialized(self):
    """Duplicates this module with its initial state without recompiling."""
    default_args = [
        self._module_class, self._backend_info, self._exported_names,
        self._artifacts_dir
    ]
    create_reinitialized_args = [self._module_blob, self._module, self._config]
    return IreeCompiledModule(*default_args, create_reinitialized_args)

  def __getattr__(self, attr):
    # Try to resolve it as a function.
    m = self._context.modules[self._module.name]
    f = m[attr]
    return _IreeFunctionWrapper(self._context, f)


class _IreeFunctionWrapper(object):
  """Wraps an IREE function, making it callable."""

  def __init__(self, context, f):
    self._context = context
    self._f = f

  def __call__(self, *args):
    return self._f(*args)


class TfCompiledModule(CompiledModule):
  """TensorFlow 'compiled' module.

  This facade exists to provide a complimentary API to IreeCompiledModule and
  normalize TensorFlow's output to Numpy.
  """

  def __init__(self,
               module_class,
               backend_info,
               exported_names=[],
               artifacts_dir=None):
    """Wrap a tf.Module in a TFCompiledModule facade.

    Args:
      module_class: the tf.Module subclass to 'compile'.
      backend_info: one of the 'tf*' elements in BackendInfo.
      exported_names: an optional iterable of strings representing which of the
        module_class's functions should be callable. If exported_names is empty
        then all functions will be callable.
      artifacts_dir: an optional path to save compilation artifacts to. Has no
        effect for this subclass as nothing is compiled.
    """
    super().__init__(module_class, backend_info, exported_names, artifacts_dir)
    self._tf_module = module_class()

  def create_reinitialized(self):
    """Duplicates this module with the starting state of module_class."""
    return TfCompiledModule(self._module_class, self._backend_info,
                            self._exported_names, self._artifacts_dir)

  def __getattr__(self, attr):
    # Try to resolve it as a function.
    exported = len(self._exported_names) == 0 or attr in self._exported_names
    if not hasattr(self._tf_module, attr) or not exported:
      raise AttributeError(f"The TensorFlow module does not have attr '{attr}'")
    f = getattr(self._tf_module, attr)
    if not f or not hasattr(f, "__call__"):
      raise AttributeError(
          f"The TensorFlow module does not have a callable attr '{attr}'")
    return _TfFunctionWrapper(f)


class _TfFunctionWrapper(object):
  """Wraps a TF function, normalizing it to numpy."""

  def __init__(self, f):
    self._f = f

  def __call__(self, *args, **kwargs):
    # TensorFlow will auto-convert all inbound args.
    results = self._f(*args, **kwargs)
    # Then unmarshal them to numpy in the same way that the other backends do.
    # Handle single result (technically ambiguous with return of a tuple,
    # which is sad).
    if not isinstance(results, tuple):
      results = (results,)
    return tf.nest.map_structure(
        lambda t: t.numpy() if isinstance(t, tf.Tensor) else t,
        *results,
        check_types=False)


class BackendInfo(
    collections.namedtuple(
        "BackendInfo",
        ["name", "CompiledModule", "iree_driver", "iree_compiler_targets"])):
  """Info object describing a backend."""

  # All BackendInfo entries by name.
  ALL = {}

  @classmethod
  def add(cls, **kwargs):
    backend_info = cls(**kwargs)
    cls.ALL[backend_info.name] = backend_info


BackendInfo.add(
    name="tf",
    CompiledModule=TfCompiledModule,
    iree_driver=None,
    iree_compiler_targets=None)
# tf_also is used for checking test consistency
# to catch any initialization/randomization issues between model runs
BackendInfo.add(
    name="tf_also",
    CompiledModule=TfCompiledModule,
    iree_driver=None,
    iree_compiler_targets=None)
BackendInfo.add(
    name="iree_vmla",
    CompiledModule=IreeCompiledModule,
    iree_driver="vmla",
    iree_compiler_targets=["vmla"])
BackendInfo.add(
    name="iree_vulkan",
    CompiledModule=IreeCompiledModule,
    iree_driver="vulkan",
    iree_compiler_targets=["vulkan-*"])
BackendInfo.add(
    name="iree_llvmjit",
    CompiledModule=IreeCompiledModule,
    iree_driver="llvm",
    iree_compiler_targets=["llvm-ir"])
