import sys
import time
from typing import List, Tuple
from io import StringIO
import platform

# logger
from logging import getLogger  # noqa

import numpy as np
import cv2
from PIL import Image

import ailia

# import original modules
sys.path.append("../../util")
from arg_utils import get_base_parser, update_parser  # noqa
from model_utils import check_and_download_models, check_and_download_file  # noqa
from detector_utils import load_image  # noqa
from math_utils import softmax

from logit_process import logits_processor

logger = getLogger(__name__)

# ======================
# Parameters
# ======================

REMOTE_PATH = "https://storage.googleapis.com/ailia-models/qwen_audio/"

AUDIO_PATH = "1272-128104-0000.flac"

COPY_BLOB_DATA = True


# ======================
# Arguemnt Parser Config
# ======================

parser = get_base_parser("Qwen-Audio", AUDIO_PATH, None, large_model=True)
parser.add_argument(
    "-p",
    "--prompt",
    type=str,
    default="what does the person say?",
    help="prompt",
)
parser.add_argument(
    "--disable_ailia_tokenizer", action="store_true", help="disable ailia tokenizer."
)
parser.add_argument(
    "--temperature",
    type=float,
    default=0.01,
    help="temperature from generation_config.json",
)
parser.add_argument(
    "--top_p",
    type=float,
    default=0.001,
    help="top_p from generation_config.json",
)
parser.add_argument(
    "--top_k",
    type=int,
    default=1,
    help="top_k from generation_config.json",
)
parser.add_argument(
    "--max_length",
    type=int,
    default=256,
    help="max_length for generation",
)
parser.add_argument("--onnx", action="store_true", help="execute onnxruntime version.")
args = update_parser(parser)


# ======================
# Model selection
# ======================

WEIGHT_PATH = "Qwen-Audio-Chat.onnx"
WEIGHT_ENC_PATH = "Qwen-Audio-Chat_encode.onnx"
MODEL_PATH = "Qwen-Audio-Chat.onnx.prototxt"
MODEL_ENC_PATH = "Qwen-Audio-Chat_encode.onnx.prototxt"
PB_PATH = "Qwen-Audio-Chat_weights.pb"


# ======================
# Secondary Functions
# ======================


# ======================
# Main functions
# ======================


def audio_encode(models, input_audios, input_audio_lengths, audio_span_tokens):
    real_input_audio_lens = input_audio_lengths[:, 0].tolist()
    max_len_in_batch = max(real_input_audio_lens)
    padding_mask = np.ones([input_audios.shape[0], max_len_in_batch], dtype=np.float16)
    for index in range(len(input_audios)):
        padding_mask[index, : input_audio_lengths[index][0]] = 0

    # feedforward
    net = models["enc"]
    if not args.onnx:
        output = net.predict([input_audios, padding_mask, input_audio_lengths])
    else:
        output = net.run(
            None,
            {
                "input_audios": input_audios,
                "padding_mask": padding_mask,
                "input_audio_lengths": input_audio_lengths,
            },
        )
    x = output[0]

    bos = np.load("bos.npy")
    eos = np.load("eos.npy")

    output_audios = []
    for i in range(len(audio_span_tokens)):
        audio_span = audio_span_tokens[i]
        audio = x[i][: audio_span - 2]
        if bos is not None:
            audio = np.concatenate([bos, audio, eos])
        assert len(audio) == audio_span
        output_audios.append(audio)

    return output_audios


def forward(
    models,
    input_ids: np.ndarray,
    position_ids: np.ndarray,
    attention_mask: np.ndarray,
    audio_info: dict,
    past_key_values: List[np.ndarray],
    blob_copy: bool,
):
    if past_key_values[0].shape[1] == 0:
        audio_start_id = 155163
        bos_pos = np.where(input_ids == audio_start_id)
        eos_pos = np.where(input_ids == audio_start_id + 1)

        audio_pos = np.stack((bos_pos[0], bos_pos[1], eos_pos[1]), axis=1)
        audios = audio_info["input_audios"]
        audio_span_tokens = audio_info["audio_span_tokens"]
        input_audio_lengths = audio_info["input_audio_lengths"]

        audio_encode(models, audios, input_audio_lengths, audio_span_tokens)
    else:
        pass

    if input_ids is None:
        input_ids = np.zeros((1, 0), dtype=np.int64)
    if inputs_embeds is None:
        inputs_embeds = np.zeros((1, 0, 2048), dtype=np.float32)

    net = models["net"]
    if not args.onnx:
        if not blob_copy:
            output = net.predict(
                [
                    input_ids,
                    position_ids,
                    inputs_embeds,
                    *past_key_values,
                ]
            )
            logits, new_past_key_values = output[0], output[1:]
        else:
            NUM_KV = 24
            key_shapes = [
                net.get_blob_shape(
                    net.find_blob_index_by_name("key_cache_out" + str(i))
                )
                for i in range(NUM_KV)
            ]
            value_shapes = [
                net.get_blob_shape(
                    net.find_blob_index_by_name("value_cache_out" + str(i))
                )
                for i in range(NUM_KV)
            ]
            net.set_input_blob_data(input_ids, net.find_blob_index_by_name("input_ids"))
            net.set_input_blob_data(
                inputs_embeds, net.find_blob_index_by_name("inputs_embeds")
            )
            net.set_input_blob_data(
                position_ids, net.find_blob_index_by_name("position_ids")
            )
            for i in range(NUM_KV):
                net.set_input_blob_shape(
                    key_shapes[i], net.find_blob_index_by_name("key_cache" + str(i))
                )
                net.set_input_blob_shape(
                    value_shapes[i], net.find_blob_index_by_name("value_cache" + str(i))
                )
                net.copy_blob_data("key_cache" + str(i), "key_cache_out" + str(i))
                net.copy_blob_data("value_cache" + str(i), "value_cache_out" + str(i))
            net.update()
            logits = net.get_blob_data(net.find_blob_index_by_name("logits"))
            new_past_key_values = [
                net.get_blob_data(net.find_blob_index_by_name("key_cache_out0"))
            ]
    else:
        output = net.run(
            None,
            {
                "input_ids": input_ids,
                "position_ids": position_ids,
                "inputs_embeds": inputs_embeds,
                "key_cache0": past_key_values[0],
                "value_cache0": past_key_values[1],
                "key_cache1": past_key_values[2],
                "value_cache1": past_key_values[3],
                "key_cache2": past_key_values[4],
                "value_cache2": past_key_values[5],
                "key_cache3": past_key_values[6],
                "value_cache3": past_key_values[7],
                "key_cache4": past_key_values[8],
                "value_cache4": past_key_values[9],
                "key_cache5": past_key_values[10],
                "value_cache5": past_key_values[11],
                "key_cache6": past_key_values[12],
                "value_cache6": past_key_values[13],
                "key_cache7": past_key_values[14],
                "value_cache7": past_key_values[15],
                "key_cache8": past_key_values[16],
                "value_cache8": past_key_values[17],
                "key_cache9": past_key_values[18],
                "value_cache9": past_key_values[19],
                "key_cache10": past_key_values[20],
                "value_cache10": past_key_values[21],
                "key_cache11": past_key_values[22],
                "value_cache11": past_key_values[23],
                "key_cache12": past_key_values[24],
                "value_cache12": past_key_values[25],
                "key_cache13": past_key_values[26],
                "value_cache13": past_key_values[27],
                "key_cache14": past_key_values[28],
                "value_cache14": past_key_values[29],
                "key_cache15": past_key_values[30],
                "value_cache15": past_key_values[31],
                "key_cache16": past_key_values[32],
                "value_cache16": past_key_values[33],
                "key_cache17": past_key_values[34],
                "value_cache17": past_key_values[35],
                "key_cache18": past_key_values[36],
                "value_cache18": past_key_values[37],
                "key_cache19": past_key_values[38],
                "value_cache19": past_key_values[39],
                "key_cache20": past_key_values[40],
                "value_cache20": past_key_values[41],
                "key_cache21": past_key_values[42],
                "value_cache21": past_key_values[43],
                "key_cache22": past_key_values[44],
                "value_cache22": past_key_values[45],
                "key_cache23": past_key_values[46],
                "value_cache23": past_key_values[47],
            },
        )
        logits, new_past_key_values = output[0], output[1:]

    return logits, new_past_key_values


def stopping_criteria(input_ids: np.array) -> np.array:
    max_length = 310
    cur_len = input_ids.shape[-1]
    is_done = cur_len >= max_length
    is_done = np.full(input_ids.shape[0], is_done)

    eos_token_id = np.array([7])
    is_done = is_done | np.isin(input_ids[:, -1], eos_token_id)

    return is_done


def sample(models, input_ids, attention_mask, audio_info):
    # pad_token_id = 7

    past_key_values = [np.zeros((1, 0, 32, 128), dtype=np.float32)] * 64

    # keep track of which sequences are already finished
    batch_size, cur_len = input_ids.shape
    this_peer_finished = False
    unfinished_sequences = np.ones(batch_size, dtype=int)
    cache_position = (
        np.cumsum(np.ones_like(input_ids[0, :], dtype=np.int64), axis=0) - 1
    )

    blob_copy = False
    while True:
        # prepare model inputs
        if 0 < past_key_values[0].shape[1]:
            # model_input_ids = input_ids[:, cache_position]
            pass
        else:
            model_input_ids = input_ids
        position_ids = attention_mask.astype(np.int32).cumsum(axis=-1) - 1
        position_ids = np.where(attention_mask == 0, 1, position_ids)
        if 0 < past_key_values[0].shape[1]:
            # position_ids = position_ids[:, -model_input_ids.shape[1] :]
            pass

        if args.benchmark:
            start = int(round(time.time() * 1000))

        logits, past_key_values = forward(
            models,
            model_input_ids,
            position_ids,
            attention_mask,
            audio_info,
            past_key_values,
            blob_copy,
        )
        blob_copy = True if COPY_BLOB_DATA else False

        if args.benchmark:
            end = int(round(time.time() * 1000))
            estimation_time = end - start
            logger.info(f"\tdecode time {estimation_time} ms")

        attention_mask = np.concatenate(
            [attention_mask, np.ones((attention_mask.shape[0], 1), dtype=int)],
            axis=-1,
        )
        cache_position = cache_position[-1:] + 1

        next_token_logits = logits[:, -1, :]

        # pre-process distribution
        next_token_scores = logits_processor(input_ids, next_token_logits)

        # token selection
        probs = softmax(next_token_scores, axis=-1)
        next_tokens = np.random.choice(len(probs[0]), size=1, p=probs[0])

        # finished sentences should have their next token be a padding token
        next_tokens = next_tokens * unfinished_sequences + pad_token_id * (
            1 - unfinished_sequences
        )

        # update generated ids, model inputs, and length for next step
        input_ids = np.concatenate([input_ids, next_tokens[:, None]], axis=-1)

        if streamer:
            streamer.put(next_tokens)

        unfinished_sequences = unfinished_sequences & ~stopping_criteria(input_ids)
        this_peer_finished = np.max(unfinished_sequences) == 0
        cur_len += 1

        if this_peer_finished:
            break

    if streamer is not None:
        streamer.end()

    return input_ids


def predict(models, images, message):
    output = sample(models, input_ids, attention_mask, audio_info)


def recognize(models):
    prompt = args.prompt

    logger.info("Prompt: %s" % prompt)

    # inference
    logger.info("Start inference...")
    if args.benchmark:
        logger.info("BENCHMARK mode")
        total_time_estimation = 0
        for i in range(args.benchmark_count):
            start = int(round(time.time() * 1000))
            output_text = predict(models, None, prompt)
            end = int(round(time.time() * 1000))
            estimation_time = end - start

            # Logging
            logger.info(f"\tailia processing estimation time {estimation_time} ms")
            if i != 0:
                total_time_estimation = total_time_estimation + estimation_time

        logger.info(
            f"\taverage time estimation {total_time_estimation / (args.benchmark_count - 1)} ms"
        )
    else:
        output_text = predict(models, None, prompt)

    if not intermediate:
        print(output_text)

    logger.info("Script finished successfully.")


def main():
    check_and_download_models(WEIGHT_PATH, MODEL_PATH, REMOTE_PATH)
    check_and_download_models(WEIGHT_ENC_PATH, MODEL_ENC_PATH, REMOTE_PATH)
    check_and_download_file(PB_PATH, REMOTE_PATH)

    env_id = args.env_id

    # initialize
    if not args.onnx:
        memory_mode = ailia.get_memory_mode(
            reduce_constant=True,
            ignore_input_with_initializer=True,
            reduce_interstage=False,
            reuse_interstage=True,
        )
        enc = ailia.Net(
            MODEL_ENC_PATH, WEIGHT_ENC_PATH, env_id=env_id, memory_mode=memory_mode
        )
        net = ailia.Net(MODEL_PATH, WEIGHT_PATH, env_id=env_id, memory_mode=memory_mode)
    else:
        import onnxruntime

        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]

        enc = onnxruntime.InferenceSession(WEIGHT_ENC_PATH, providers=providers)
        net = onnxruntime.InferenceSession(WEIGHT_PATH, providers=providers)

    # # args.disable_ailia_tokenizer = True
    # if args.disable_ailia_tokenizer:
    #     import transformers

    #     tokenizer = transformers.Qwen2TokenizerFast.from_pretrained("./tokenizer")
    # else:
    #     from ailia_tokenizer import GPT2Tokenizer

    #     tokenizer = GPT2Tokenizer.from_pretrained("./tokenizer")
    #     tokenizer.add_special_tokens(
    #         {
    #             "additional_special_tokens": [
    #                 "<|end_of_text|>",
    #                 "<|im_start|>",
    #                 "<|im_end|>",
    #                 "<|object_ref_start|>",
    #                 "<|object_ref_end|>",
    #                 "<|box_start|>",
    #                 "<|box_end|>",
    #                 "<|quad_start|>",
    #                 "<|quad_end|>",
    #                 "<|vision_start|>",
    #                 "<|vision_end|>",
    #                 "<|vision_pad|>",
    #                 "<|image_pad|>",
    #                 "<|video_pad|>",
    #             ]
    #         }
    #     )

    models = {
        # "tokenizer": tokenizer,
        "enc": enc,
        "net": net,
    }

    # generate
    recognize(models)


if __name__ == "__main__":
    main()
