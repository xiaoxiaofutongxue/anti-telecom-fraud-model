"""Persist the current report without reopening a stale or modified result."""
from pathlib import Path
import hashlib,json
from .io_utils import atomic_json


def review_signature(result):
    rows=[{'case_id':c['case_id'],**r} for c in result['cases'] for r in c.get('reviews',[])]
    rows.sort(key=lambda r:(r['case_id'],r['finding_id']))
    return hashlib.sha256(json.dumps(rows,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def validate_report(directory,corpus,result,workspace):
    directory=Path(directory).resolve();workspace=Path(workspace).resolve();corpus.validate()
    if not directory.is_relative_to(workspace):raise ValueError('报告路径不在当前工作区内')
    hashes=json.loads((directory/'checksums.json').read_text(encoding='utf-8'))
    if not isinstance(hashes,dict) or not {'index.html','result.json','records.json','narrative_manifest.json'}<=hashes.keys():raise ValueError('报告校验清单不完整')
    for name,digest in hashes.items():
        if not isinstance(name,str) or not isinstance(digest,str):raise ValueError('报告校验清单格式错误')
        relative=Path(name);path=(directory/relative).resolve()
        if relative.is_absolute() or relative.drive or '..' in relative.parts or not path.is_relative_to(directory) or path.is_symlink():raise ValueError('报告文件路径越界')
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise ValueError('报告文件缺失或已修改，请重新导出')
    saved=json.loads((directory/'result.json').read_text(encoding='utf-8'))
    if saved.get('corpus_fingerprint')!=corpus.fingerprint or saved.get('version')!=result['version']:raise ValueError('报告对应不同的资料或解析版本，请重新导出')
    if review_signature(saved)!=review_signature(result):raise ValueError('报告早于当前核验意见，请重新导出')
    return directory


def remember(workspace,corpus,result,output=None):
    workspace=Path(workspace).resolve();root=corpus.root.resolve()
    state={'path':root.relative_to(workspace).as_posix() if root.is_relative_to(workspace) else str(root)}
    if output is not None:
        output=validate_report(output,corpus,result,workspace)
        state['output']=output.relative_to(workspace).as_posix()
    atomic_json(workspace/'runtime/narrative_latest.json',state)


def restore_report(workspace,corpus,result):
    workspace=Path(workspace).resolve();pointer=workspace/'runtime/narrative_latest.json'
    try:
        state=json.loads(pointer.read_text(encoding='utf-8'));value=state.get('output')
        if not value:return None,''
        if not isinstance(value,str):raise ValueError('报告路径格式错误')
        path=Path(value);path=path if path.is_absolute() else workspace/path
        return validate_report(path,corpus,result,workspace),''
    except (OSError,ValueError,KeyError,TypeError) as exc:return None,'上次报告未恢复：'+str(exc)
