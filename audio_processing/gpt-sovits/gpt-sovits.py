import sys
sys.path.append('./GPT_SoVITS')

from module.models_onnx import SynthesizerTrn, symbols
from AR.models.t2s_lightning_module_onnx import Text2SemanticLightningModule
import torch
import torchaudio
from torch import nn
from feature_extractor import cnhubert

import os
cnhubert_base_path = os.environ.get(
    "cnhubert_base_path", "GPT_SoVITS/pretrained_models/chinese-hubert-base"
)


cnhubert.cnhubert_base_path=cnhubert_base_path
ssl_model = cnhubert.get_model()
from text import cleaned_text_to_sequence
import soundfile
from my_utils import load_audio
import os
import json

def spectrogram_torch(y, n_fft, sampling_rate, hop_size, win_size, center=False):
    hann_window = torch.hann_window(win_size).to(
            dtype=y.dtype, device=y.device
        )
    y = torch.nn.functional.pad(
        y.unsqueeze(1),
        (int((n_fft - hop_size) / 2), int((n_fft - hop_size) / 2)),
        mode="reflect",
    )
    y = y.squeeze(1)
    spec = torch.stft(
        y,
        n_fft,
        hop_length=hop_size,
        win_length=win_size,
        window=hann_window,
        center=center,
        pad_mode="reflect",
        normalized=False,
        onesided=True,
        return_complex=False,
    )
    spec = torch.sqrt(spec.pow(2).sum(-1) + 1e-6)
    return spec


class DictToAttrRecursive(dict):
    def __init__(self, input_dict):
        super().__init__(input_dict)
        for key, value in input_dict.items():
            if isinstance(value, dict):
                value = DictToAttrRecursive(value)
            self[key] = value
            setattr(self, key, value)

    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError:
            raise AttributeError(f"Attribute {item} not found")

    def __setattr__(self, key, value):
        if isinstance(value, dict):
            value = DictToAttrRecursive(value)
        super(DictToAttrRecursive, self).__setitem__(key, value)
        super().__setattr__(key, value)

    def __delattr__(self, item):
        try:
            del self[item]
        except KeyError:
            raise AttributeError(f"Attribute {item} not found")


class T2SEncoder(nn.Module):
    def __init__(self, t2s, vits):
        super().__init__()
        self.encoder = t2s.onnx_encoder
        self.vits = vits
    
    def forward(self, ref_seq, text_seq, ref_bert, text_bert, ssl_content):
        codes = self.vits.extract_latent(ssl_content)
        prompt_semantic = codes[0, 0]
        bert = torch.cat([ref_bert.transpose(0, 1), text_bert.transpose(0, 1)], 1)
        all_phoneme_ids = torch.cat([ref_seq, text_seq], 1)
        bert = bert.unsqueeze(0)
        prompt = prompt_semantic.unsqueeze(0)
        return self.encoder(all_phoneme_ids, bert), prompt


class T2SModel(nn.Module):
    def __init__(self, t2s_path, vits_model):
        super().__init__()
        dict_s1 = torch.load(t2s_path, map_location="cpu")
        self.config = dict_s1["config"]
        self.t2s_model = Text2SemanticLightningModule(self.config, "ojbk", is_train=False)
        self.t2s_model.load_state_dict(dict_s1["weight"])
        self.t2s_model.eval()
        self.vits_model = vits_model.vq_model
        self.hz = 50
        self.max_sec = self.config["data"]["max_sec"]
        self.t2s_model.model.top_k = torch.LongTensor([self.config["inference"]["top_k"]])
        self.t2s_model.model.early_stop_num = torch.LongTensor([self.hz * self.max_sec])
        self.t2s_model = self.t2s_model.model
        self.t2s_model.init_onnx()
        self.onnx_encoder = T2SEncoder(self.t2s_model, self.vits_model)
        self.first_stage_decoder = self.t2s_model.first_stage_decoder
        self.stage_decoder = self.t2s_model.stage_decoder
        #self.t2s_model = torch.jit.script(self.t2s_model)

    def forward(self, ref_seq, text_seq, ref_bert, text_bert, ssl_content, debug=False):
        early_stop_num = self.t2s_model.early_stop_num

        if debug:
            import onnxruntime
            sess_encoder = onnxruntime.InferenceSession(f"nahida_t2s_encoder.onnx", providers=["CPU"])
            sess_fsdec = onnxruntime.InferenceSession(f"nahida_t2s_fsdec.onnx", providers=["CPU"])
            sess_sdec = onnxruntime.InferenceSession(f"nahida_t2s_sdec.onnx", providers=["CPU"])

        #[1,N] [1,N] [N, 1024] [N, 1024] [1, 768, N]
        if debug:
            x, prompts = sess_encoder.run(None, {"ref_seq":ref_seq.detach().numpy(), "text_seq":text_seq.detach().numpy(), "ref_bert":ref_bert.detach().numpy(), "text_bert":text_bert.detach().numpy(), "ssl_content":ssl_content.detach().numpy()})
            x = torch.from_numpy(x)
            prompts = torch.from_numpy(prompts)
        else:
            x, prompts = self.onnx_encoder(ref_seq, text_seq, ref_bert, text_bert, ssl_content)

        prefix_len = prompts.shape[1]

        #[1,N,512] [1,N]
        if debug:
            y, k, v, y_emb, x_example = sess_fsdec.run(None, {"x":x.detach().numpy(), "prompts":prompts.detach().numpy()})
            y = torch.from_numpy(y)
            k = torch.from_numpy(k)
            v = torch.from_numpy(v)
            y_emb = torch.from_numpy(y_emb)
            x_example = torch.from_numpy(x_example)
        else:
            y, k, v, y_emb, x_example = self.first_stage_decoder(x, prompts)

        stop = False
        for idx in range(1, 1500):
            #[1, N] [N_layer, N, 1, 512] [N_layer, N, 1, 512] [1, N, 512] [1] [1, N, 512] [1, N]
            if debug:
                y, k, v, y_emb, logits, samples = sess_sdec.run(None, {"iy":y.detach().numpy(), "ik":k.detach().numpy(), "iv":v.detach().numpy(), "iy_emb":y_emb.detach().numpy(), "ix_example":x_example.detach().numpy()})
                y = torch.from_numpy(y)
                k = torch.from_numpy(k)
                v = torch.from_numpy(v)
                y_emb = torch.from_numpy(y_emb)
                logits = torch.from_numpy(logits)
                samples = torch.from_numpy(samples)
            else:
                enco = self.stage_decoder(y, k, v, y_emb, x_example)
                y, k, v, y_emb, logits, samples = enco
            if early_stop_num != -1 and (y.shape[1] - prefix_len) > early_stop_num:
                stop = True
            if torch.argmax(logits, dim=-1)[0] == self.t2s_model.EOS or samples[0, 0] == self.t2s_model.EOS:
                stop = True
            if stop:
                break
        y[0, -1] = 0

        return y[:, -idx:].unsqueeze(0)




class VitsModel(nn.Module):
    def __init__(self, vits_path):
        super().__init__()
        dict_s2 = torch.load(vits_path,map_location="cpu")
        self.hps = dict_s2["config"]
        self.hps = DictToAttrRecursive(self.hps)
        self.hps.model.semantic_frame_rate = "25hz"
        self.vq_model = SynthesizerTrn(
            self.hps.data.filter_length // 2 + 1,
            self.hps.train.segment_size // self.hps.data.hop_length,
            n_speakers=self.hps.data.n_speakers,
            **self.hps.model
        )
        self.vq_model.eval()
        self.vq_model.load_state_dict(dict_s2["weight"], strict=False)
        
    def forward(self, text_seq, pred_semantic, ref_audio):
        refer = spectrogram_torch(
            ref_audio,
            self.hps.data.filter_length,
            self.hps.data.sampling_rate,
            self.hps.data.hop_length,
            self.hps.data.win_length,
            center=False
        )
        return self.vq_model(pred_semantic, text_seq, refer)[0, 0]


class GptSoVits(nn.Module):
    def __init__(self, vits, t2s):
        super().__init__()
        self.vits = vits
        self.t2s = t2s
    
    def forward(self, ref_seq, text_seq, ref_bert, text_bert, ref_audio, ssl_content, debug=False):
        pred_semantic = self.t2s(ref_seq, text_seq, ref_bert, text_bert, ssl_content, debug)
        #audio = self.vits(text_seq, pred_semantic, ref_audio)
        import onnxruntime
        sess = onnxruntime.InferenceSession("nahida_vits.onnx", providers=["CPU"])
        audio1 = sess.run(None, {
            "text_seq" : text_seq.detach().cpu().numpy(),
            "pred_semantic" : pred_semantic.detach().cpu().numpy(), 
            "ref_audio" : ref_audio.detach().cpu().numpy()
        })
        return torch.from_numpy(audio1[0])



class SSLModel(nn.Module):
    def __init__(self):
        super().__init__()
        self.ssl = ssl_model

    def forward(self, ref_audio_16k):
        import onnxruntime
        sess = onnxruntime.InferenceSession("nahida_cnhubert.onnx", providers=["CPU"])
        last_hidden_state = sess.run(None, {
            "ref_audio_16k" : ref_audio_16k.detach().cpu().numpy()
        })
        return torch.from_numpy(last_hidden_state[0])


from text import cleaned_text_to_sequence
from text.cleaner import clean_text

def get_bert_feature(text, word2ph):
    with torch.no_grad():
        inputs = tokenizer(text, return_tensors="pt")
        for i in inputs:
            inputs[i] = inputs[i].to(device)
        res = bert_model(**inputs, output_hidden_states=True)
        res = torch.cat(res["hidden_states"][-3:-2], -1)[0].cpu()[1:-1]
    assert len(word2ph) == len(text)
    phone_level_feature = []
    for i in range(len(word2ph)):
        repeat_feature = res[i].repeat(word2ph[i], 1)
        phone_level_feature.append(repeat_feature)
    phone_level_feature = torch.cat(phone_level_feature, dim=0)
    return phone_level_feature.T

def clean_text_inf(text, language):
    phones, word2ph, norm_text = clean_text(text, language)
    print(phones)
    phones = cleaned_text_to_sequence(phones)
    return phones, word2ph, norm_text

def get_bert_inf(phones, word2ph, norm_text, language):
    language=language.replace("all_","")
    if language == "zh":
        bert = get_bert_feature(norm_text, word2ph)
    else:
        bert = torch.zeros(
            (1024, len(phones)),
            dtype=torch.float32,
        )

    return bert

import LangSegment

def get_phones_and_bert(text,language):
    if language in {"en","all_zh","all_ja"}:
        language = language.replace("all_","")
        if language == "en":
            LangSegment.setfilters(["en"])
            formattext = " ".join(tmp["text"] for tmp in LangSegment.getTexts(text))
        else:
            # 因无法区别中日文汉字,以用户输入为准
            formattext = text
        while "  " in formattext:
            formattext = formattext.replace("  ", " ")
        phones, word2ph, norm_text = clean_text_inf(formattext, language)
        if language == "zh":
            bert = get_bert_feature(norm_text, word2ph)
        else:
            bert = torch.zeros(
                (1024, len(phones)),
                dtype=torch.float32,
            )
    elif language in {"zh", "ja","auto"}:
        textlist=[]
        langlist=[]
        LangSegment.setfilters(["zh","ja","en"])
        if language == "auto":
            for tmp in LangSegment.getTexts(text):
                langlist.append(tmp["lang"])
                textlist.append(tmp["text"])
        else:
            for tmp in LangSegment.getTexts(text):
                if tmp["lang"] == "en":
                    langlist.append(tmp["lang"])
                else:
                    # 因无法区别中日文汉字,以用户输入为准
                    langlist.append(language)
                textlist.append(tmp["text"])
        print(textlist)
        print(langlist)
        phones_list = []
        bert_list = []
        norm_text_list = []
        for i in range(len(textlist)):
            lang = langlist[i]
            phones, word2ph, norm_text = clean_text_inf(textlist[i], lang)
            bert = get_bert_inf(phones, word2ph, norm_text, lang)
            phones_list.append(phones)
            norm_text_list.append(norm_text)
            bert_list.append(bert)
        bert = torch.cat(bert_list, dim=1)
        phones = sum(phones_list, [])
        norm_text = ''.join(norm_text_list)

    return phones,bert.to(dtype = torch.float32),norm_text

def export(vits_path, gpt_path, project_name):
    print("1")
    vits = VitsModel(vits_path)
    gpt = T2SModel(gpt_path, vits)
    gpt_sovits = GptSoVits(vits, gpt)
    ssl = SSLModel()

    print("2")

    ref_audio = torch.randn((1, 48000 * 5)).float()

    #ref_audio = torch.tensor([load_audio("JSUT.wav", 48000)]).float()
    #ref_seq = torch.LongTensor([cleaned_text_to_sequence(['m', 'i', 'z', 'u', 'o', 'm', 'a', 'r', 'e', 'e', 'sh', 'i', 'a', 'k', 'a', 'r', 'a', 'k', 'a', 'w', 'a', 'n', 'a', 'k', 'U', 't', 'e', 'w', 'a', 'n', 'a', 'r', 'a', 'n', 'a', 'i', '.'])])

    ref_audio = torch.tensor([load_audio("kyakuno.wav", 48000)]).float()
    ref_seq = torch.LongTensor([cleaned_text_to_sequence(['a', 'a', 'r', 'u', 'b', 'u', 'i', 'sh', 'i', 'i', 'o', 'sh', 'i', 'y', 'o', 'o', 'sh', 'I', 't', 'a', 'b', 'o', 'i', 's', 'U', 'ch', 'e', 'N', 'j', 'a', 'a', 'o', 'ts', 'U', 'k', 'u', 'r', 'u', '.'])])

    phones1,bert1,norm_text1=get_phones_and_bert("RVCを使用したボイスチェンジャーを作る。", "all_ja")
    print(phones1)

    #text_seq = torch.LongTensor([cleaned_text_to_sequence(['m', 'i', 'z', 'u', 'w', 'a', ',', 'i', 'r', 'i', 'm', 'a', 's', 'e', 'N', 'k', 'a', '?'])])
    text_seq = torch.LongTensor([cleaned_text_to_sequence(['ky', 'o', 'o', 'w', 'a', 'h', 'a', 'r', 'e', 'd', 'e', 'sh', 'o', 'o', 'k', 'a', '?'])])
    ref_bert = torch.randn((ref_seq.shape[1], 1024)).float()
    text_bert = torch.randn((text_seq.shape[1], 1024)).float()

    ref_audio_16k = torchaudio.functional.resample(ref_audio,48000,16000).float()
    ref_audio_sr = torchaudio.functional.resample(ref_audio,48000,vits.hps.data.sampling_rate).float()

    ssl_content = ssl(ref_audio_16k).float()
    
    debug = True

    print("1")

    if debug:
        print("gpt_sovits")
        a = gpt_sovits(ref_seq, text_seq, ref_bert, text_bert, ref_audio_sr, ssl_content, debug=debug)
        soundfile.write("out.wav", a.cpu().detach().numpy(), vits.hps.data.sampling_rate)
        return

if __name__ == "__main__":
    gpt_path = "GPT_SoVITS/pretrained_models/s1bert25hz-2kh-longer-epoch=68e-step=50232.ckpt"#"GPT_weights/nahida-e25.ckpt"
    vits_path = "GPT_SoVITS/pretrained_models/s2G488k.pth"#"SoVITS_weights/nahida_e30_s3930.pth"
    exp_path = "nahida"
    export(vits_path, gpt_path, exp_path)

    # soundfile.write("out.wav", a, vits.hps.data.sampling_rate)