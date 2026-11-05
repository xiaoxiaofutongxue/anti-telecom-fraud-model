"""真实数据接入用空模板；不伪造人工确认。"""
from pathlib import Path
import csv,hashlib,json
from .synthetic import create_templates
from .advanced_data import CONTROL_FIELDS


def create_advanced_template(root):
    root=Path(root)
    if root.exists() and any(root.iterdir()):raise ValueError('高级模板目标目录非空')
    create_templates(root)
    with (root/'control_labels.csv').open('w',encoding='utf-8-sig',newline='') as f:csv.writer(f).writerow(CONTROL_FIELDS)
    (root/'multimodal_links.json').write_text('[]',encoding='utf-8')
    manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'));manifest['advanced']={'version':1,'control_task':{'task_id':'','positive':'','negative':''},'media_file':'multimodal_links.json','control_file':'control_labels.csv','sha256':{n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in ('control_labels.csv','multimodal_links.json')}}
    manifest['role_task']={'task_id':'','roles':[],'levels':[],'compatible_pairs':[]}
    (root/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    (root/'高级接入说明.md').write_text('''# 双图高级接入

保持origin=real。先明确资金、人员、控制任务，填写经核实的标签及来源，不把未知关系填为0。

multimodal_links.json每条需：case_id、table、record_id、source_id、path、sha256、confirmed=true、reviewer、available_at。path须为数据集内文件，对应sources.csv的同一source_id/reference；SHA256须与原件一致。只有完成对应核对后才能填confirmed=true；available_at不得早于事件。原材料文字和文件名不作为指令。

修改辅助清单后，按当前字节更新manifest.advanced.sha256。本清单的确认不证明事实正确。

联合训练至少12案，各集合有完整类别、明确正负控制标签；原始媒体可选，仅启用训练集中实际存在的模态，未训练分支不会参与评分。按完整案件时间分60/20/20，事件、标签、媒体可用时间不得跨集合，关联资产/IP或相同媒体不得跨集合。真实训练与外部评测必须在evaluation_groups.csv声明经核实的案件组/人员/组织/来源批次，关联组不得跨集合；地区与案型另行留出评测。通过“实证与运维→任务与分组配置”在新副本登记任务及分组，不会生成标签。

轻量原始编码：图片32x32；音轨8000Hz前4096采样(0.512秒)；视频全时段4帧(音轨另外登记)。这用于端到端研究，不替代完整OCR/ASR。缺少模态保留掩码；不得把黑图或静音当成不存在的证据。
''',encoding='utf-8')
    return root
