# ArchiveFixer

> 版本提示：当前源码与新构建对应 **v0.0.5 测试版**。v0.0.1～v0.0.4 的旧附件不包含本轮修复。请优先使用 Releases 中的 v0.0.5 便携 ZIP；附件尚未发布时可以从源码运行。

下载的压缩包有时会被改名成 `example.7zshan`、`example.shan7z`，或拆成 `.001`、`.002` 等分卷。反复改名、解压和输入密码很麻烦，所以做了这个工具。

目前支持 Windows，可调用本机安装的 7-Zip、WinRAR 或 Bandizip 解压。

这是一个 Windows 图形工具。它能按你的选择把伪装后缀改成 `.7z` 或 `.zip`，例如 `旅行照片.shan7z` → `旅行照片.7z`。它也能处理 `.7z.001` 等分卷、批量解压多个压缩包、依次尝试最多 5 个密码，并继续展开压缩包中的下一层。拖入含压缩包的文件夹也可以处理；结果默认放在原位置旁的独立文件夹中。

**Windows 系统是必需的。普通用户运行 EXE 无需安装 Python 或开发工具；解压时需安装所选的压缩软件。**可选 [7-Zip](https://www.7-zip.org/download.html) 的 `7z.exe`、[WinRAR](https://www.rarlab.com/download.htm) 的 `WinRAR.exe` 或 [Bandizip](https://en.bandisoft.com/bandizip/) 的 `bz.exe`。WinRAR 不能选择只支持 RAR 的 `Rar.exe`。Bandizip 部分便携版和 Microsoft Store 版不附带 `bz.exe`，这时请安装包含命令行工具的版本。仅改名不需要安装解压软件。EXE 不包含这三个第三方程序；窗口会尝试自动找到它们，找不到时可点击“浏览”选择对应程序。

## 普通用户：下载便携版

1. 打开本项目的 GitHub **Releases** 页面，选择最新的测试版本并下载 `ArchiveFixer-portable.zip`。把 ZIP 放在“下载”或其他方便的位置即可，无需安装 Git。
2. 右键 ZIP → “全部解压”，打开解压出的 `ArchiveFixer` 文件夹，双击里面的 `ArchiveFixer.exe`。不要只把文件夹里的 EXE 单独拿出来运行。
3. 如果需要解压，先安装上面列出的任意一种压缩软件；只改名可以略过这一步。
4. 在窗口中选择“改名后缀” `.7z` 或 `.zip`，并选择“解压程序”。点击“添加文件”或“添加文件夹”，也可以把文件、文件夹拖进窗口。检查列表里的改名预览。
5. 有密码时，在“密码 1”至“密码 5”输入可能的密码，程序会依次尝试。点击“保存预设”才会为下次启动保存。没有密码时全部留空。
6. 只需改后缀，选中项目后点“改名所选”。需要解压时点“改名并逐层解压所选”。如果文件名已正确，可点“逐层解压所选”。
7. 默认会在压缩包旁生成如 `example_解压` 的文件夹，展开成功后整理掉多余的压缩层目录。若设置了“解压目标目录”，结果会放到所选目录。文件夹任务的结果仍位于原文件夹。

**删除选项默认关闭。**只有主动勾选“全部层级成功后删除原始及中间压缩包”，程序才会在任务成功后清理源压缩包。未勾选时，原包和“压缩层备份”目录都会保留，会占用更多磁盘空间。第一次使用建议用文件副本试运行。改名或结果目录发生冲突时，程序会跳过并在窗口记录原因；不会默默覆盖已有文件。同一批中某个任务失败，不会阻止其他成功任务交付结果；失败任务的源压缩包会保留。最终内容已交付但清理失败时，任务仍计为成功，并单独显示“清理提示”和保留路径。

### 文件名示例

| 处理前 | 改名后 |
| --- | --- |
| `旅行照片.shan7z` | `旅行照片.7z` |
| `动漫合集.7shanz` | `动漫合集.7z` |
| `旅行照片.shan7z`，选择 `.zip` | `旅行照片.zip` |
| `example.7zshan` | `example.7z` |
| `hello.world.shan7z` | `hello.world.7z` |
| `example.7z.001删` | `example.7z.001` |

**选择 `.zip` 只改变文件名，不会把 7z 内容转换成 ZIP 格式。**请按压缩包的实际格式选择后缀。文件名中的中文、空格、数字和多个点都可以保留；分卷仍保留 `.001`、`.002` 编号。所选后缀也会用于文件夹任务中识别出的压缩包，执行前可在列表中查看待改名数和冲突数。文件夹扫描会识别 `.shan7z`、`.7shanz` 和 `.7zshan`。能否解压取决于所选软件是否支持文件的真实格式。

手动添加单个文件时，改名按钮会按文件名规则处理，即使文件内容并非压缩包。执行前请核对预览和文件来源。

## 常见问题

- **找不到解压程序**：先选择窗口中的 7-Zip、WinRAR 或 Bandizip；如未自动识别，点击“浏览”分别选择 `7z.exe`、`WinRAR.exe` 或 `bz.exe`。改名功能可单独使用。
- **密码不对或压缩包损坏**：检查密码及所有分卷是否完整，再查看窗口下方记录。不能成功完成的任务不会删除源压缩包。
- **失败后出现 `.archive_work_*` 或 `.archive_stage_*` 文件夹**：这是解压与整理过程的临时目录。工具会在操作记录中写出失败任务留下的目录。先检查里面是否有需要保留的文件；确认不需要后可手动删除，再重试任务。
- **目标 `.7z` 或 `.zip` 已存在**：工具会报告冲突并跳过；请先自行核对两个文件，不要直接覆盖。
- **文件夹没有显示可处理项目**：空目录，或里面没有可识别压缩包。存档、备份以及部分程序数据压缩包会视为最终内容，避免继续误展开。
- **提示嵌套文件夹重复选择**：同一批不要同时选择父文件夹和其中的子文件夹；保留其中一个任务后重试。
- **链接目录中的压缩包没有被扫描**：工具会跳过符号链接和 Windows junction，避免处理到所选目录之外的文件。
- **无法改名或写入**：检查文件是否正被其他程序占用，以及当前用户是否有该目录的写入权限。单个文件失败会记录原因，不会让整批任务直接退出。
- **Windows 首次启动有安全提示**：便携版采用目录打包，不使用单文件自解压或 UPX。EXE 仍没有代码签名；这不能保证免于杀毒拦截。请确认文件来自你信任的发布页；也可检查源码并自己构建。
- **提示“文件包含病毒或潜在的垃圾软件”**：这是 Microsoft Defender 的检测。打开“Windows 安全中心 → 病毒和威胁防护 → 保护历史记录”查看检测名称，并保持隔离。可运行 `Get-FileHash "$env:USERPROFILE\Downloads\ArchiveFixer-portable.zip" -Algorithm SHA256`，与 Release 提供的 `SHA256SUMS.txt` 比较；哈希仅证明文件一致，不代表没有风险。将有争议的构建通过 [Microsoft Security Intelligence 文件提交页面](https://www.microsoft.com/wdsi/filesubmission)提交给微软审核，并等待结论。不要因为来自本仓库或哈希一致就直接放行检测到的文件。

密码预设仅在点击“保存预设”后写入当前 Windows 用户的 `%APPDATA%\ArchiveFixer\password-presets.json`，使用 Windows DPAPI 加密。旧版明文预设可以读取，再次保存时转换为加密格式。加密文件不能直接复制给另一个 Windows 用户使用；文件损坏或无法解密时密码框为空，需要重新输入。解压期间密码仍需传给本机解压程序，DPAPI 保护的是保存文件。不要上传预设文件或真实密码。

## 从源码运行（开发者）

从全新 Windows 电脑开始：

1. 安装 [Python 3.13 Windows 版](https://www.python.org/downloads/windows/)。安装时勾选 **Add python.exe to PATH**。安装完成后打开“命令提示符”，输入 `python --version`，确认显示 `Python 3.13.x`。构建脚本还要用到安装 Python 时提供的 `py` 启动器。
2. 在 GitHub 仓库页面点击 **Code → Download ZIP**，下载并解压项目。若已有 Git，也可用 `git clone <仓库地址>`。进入解压后的项目文件夹，在地址栏输入 `cmd` 并按 Enter，就能在该目录打开命令提示符。
3. 依次执行：

```bat
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python main.py
```

4. 需要解压时另行安装 7-Zip、WinRAR 或 Bandizip；源码和 EXE 使用同一套查找逻辑。WinRAR 请选择 `WinRAR.exe`，Bandizip 请选择 `bz.exe`。

如果系统的 `python` 命令找不到，可以试 `py -3.13 --version`，然后用 `py -3.13 -m venv .venv` 创建环境。

## 构建 Windows EXE

安装 Python 3.13 后，双击项目根目录的 `build.bat`，或在该目录的命令提示符运行 `build.bat`。脚本会创建 `.venv`、安装依赖，并由 `build_release.py` 在独立工作区构建。GUI 与拖拽冒烟检查成功后才替换 `dist\ArchiveFixer` 和 `dist\ArchiveFixer-portable.zip`；构建失败保留旧产物，替换中断会尝试恢复。便携 ZIP 包含使用说明、许可证和第三方声明，另生成 `dist\SHA256SUMS.txt`。构建需要网络下载 Python 依赖，使用便携版无需 Python。

构建后可运行 `dist\ArchiveFixer\ArchiveFixer.exe --smoke-test`。退出代码为 0 表示 GUI 和拖拽组件加载成功，并完成临时文件实际改名。发布时上传 `ArchiveFixer-portable.zip` 与 `SHA256SUMS.txt`；第三方声明已包含在 ZIP 中，也可单独附上。源码仓库不提交 `dist/`。

## 工作原理

文件或文件夹进入列表后，程序扫描候选文件，检查压缩包特征和已知后缀，解析 `.001` 等分卷编号与伪装后缀，按所选 `.7z` 或 `.zip` 预览目标文件名。执行改名时检查所选项目的冲突；“改名并解压”还会先检查重复文件夹、解压程序、并行任务数和现有输出目录。解压时调用本机 `7z.exe`、`WinRAR.exe` 或 `bz.exe`，在临时目录逐层展开，用同一组密码依次尝试。每个任务成功后提交结果，按删除选项清理压缩包。包含 `save`、`backup`、`存档` 等名称的存档压缩包（包括伪装后缀），以及已知程序数据容器，会作为最终文件保留。

主要源码：`archive_logic.py` 负责命名与冲突预览；`archive_extractors.py` 负责解压程序适配；`archive_engine.py` 负责发现、分卷、解压和结果整理；`archive_gui.py` 负责窗口与拖拽；`password_store.py` 负责密码预设；`main.py` 是启动入口。

## 测试

运行 `python -m unittest discover -s tests -v`。真实压缩包测试使用本机解压程序，未安装的后端显示跳过；可设置环境变量 `ARCHIVEFIXER_WINRAR` 和 `ARCHIVEFIXER_BANDIZIP` 指向相应可执行文件，再验证多层、分卷、密码切换与损坏包保护。测试仅生成临时样本，不解压用户下载目录。GitHub Actions 在 Windows 上自动运行可用的测试与 GUI 冒烟检查。
