# 上传到GitHub

本目录为上传专用副本。原开发项目仍在D:\Projects\FourFlowAnalysis；在原目录修改代码不会自动同步本副本。不要向本目录加入真实案件、个人核验数据库或运行报告。

## 第一次上传

1. 在GitHub创建空仓库，当前仓库名anti-telecom-fraud-model；新建其他仓库时可按实际需要选择可见性。不要自动创建README、.gitignore或许可证，避免首次推送冲突。可从 https://github.com/new 创建。
2. 在本目录打开PowerShell，执行下面命令。姓名和邮箱换成自己的提交身份，可使用GitHub提供的noreply邮箱；这里不是账户密码。

```powershell
git init -b main
git config user.name "你的提交显示名"
git config user.email "你的提交邮箱"
git add .
git status --short
git commit -m "Initial import of FourFlowAnalysis 1.9.1"
git remote add origin https://github.com/xiaoxiaofutongxue/anti-telecom-fraud-model.git
git push -u origin main
```

提交前阅读git status。推送时按照Git凭据管理器的登录提示操作，不把密码或令牌写入源码、命令或文档。上传成功后刷新仓库，确认app.py、fourflow、tests、data及README等文件可见。

## 在其他电脑运行

按README重建虚拟环境并安装requirements-lock.txt。需要音频转写时再显式执行tools/prepare_local_models.py下载公开语音权重。现有资金、人员和双图模型仍为合成训练模型，不用于真实案件评分。全新电脑安装尚未在本次准备中实测。

## 后续更新

在此副本维护要上传的代码，核对变更后执行git add、git commit和git push。原开发目录的改动应先检查再复制需要的源码；不要整目录覆盖runtime或data。私有仓库也不作为案件资料备份。以后若决定公开，另行核对文档、截图和提交历史中的案例信息及授权，并选择适当的开源许可证。

## 大文件

普通Git仓库拒绝超过100 MiB的文件。当前约138 MiB的Whisper model.bin已排除，通过准备脚本下载；经核对的完整交付ZIP也可另作为Release附件提供，不要将ZIP直接git add到源码仓库。

官方说明：
- https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github
- https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github

## 三份Word文档的独立目录

三份定稿放在根目录“项目文档”中，目录内README说明阅读顺序，项目首页也有入口。首次执行整项目git add、commit和push时，该目录会随源码一起上传，并在GitHub文件列表中独立显示。不要将Word文件散放到fourflow、tests或项目根目录。

这个文件夹只负责目录分开，访问权限跟随仓库；如果以后文档需要不同的访问权限，应另建仓库管理。
