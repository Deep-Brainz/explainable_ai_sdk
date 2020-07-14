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


"""Tests for the Explanation class."""
from matplotlib import pyplot as plt

import mock
import numpy as np
import tensorflow.compat.v1 as tf

from explainable_ai_sdk.common import explain_metadata
from explainable_ai_sdk.model import constants
from explainable_ai_sdk.model import explanation



class ExplanationTest(tf.test.TestCase):

  def setUp(self):
    super(ExplanationTest, self).setUp()

    fake_attr_dict_1 = {
        'attributions': {
            'data': [0.01, 0.02, 0.03],
            'test': [0.1, 0.2, 0.3]
        },
        'baseline_score': 0.0001,
        'example_score': 0.4,
        'label_index': 170,
        'output_name': 'probability',
        'approx_error': 0.033
    }

    fake_attr_dict_2 = {
        'attributions': {
            'data': [0.3, 0.01, 0.13],
            'test': [0.05, 0.02, 0.23]
        },
        'baseline_score': 0.0001,
        'example_score': 0.17658,
        'label_index': 2,
        'output_name': 'probability',
        'approx_error': 0.02
    }

    fake_explanation_dict = {
        'attributions_by_label': [fake_attr_dict_1, fake_attr_dict_2]
    }

    modality_input_list_map = {
        constants.ALL_MODALITY: ['data', 'test'],
        explain_metadata.Modality.NUMERIC: ['data', 'test']
    }

    self.explanation = explanation.Explanation.from_ai_platform_response(
        fake_explanation_dict, {}, modality_input_list_map)

  def test_get_attribution_no_class_index(self):
    target_class_attr = self.explanation.get_attribution()
    self.assertEqual(target_class_attr.label_index, 170)

  def test_get_attribution_with_class_index(self):
    target_class_attr = self.explanation.get_attribution(class_index=2)
    self.assertEqual(target_class_attr.label_index, 2)

  def test_get_attribution_with_non_existing_class_index(self):
    with self.assertRaises(KeyError):
      self.explanation.get_attribution(class_index=9)

  def test_get_approx_error_no_class_index(self):
    target_class_attr = self.explanation.get_attribution()
    self.assertTrue(np.isclose(target_class_attr.approx_error, 0.033))

  def test_feature_importance_no_class_index(self):

    importance_dict = self.explanation.feature_importance()
    self.assertTrue(np.isclose(importance_dict['data'], 0.06))
    self.assertTrue(np.isclose(importance_dict['test'], 0.6))
    self.assertIsInstance(importance_dict['test'], float)

  def test_feature_importance_with_class_index(self):

    importance_dict = self.explanation.feature_importance(class_index=2)
    self.assertTrue(np.isclose(importance_dict['data'], 0.44))

  def test_as_tensor_no_class_index(self):

    tensor_dict = self.explanation.as_tensors()
    self.assertTrue(
        np.array_equal(tensor_dict['data'], np.asarray([0.01, 0.02, 0.03])))

  def test_as_tensor_with_class_index(self):

    tensor_dict = self.explanation.as_tensors(class_index=2)
    self.assertTrue(
        np.array_equal(tensor_dict['test'], np.asarray([0.05, 0.02, 0.23])))

  @mock.patch.object(plt, 'show', autospec=True)
  def test_visualize_top_k_features(self, mock_show):
    self.explanation.visualize_top_k_features()
    self.assertTrue(mock_show.called)

  @mock.patch.object(plt, 'show', autospec=True)
  def test_visualize_attributions(self, mock_show):
    self.explanation.visualize_attributions()
    self.assertTrue(mock_show.called)


if __name__ == '__main__':
  tf.test.main()