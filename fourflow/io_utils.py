"""同目录原子替换，避免配置/模型指针因中断留下半个JSON。"""
import json,os,tempfile
from pathlib import Path


def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    payload=json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False).encode('utf-8')
    fd,name=tempfile.mkstemp(prefix='.'+path.name+'.',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:f.write(payload);f.flush();os.fsync(f.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def point_model(root,pointer,directory):
    root=Path(root).resolve();directory=Path(directory).resolve()
    if pointer not in {'latest.json','roles_latest.json','joint_latest.json'}:raise ValueError('不支持的模型指针')
    if not all((directory/n).is_file() for n in ('model_card.json','weights.npz')):raise ValueError('模型目录缺少模型卡或权重')
    path=directory.relative_to(root).as_posix() if directory.is_relative_to(root) else str(directory)
    atomic_json(root/'runtime/models'/pointer,{'path':path})
