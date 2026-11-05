"""构建1.8源码及离线模型包；仅打包合成数据白名单和空模板。"""
from pathlib import Path
import csv
import hashlib
import json
import zipfile
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from fourflow.model import resolve_model_directory,TemporalHeteroGNN
from fourflow.roles import RoleHierarchyModel
from fourflow.dual_model import DualFlowModel
from fourflow import __version__


def main():
    model_path=resolve_model_directory(ROOT).resolve()
    role_path=Path(json.loads((ROOT/'runtime/models/roles_latest.json').read_text(encoding='utf-8'))['path'])
    if not role_path.is_absolute():role_path=ROOT/role_path
    role_path=role_path.resolve()
    if any(not p.is_relative_to(ROOT) for p in (model_path,role_path)):raise ValueError('交付模型必须位于项目目录内')
    if TemporalHeteroGNN.load(model_path).metadata['origin']!='synthetic' or RoleHierarchyModel.load(role_path).metadata['origin']!='synthetic':raise ValueError('通用交付包仅允许合成模型，禁止混入真实案件模型')
    joint_path=Path(json.loads((ROOT/'runtime/models/joint_latest.json').read_text(encoding='utf-8'))['path'])
    if not joint_path.is_absolute():joint_path=ROOT/joint_path
    joint_path=joint_path.resolve()
    if not joint_path.is_relative_to(ROOT) or DualFlowModel.load(joint_path).metadata['origin']!='synthetic':raise ValueError('联合交付模型必须是项目内合成模型')
    chosen=[ROOT/name for name in ('app.py','cli.py','launch.pyw','启动系统.vbs','requirements.txt','requirements-lock.txt','README.md','.gitignore')]
    for directory in ('fourflow','tests','tools','docs','.run'):
        chosen.extend(p for p in (ROOT/directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts and 'screenshots' not in p.parts and not p.is_symlink())
    for directory in ('enhanced_sample','role_training_synthetic','sample','training_synthetic','media_demo','causal_demo','stress_training_synthetic','stress_holdout_synthetic','final_acceptance_synthetic','advanced_training_synthetic','advanced_region_holdout_synthetic','advanced_type_holdout_synthetic','advanced_joint_holdout_synthetic','advanced_sample_synthetic','intake_demo','narrative_demo_synthetic'):
        folder=ROOT/'data'/directory
        manifest=folder/('causal_design.json' if directory=='causal_demo' else 'manifest.json')
        if json.loads(manifest.read_text(encoding='utf-8-sig')).get('origin')!='synthetic':raise ValueError('白名单演示目录不再是合成来源：'+directory)
        chosen.extend(p for p in folder.rglob('*') if p.is_file() and not p.is_symlink() and p.resolve().is_relative_to(folder.resolve()))
    for directory in ('templates_1_2','causal_templates','advanced_templates_1_4'):
        folder=ROOT/'data'/directory
        for path in folder.glob('*.csv'):
            with path.open(encoding='utf-8-sig',newline='') as stream:
                reader=csv.reader(stream);next(reader,None)
                if any(any(value.strip() for value in row) for row in reader):raise ValueError('空模板已填写，拒绝打包：'+str(path))
        chosen.extend(p for p in folder.glob('*') if p.is_file() and not p.is_symlink())
        if directory=='advanced_templates_1_4' and json.loads((folder/'multimodal_links.json').read_text())!=[]:raise ValueError('高级媒体模板已填写，拒绝打包')
    for folder in (model_path,role_path,joint_path,ROOT/'runtime/models/joint_1_4_dro'):chosen.extend(folder/name for name in ('model_card.json','weights.npz'))
    whisper=ROOT/'runtime/models/whisper_base'
    chosen.extend(whisper/name for name in ('model.bin','config.json','tokenizer.json','vocabulary.txt'))
    chosen.append(ROOT/'runtime/models/public_models.json')
    # 附带历史1.5合成验收示例；当前用户案件报告不进入通用包。
    example=ROOT/'examples/1.5'
    if not example.is_dir():raise ValueError('缺少历史1.5合成验收示例')
    receipt=json.loads((example/'result.json').read_text(encoding='utf-8'))
    if receipt.get('origin')!='synthetic' or receipt.get('version')!='1.5.0' or receipt.get('failed')!=0:raise ValueError('历史1.5示例必须通过合成验收')
    for path in example.rglob('manifest.json'):
        if json.loads(path.read_text(encoding='utf-8-sig')).get('origin')!='synthetic':raise ValueError('示例含非合成数据，拒绝通用交付')
    chosen.extend(p for p in example.rglob('*') if p.is_file() and not p.is_symlink())
    contents={p.relative_to(ROOT).as_posix():p.read_bytes() for p in chosen}
    contents['runtime/models/latest.json']=json.dumps({'path':model_path.relative_to(ROOT).as_posix()}).encode()
    contents['runtime/models/roles_latest.json']=json.dumps({'path':role_path.relative_to(ROOT).as_posix()}).encode()
    contents['runtime/models/joint_latest.json']=json.dumps({'path':joint_path.relative_to(ROOT).as_posix()}).encode()
    hashes={name:hashlib.sha256(data).hexdigest() for name,data in sorted(contents.items())}
    contents['交付文件校验.json']=json.dumps(hashes,ensure_ascii=False,indent=2).encode('utf-8')
    output=ROOT.parent/('FourFlowAnalysis_'+__version__+'.zip')
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,data in sorted(contents.items()):archive.writestr('FourFlowAnalysis/'+name,data)
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        for name,digest in hashes.items():assert hashlib.sha256(archive.read('FourFlowAnalysis/'+name)).hexdigest()==digest
        assert not any('/.venv/' in n or '__pycache__' in n or n.endswith('analysis.sqlite3') or n.endswith('settings.json') or '/hf_cache/' in n or '/extractions/' in n for n in archive.namelist())
        for pointer in ('latest.json','roles_latest.json','joint_latest.json'):
            path=json.loads(archive.read('FourFlowAnalysis/runtime/models/'+pointer))['path']
            assert not Path(path).is_absolute() and 'FourFlowAnalysis/'+path+'/weights.npz' in archive.namelist()
    print(json.dumps({'path':str(output),'verified_files':len(hashes),'bytes':output.stat().st_size,'sha256':hashlib.sha256(output.read_bytes()).hexdigest()},ensure_ascii=False))


if __name__=='__main__':main()
