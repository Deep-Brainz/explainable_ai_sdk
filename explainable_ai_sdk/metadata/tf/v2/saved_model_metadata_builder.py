# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Metadata builder for models built with Tensorflow 2.X.

This metadata builder supports all three flavors of Keras model interface:
sequential, functional, and subclassed.

Inputs and outputs of a model can be inferred from a saved model. Using the
provided saved model signatures, we create input and output metadata. Users have
the option to remove and modify these metadata. Users have the option to resave
their model with the metadata file or get the metadata from the builder to save
it themselves.

This builder infers metadata for all inputs and outputs. However, explainability
service supports only single output. Users need to use remove_output_metadata
and get_metadata functions to remove the ones they don't need.
"""

import tensorflow as tf
from explainable_ai_sdk.common import explain_metadata
from explainable_ai_sdk.common import types
from explainable_ai_sdk.metadata import constants
from explainable_ai_sdk.metadata import metadata_builder
from explainable_ai_sdk.metadata import utils


class SavedModelMetadataBuilder(metadata_builder.MetadataBuilder):
  """Class for generating metadata for a model built with TF 2.X Keras API."""

  def __init__(
      self,
      model_path,
      signature_name = tf.saved_model.DEFAULT_SERVING_SIGNATURE_DEF_KEY,
      **kwargs):
    """Initializes a SavedModelMetadataBuilder object.

    Args:
      model_path: Path to load the saved model from.
      signature_name: Name of the signature to be explained. Inputs and outputs
        of this signature will be written in the metadata. If not provided, the
        default signature will be used.
      **kwargs: Any keyword arguments to be passed to tf.saved_model.save()
        function.
    """
    self._saved_model_args = kwargs
    self._loaded_model = tf.saved_model.load(model_path)
    self._inputs, self._outputs = self._infer_metadata_entries_from_model(
        signature_name)

  def _infer_metadata_entries_from_model(
      self, signature_name
  ):
    """Infers metadata inputs and outputs."""
    loaded_sig = self._loaded_model.signatures[signature_name]
    _, input_sig = loaded_sig.structured_input_signature
    output_sig = loaded_sig.structured_outputs
    input_mds = {name: explain_metadata.InputMetadata(name, name)
                 for name in input_sig}

    if len(output_sig) > 1:
      print('There are multiple outputs for the signature: %s. We support only'
            ' one output. Please use "remove_output_metadata" function to'
            " reduce the number of outputs to 1 before saving." %
            signature_name)
    output_mds = {name: explain_metadata.OutputMetadata(name, name)
                  for name in output_sig}
    return input_mds, output_mds

  def set_numeric_metadata(
      self,
      input_name,
      new_name = None,
      input_baselines = None,
      index_feature_mapping = None):
    """Sets an existing metadata identified by input as numeric with params.

    Args:
      input_name: Input name in the metadata to be set as numeric.
      new_name: Optional (unique) new name for this feature.
      input_baselines: A list of baseline values. Each baseline value can be a
        single entity or of the same shape as the model_input (except for the
        batch dimension).
      index_feature_mapping: A list of feature names for each index in the input
        tensor.

    Raises:
      ValueError: If input_name cannot be found in the metadata.
    """
    if input_name not in self._inputs:
      raise ValueError("Input with with name '%s' does not exist." % input_name)
    name = new_name if new_name else input_name
    tensor_name = self._inputs.pop(input_name).input_tensor_name

    if index_feature_mapping:
      encoding = explain_metadata.Encoding.BAG_OF_FEATURES
    else:
      encoding = explain_metadata.Encoding.IDENTITY
    self._inputs[name] = explain_metadata.InputMetadata(
        name=name,
        input_tensor_name=tensor_name,
        input_baselines=input_baselines,
        index_feature_mapping=index_feature_mapping,
        encoding=encoding)

  def set_image_metadata(
      self,
      input_name,
      new_name = None,
      input_baselines = None,
      visualization = None):
    """Sets an existing metadata identified by input as image with params.

    Args:
      input_name: Input name in the metadata to be set as numeric.
      new_name: Optional (unique) new name for this feature.
      input_baselines: A list of baseline values. Each baseline value can be a
        single entity or of the same shape as the model_input (except for the
        batch dimension).
      visualization: A dictionary specifying visualization parameters. Check out
        original documentation for possible keys and values:
        https://cloud.google.com/ai-platform/prediction/docs/ai-explanations/visualizing-explanations

    Raises:
      ValueError: If input_name cannot be found in the metadata.
    """
    if input_name not in self._inputs:
      raise ValueError("Input with with name '%s' does not exist." % input_name)
    name = new_name if new_name else input_name
    tensor_name = self._inputs.pop(input_name).input_tensor_name
    self._inputs[name] = explain_metadata.InputMetadata(
        name=name,
        input_tensor_name=tensor_name,
        input_baselines=input_baselines,
        modality=explain_metadata.Modality.IMAGE,
        visualization=visualization)

  def set_output_metadata(self, output_name, new_name):
    """Updates an existing output metadata identified by output_name.

    Args:
      output_name: Name of the output that needs to be updated.
      new_name: New (unique) friendly name for the output.

    Raises:
      ValueError: If output with the given name doesn't exist.
    """
    if output_name not in self._outputs:
      raise ValueError("Output with name '%s' does not exist." %
                       output_name)
    if output_name == new_name:
      return
    old_output = self._outputs.pop(output_name)
    self._outputs[new_name] = explain_metadata.OutputMetadata(
        new_name, old_output.output_tensor_name)

  def remove_input_metadata(self, name):
    """Removes input metadata with the name."""
    if name not in self._inputs:
      raise ValueError("Input with with name '%s' does not exist." % name)
    del self._inputs[name]

  def remove_output_metadata(self, name):
    """Removes output metadata with the name."""
    if name not in self._outputs:
      raise ValueError("Output with with name '%s' does not exist." % name)
    del self._outputs[name]

  def get_metadata(self):
    """Returns the current metadata as a dictionary."""
    current_md = explain_metadata.ExplainMetadata(
        inputs=list(self._inputs.values()),
        outputs=list(self._outputs.values()),
        framework=explain_metadata.Framework.TENSORFLOW2,
        tags=[constants.METADATA_TAG])
    return current_md.to_dict()

  def save_metadata(self, file_path):
    """Saves model metadata to the given folder.

    Args:
      file_path: Path to save the model and the metadata. It can be a GCS bucket
        or a local folder. The folder needs to be empty.

    Raises:
      ValueError: If current number of outputs is greater than 1.
    """
    if len(self._outputs) > 1:
      raise ValueError("Number of outputs is more than 1.")
    utils.write_metadata_to_file(self.get_metadata(), file_path)

  def save_model_with_metadata(self, file_path):
    """Saves the model and the generated metadata to the given file path.

    Args:
      file_path: Path to save the model and the metadata. It can be a GCS bucket
        or a local folder. The folder needs to be empty.

    Returns:
      Full file path where the model and the metadata are written.
    """
    tf.saved_model.save(self._loaded_model, file_path, self._saved_model_args)
    self.save_metadata(file_path)
    return file_path