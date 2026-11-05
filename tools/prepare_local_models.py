"""仅显式执行时联网准备公开语音权重；识别模块本身只读取本地权重。"""
import os
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def main():
    for name,suffix in [('HF_HOME','hf_cache'),('HF_HUB_CACHE','hf_cache/hub'),('HF_XET_CACHE','hf_cache/xet')]:os.environ[name]=str(ROOT/'runtime'/suffix)
    from faster_whisper.utils import download_model
    from faster_whisper import WhisperModel
    directory=ROOT/'runtime/models/whisper_base';directory.mkdir(parents=True,exist_ok=True)
    required=['model.bin','config.json','tokenizer.json','vocabulary.txt']
    if not all((directory/name).is_file() for name in required):download_model('base',output_dir=str(directory))
    WhisperModel(str(directory),device='cpu',compute_type='int8',local_files_only=True,cpu_threads=4)
    print('离线语音权重可用：'+str(directory))


if __name__=='__main__':main()
