"""单机自检、可恢复备份与模型卡清单。备份只写本地新目录。"""
from pathlib import Path
from datetime import datetime
import hashlib,importlib.metadata,json,sqlite3,zipfile,tempfile
from contextlib import closing
from .core import TZ,load_dataset
from .io_utils import atomic_json


def environment_check(root,dataset=None):
    root=Path(root);checks=[]
    for package in ('networkx','numpy','scipy','scikit-learn','Pillow','openpyxl','torch','rapidocr','onnxruntime','faster-whisper','av'):
        try:version=importlib.metadata.version(package);checks.append({'item':package,'status':'通过','detail':version})
        except importlib.metadata.PackageNotFoundError:checks.append({'item':package,'status':'缺失','detail':'按requirements-lock.txt安装依赖'})
    for name in ('latest.json','roles_latest.json','joint_latest.json'):
        try:
            p=Path(json.loads((root/'runtime/models'/name).read_text(encoding='utf-8'))['path']);p=p if p.is_absolute() else root/p;card=json.loads((p/'model_card.json').read_text(encoding='utf-8'))
            valid=hashlib.sha256((p/'weights.npz').read_bytes()).hexdigest()==card['weights_sha256'];checks.append({'item':name,'status':'通过' if valid else '失败','detail':card.get('model_id','')+' / '+card.get('origin','未知')})
        except (OSError,ValueError,KeyError,TypeError) as exc:checks.append({'item':name,'status':'未就绪','detail':str(exc)})
    if dataset:
        valid=load_dataset(dataset.root).fingerprint==dataset.fingerprint;checks.append({'item':'当前数据快照','status':'通过' if valid else '失败','detail':'文件与已载入快照一致' if valid else '需重新载入'})
    if (root/'runtime/narrative_latest.json').is_file():
        try:
            from .narrative import load_corpus
            path=Path(json.loads((root/'runtime/narrative_latest.json').read_text(encoding='utf-8'))['path']);corpus=load_corpus(path if path.is_absolute() else root/path);checks.append({'item':'当前案件文本原件与解析记录','status':'通过','detail':str(len(corpus.cases))+'份资料'})
        except (ValueError,OSError,KeyError) as exc:checks.append({'item':'当前案件文本原件与解析记录','status':'失败','detail':str(exc)})
    if (root/'runtime/analysis.sqlite3').is_file():
        from .store import Store
        integrity=Store(root/'runtime/analysis.sqlite3').verify_audit();checks.append({'item':'本地审计一致性','status':'通过' if integrity['valid'] else '失败','detail':integrity['scope'] if integrity['valid'] else '；'.join(integrity['errors'])})
    return {'created_at':datetime.now(TZ).isoformat(),'checks':checks,'ready':all(r['status']=='通过' for r in checks),'scope':'只确认本机依赖/文件一致性，不代表模型实证效果通过或多人部署就绪'}


def backup_workspace(root,dataset,output_root,max_bytes=600*1024*1024):
    root=Path(root).resolve();output_root=Path(output_root).resolve()
    if output_root.is_relative_to(dataset.root.resolve()):raise ValueError('备份目标不能放在源数据目录内')
    if load_dataset(dataset.root).fingerprint!=dataset.fingerprint:raise ValueError('当前数据已变化，请重新载入后备份')
    output_root.mkdir(parents=True,exist_ok=True);output=output_root/datetime.now(TZ).strftime('workspace_%Y%m%d_%H%M%S_%f.zip')
    files={}
    for name in ('fourflow','tools','docs','.run'):
        for path in (root/name).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts and not path.is_symlink():files[path.relative_to(root).as_posix()]=path
    for name in ('app.py','cli.py','launch.pyw','启动系统.vbs','requirements.txt','requirements-lock.txt','README.md'):
        if (root/name).is_file():files[name]=root/name
    for path in dataset.root.rglob('*'):
        if path.is_symlink() or not path.resolve().is_relative_to(dataset.root.resolve()):raise ValueError('源数据包含符号链接或越界文件，停止备份')
        if path.is_file():files['data/restored_dataset/'+path.relative_to(dataset.root).as_posix()]=path
    pointers={}
    narrative=None
    narrative_pointer=root/'runtime/narrative_latest.json'
    if narrative_pointer.is_file():
        from .narrative import load_corpus
        narrative_root=Path(json.loads(narrative_pointer.read_text(encoding='utf-8'))['path']);narrative=load_corpus(narrative_root if narrative_root.is_absolute() else root/narrative_root)
        for path in narrative.root.rglob('*'):
            if path.is_symlink() or not path.resolve().is_relative_to(narrative.root):raise ValueError('案件文本包含越界链接，停止备份')
            if path.is_file():files['data/narrative_current/'+path.relative_to(narrative.root).as_posix()]=path
        pointers['runtime/narrative_latest.json']={'path':'data/narrative_current'}
    for number,name in enumerate(('latest.json','roles_latest.json','joint_latest.json')):
        pointer=root/'runtime/models'/name
        if not pointer.exists():continue
        directory=Path(json.loads(pointer.read_text(encoding='utf-8'))['path']);directory=directory if directory.is_absolute() else root/directory
        for filename in ('model_card.json','weights.npz'):files[f'runtime/models/active_{number}/{filename}']=directory/filename
        pointers['runtime/models/'+name]={'path':f'runtime/models/active_{number}'}
    whisper=root/'runtime/models/whisper_base'
    if whisper.exists():
        for filename in ('model.bin','config.json','tokenizer.json','vocabulary.txt'):
            if (whisper/filename).is_file():files['runtime/models/whisper_base/'+filename]=whisper/filename
    for dirname in ('deployments','empirical_runs'):
        for path in (root/'runtime'/dirname).rglob('*'):
            if path.is_symlink() or not path.resolve().is_relative_to(root):raise ValueError('实证记录含越界链接，停止备份')
            if path.is_file():files[path.relative_to(root).as_posix()]=path
    if sum(p.stat().st_size for p in files.values())>max_bytes:raise ValueError('备份超过600MB本地默认上限，请分开备份较大原始材料')
    with tempfile.TemporaryDirectory(prefix='backup_',dir=root/'runtime') as temp:
        db=Path(temp)/'analysis.sqlite3'
        with closing(sqlite3.connect(root/'runtime/analysis.sqlite3')) as src,closing(sqlite3.connect(db)) as dst:src.backup(dst)
        files['runtime/analysis.sqlite3']=db;hashes={};temporary=Path(temp)/'backup.zip'
        with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as archive:
            for name,path in files.items():
                raw=path.read_bytes();archive.writestr(name,raw);hashes[name]=hashlib.sha256(raw).hexdigest()
            for name,value in {**pointers,'runtime/settings.json':{'dataset':'data/restored_dataset'}}.items():
                raw=json.dumps(value,ensure_ascii=False).encode('utf-8');archive.writestr(name,raw);hashes[name]=hashlib.sha256(raw).hexdigest()
            archive.writestr('backup_manifest.json',json.dumps({'version':1,'created_at':datetime.now(TZ).isoformat(),'origin':dataset.manifest['origin'],'dataset_fingerprint':dataset.fingerprint,'files':hashes,'scope':'含本地数据和核验意见的私有备份，不作为通用发布包；未加密。'},ensure_ascii=False,indent=2))
        verify_backup(temporary)
        if load_dataset(dataset.root).fingerprint!=dataset.fingerprint:raise ValueError('备份过程中源数据变化，未发布该备份')
        if narrative:narrative.validate()
        temporary.replace(output)
    return output


def verify_backup(path):
    with zipfile.ZipFile(path) as archive:
        names=archive.namelist()
        if len(names)>100000 or sum(row.file_size for row in archive.infolist())>800*1024*1024:raise ValueError('备份展开体积或文件数超出单机校验上限')
        if len(names)!=len(set(names)) or archive.testzip() is not None:raise ValueError('备份条目重复或损坏')
        manifest=json.loads(archive.read('backup_manifest.json'))
        if set(names)!=set(manifest['files'])|{'backup_manifest.json'}:raise ValueError('备份文件列表不匹配')
        for name,digest in manifest['files'].items():
            p=Path(name)
            if p.is_absolute() or p.drive or '..' in p.parts or hashlib.sha256(archive.read(name)).hexdigest()!=digest:raise ValueError('备份路径或文件校验失败')
            if name.endswith('/model_card.json'):
                card=json.loads(archive.read(name));weights=str(Path(name).with_name('weights.npz')).replace('\\','/')
                if weights not in manifest['files'] or hashlib.sha256(archive.read(weights)).hexdigest()!=card['weights_sha256']:raise ValueError('备份中的模型卡与权重不一致')
    return manifest


def restore_backup(path,destination):
    manifest=verify_backup(path);destination=Path(destination).resolve()
    if destination.exists() and any(destination.iterdir()):raise ValueError('恢复只能写新的空目录，不能覆盖正在使用的工作区')
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if not (destination/name).resolve().is_relative_to(destination):raise ValueError('恢复文件路径越界')
        destination.mkdir(parents=True,exist_ok=True);archive.extractall(destination)
    dataset=load_dataset(destination/'data/restored_dataset')
    if dataset.fingerprint!=manifest['dataset_fingerprint']:raise ValueError('恢复后数据指纹不匹配')
    if (destination/'runtime/narrative_latest.json').is_file():
        from .narrative import load_corpus
        narrative_path=Path(json.loads((destination/'runtime/narrative_latest.json').read_text(encoding='utf-8'))['path'])
        if narrative_path.is_absolute() or not (destination/narrative_path).resolve().is_relative_to(destination):raise ValueError('备份案件文本引用越界')
        load_corpus(destination/narrative_path)
    return destination
