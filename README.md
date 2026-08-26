# Minecraft Mods 批量分析器

基于爬取 [MC百科](https://www.mcmod.cn/) 的 Mods 批量分析器，它可以分析你的 Mods 文件夹下的 Mod，给出每个 Mod 对应的百科页面的名称、Mod 是否为*服务端需装*、Mod 是否为 *Forge Mod* 等信息。这些信息将汇总成 Excel 表显示。

## 使用方法

1. [下载最新版本](https://github.com/APeng215/ModBatchAnalyzer/releases/latest)的 ***ModBatchAnalyzer-x.x.x.exe***
2. 将想要分析的 ***mods*** 文件夹（注意是**文件夹**）与 ***ModBatchAnalyzer-x.x.x.exe*** 放在同一目录下
3. 运行 ***ModBatchAnalyzer-x.x.x.exe*** 

![使用方法](README_resources/使用方法1.png)

## 开发者构建

在仓库根目录使用 PowerShell 执行环境搭建脚本：

```powershell
.\scripts\setup.ps1
```

脚本会创建或更新 `.venv`，并安装 `requests`、`lxml`、`xlwings` 和 `pyinstaller`。如需在当前终端中手动使用虚拟环境，可执行：

```powershell
.\.venv\Scripts\Activate.ps1
```

环境准备完成后执行统一构建脚本：

```powershell
.\scripts\build.ps1
```

构建完成后，可执行文件位于 `dist/ModBatchAnalyzer.exe`；临时文件和生成的 spec 文件位于 `build/`。

## 画廊

![分析结果](README_resources/mods_analysis.png)
