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
import sys
import numpy as np
import paddle.v2 as paddle
import paddle.fluid as fluid
import paddle.fluid.framework as framework
import paddle.fluid.layers as layers
import paddle.fluid.nets as nets
from paddle.fluid.executor import Executor
from paddle.fluid.optimizer import SGDOptimizer

IS_SPARSE = True
USE_GPU = False
BATCH_SIZE = 256


def get_usr_combined_features():
    # FIXME(dzh) : old API integer_value(10) may has range check.
    # currently we don't have user configurated check.

    USR_DICT_SIZE = paddle.dataset.movielens.max_user_id() + 1

    uid = layers.data(name='user_id', shape=[1], dtype='int64')

    usr_emb = layers.embedding(
        input=uid,
        dtype='float32',
        size=[USR_DICT_SIZE, 32],
        param_attr='user_table',
        is_sparse=IS_SPARSE)

    usr_fc = layers.fc(input=usr_emb, size=32)

    USR_GENDER_DICT_SIZE = 2

    usr_gender_id = layers.data(name='gender_id', shape=[1], dtype='int64')

    usr_gender_emb = layers.embedding(
        input=usr_gender_id,
        size=[USR_GENDER_DICT_SIZE, 16],
        param_attr='gender_table',
        is_sparse=IS_SPARSE)

    usr_gender_fc = layers.fc(input=usr_gender_emb, size=16)

    USR_AGE_DICT_SIZE = len(paddle.dataset.movielens.age_table)
    usr_age_id = layers.data(name='age_id', shape=[1], dtype="int64")

    usr_age_emb = layers.embedding(
        input=usr_age_id,
        size=[USR_AGE_DICT_SIZE, 16],
        is_sparse=IS_SPARSE,
        param_attr='age_table')

    usr_age_fc = layers.fc(input=usr_age_emb, size=16)

    USR_JOB_DICT_SIZE = paddle.dataset.movielens.max_job_id() + 1
    usr_job_id = layers.data(name='job_id', shape=[1], dtype="int64")

    usr_job_emb = layers.embedding(
        input=usr_job_id,
        size=[USR_JOB_DICT_SIZE, 16],
        param_attr='job_table',
        is_sparse=IS_SPARSE)

    usr_job_fc = layers.fc(input=usr_job_emb, size=16)

    concat_embed = layers.concat(
        input=[usr_fc, usr_gender_fc, usr_age_fc, usr_job_fc], axis=1)

    usr_combined_features = layers.fc(input=concat_embed, size=200, act="tanh")

    return usr_combined_features


def get_mov_combined_features():

    MOV_DICT_SIZE = paddle.dataset.movielens.max_movie_id() + 1

    mov_id = layers.data(name='movie_id', shape=[1], dtype='int64')

    mov_emb = layers.embedding(
        input=mov_id,
        dtype='float32',
        size=[MOV_DICT_SIZE, 32],
        param_attr='movie_table',
        is_sparse=IS_SPARSE)

    mov_fc = layers.fc(input=mov_emb, size=32)

    CATEGORY_DICT_SIZE = len(paddle.dataset.movielens.movie_categories())

    category_id = layers.data(
        name='category_id', shape=[1], dtype='int64', lod_level=1)

    mov_categories_emb = layers.embedding(
        input=category_id, size=[CATEGORY_DICT_SIZE, 32], is_sparse=IS_SPARSE)

    mov_categories_hidden = layers.sequence_pool(
        input=mov_categories_emb, pool_type="sum")

    MOV_TITLE_DICT_SIZE = len(paddle.dataset.movielens.get_movie_title_dict())

    mov_title_id = layers.data(
        name='movie_title', shape=[1], dtype='int64', lod_level=1)

    mov_title_emb = layers.embedding(
        input=mov_title_id, size=[MOV_TITLE_DICT_SIZE, 32], is_sparse=IS_SPARSE)

    mov_title_conv = nets.sequence_conv_pool(
        input=mov_title_emb,
        num_filters=32,
        filter_size=3,
        act="tanh",
        pool_type="sum")

    concat_embed = layers.concat(
        input=[mov_fc, mov_categories_hidden, mov_title_conv], axis=1)

    # FIXME(dzh) : need tanh operator
    mov_combined_features = layers.fc(input=concat_embed, size=200, act="tanh")

    return mov_combined_features


def model():
    usr_combined_features = get_usr_combined_features()
    mov_combined_features = get_mov_combined_features()

    # need cos sim
    inference = layers.cos_sim(X=usr_combined_features, Y=mov_combined_features)
    scale_infer = layers.scale(x=inference, scale=5.0)

    label = layers.data(name='score', shape=[1], dtype='float32')
    square_cost = layers.square_error_cost(input=scale_infer, label=label)
    avg_cost = layers.mean(square_cost)

    return scale_infer, avg_cost


def train(use_cuda, save_dirname):
    scale_infer, avg_cost = model()

    # test program
    test_program = fluid.default_main_program().clone()

    sgd_optimizer = SGDOptimizer(learning_rate=0.2)
    opts = sgd_optimizer.minimize(avg_cost)

    place = fluid.CUDAPlace(0) if use_cuda else fluid.CPUPlace()

    exe = Executor(place)
    exe.run(framework.default_startup_program())

    train_reader = paddle.batch(
        paddle.reader.shuffle(
            paddle.dataset.movielens.train(), buf_size=8192),
        batch_size=BATCH_SIZE)
    test_reader = paddle.batch(
        paddle.dataset.movielens.test(), batch_size=BATCH_SIZE)

    feeding = {
        'user_id': 0,
        'gender_id': 1,
        'age_id': 2,
        'job_id': 3,
        'movie_id': 4,
        'category_id': 5,
        'movie_title': 6,
        'score': 7
    }

    def func_feed(feeding, data):
        feed_tensors = {}
        for (key, idx) in feeding.iteritems():
            tensor = fluid.LoDTensor()
            if key != "category_id" and key != "movie_title":
                if key == "score":
                    numpy_data = np.array(map(lambda x: x[idx], data)).astype(
                        "float32")
                else:
                    numpy_data = np.array(map(lambda x: x[idx], data)).astype(
                        "int64")
            else:
                numpy_data = map(lambda x: np.array(x[idx]).astype("int64"),
                                 data)
                lod_info = [len(item) for item in numpy_data]
                offset = 0
                lod = [offset]
                for item in lod_info:
                    offset += item
                    lod.append(offset)
                numpy_data = np.concatenate(numpy_data, axis=0)
                tensor.set_lod([lod])

            numpy_data = numpy_data.reshape([numpy_data.shape[0], 1])
            tensor.set(numpy_data, place)
            feed_tensors[key] = tensor
        return feed_tensors

    PASS_NUM = 100
    for pass_id in range(PASS_NUM):
        for batch_id, data in enumerate(train_reader()):
            # train a mini-batch
            outs = exe.run(program=fluid.default_main_program(),
                           feed=func_feed(feeding, data),
                           fetch_list=[avg_cost])
            out = np.array(outs[0])
            if (batch_id + 1) % 10 == 0:
                avg_cost_set = []
                for test_data in test_reader():
                    avg_cost_np = exe.run(program=test_program,
                                          feed=func_feed(feeding, test_data),
                                          fetch_list=[avg_cost])
                    avg_cost_set.append(avg_cost_np[0])
                    break  # test only 1 segment for speeding up CI

                # get test avg_cost
                test_avg_cost = np.array(avg_cost_set).mean()
                if test_avg_cost < 6.0:
                    # if avg_cost less than 6.0, we think our code is good.
                    if save_dirname is not None:
                        fluid.io.save_inference_model(save_dirname, [
                            "user_id", "gender_id", "age_id", "job_id",
                            "movie_id", "category_id", "movie_title"
                        ], [scale_infer], exe)
                    return

            if math.isnan(float(out[0])):
                sys.exit("got NaN loss, training failed.")


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

    def create_lod_tensor(data, lod=None):
        tensor = fluid.LoDTensor()
        if lod is None:
            # Tensor, the shape is [batch_size, 1]
            index = 0
            lod_0 = [index]
            for l in range(len(data)):
                index += 1
                lod_0.append(index)
            lod = [lod_0]
        tensor.set_lod(lod)

        flattened_data = np.concatenate(data, axis=0).astype("int64")
        flattened_data = flattened_data.reshape([len(flattened_data), 1])
        tensor.set(flattened_data, place)
        return tensor

    # Use the first data from paddle.dataset.movielens.test() as input
    assert feed_target_names[0] == "user_id"
    user_id = create_lod_tensor([[1]])

    assert feed_target_names[1] == "gender_id"
    gender_id = create_lod_tensor([[1]])

    assert feed_target_names[2] == "age_id"
    age_id = create_lod_tensor([[0]])

    assert feed_target_names[3] == "job_id"
    job_id = create_lod_tensor([[10]])

    assert feed_target_names[4] == "movie_id"
    movie_id = create_lod_tensor([[783]])

    assert feed_target_names[5] == "category_id"
    category_id = create_lod_tensor([[10], [8], [9]], [[0, 3]])

    assert feed_target_names[6] == "movie_title"
    movie_title = create_lod_tensor([[1069], [4140], [2923], [710], [988]],
                                    [[0, 5]])

    # Construct feed as a dictionary of {feed_target_name: feed_target_data}
    # and results will contain a list of data corresponding to fetch_targets.
    results = exe.run(inference_program,
                      feed={
                          feed_target_names[0]: user_id,
                          feed_target_names[1]: gender_id,
                          feed_target_names[2]: age_id,
                          feed_target_names[3]: job_id,
                          feed_target_names[4]: movie_id,
                          feed_target_names[5]: category_id,
                          feed_target_names[6]: movie_title
                      },
                      fetch_list=fetch_targets,
                      return_numpy=False)
    print("inferred score: ", np.array(results[0]))


def main(use_cuda):
    if use_cuda and not fluid.core.is_compiled_with_cuda():
        return

    # Directory for saving the inference model
    save_dirname = "recommender_system.inference.model"

    train(use_cuda, save_dirname)
    infer(use_cuda, save_dirname)


if __name__ == '__main__':
    main(USE_GPU)
