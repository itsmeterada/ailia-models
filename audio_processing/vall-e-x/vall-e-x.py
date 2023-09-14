import sys
import time
from logging import getLogger
import os

import numpy as np
import soundfile as sf

import ailia

# import original modules
sys.path.append('../../util')
from microphone_utils import start_microphone_input  # noqa
from model_utils import check_and_download_models  # noqa
from arg_utils import get_base_parser, get_savepath, update_parser  # noqa

from utils.generation import generate_audio

logger = getLogger(__name__)

# ======================
# Parameters
# ======================
WEIGHT_NAR_DECODER_PATH = "nar_decoder.onnx"
WEIGHT_NAR_PREDICT_LAYERS_PATH = "nar_predict_layers.onnx"
WEIGHT_AR_AUDIO_EMBEDDING_PATH = "ar_audio_embedding.onnx"
WEIGHT_AR_LANGUAGE_EMBEDDING_PATH = "ar_language_embedding.onnx"
WEIGHT_AR_TEXT_EMBEDDING_PATH = "ar_text_embedding.onnx"
WEIGHT_NAR_AUDIO_EMBEDDING_BASE_PATH = "nar_audio_embeddings_[layer_no].onnx"
WEIGHT_NAR_LANGUAGE_EMBEDDING_PATH = "nar_language_embedding.onnx"
WEIGHT_NAR_TEXT_EMBEDDING_PATH = "nar_text_embedding.onnx"
WEIGHT_DECODER_PATH = "ar_decoder.opt.onnx"
WEIGHT_ENCODEC_PATH = "encodec.onnx"
WEIGHT_VOCOS_PATH = "vocos.onnx"
WEIGHT_AUDIO_EMBEDDING_PATH = "audio_embedding.onnx"

ALL_MODELS = [
    WEIGHT_NAR_DECODER_PATH,
    WEIGHT_NAR_PREDICT_LAYERS_PATH,
    WEIGHT_AR_AUDIO_EMBEDDING_PATH,
    WEIGHT_AR_LANGUAGE_EMBEDDING_PATH,
    WEIGHT_AR_TEXT_EMBEDDING_PATH,
    WEIGHT_NAR_AUDIO_EMBEDDING_BASE_PATH.replace("[layer_no]","0"),
    WEIGHT_NAR_AUDIO_EMBEDDING_BASE_PATH.replace("[layer_no]","1"),
    WEIGHT_NAR_AUDIO_EMBEDDING_BASE_PATH.replace("[layer_no]","2"),
    WEIGHT_NAR_AUDIO_EMBEDDING_BASE_PATH.replace("[layer_no]","3"),
    WEIGHT_NAR_AUDIO_EMBEDDING_BASE_PATH.replace("[layer_no]","4"),
    WEIGHT_NAR_AUDIO_EMBEDDING_BASE_PATH.replace("[layer_no]","5"),
    WEIGHT_NAR_AUDIO_EMBEDDING_BASE_PATH.replace("[layer_no]","6"),
    WEIGHT_NAR_AUDIO_EMBEDDING_BASE_PATH.replace("[layer_no]","7"),
    WEIGHT_NAR_LANGUAGE_EMBEDDING_PATH,
    WEIGHT_NAR_TEXT_EMBEDDING_PATH,
    WEIGHT_DECODER_PATH,
    WEIGHT_ENCODEC_PATH,
    WEIGHT_VOCOS_PATH,
    WEIGHT_AUDIO_EMBEDDING_PATH
]

REMOTE_PATH = 'https://storage.googleapis.com/ailia-models/vall-e-x/'

SAMPLE_RATE = 16000

# ======================
# PARAMETERS
# ======================

SAVE_WAV_PATH = 'output.wav'

# ======================
# Arguemnt Parser Config
# ======================

parser = get_base_parser( 'VALL-E-X', None, SAVE_WAV_PATH)
# overwrite
parser.add_argument(
    '--input', '-i', metavar='TEXT', default=None,
    help='input text'
)
parser.add_argument(
    '--onnx', action='store_true',
    help='use onnx runtime'
)
parser.add_argument(
    '--profile', action='store_true',
    help='use profile model'
)
args = update_parser(parser, check_input_type=False)


# ======================
# Parameters
# ======================

if args.onnx:
    import onnxruntime
else:
    import ailia

if args.input:
    text = args.input
else:
    text ="音声合成のテストを行なっています。"

sampling_rate = 24000

def generate_voice(models):
    # onnx
    logger.info("Input text : " + text)

    if args.benchmark:
        start = int(round(time.time() * 1000))

    model_name = "jsut"

    if model_name != None:
        from utils.prompt_making import make_prompt
        make_prompt(name=model_name, audio_prompt_path="BASIC5000_0001.wav", transcript="水をマレーシアから買わなくてはならないのです", models=models) # Disable whisper

    output = generate_audio(text, prompt=model_name, language='auto', accent='no-accent', benchmark=args.benchmark, models=models)
    print(output.shape)

    if args.benchmark:
        end = int(round(time.time() * 1000))
        estimation_time = (end - start)
        logger.info(f'\twavegrow processing time {estimation_time} ms')

    # export to audio
    savepath = args.savepath
    logger.info(f'saved at : {savepath}')
    sf.write(savepath, output.astype(np.float32), sampling_rate)
    #logger.info('Script finished successfully.')

    from scipy.io.wavfile import write as write_wav
    write_wav("vallex_generation.wav", sampling_rate, output)

def main():
    # model files check and download
    os.makedirs("onnx", exist_ok=True)
    for model in ALL_MODELS:
        check_and_download_models("./onnx/"+model, "./onnx/"+model+".prototxt", REMOTE_PATH)

    models = {}
    if args.onnx:
        for model in ALL_MODELS:
            net = onnxruntime.InferenceSession( "./onnx/"+model)
            models[model] = net
    else:
        memory_mode = ailia.get_memory_mode(reduce_constant=True, ignore_input_with_initializer=True, reduce_interstage=False, reuse_interstage=True)
        for model in ALL_MODELS:
            net = ailia.Net(stream = "./onnx/"+model + ".prototxt", weight = "./onnx/"+model, memory_mode = memory_mode, env_id = args.env_id)
            if args.profile:
               net.set_profile_mode(True)
            models[model] = net

    generate_voice(models)

    if args.profile:
        for model in ALL_MODELS:
            if model == "ar_decoder.onnx":
                print(model)
                print(models[model].get_summary())

if __name__ == '__main__':
    main()
