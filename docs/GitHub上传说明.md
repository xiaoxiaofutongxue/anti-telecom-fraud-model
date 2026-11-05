# GitHub 上传与后续更新说明

当前仓库已经建立并完成首次上传：

[反电诈模型仓库](https://github.com/xiaoxiaofutongxue/anti-telecom-fraud-model)

这份文件解释以后怎么更新。**正常使用系统不需要提交代码，也不需要重新创建仓库。**

## 1. 先分清两个本地文件夹

| 位置 | 用途 |
| --- | --- |
| `D:\Projects\FourFlowAnalysis` | 原开发和业务使用目录，可能有真实原件、核验意见和运行报告 |
| `D:\Projects\FourFlowAnalysis_GitHub` | 上传专用副本，放公开发布的代码、合成样例、小型模型和说明 |

它们不是自动同步的。修改原目录，不会自动出现在上传副本或网页上。后续先比较修改内容，再复制确实需要发布的代码或文档；不要整目录覆盖 `data` 或 `runtime`。

仓库当前为公开仓库。案件原件、个人核验库、导入批次、业务报告和大型语音权重不在公开提交范围内。

## 2. 三份 Word 单独放在哪里

根目录的 **项目文档** 文件夹集中放三份定稿，首页有独立链接；程序代码仍在根目录和 `fourflow` 中，不与 Word 散放在一起。

更新某一份 Word 时，把新定稿放回这个文件夹，并核对目录内 `README.md` 的文件名和说明。文件夹只实现目录分开，访问权限仍跟随仓库。

## 3. 先检查 Git 和当前目录

Git 是负责记录版本和上传变更的工具，GitHub 是存放远端仓库的网站，二者不是同一个程序。

在 PowerShell 中逐条执行：

```powershell
git --version
```

应显示 Git 版本。如果提示找不到命令，确认已安装 Git，并重新打开终端。原机 Git 的位置是 `E:\git\Git\cmd\git.exe`；这是原电脑配置，其他电脑不一定相同。必要时用完整路径，例如 `& "E:\git\Git\cmd\git.exe" --version`，后续的 `git` 也相应替换为完整路径。

进入**上传副本**：

```powershell
Set-Location "D:\Projects\FourFlowAnalysis_GitHub"
```

确认提交要发往哪里：

```powershell
git remote -v
```

应指向 `xiaoxiaofutongxue/anti-telecom-fraud-model`。如果不同，先查清所处目录，不要直接推送到不认识的仓库。

## 4. 看清这次到底改了什么

```powershell
git status --short
```

常见前缀：`M` 表示修改，`??` 表示新文件，`D` 表示删除。没有输出通常表示工作目录没有待提交变化。

查看文字改动：

```powershell
git diff
```

按需逐个检查要上传的文件。Word 是二进制文件，Git 不会像正文一样显示全部修改，需要下载/本地打开检查。

项目的 `.gitignore` 会忽略很多私有数据和运行输出，但忽略规则不能自动移除已经提交过的文件，也不能保证任意新文件都适合公开。新增样例、文档和截图需确认确实属于发布内容。

仓库的 `GitHub文件校验.json` 记录发布文件的 SHA-256 摘要，可以理解为各文件的“内容指纹”。发布文件变化时应同步更新清单；单独计算一个文件的指纹可用：

```powershell
Get-FileHash -Algorithm SHA256 .\README.md
```

这个命令只查看指纹，不会自动更新整份清单。

## 5. 将指定文件放进本次提交

例如本次只更新首页说明，可以执行：

```powershell
git add -- README.md GitHub文件校验.json
```

有其他文档时，把它们的实际相对路径一并写在后面。含空格的路径用双引号。`git add` 只是把指定文件选进“待提交清单”，尚未上传。

再次检查：

```powershell
git diff --cached --stat
```

这里显示本次已选文件和改动概况，确认只包含预期文件。

## 6. 保存本地版本，再上传

```powershell
git commit -m "完善零基础使用说明"
```

`commit` 表示“保存一个本地版本”，引号内是本次修改说明，可以换成实际内容。提交显示名和邮箱只是版本署名，不是 GitHub 密码。

再上传：

```powershell
git push origin main
```

`push` 才会把已保存的提交传到 GitHub；`origin` 是当前远端仓库的简称，`main` 是当前使用的主分支。

第一次需要认证时按 Git 凭据管理器的提示登录自己的 GitHub 账户，不把密码或令牌写入源码、文档或仓库地址。

## 7. 确认真正上传成功

1. 确认推送命令正常结束，没有权限或网络错误。
2. 刷新仓库首页，打开修改过的文件，检查最新文字。
3. 用下面两条命令比较本地版本和远端版本。

```powershell
git rev-parse HEAD
```

```powershell
git ls-remote --heads origin main
```

前者显示本地最新提交的长编号；后者显示远端 `main` 的编号。相同说明远端已经收到这个版本。仅执行了 `git add` 或 `git commit` 不代表上传完成。

如果提示远端有本地没有的提交，先检查是否有人通过网页或其他电脑更新过，正常同步和解决冲突；不要直接强制覆盖远端。

## 8. 新电脑下载和大文件怎样处理

只想使用系统，直接按 [README 的首次安装](../README.md#install-new)下载 ZIP、解压、建环境即可，不要求先学 Git。

大型语音权重由 `tools/prepare_local_models.py` 下载，不放进普通源码提交。普通 GitHub Git 仓库会阻止超过 100 MiB 的单个文件；本项目排除的语音 `model.bin` 超过此大小。体积大的完整交付包如需公开分发，可另行准备 Release 附件，不能混入私人工作区备份。

官方参考：[GitHub 本地代码上传说明](https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github) · [GitHub 大文件限制](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)。

[返回项目首页](../README.md)
