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


r"""Modules for monkey patching to observe tensors created with feature columns.

This file solely focuses on models created with tf.feature_columns. Generation
of tensors are hidden from the user in this setting. Thus, we monkey-patch
certain functions to observe what tensors are created while saving the model.

Hierarchy of feature columns can be seen below:

      Categorical Column                     Dense Column
             ^                <----_  _----->       ^
             |                      \/              |
             |              bucketized_column       |
categorical_column_with_identity           numeric_column
categorical_column_with_vocabulary_file    indicator_column
categorical_column_with_vocabulary_list    embedding_column
categorical_column_with_with_hash_bucket
crossed_column

Categorical columns are densified via indicator or embedding columns before
being fed to a DNN model; but they can be directly fed to linear models. Crossed
column acts as a categorical column even if any of its keys is a dense column.
Hence, we collect input tensors as sparse tensors generated by categorical
columns or dense tensors for numeric columns. We also collect densified
versions of those tensors from indicator and embedding columns for DNNs (Linear
models are also densified, but in a different way). For each input tensor(s), we
collect their encoded tensors from these densifications.

There are many edge and corner cases on how to build a model with feature
columns and how Tensorflow creates tensors for each case. For example,
DNNLinearCombinedClassifier creates two sets of all inputs and encoded tensors
for each feature. We keep track of all input tensors and potentially encoded
tensors with a class called FeatureTensors. For each base feature, we hold an
array of FeatureTensors objects.

feature tensors:
{<feature name 1>: [FeatureTensors(<input tensor>,
                                  [<embedding tensor>, <indicator tensor>])],
 <feature name 2>: [FeatureTensors(<input tensor 2>, [])],
 <feature name 3>: [FeatureTensors(<input tensor 3>, [])]}

Number of FeatureTensors object for each base column will be one most of the
time. In the rare case of wide deep classifier, there will be two sets of input
and encoded tensors, which leads to a list of FeatureTensors object.
"""

from __future__ import absolute_import
from __future__ import division

from __future__ import print_function

import contextlib

from six.moves import zip
import tensorflow.compat.v1 as tf

from tensorflow.python.feature_column import feature_column_v2 as fc2
from tensorflow_estimator.python.estimator.canned import prediction_keys
from tensorflow_estimator.python.estimator.export import export_lib


_FEATURE_COLUMNS_TO_PATCH_DENSE = [
    fc2.DenseColumn, fc2.NumericColumn, fc2.EmbeddingColumn,
    fc2.IndicatorColumn, fc2.BucketizedColumn  # Bucketized is dense and sparse.
]

_FEATURE_COLUMNS_TO_PATCH_SPARSE = [
    fc2.VocabularyListCategoricalColumn, fc2.VocabularyFileCategoricalColumn,
    fc2.IdentityCategoricalColumn, fc2.HashedCategoricalColumn,
    fc2.CrossedColumn, fc2.BucketizedColumn,  # Bucketized is dense and sparse.
    fc2.WeightedCategoricalColumn
]


class FeatureTensors(object):
  """Groups of tensors representing a feature."""

  def __init__(
      self,
      input_tensor,
      encoded_tensors = ()):
    """Initialize a FeatureTensors object.

    Args:
      input_tensor: Input tensor representing this feature. Can be sparse.
      encoded_tensors: A set of tensors encoding input_tensor.
    """
    self._input_tensor = input_tensor
    self._encoded_tensors = encoded_tensors or []

  @property
  def input_tensor(self):
    return self._input_tensor

  @property
  def encoded_tensors(self):
    return self._encoded_tensors


class EstimatorMonkeyPatchHelper(object):
  """Monkey patches functions to observe important tensors in Estimators."""

  def __init__(self):
    """Create an EstimatorMonkeyPatchHelper instance.

    EstimatorMonkeyPatchHelper is used for observing tensors created during an
    estimator export. It provides a context manager with exporting_context. Once
    initialized, estimators can be exported within that context. Later, observed
    parameters can be fetched from feature_tensors_dict, output_tensors_dict,
    crossed_columns properties.
    """
    self._feature_tensors_dict = {}
    self._output_tensors_dict = {}
    self._crossed_columns = set()

  @property
  def feature_tensors_dict(self):
    return self._feature_tensors_dict

  @property
  def output_tensors_dict(self):
    return self._output_tensors_dict

  @property
  def crossed_columns(self):
    return self._crossed_columns

  def _make_observing_export_outputs_for_mode(
      self, old_fn,
      observing_dict,
      output_key = None):
    """Make an observing function that records output tensors.

    When a model is exported, export_outputs_for_mode() function is called. We
    observe output tensors in predictions parameter given output key. If no
    output key is provided, we resort to common keys which are 'predictions' for
    regression and 'probabilities' for classification.

    Args:
      old_fn: Original export_outputs_for_mode() function.
      observing_dict: Dictionary to record output names and corresponding tensor
        names.
      output_key: Key in prediction dictionary.

    Returns:
      Monkey-patched export_outputs_for_mode function

    Raises:
      ValueError: If no output_key provided and output key cannot be inferred
        from common keys. Or provided output key is not in head's output
        predictions dictionary.
    """

    def _observing_export_outputs_for_mode(mode,
                                           serving_export_outputs=None,
                                           predictions=None,
                                           loss=None,
                                           metrics=None):
      """Wrapper around export_outputs_for_mode that observes arguments."""
      pred_keys = prediction_keys.PredictionKeys
      if output_key:
        if output_key not in predictions:
          raise ValueError('Output key %s is not found.' % output_key)
        observing_dict[output_key] = predictions[output_key]
      elif pred_keys.LOGITS in predictions:
        observing_dict[pred_keys.LOGITS] = predictions[pred_keys.LOGITS]
      elif pred_keys.PREDICTIONS in predictions:
        observing_dict[pred_keys.PREDICTIONS] = predictions[
            pred_keys.PREDICTIONS]
      else:
        raise ValueError('Output keys are not specified and not inferred.')

      result = old_fn(mode, serving_export_outputs, predictions, loss, metrics)
      return result

    return _observing_export_outputs_for_mode

  def _make_observing_get_dense_tensor(
      self, old_fn,
      feature_tensors,
      crossed_columns):
    """Returns a function that wraps get_dense_tensors and observes arguments.

    The returned function can be used to replace get_dense_tensor() of feture
    columns so we can observe the constructed dense tensors. These dense tensors
    could refer to encoded tensors or input tensors depending on the feature.

    Args:
      old_fn: The old call function.
      feature_tensors: Dictionary to write observed input tensors.
      crossed_columns: A set to write crossed feature columns names.

    Returns:
      A function that wraps old_fn and observes arguments, that can be used
      to replace DenseFeatures.call and LinearModelLayer.call.
    """

    def _observing_get_dense_tensor(instance, *args, **kwargs):
      """Wrapper around get_dense_tensor that observes arguments."""
      result = old_fn(instance, *args, **kwargs)
      fc = instance
      if hasattr(fc, 'categorical_column'):
        fc = fc.categorical_column
        # Source column of bucketized is already added.
        if isinstance(fc, fc2.BucketizedColumn):
          return result
        elif isinstance(fc, fc2.CrossedColumn):
          for column in fc.keys:
            crossed_columns.add(column.name)
            self._add_encoded_tensor_to_dict(feature_tensors, column, result)
          return result
        else:
          self._add_encoded_tensor_to_dict(feature_tensors, fc, result)
      elif not isinstance(fc, fc2.BucketizedColumn):
        self._add_input_tensor_to_dict(feature_tensors, fc, result)
      return result

    return _observing_get_dense_tensor

  def _add_input_tensor_to_dict(
      self,
      feature_tensors,
      fc,
      tensor):
    """Add input tensor to list of FeatureTensors."""
    feature_tensors_list = feature_tensors.get(fc.name, [])
    if tensor not in [feature.input_tensor for feature in feature_tensors_list]:
      feature_tensors_list.append(FeatureTensors(tensor))
    feature_tensors[fc.name] = feature_tensors_list

  def _add_encoded_tensor_to_dict(
      self,
      feature_tensors,
      fc,
      tensor):
    """Add encoded tensor to list of FeatureTensors."""
    if fc.name not in feature_tensors:
      raise ValueError('Trying to add encoded tensors with no input tensor.')
    feature_tensor = feature_tensors[fc.name][-1]
    if tensor not in feature_tensor.encoded_tensors:
      feature_tensor.encoded_tensors.append(tensor)

  def _make_observing_get_sparse_tensors(
      self, old_fn,
      feature_tensors
  ):
    """Returns a function that wraps get_sparse_tensors and observes arguments.

    The returned function can be used to replace get_sparse_tensors function of
    all feature columns so we can observe generated sparse tensors.

    Args:
      old_fn: The old call function.
      feature_tensors: Dictionary to write observed sparse tensors.

    Returns:
      A function that wraps old_fn and observes arguments, that can be used
      to replace <feature column>.get_sparse_tensors().
    """

    def _observing_get_sparse_tensors(instance, *args, **kwargs):
      """Wrapper around get_sparse_tensors that observes arguments."""
      result = old_fn(instance, *args, **kwargs)
      if not isinstance(instance, (fc2.CrossedColumn, fc2.BucketizedColumn)):
        fc = (instance
              if not isinstance(instance, fc2.WeightedCategoricalColumn)
              else instance.categorical_column)
        self._add_input_tensor_to_dict(feature_tensors, fc, result)
      return result

    return _observing_get_sparse_tensors

  def _make_observing_create_weighted_sum(
      self, old_fn,
      feature_tensors,
      crossed_columns):
    """Make an observing function for LinearEstimators."""

    def _observing_create_weighted_sum(column, *args, **kwargs):
      """Wrapper around _create_weighted_sum that observes arguments."""
      result = old_fn(column, *args, **kwargs)
      if (isinstance(column, fc2.CategoricalColumn) and
          not isinstance(column, fc2.BucketizedColumn)):
        if isinstance(column, fc2.CrossedColumn):
          crossed_columns.update({sub_col.name for sub_col in column.keys})
          return result
        fc = (
            column.categorical_column
            if hasattr(column, 'categorical_column') else column)
        self._add_encoded_tensor_to_dict(feature_tensors, fc, result)

      return result

    return _observing_create_weighted_sum

  def _make_observing_transform_features_v2(
      self, old_fn,
      feature_tensors):
    """Make an observing function for _transform_features_v2 of fc2."""

    def _observing_transform_features_v2(features, feature_columns, *args,
                                         **kwargs):
      """Wrapper around fc2._transform_features_v2."""
      result = old_fn(features, feature_columns, *args, **kwargs)
      for fc, tensor in result.items():
        # Boosted trees don't call get_dense_tensor or get_sparse_tensor
        # (if the column is wrapped).
        if (not hasattr(fc, 'categorical_column') and
            fc.name not in feature_tensors):
          if isinstance(tensor, tf.SparseTensor):
            tensor = fc2.CategoricalColumn.IdWeightPair(tensor, None)
          self._add_input_tensor_to_dict(feature_tensors, fc, tensor)
      return result

    return _observing_transform_features_v2

  def _patch_entities(self, objects_to_patch, attr_name,
                      patching_function,
                      **kwargs):
    """Patch given set of object's function with provided function.

    Args:
      objects_to_patch: A list of objects we want to patch with the given
        patching function.
      attr_name: Name of the function (or attribute) we want to patch.
      patching_function: A function that returns a replacement for attr_name.
      **kwargs: Additional args to provide to patching_function.

    Returns:
      A list of original attributes to patch back later.
    """
    originals = []
    for object_to_patch in objects_to_patch:
      original = getattr(object_to_patch, attr_name)
      new_attr = patching_function(original, **kwargs)
      setattr(object_to_patch, attr_name, new_attr)
      originals.append(original)
    return originals

  def _unpatch_entities(self, objects_patched, attr_name,
                        originals):
    """Replaces patched object with the originals.

    Args:
      objects_patched: A list of patched classes, modules, etc.
      attr_name: Name of the attribute to patch back.
      originals: List of original functions to put back.
    """
    for patched_object, original_fun in zip(objects_patched, originals):
      setattr(patched_object, attr_name, original_fun)

  def _patch_estimator_to_observe(self, output_key = None):
    """Patch all functions to observe tensor creation."""
    # Patch get_dense_tensor() for all dense feature columns.
    self._actual_get_dense_tensor_list = self._patch_entities(
        _FEATURE_COLUMNS_TO_PATCH_DENSE,
        'get_dense_tensor',
        self._make_observing_get_dense_tensor,
        feature_tensors=self._feature_tensors_dict,
        crossed_columns=self._crossed_columns)

    # Patch get_sparse_tensors() for all categorical feature columns.
    self._actual_get_sparse_tensors_list = self._patch_entities(
        _FEATURE_COLUMNS_TO_PATCH_SPARSE,
        'get_sparse_tensors',
        self._make_observing_get_sparse_tensors,
        feature_tensors=self._feature_tensors_dict)

    # Patch _create_weighted_sum() to get dense versions for linear models.
    self._actual_create_weighted_sum = self._patch_entities(
        [fc2],
        '_create_weighted_sum',
        self._make_observing_create_weighted_sum,
        feature_tensors=self._feature_tensors_dict,
        crossed_columns=self._crossed_columns)[0]

    # Patch export_outputs_for_mode() to get output tensors.
    self._actual_export_for_mode = self._patch_entities(
        [export_lib],
        'export_outputs_for_mode',
        self._make_observing_export_outputs_for_mode,
        output_key=output_key,
        observing_dict=self._output_tensors_dict)[0]

    # Patch _transform_features_v2() to observe input tensors for Boosted Trees.
    self._actual_transform = self._patch_entities(
        [fc2],
        '_transform_features_v2',
        self._make_observing_transform_features_v2,
        feature_tensors=self._feature_tensors_dict)[0]

  def _unpatch_estimator_to_observe(self):
    """Unpatch all patched functions."""
    self._unpatch_entities(_FEATURE_COLUMNS_TO_PATCH_DENSE, 'get_dense_tensor',
                           self._actual_get_dense_tensor_list)
    self._unpatch_entities(_FEATURE_COLUMNS_TO_PATCH_SPARSE,
                           'get_sparse_tensors',
                           self._actual_get_sparse_tensors_list)
    self._unpatch_entities([fc2], '_create_weighted_sum',
                           [self._actual_create_weighted_sum])
    self._unpatch_entities([export_lib], 'export_outputs_for_mode',
                           [self._actual_export_for_mode])
    self._unpatch_entities([fc2], '_transform_features_v2',
                           [self._actual_transform])

  @contextlib.contextmanager
  def exporting_context(self, output_key = None):
    """Context for exporting the estimator.

    Args:
      output_key: Output key used in model's output signature. Common keys are
        'logits', 'probabilities' for classification tasks; 'prediction' for
        regression tasks.

    Yields:
      nothing.
    """
    self._patch_estimator_to_observe(output_key)
    try:
      yield

    finally:
      self._unpatch_estimator_to_observe()
