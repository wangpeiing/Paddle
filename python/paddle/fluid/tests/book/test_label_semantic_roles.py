#   Copyright (c) 2018 PaddlePaddle Authors. All Rights Reserved.
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

import math

import numpy as np
import paddle.v2 as paddle
import paddle.v2.dataset.conll05 as conll05
import paddle.fluid as fluid
from paddle.fluid.initializer import init_on_cpu
import contextlib
import time
import unittest

word_dict, verb_dict, label_dict = conll05.get_dict()
word_dict_len = len(word_dict)
label_dict_len = len(label_dict)
pred_len = len(verb_dict)

mark_dict_len = 2
word_dim = 32
mark_dim = 5
hidden_dim = 512
depth = 8
mix_hidden_lr = 1e-3

IS_SPARSE = True
PASS_NUM = 10
BATCH_SIZE = 10

embedding_name = 'emb'


def load_parameter(file_name, h, w):
    with open(file_name, 'rb') as f:
        f.read(16)  # skip header.
        return np.fromfile(f, dtype=np.float32).reshape(h, w)


def db_lstm(word, predicate, ctx_n2, ctx_n1, ctx_0, ctx_p1, ctx_p2, mark,
            **ignored):
    # 8 features
    predicate_embedding = fluid.layers.embedding(
        input=predicate,
        size=[pred_len, word_dim],
        dtype='float32',
        is_sparse=IS_SPARSE,
        param_attr='vemb')

    mark_embedding = fluid.layers.embedding(
        input=mark,
        size=[mark_dict_len, mark_dim],
        dtype='float32',
        is_sparse=IS_SPARSE)

    word_input = [word, ctx_n2, ctx_n1, ctx_0, ctx_p1, ctx_p2]
    emb_layers = [
        fluid.layers.embedding(
            size=[word_dict_len, word_dim],
            input=x,
            param_attr=fluid.ParamAttr(
                name=embedding_name, trainable=False)) for x in word_input
    ]
    emb_layers.append(predicate_embedding)
    emb_layers.append(mark_embedding)

    hidden_0_layers = [
        fluid.layers.fc(input=emb, size=hidden_dim) for emb in emb_layers
    ]

    hidden_0 = fluid.layers.sums(input=hidden_0_layers)

    lstm_0 = fluid.layers.dynamic_lstm(
        input=hidden_0,
        size=hidden_dim,
        candidate_activation='relu',
        gate_activation='sigmoid',
        cell_activation='sigmoid')

    # stack L-LSTM and R-LSTM with direct edges
    input_tmp = [hidden_0, lstm_0]

    for i in range(1, depth):
        mix_hidden = fluid.layers.sums(input=[
            fluid.layers.fc(input=input_tmp[0], size=hidden_dim),
            fluid.layers.fc(input=input_tmp[1], size=hidden_dim)
        ])

        lstm = fluid.layers.dynamic_lstm(
            input=mix_hidden,
            size=hidden_dim,
            candidate_activation='relu',
            gate_activation='sigmoid',
            cell_activation='sigmoid',
            is_reverse=((i % 2) == 1))

        input_tmp = [mix_hidden, lstm]

    feature_out = fluid.layers.sums(input=[
        fluid.layers.fc(input=input_tmp[0], size=label_dict_len),
        fluid.layers.fc(input=input_tmp[1], size=label_dict_len)
    ])

    return feature_out


def to_lodtensor(data, place):
    seq_lens = [len(seq) for seq in data]
    cur_len = 0
    lod = [cur_len]
    for l in seq_lens:
        cur_len += l
        lod.append(cur_len)
    flattened_data = np.concatenate(data, axis=0).astype("int64")
    flattened_data = flattened_data.reshape([len(flattened_data), 1])
    res = fluid.LoDTensor()
    res.set(flattened_data, place)
    res.set_lod([lod])
    return res


def create_random_lodtensor(lod, place, low, high):
    data = np.random.random_integers(low, high, [lod[-1], 1]).astype("int64")
    res = fluid.LoDTensor()
    res.set(data, place)
    res.set_lod([lod])
    return res


def train(use_cuda, save_dirname=None):
    # define network topology
    word = fluid.layers.data(
        name='word_data', shape=[1], dtype='int64', lod_level=1)
    predicate = fluid.layers.data(
        name='verb_data', shape=[1], dtype='int64', lod_level=1)
    ctx_n2 = fluid.layers.data(
        name='ctx_n2_data', shape=[1], dtype='int64', lod_level=1)
    ctx_n1 = fluid.layers.data(
        name='ctx_n1_data', shape=[1], dtype='int64', lod_level=1)
    ctx_0 = fluid.layers.data(
        name='ctx_0_data', shape=[1], dtype='int64', lod_level=1)
    ctx_p1 = fluid.layers.data(
        name='ctx_p1_data', shape=[1], dtype='int64', lod_level=1)
    ctx_p2 = fluid.layers.data(
        name='ctx_p2_data', shape=[1], dtype='int64', lod_level=1)
    mark = fluid.layers.data(
        name='mark_data', shape=[1], dtype='int64', lod_level=1)
    feature_out = db_lstm(**locals())
    target = fluid.layers.data(
        name='target', shape=[1], dtype='int64', lod_level=1)
    crf_cost = fluid.layers.linear_chain_crf(
        input=feature_out,
        label=target,
        param_attr=fluid.ParamAttr(
            name='crfw', learning_rate=mix_hidden_lr))
    avg_cost = fluid.layers.mean(crf_cost)

    # TODO(qiao)
    # check other optimizers and check why out will be NAN
    global_step = fluid.layers.create_global_var(
        shape=[1], value=0, dtype='float32', force_cpu=True, persistable=True)
    sgd_optimizer = fluid.optimizer.SGD(
        learning_rate=fluid.learning_rate_decay.exponential_decay(
            learning_rate=0.0001,
            global_step=global_step,
            decay_steps=100000,
            decay_rate=0.5,
            staircase=True),
        global_step=global_step)
    sgd_optimizer.minimize(avg_cost)

    # TODO(qiao)
    # add dependency track and move this config before optimizer
    crf_decode = fluid.layers.crf_decoding(
        input=feature_out, param_attr=fluid.ParamAttr(name='crfw'))

    chunk_evaluator = fluid.evaluator.ChunkEvaluator(
        input=crf_decode,
        label=target,
        chunk_scheme="IOB",
        num_chunk_types=int(math.ceil((label_dict_len - 1) / 2.0)))

    train_data = paddle.batch(
        paddle.reader.shuffle(
            paddle.dataset.conll05.test(), buf_size=8192),
        batch_size=BATCH_SIZE)

    place = fluid.CUDAPlace(0) if use_cuda else fluid.CPUPlace()
    feeder = fluid.DataFeeder(
        feed_list=[
            word, ctx_n2, ctx_n1, ctx_0, ctx_p1, ctx_p2, predicate, mark, target
        ],
        place=place)
    exe = fluid.Executor(place)

    exe.run(fluid.default_startup_program())

    embedding_param = fluid.global_scope().find_var(embedding_name).get_tensor()
    embedding_param.set(
        load_parameter(conll05.get_embedding(), word_dict_len, word_dim), place)

    start_time = time.time()
    batch_id = 0
    for pass_id in xrange(PASS_NUM):
        chunk_evaluator.reset(exe)
        for data in train_data():
            cost, precision, recall, f1_score = exe.run(
                fluid.default_main_program(),
                feed=feeder.feed(data),
                fetch_list=[avg_cost] + chunk_evaluator.metrics)
            pass_precision, pass_recall, pass_f1_score = chunk_evaluator.eval(
                exe)

            if batch_id % 10 == 0:
                print("avg_cost:" + str(cost) + " precision:" + str(
                    precision) + " recall:" + str(recall) + " f1_score:" + str(
                        f1_score) + " pass_precision:" + str(
                            pass_precision) + " pass_recall:" + str(pass_recall)
                      + " pass_f1_score:" + str(pass_f1_score))
                if batch_id != 0:
                    print("second per batch: " + str((time.time() - start_time)
                                                     / batch_id))
                # Set the threshold low to speed up the CI test
                if float(pass_precision) > 0.05:
                    if save_dirname is not None:
                        fluid.io.save_inference_model(save_dirname, [
                            'word_data', 'verb_data', 'ctx_n2_data',
                            'ctx_n1_data', 'ctx_0_data', 'ctx_p1_data',
                            'ctx_p2_data', 'mark_data'
                        ], [feature_out], exe)
                    return

            batch_id = batch_id + 1


def infer(use_cuda, save_dirname=None):
    if save_dirname is None:
        return

    place = fluid.CUDAPlace(0) if use_cuda else fluid.CPUPlace()
    exe = fluid.Executor(place)

    # Use fluid.io.load_inference_model to obtain the inference program desc,
    # the feed_target_names (the names of variables that will be feeded 
    # data using feed operators), and the fetch_targets (variables that 
    # we want to obtain data from using fetch operators).
    [inference_program, feed_target_names,
     fetch_targets] = fluid.io.load_inference_model(save_dirname, exe)

    lod = [0, 4, 10]
    ts_word = create_random_lodtensor(lod, place, low=0, high=1)
    ts_pred = create_random_lodtensor(lod, place, low=0, high=1)
    ts_ctx_n2 = create_random_lodtensor(lod, place, low=0, high=1)
    ts_ctx_n1 = create_random_lodtensor(lod, place, low=0, high=1)
    ts_ctx_0 = create_random_lodtensor(lod, place, low=0, high=1)
    ts_ctx_p1 = create_random_lodtensor(lod, place, low=0, high=1)
    ts_ctx_p2 = create_random_lodtensor(lod, place, low=0, high=1)
    ts_mark = create_random_lodtensor(lod, place, low=0, high=1)

    # Construct feed as a dictionary of {feed_target_name: feed_target_data}
    # and results will contain a list of data corresponding to fetch_targets.
    assert feed_target_names[0] == 'word_data'
    assert feed_target_names[1] == 'verb_data'
    assert feed_target_names[2] == 'ctx_n2_data'
    assert feed_target_names[3] == 'ctx_n1_data'
    assert feed_target_names[4] == 'ctx_0_data'
    assert feed_target_names[5] == 'ctx_p1_data'
    assert feed_target_names[6] == 'ctx_p2_data'
    assert feed_target_names[7] == 'mark_data'

    results = exe.run(inference_program,
                      feed={
                          feed_target_names[0]: ts_word,
                          feed_target_names[1]: ts_pred,
                          feed_target_names[2]: ts_ctx_n2,
                          feed_target_names[3]: ts_ctx_n1,
                          feed_target_names[4]: ts_ctx_0,
                          feed_target_names[5]: ts_ctx_p1,
                          feed_target_names[6]: ts_ctx_p2,
                          feed_target_names[7]: ts_mark
                      },
                      fetch_list=fetch_targets,
                      return_numpy=False)
    print(results[0].lod())
    np_data = np.array(results[0])
    print("Inference Shape: ", np_data.shape)
    print("Inference results: ", np_data)


def main(use_cuda):
    if use_cuda and not fluid.core.is_compiled_with_cuda():
        return

    # Directory for saving the trained model
    save_dirname = "label_semantic_roles.inference.model"

    train(use_cuda, save_dirname)
    infer(use_cuda, save_dirname)


class TestLabelSemanticRoles(unittest.TestCase):
    def test_cuda(self):
        with self.scope_prog_guard():
            main(use_cuda=True)

    def test_cpu(self):
        with self.scope_prog_guard():
            main(use_cuda=False)

    @contextlib.contextmanager
    def scope_prog_guard(self):
        prog = fluid.Program()
        startup_prog = fluid.Program()
        scope = fluid.core.Scope()
        with fluid.scope_guard(scope):
            with fluid.program_guard(prog, startup_prog):
                yield


if __name__ == '__main__':
    unittest.main()
