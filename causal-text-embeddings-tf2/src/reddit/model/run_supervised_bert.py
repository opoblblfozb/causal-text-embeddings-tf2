# Copyright 2019 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""BERT classification finetuning runner in tf2.0."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import time

import pandas as pd

from absl import app
from absl import flags
import tensorflow as tf

# pylint: disable=g-import-not-at-top,redefined-outer-name,reimported
from tf_official.nlp import bert_modeling as modeling, optimization
# from nlp import bert_models
from tf_official.nlp.bert import tokenization, common_flags, model_saving_utils
from tf_official.utils.misc import tpu_lib
from causal_bert import bert_models

from reddit.dataset.dataset import make_input_fn_from_file

common_flags.define_common_bert_flags()

flags.DEFINE_enum(
    'mode', 'train_and_eval', ['train_and_eval', 'export_only'],
    'One of {"train_and_eval", "export_only"}. `train_and_eval`: '
    'trains the model and evaluates in the meantime. '
    '`export_only`: will take the latest checkpoint inside '
    'model_dir and export a `SavedModel`.')

flags.DEFINE_bool(
    "do_masking", True,
    "Whether to randomly mask input words during training (serves as a sort of regularization)")

flags.DEFINE_string('input_files', None,
                    'File path to retrieve training data for pre-training.')

# Model training specific flags.
flags.DEFINE_string(
    'input_meta_data_path', None,
    'Path to file that contains meta data about input '
    'to be used for training and evaluation.')
flags.DEFINE_integer(
    'max_seq_length', 250,
    'The maximum total input sequence length after WordPiece tokenization. '
    'Sequences longer than this will be truncated, and sequences shorter '
    'than this will be padded.')
flags.DEFINE_integer('max_predictions_per_seq', 20,
                     'Maximum predictions per sequence_output.')

flags.DEFINE_integer('train_batch_size', 32, 'Batch size for training.')
flags.DEFINE_integer('eval_batch_size', 32, 'Batch size for evaluation.')
flags.DEFINE_string(
    'hub_module_url', None, 'TF-Hub path/url to Bert module. '
                            'If specified, init_checkpoint flag should not be used.')

flags.DEFINE_string("vocab_file", None,
                    "The vocabulary file that the BERT model was trained on.")
flags.DEFINE_bool(
    "do_lower_case", True,
    "Whether to lower case the input text. Should be True for uncased "
    "models and False for cased models.")

flags.DEFINE_integer("seed", 0, "Seed for rng.")

# Data splitting details
flags.DEFINE_integer("num_splits", 10,
                     "number of splits")
flags.DEFINE_string("dev_splits", '9', "indices of development splits")
flags.DEFINE_string("test_splits", '9', "indices of test splits")

# Flags specifically related to PeerRead experiment

flags.DEFINE_string(
    "treatment", "theorem_referenced",
    "Covariate used as treatment."
)

flags.DEFINE_string("simulated", 'real', "whether to use real data ('real'), attribute based ('attribute'), "
                                         "or propensity score-based ('propensity') simulation"),
flags.DEFINE_float("beta0", 0.0, "param passed to simulated labeler, treatment strength")
flags.DEFINE_float("beta1", 0.0, "param passed to simulated labeler, confounding strength")
flags.DEFINE_float("gamma", 0.0, "param passed to simulated labeler, noise level")
flags.DEFINE_float("exogenous_confounding", 0.0, "amount of exogenous confounding in propensity based simulation")
flags.DEFINE_string("base_propensities_path", '', "path to .tsv file containing a 'propensity score' for each unit,"
                                                  "used for propensity score-based simulation")

flags.DEFINE_string("simulation_mode", 'simple', "simple, multiplicative, or interaction")

flags.DEFINE_string("prediction_file", "../output/predictions.tsv", "path where predictions (tsv) will be written")

FLAGS = flags.FLAGS


def _make_length_labels(features, labels):
    post_length = tf.reduce_sum(features['input_mask'])
    labels.update({'y':tf.cast(post_length, tf.float32)})
    return features,labels


def make_dataset(is_training: bool, do_masking=False):
    tokenizer = tokenization.FullTokenizer(
        vocab_file=FLAGS.vocab_file, do_lower_case=FLAGS.do_lower_case)

    dev_splits = [int(s) for s in str.split(FLAGS.dev_splits)]
    test_splits = [int(s) for s in str.split(FLAGS.test_splits)]

    train_input_fn = make_input_fn_from_file(
        input_files_or_glob=FLAGS.input_files,
        seq_length=FLAGS.max_seq_length,
        num_splits=FLAGS.num_splits,
        dev_splits=dev_splits,
        test_splits=test_splits,
        tokenizer=tokenizer,
        do_masking=do_masking,
        is_training=is_training,
        shuffle_buffer_size=25000,  # note: bert hardcoded this, and I'm following suit
        seed=FLAGS.seed)

    batch_size = FLAGS.train_batch_size if is_training else FLAGS.eval_batch_size

    dataset = train_input_fn(params={'batch_size': batch_size})
    dataset = dataset.map(_make_length_labels)
    return dataset

def main(_):
    # Users should always run this script under TF 2.x
    assert tf.version.VERSION.startswith('2.')

    # with tf.io.gfile.GFile(FLAGS.input_meta_data_path, 'rb') as reader:
    #     input_meta_data = json.loads(reader.read().decode('utf-8'))

    if not FLAGS.model_dir:
        FLAGS.model_dir = '/tmp/bert20/'
    #
    # Configuration stuff
    #
    bert_config = modeling.BertConfig.from_json_file(FLAGS.bert_config_file)
    epochs = FLAGS.num_train_epochs
    # train_data_size = 11778
    train_data_size = 5000
    steps_per_epoch = int(train_data_size / FLAGS.train_batch_size)  # 368
    warmup_steps = int(epochs * train_data_size * 0.1 / FLAGS.train_batch_size)
    initial_lr = FLAGS.learning_rate

    strategy = None
    if FLAGS.strategy_type == 'mirror':
        strategy = tf.distribute.MirroredStrategy()
    elif FLAGS.strategy_type == 'tpu':
        cluster_resolver = tpu_lib.tpu_initialize(FLAGS.tpu)
        strategy = tf.distribute.experimental.TPUStrategy(cluster_resolver)
    else:
        raise ValueError('The distribution strategy type is not supported: %s' %
                         FLAGS.strategy_type)

    #
    # Modeling and training
    #

    # the model
    def _get_supervised_model():
        supervised_model, core_model = (
            bert_models.classifier_model(
                bert_config,
                float_type=tf.float32,
                num_labels=1
                max_seq_length=128))
        # WARNING: the original optimizer causes a bug where loss increases after first epoch
        # dragon_model.optimizer = optimization.create_optimizer(
        #     FLAGS.train_batch_size * initial_lr, steps_per_epoch * epochs, warmup_steps)
        supervised_model.optimizer = tf.keras.optimizers.SGD(learning_rate=FLAGS.train_batch_size * initial_lr)
        return supervised_model, core_model

    # training. strategy.scope context allows use of multiple devices
    do_training = False
    with strategy.scope():
        keras_train_data = make_dataset(is_training=True, do_masking=FLAGS.do_masking)

        supervised_model, core_model = _get_supervised_model()
        optimizer = supervised_model.optimizer

        if FLAGS.init_checkpoint:
            checkpoint = tf.train.Checkpoint(model=core_model)
            checkpoint.restore(FLAGS.init_checkpoint).assert_existing_objects_matched()

        supervised_model.compile(optimizer=optimizer,
                             loss={'y': 'mean_squared_error'})


        summary_callback = tf.keras.callbacks.TensorBoard(FLAGS.model_dir, update_freq=128)
        checkpoint_dir = os.path.join(FLAGS.model_dir, 'model_checkpoint.{epoch:02d}')
        checkpoint_callback = tf.keras.callbacks.ModelCheckpoint(checkpoint_dir, save_weights_only=True)

        callbacks = [summary_callback, checkpoint_callback]

        supervised_model.fit(
            x=keras_train_data,
            # validation_data=evaluation_dataset,
            steps_per_epoch=steps_per_epoch,
            epochs=epochs,
            # vailidation_steps=eval_steps,
            callbacks=callbacks)

    # save a final model checkpoint (so we can restore weights into model w/o training idiosyncracies)
    if FLAGS.model_export_path:
        model_export_path = FLAGS.model_export_path
    else:
        model_export_path = os.path.join(FLAGS.model_dir, 'trained/supervised.ckpt')

    checkpoint = tf.train.Checkpoint(model=supervised_model)
    saved_path = checkpoint.save(model_export_path)

    # make predictions and write to file
    # NOTE: theory suggests we should make predictions on heldout data ("cross fitting" or "sample splitting")
    # but our experiments showed best results by just reusing the data
    # You can accomodate sample splitting by using the splitting arguments for the dataset creation

    # create data and model w/o masking
    eval_data = make_dataset(is_training=False, do_masking=False)
    supervised_model, core_model = _get_supervised_model()
    # reload the model weights (necessary because we've obliterated the masking)
    checkpoint = tf.train.Checkpoint(model=supervised_model)
    checkpoint.restore(saved_path).assert_existing_objects_matched()
    supervised_model.compile()

    with tf.io.gfile.GFile(FLAGS.prediction_file, "w") as writer:
        attribute_names = ['in_test',
                           'length',
                           'expected_length',
                           'index']
        header = "\t".join(
            attribute_name for attribute_name in attribute_names) + "\n"
        writer.write(header)

        for f, l in eval_data:
            expected_length = supervised_model.predict(f)
            attributes = [l['in_test'].numpy(),
                          expected_length,
                          l['length'].numpy()]
            at_df = pd.DataFrame(attributes).T
            writer.write(at_df.to_csv(sep="\t", header=False))


if __name__ == '__main__':
    flags.mark_flag_as_required('bert_config_file')
    # flags.mark_flag_as_required('input_meta_data_path')
    # flags.mark_flag_as_required('model_dir')
    app.run(main)
