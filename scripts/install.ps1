<#
.SYNOPSIS
    repo-autopilot 插件的 Windows 一键安装入口（**薄包装**）。

.DESCRIPTION
    这个脚本只做一件 install.py 做不到的事：**先找到一个能用的 Python**，然后把它交出去。
    真正的检查、报告、引导逻辑全在 scripts/install.py 里（那边能被 Python 测试覆盖）。
    刻意不在这里重复一遍逻辑 —— 两套判据迟早会漂移。

    找解释器分**两趟**：先找「版本够 **且依赖齐**」的，找不到再退而求其次找「版本够」的。
    不分两趟的话，本机那种「PATH 上有一个没装依赖的 python」会把真正能用的解释器挡在后面
    （2026-09-18 实测就是这个症状）。

    找不到 Python 时，探测 winget / uv 并打印**可复制粘贴**的补救命令；
    只有加 -UseUv 且本机确有 uv 时才会真的建环境（默认只打印，不下载、不安装）。

.NOTES
    本文件是 **UTF-8 with BOM**。这不是洁癖：Windows PowerShell 5.1 在没有 BOM 时
    会把 UTF-8 当 ANSI 读，中文注释被解码成乱码，解析器接着就在字符串里找不到收尾引号、
    直接报 ParserError —— 实测整个脚本跑不起来。改动本文件时**务必保留 BOM**。

.PARAMETER RepoRoot
    repo-autopilot 仓库的绝对路径；不给则交给 install.py 自动查找。

.PARAMETER Python
    指定解释器（绝对路径或命令名）。优先级最高。

.PARAMETER InstallDeps
    缺依赖时用当前解释器的 pip 装上。

.PARAMETER UseUv
    缺依赖时用 uv 建 .venv 并装依赖（需要本机有 uv）。

.PARAMETER DryRun
    只打印要做什么，不执行。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -RepoRoot "D:\work\repo-autopilot"

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -RepoRoot "D:\work\repo-autopilot" -UseUv
#>
[CmdletBinding()]
param(
    [string]$RepoRoot,
    [string]$Python,
    [switch]$InstallDeps,
    [switch]$UseUv,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$installPy = Join-Path $here 'install.py'
if (-not (Test-Path -LiteralPath $installPy)) {
    Write-Host "[!] 找不到 $installPy —— 请确认脚本在 scripts/ 目录下运行。"
    exit 1
}

# 探测代码用**单引号**包住，避免 PowerShell 抢先展开里面的内容。
#
# 两条探测都**刻意写成不会往 stderr 写东西**：版本不够返回 1，依赖不齐返回 3。
# 为什么这么讲究：探测**注定会失败至少一次**（PATH 上那个依赖不齐的解释器），
# 而 `$ErrorActionPreference = 'Stop'` 会让原生命令的 stderr 变成**终止性错误**，
# 直接把整个脚本打断 —— 实测就是这么挂的（只输出了 8 行就没了）。
# 让探测"用退出码说话、不用 stderr 抱怨"，比事后想办法吞掉 stderr 可靠得多。
$probeVersion = 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'
# 依赖探测里**一个双引号都不许有**：PowerShell 5.1 给原生命令传参时对双引号的处理是有损的，
# 嵌在代码里的 `"yaml"` 经函数传参后会被搞坏，Python 报语法错、退出码 1 ——
# 于是**装好依赖的解释器也被判成"依赖不齐"**（实测：venv 明明齐，却被判不齐）。
# 所以这里用 PowerShell 的单引号转义（`''`）在 Python 里造出字符串，全程不见双引号。
$probeDeps = 'import sys, importlib.util; raise SystemExit(0 if all(map(importlib.util.find_spec, ''yaml requests numpy''.split())) else 3)'

# 候选按优先级排列；每项是「命令 + 前置参数」的数组（`py` 启动器需要 `-3.12`）。
$candidates = @()
if ($Python) { $candidates += , @($Python) }
if ($env:REPO_AUTOPILOT_PYTHON) { $candidates += , @($env:REPO_AUTOPILOT_PYTHON) }
foreach ($venv in @(
        (Join-Path $here '..\.venv\Scripts\python.exe'),
        (Join-Path $here '..\..\.venv\Scripts\python.exe')
    )) {
    if (Test-Path -LiteralPath $venv) { $candidates += , @($venv) }
}
$candidates += , @('python3')
$candidates += , @('python')
if (Get-Command 'py' -ErrorAction SilentlyContinue) { $candidates += , @('py', '-3.12') }

function Invoke-Candidate {
    param([string[]]$Argv, [string]$Code)
    $exe = $Argv[0]
    $rest = @()
    if ($Argv.Count -gt 1) { $rest = $Argv[1..($Argv.Count - 1)] }
    # 双保险：就算某个候选真的往 stderr 写了东西，也不许它把整个安装打断。
    # 探测失败是**预期情况**，不是错误。
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $exe @rest -c $Code *> $null
    }
    finally {
        $ErrorActionPreference = $previous
    }
    return ($LASTEXITCODE -eq 0)
}

Write-Host '=== 0/4 找解释器 ==='
$foundArgv = $null

# 第一趟：版本够 且 依赖齐
foreach ($candidate in $candidates) {
    if (-not (Get-Command $candidate[0] -ErrorAction SilentlyContinue)) { continue }
    if ((Invoke-Candidate -Argv $candidate -Code $probeVersion) -and
        (Invoke-Candidate -Argv $candidate -Code $probeDeps)) {
        $foundArgv = $candidate
        Write-Host "  [OK] $($candidate -join ' ')（版本够 + 依赖齐）"
        break
    }
}

# 第二趟：只要求版本够（依赖缺了交给 install.py 报，它报得更细）
if (-not $foundArgv) {
    foreach ($candidate in $candidates) {
        if (-not (Get-Command $candidate[0] -ErrorAction SilentlyContinue)) { continue }
        if (Invoke-Candidate -Argv $candidate -Code $probeVersion) {
            $foundArgv = $candidate
            Write-Host "  [OK] $($candidate -join ' ')（版本够，但依赖不齐 —— 下一步会报出来）"
            break
        }
    }
}

if (-not $foundArgv) {
    Write-Host '  [--] 一个可用的都没有'
    Write-Host ''
    Write-Host '[!] 没找到 Python >= 3.12。补法（挑一条自己执行）：'
    $hasUv = [bool](Get-Command 'uv' -ErrorAction SilentlyContinue)
    $hasWinget = [bool](Get-Command 'winget' -ErrorAction SilentlyContinue)
    if ($hasUv) {
        Write-Host '  · 有 uv：'
        Write-Host '      uv python install 3.12'
        Write-Host '      uv venv .venv --python 3.12'
        Write-Host '      uv pip install --python .venv pyyaml requests numpy'
        Write-Host '      $env:REPO_AUTOPILOT_PYTHON = ".\.venv\Scripts\python.exe"'
    }
    if ($hasWinget) {
        Write-Host '  · 有 winget：'
        Write-Host '      winget install --id astral-sh.uv          # 推荐：uv 能顺带管 Python'
        Write-Host '      winget install --id Python.Python.3.12    # 或者直接装 Python'
    }
    if (-not $hasUv -and -not $hasWinget) {
        Write-Host '  · 既没有 uv 也没有 winget：https://www.python.org/downloads/'
    }
    Write-Host '  · 装完**重开一个终端**再跑一次。'
    exit 1
}

# 交给 install.py：所有检查与报告都在那边，这里只转参数。
$forward = @($installPy)
if ($RepoRoot) { $forward += @('--repo-root', $RepoRoot) }
if ($Python) { $forward += @('--python', $Python) }
if ($InstallDeps) { $forward += '--install-deps' }
if ($UseUv) { $forward += '--use-uv' }
if ($DryRun) { $forward += '--dry-run' }

$exe = $foundArgv[0]
$rest = @()
if ($foundArgv.Count -gt 1) { $rest = $foundArgv[1..($foundArgv.Count - 1)] }
& $exe @rest @forward
exit $LASTEXITCODE
