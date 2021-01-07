import tensorflow as tf
from constants import INPUT_PADDING_TOKEN, LABEL_PAD, INPUT_PAD
from data_generator import ReturnsDataGen
from clickstream_model import ClickstreamModel
import os


def parse_seq_example(x, context_feature_spec, sequence_feature_spec):
    parsed_context, parsed_sequence = tf.io.parse_single_sequence_example(
        serialized=x,
        context_features=context_feature_spec,
        sequence_features=sequence_feature_spec
    )

    # A VarLenFeature is always parsed to a SparseTensor
    for key in parsed_sequence.keys():
        if isinstance(parsed_sequence[key], tf.SparseTensor):
            parsed_sequence[key] = tf.sparse.to_dense(parsed_sequence[key])
    return {**parsed_context, **parsed_sequence}


def parse_examples(serialized_example, feature_spec):
    features = tf.io.parse_example(serialized=serialized_example, features=feature_spec)

    # A VarLenFeature is always parsed to a SparseTensor
    for key in features.keys():
        if isinstance(features[key], tf.SparseTensor):
            features[key] = tf.sparse.to_dense(features[key])

    return features


def random_choice(x, size, axis=0):
    """
    This is the equivalent of np.random.choice. But it always returns unique choices, i.e. keep_features=False in numpy
    """
    dim_x = tf.cast(tf.shape(x)[axis], tf.int64)
    indices = tf.range(0, dim_x, dtype=tf.int64)
    sample_index = tf.random.shuffle(indices)[:size]
    sample = tf.gather(x, sample_index, axis=axis)

    return sample, sample_index


def assign_labels(feature_dict, reduce_basket=False):
    # returned_quantity may contain pads (depending on where it is placed in the input pipeline) which should be kept.
    labels = tf.clip_by_value(  # clipping avoids type mismatches that happen with tf.where
        feature_dict['returned_quantity'],
        clip_value_min=LABEL_PAD,  # pad = -1
        clip_value_max=1.0  # label=1 if returned_quantity >= 1 else 0
    )
    if reduce_basket:
        labels = tf.reduce_max(labels)
    feature_dict['labels'] = tf.cast(labels, tf.float32)  # Labels need to be float
    return feature_dict


def format_labels(feature_dict):
    """
        This mapping is applied after batching. It adds padding (-1) to the left of labels (which are associated with
        seq_2 items). The seq_1 component does not have labels and should be assigned pads instead of labels.
    """
    batch_size = tf.shape(feature_dict['event_name'])[0]
    session_len = tf.shape(feature_dict['event_name'])[1]

    session_and_tokens_label_padding = tf.fill(
        dims=(batch_size, session_len+2),  # +2 allows for the two tokens (CLS and SEP) that will be in the input tensor
        value=LABEL_PAD
    )
    left_padded_labels = tf.concat([
        session_and_tokens_label_padding,
        feature_dict['labels']
    ], axis=1)

    feature_dict['labels'] = left_padded_labels

    return feature_dict


def load_vocabulary(vocab_file):
    with tf.io.gfile.GFile(vocab_file, 'r') as f:
        return tf.strings.strip(f.readlines())


def create_tf_dataset(source, training, batch_size):

    if isinstance(source, str):
        filenames = tf.data.Dataset.list_files(source)
        dataset = tf.data.TFRecordDataset(filenames=filenames)

        context_feature_spec = {
            'userID': tf.io.FixedLenFeature([], tf.int64)
        }
        sequence_feature_spec = {
            'basket': tf.io.VarLenFeature(tf.int64)
        }

        def parse_fn(ex):
            return parse_seq_example(ex, context_feature_spec, sequence_feature_spec)

        dataset = dataset.map(parse_fn, num_parallel_calls=tf.data.experimental.AUTOTUNE)

    # elif callable(source):
    #     # ToDo: It might be possible to define this only once (like above) and deduce type and shape from that
    #     #  so that we can unify this with the one above
    #     data_types = {
    #         'seq_1_items': tf.string,
    #         'seq_1_events': tf.string,
    #         'seq_2_items': tf.string,
    #         'seq_2_events': tf.string,
    #         'side_feature_1': tf.float32,
    #         'label': tf.float32  # Label needs to be float for calculations in the loss function. int won't work
    #     }
    #     tensor_shapes = {
    #         'seq_1_items': tf.TensorShape([None]),
    #         'seq_1_events': tf.TensorShape([None]),
    #         'seq_2_items': tf.TensorShape([None]),
    #         'seq_2_events': tf.TensorShape([None]),
    #         'side_feature_1': tf.TensorShape([]),
    #         'label': tf.TensorShape([None])
    #     }
    #
    #     dataset = tf.data.Dataset.from_generator(source,
    #                                              output_types=data_types,
    #                                              output_shapes=tensor_shapes)

    else:
        raise TypeError('Source must be either str or callable.')

    # Shuffle then repeat. Batch should always be after these two (https://stackoverflow.com/a/49916221/4936825)
    if training:
        dataset = dataset.shuffle(buffer_size=1000, reshuffle_each_iteration=True)

    dataset = dataset.repeat(None)

    dataset = dataset.padded_batch(
        batch_size=batch_size,
        padded_shapes={  # Pad all to longest in batch
            'userID': [],
            'basket': [None, None]
        },
        padding_values={
            'userID': tf.cast(INPUT_PAD, tf.int64),
            'basket': tf.cast(INPUT_PAD, tf.int64)
        }
    )

    # TODO: TensorFlow docs mention wrapping map functions in py_function. I don't know if it is for optimization or
    #  just for eager mode compatibility. But check that out.

    # TODO: Figure out caching. This doesn't work right now.
    # dataset = dataset.cache()  # Cache to memory to speed up subsequent reads
    # def temp_pop_extras(features):
    #     # features.pop('side_feature_1')
    #     features.pop('seq_1_events')
    #     features.pop('seq_2_events')
    #     return features
    #
    # dataset = dataset.map(temp_pop_extras, num_parallel_calls=tf.data.experimental.AUTOTUNE)

    # def pop_labels(feature_dict):
    #     labels = feature_dict.pop('label')
    #     return feature_dict, labels
    #
    # dataset = dataset.map(pop_labels, num_parallel_calls=tf.data.experimental.AUTOTUNE)

    # TODO: Consider interleave as well
    dataset = dataset.prefetch(tf.data.experimental.AUTOTUNE)

    return dataset


def print_features(x, select=None):
    for k in x:
        if (select is None) or (k in select):
            print(k)
            print('\t', x[k])
            print('*'*80)


def dataset_benchmark(dataset, n_steps):
    import time
    start_time = time.perf_counter()
    for x in dataset.take(n_steps):
        time.sleep(0.01)  # train step
    return time.perf_counter() - start_time


if __name__ == '__main__':
    # data_gen = ClickStreamGenerator(
    #     n_items=1000,
    #     n_events=10,
    #     session_cohesiveness=5,
    #     positive_rate=0.5,
    #     write_vocab_files=True,
    #     vocab_dir='../data/vocabs'
    # )

    data = create_tf_dataset(
        source='../data_prep/test_data_0_of_1.tfrecord',
        training=True,
        batch_size=20
    )

    sequential_input_config = {
        'items': ['seq_1_items', 'seq_2_items'],
        # 'events': ['seq_1_events', 'seq_2_events']
    }

    feature_vocabularies = {
        'items': '../data/vocabs/item_vocab.txt',
        # 'events': '../data/vocabs/event_vocab.txt'
    }

    embedding_dims = {
        'items': 4,
        # 'events': 2
    }

    # clickstream_model = ClickstreamModel(
    #     sequential_input_config=sequential_input_config,
    #     feature_vocabs=feature_vocabularies,
    #     embedding_dims=embedding_dims,
    #     segment_to_head=2,
    #     num_encoder_layers=1,
    #     num_attention_heads=1,
    #     dropout_rate=0.1,
    #     final_layers_dims=[10, 5]
    # )

    from pprint import pprint
    for x in data.take(1):
        pprint(x)

