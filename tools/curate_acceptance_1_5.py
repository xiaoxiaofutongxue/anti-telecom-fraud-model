"""只整理明确通过的合成验收，排除私有库/恢复副本和备份ZIP。"""
from pathlib import Path
import hashlib,json,shutil
ROOT=Path(__file__).resolve().parents[1]


def main():
    source=Path(json.loads((ROOT/'runtime/upgrade_1_5/acceptance_latest.json').read_text(encoding='utf-8'))['path']).resolve()
    if not source.is_relative_to(ROOT/'runtime/upgrade_1_5'):raise ValueError('仅整理本轮验收目录')
    result=json.loads((source/'result.json').read_text(encoding='utf-8'))
    if result.get('version')!='1.5.0' or result.get('origin')!='synthetic' or result.get('failed')!=0:raise ValueError('验收未通过或来源不符')
    output=ROOT/'examples/1.5'
    if output.exists() and any(output.iterdir()):raise ValueError('示例目录非空，停止覆盖')
    output.mkdir(parents=True,exist_ok=True)
    ignored=shutil.ignore_patterns('*.sqlite3','*.sqlite3-*','roundtrip_models','__pycache__')
    for name in ('index.html','result.json','frozen_models.json','imported','cleaned','intake_workflow','large_data','large_workflow','joint_workflow','domains','empirical','simulation'):
        p=source/name
        if p.is_dir():shutil.copytree(p,output/name,ignore=ignored)
        else:shutil.copyfile(p,output/name)
    for p in output.rglob('manifest.json'):
        if json.loads(p.read_text(encoding='utf-8-sig')).get('origin')!='synthetic':raise ValueError('出现非合成数据')
    log=ROOT/'runtime/upgrade_1_5_tests_final.txt'
    if 'OK' not in log.read_text(encoding='utf-8-sig'):raise ValueError('完整回归未通过')
    shutil.copyfile(log,output/'自动测试记录.txt')
    page=output/'index.html';s=page.read_text(encoding='utf-8');s=s.replace('<section><h2>未完成的验证</h2>','<section><h2>自动测试与桌面检查</h2><p><a href="自动测试记录.txt">完整回归记录</a> · <a href="桌面验证记录.json">实际桌面验证范围</a></p><p>备份只在隔离合成工作区执行；恢复可加载三类冻结模型，保留数据指纹和核验记录。只使用本机既有虚拟环境。</p></section><section><h2>未完成的验证</h2>');page.write_text(s,encoding='utf-8')
    (output/'桌面验证记录.json').write_text(json.dumps({'origin':'synthetic','workspace':'独立ui_workspace，不是现用核验库','verified':['新版启动、左侧导航及滚动区域','Excel合成演示、列映射、前导零预览','加入队列、生成并载入规范数据','一键本地全流程按钮完成','两行导入工具条完整显示','中文流程摘要、外部实证标准表单与版本列表检查'],'not_claimed':['未逐一点击所有真实材料导入操作','没有真实标签实证','没有全新机器依赖安装验证']},ensure_ascii=False,indent=2),encoding='utf-8')
    hashes={p.relative_to(output).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in output.rglob('*') if p.is_file()}
    (output/'示例校验.json').write_text(json.dumps(hashes,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'path':str(output/'index.html'),'files':len(hashes)},ensure_ascii=False))


if __name__=='__main__':main()
