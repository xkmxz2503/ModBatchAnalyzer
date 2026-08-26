# Repository Guidelines

## 项目结构

这是一个用于批量分析 Minecraft Mod 的单文件 Python 工具。`Main.py` 是唯一的程序入口，负责读取 Mod JAR、查询 MC 百科并生成 Excel。`README.md` 保存面向用户的使用说明；`README_resources/` 存放 README 截图。`mods/` 是本地待分析输入目录，`searchWeb.html`、`modWeb.html` 和 `result.xlsx` 是运行产物，均不得提交。

## 开发与运行

在仓库根目录使用虚拟环境，并安装当前代码直接依赖的库：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install requests lxml xlwings
python Main.py
```

运行前创建 `mods/` 并放入待分析的 JAR 文件。程序会访问 `mcmod.cn`、在根目录写入 HTML 缓存，并生成 `result.xlsx`；运行需要本机可用的 Excel 环境。仓库目前没有构建脚本或锁定的依赖清单。

## 代码风格

使用四个空格缩进。类名采用 `PascalCase`，函数、变量和文件使用 `snake_case`；新代码遵循现有同步执行模型与标准库优先的导入分组。改动应保持单文件架构和现有行为，避免无关重命名、全量格式化或额外框架。新增网络调用应设置合理超时，并避免日志输出用户路径、文件内容或其他敏感信息。

## 测试与验证

当前未配置自动化测试、格式化工具或覆盖率门槛。新增可复用逻辑时，在 `tests/` 下使用 `unittest` 编写 `test_*.py` 用例，模拟 HTTP 响应和临时 JAR 文件，执行 `python -m unittest discover`。每次相关改动还应手动验证：成功查询、无搜索结果、非 JAR 输入，以及生成的 Excel 列和值。

## 提交与拉取请求

Git 历史使用简短、祈使式英文标题，例如 `Update usage to README.md`。每个提交聚焦一个目的。拉取请求应说明行为变化、验证方式和关联 Issue；用户可见输出或 README 变化附截图或示例说明。不得提交 `mods/` 内容、抓取缓存、生成的 Excel、`build/`、`dist/` 或本机 IDE 文件。
