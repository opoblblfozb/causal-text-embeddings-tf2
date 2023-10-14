# dynamic word piece masking for BERT models
# approximately adapted from The Google AI Language Team Authors (Apache 2018)

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections

import tensorflow as tf

MaskedLmInstance = collections.namedtuple("MaskedLmInstance",
                                          ["index", "label"])


def create_masked_lm_predictions(token_ids, masked_lm_prob, max_predictions_per_seq, vocab, seed):
    """Creates the predictions for the masked LM objective.

    This should be essentially equivalent to the bits that Bert loads from pre-processed tfrecords

    Except: we just include masks instead of randomly letting the words through or randomly replacing
    """

    basic_mask = tf.less(
        tf.random.uniform(token_ids.shape, minval=0, maxval=1, dtype=tf.float32, seed=seed),
        masked_lm_prob)

    # don't mask special characters or padding
    cand_indexes = tf.logical_and(tf.not_equal(token_ids, vocab["[CLS]"]),
                                  tf.not_equal(token_ids, vocab["[SEP]"]))
    cand_indexes = tf.logical_and(cand_indexes, tf.not_equal(token_ids, 0))
    mask = tf.logical_and(cand_indexes, basic_mask)

    # sometimes nothing gets masked. In that case, just mask the first valid token
    masked_lm_positions = tf.cond(pred=tf.reduce_any(mask),
                                  true_fn=lambda: tf.where(mask),
                                  false_fn=lambda: tf.where(cand_indexes)[0:2])

    masked_lm_positions = masked_lm_positions[:, 0]

    # truncate to max predictions for ease of padding
    masked_lm_positions = tf.random.shuffle(masked_lm_positions, seed=seed)
    masked_lm_positions = masked_lm_positions[0:max_predictions_per_seq]
    masked_lm_positions = tf.cast(masked_lm_positions, dtype=tf.int32)
    masked_lm_ids = tf.gather(token_ids, masked_lm_positions)

    mask = tf.cast(
        tf.scatter_nd(tf.expand_dims(masked_lm_positions, 1), tf.ones_like(masked_lm_positions), token_ids.shape),
        bool)

    output_ids = tf.where(mask, vocab["[MASK]"] * tf.ones_like(token_ids), token_ids)

    # pad out to max_predictions_per_seq
    masked_lm_weights = tf.ones_like(masked_lm_ids, dtype=tf.float32)  # tracks padding
    add_pad = [[0, max_predictions_per_seq - tf.shape(input=masked_lm_positions)[0]]]
    masked_lm_weights = tf.pad(tensor=masked_lm_weights, paddings=add_pad, mode='constant')
    masked_lm_positions = tf.pad(tensor=masked_lm_positions, paddings=add_pad, mode='constant')
    masked_lm_ids = tf.pad(tensor=masked_lm_ids, paddings=add_pad, mode='constant')

    return output_ids, masked_lm_positions, masked_lm_ids, masked_lm_weights


def main(_):
    pass


if __name__ == "__main__":
    main()
