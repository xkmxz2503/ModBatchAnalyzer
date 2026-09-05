# Minecraft Mods 批量分析器

这是一个单文件 Python 工具，用于批量读取 `mods/` 目录中的 Minecraft Mod JAR，并把识别结果写入 Excel。Minecraft 版本只读取 JAR 元数据中的 Minecraft 依赖声明，不读取文件名或 Mod 自身版本，也不会按版本做兼容性过滤。

## 使用方法

1. 将 `mods` 文件夹与程序放在同一目录，并把需要分析的 `.jar` 文件放入其中。
2. 运行 `ModBatchAnalyzer.exe`，或在开发环境执行 `python Main.py`。
3. 程序生成 `result.xlsx`。每个 JAR（包括元数据损坏或查询失败的文件）恰好占一行，非 JAR 文件会跳过。运行过程和网络重试/失败会直接输出到控制台，不创建日志文件。

## Excel 列

输出固定为 14 列：

`序号`、`Mod 文件名`、`Mod ID`、`名称`、`中文名`、`作者`、`Minecraft 版本`、`加载器`、`客户端安装`、`服务端安装`、`简介`、`来源链接`、`匹配置信度`、`查询状态`。

长文本列会自动换行；程序为 14 列设置适合 WPS 的明确列宽，并按每行文本长度显式计算行高，避免依赖 WPS 对自动调整尺寸支持不一致而出现白板、截断或重叠。

## PCL 风格搜索流程

程序优先从 `META-INF/neoforge.mods.toml`、`META-INF/mods.toml`、`fabric.mod.json` 和 `quilt.mod.json` 读取 Mod ID、名称、作者、简介、加载器及 Minecraft 依赖版本；Mod 自身 `version` 字段和文件名版本号不会填入 `Minecraft 版本`。文件名仍可用于搜索关键词。

MC 百科和 Modrinth 首先并行查询。Modrinth 使用公开搜索接口并限定项目类型为 Mod；MC 百科解析搜索页。没有直接结果的站点，再使用 Bing 进行站点限定发现：

- `site:mcmod.cn`
- `site:modrinth.com`
- `site:curseforge.com/minecraft/mc-mods`

CurseForge 始终通过 Bing 发现。Bing 只负责找到可能的详情页，候选仍需经过域名校验、项目 ID/slug/名称精确匹配和详情页确认，搜索摘要不会直接写入结果。整合包、资源包、插件等非 Mod 项目会排除。候选按 ID、slug、名称、文件名标识、加载器和版本评分，最高分结果落表；同分或分数接近时标记 `低，需人工审核`。

## 状态与限制

`匹配置信度` 只使用 `高`、`低，需人工审核`、`未找到`、`请求失败`。`查询状态` 按 MC 百科、Modrinth、Bing、CurseForge 记录结果，并可能追加 `多 Mod JAR，需人工审核` 和候选数量。

程序不需要 API Key，不保存 API Key，也不绕过验证码、反爬或访问控制。网络请求带有超时、重试和限流；任一来源失败不会中断其余 JAR 的处理。控制台会显示 `[开始]`、`[结果]`、`[跳过]`、`[元数据异常]`、`[网络重试]`、`[网络失败]` 和 `[完成]` 等诊断信息。

## 开发者构建

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install requests lxml xlwings pyinstaller
python -m unittest discover
```

如需构建 Windows 可执行文件，可执行仓库中的 `scripts/build.ps1`。运行 Excel 输出需要本机安装 Microsoft Excel，并安装 `xlwings`。
