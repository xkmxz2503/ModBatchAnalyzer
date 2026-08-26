[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

function Stop-Setup {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    Write-Error "环境搭建失败：$Message" -ErrorAction Continue
    exit 1
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPath = Join-Path $repoRoot ".venv"
$venvPythonPath = Join-Path $venvPath "Scripts\python.exe"

$systemPython = Get-Command python -ErrorAction SilentlyContinue
if ($null -eq $systemPython) {
    Stop-Setup "未找到系统 Python。请先安装 Python 3，并确保 python 命令已加入 PATH。"
}

& $systemPython.Source --version
if ($LASTEXITCODE -ne 0) {
    Stop-Setup "无法运行系统 Python：$($systemPython.Source)。"
}

Write-Output "正在创建或更新虚拟环境：$venvPath"
& $systemPython.Source -m venv $venvPath
if ($LASTEXITCODE -ne 0) {
    Stop-Setup "创建虚拟环境失败。"
}

if (-not (Test-Path -LiteralPath $venvPythonPath -PathType Leaf)) {
    Stop-Setup "虚拟环境创建完成，但未找到 Python：$venvPythonPath。"
}

$packages = @("requests", "lxml", "xlwings", "pyinstaller")
$packageList = $packages -join " "
Write-Output "正在安装项目依赖：$packageList"
& $venvPythonPath -m pip install $packages
if ($LASTEXITCODE -ne 0) {
    Stop-Setup "依赖安装失败。请检查网络连接和 Python 包管理器配置。"
}

foreach ($package in @("requests", "lxml", "xlwings")) {
    & $venvPythonPath -c "import $package" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Stop-Setup "依赖安装完成后仍无法导入：$package。"
    }
}

& $venvPythonPath -m PyInstaller --version 2>$null
if ($LASTEXITCODE -ne 0) {
    Stop-Setup "依赖安装完成后仍无法运行 PyInstaller。"
}

Write-Output "环境搭建完成。虚拟环境 Python：$venvPythonPath"
Write-Output "下一步可执行：.\scripts\build.ps1"
