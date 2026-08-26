[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

function Stop-Build {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    Write-Error "构建失败：$Message" -ErrorAction Continue
    exit 1
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"
$mainPath = Join-Path $repoRoot "Main.py"
$buildPath = Join-Path $repoRoot "build"
$distPath = Join-Path $repoRoot "dist"
$outputPath = Join-Path $distPath "ModBatchAnalyzer.exe"

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    Stop-Build "未找到项目虚拟环境 Python：$pythonPath。请先执行 python -m venv .venv。"
}

if (-not (Test-Path -LiteralPath $mainPath -PathType Leaf)) {
    Stop-Build "未找到入口脚本：$mainPath。"
}

$requiredPackages = @("requests", "lxml", "xlwings")
$missingPackages = @()
foreach ($package in $requiredPackages) {
    & $pythonPath -c "import $package" 2>$null
    if ($LASTEXITCODE -ne 0) {
        $missingPackages += $package
    }
}

if ($missingPackages.Count -gt 0) {
    $packageList = $missingPackages -join " "
    Stop-Build "虚拟环境缺少依赖：$packageList。请执行 python -m pip install $packageList pyinstaller。"
}

& $pythonPath -m PyInstaller --version 2>$null
if ($LASTEXITCODE -ne 0) {
    Stop-Build "虚拟环境中未找到 PyInstaller。请执行 python -m pip install pyinstaller。"
}

New-Item -ItemType Directory -Force -Path $buildPath, $distPath | Out-Null

$pyInstallerArguments = @(
    "--onefile",
    "--console",
    "--clean",
    "--noconfirm",
    "--name", "ModBatchAnalyzer",
    "--distpath", $distPath,
    "--workpath", $buildPath,
    "--specpath", $buildPath,
    $mainPath
)

$buildExitCode = 0
Push-Location $repoRoot
try {
    & $pythonPath -m PyInstaller @pyInstallerArguments
    $buildExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

if ($buildExitCode -ne 0) {
    Stop-Build "PyInstaller 返回退出码 $buildExitCode。"
}

if (-not (Test-Path -LiteralPath $outputPath -PathType Leaf)) {
    Stop-Build "构建命令已完成，但未找到输出文件：$outputPath。"
}

Write-Output "构建成功：$outputPath"
