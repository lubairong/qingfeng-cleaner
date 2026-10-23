#Requires -Version 5.1
<#
.SYNOPSIS
    清风清理 QingFeng Cleaner —— 打包 Windows 交付产物（默认只产出目录版）。

.DESCRIPTION
    在项目内的 .venv 里安装 PyInstaller，然后产出交付结构：

        dist\QingFengCleaner-onedir\QingFengCleaner.exe   目录版（**唯一默认交付形态**）
        dist\启动清风清理.bat                               双击即用的启动器（转调目录版 exe）

    为什么默认不再交付单文件 exe：
      PyInstaller onefile 每次启动都要把自身解压到临时目录；实测在**中文路径**下会弹出
      「Could not create temporary directory!」并挂起（同一 exe 放到 ASCII 路径、真实用户令牌下才正常）。
      目录版不需要运行时自解压，已在真实用户令牌 + 中文路径下验证可运行，因此作为唯一交付形态。
      需要单文件版做实验时显式加 `-OneFile`，产物落在 `dist\_experimental\`，不属于正式交付物。

    产品本体是零第三方依赖的（只用 Python 3.12 标准库 + tkinter + ctypes）；
    这里的 .venv / PyInstaller 只是打包工具链，运行时不需要 Python 环境。

    如果 pip 装不上（网络/权限受限），脚本会明确报错并给出一条始终可用的替代路径：
    直接用 packaging\run_dev.bat 跑源码，功能完全一致。

.PARAMETER Python
    指定用于创建 .venv 的基础解释器；默认自动查找（本机内置 3.12 → PATH 上的 python）。

.PARAMETER OneFile
    额外构建单文件 exe 作为**实验性**产物，输出到 dist\_experimental\。默认不构建、不交付。

.PARAMETER SkipInstall
    跳过 PyInstaller 安装步骤（要求 .venv 里已经有 PyInstaller）。

.PARAMETER NoClean
    不向 PyInstaller 传 --clean（复用缓存，构建更快，但可能残留旧中间产物）。

.PARAMETER ForceRecreateVenv
    删除并重建 .venv。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -OneFile

.NOTES
    退出码：0 成功；1 参数/环境错误；2 打包工具链不可用（网络/权限受限）；3 打包失败。
#>
[CmdletBinding()]
param(
    [string]$Python = '',
    [switch]$OneFile,
    [switch]$SkipInstall,
    [switch]$NoClean,
    [switch]$ForceRecreateVenv
)

$ErrorActionPreference = 'Stop'
$Script:Root = Split-Path -Parent $PSScriptRoot
$Script:Sw = [System.Diagnostics.Stopwatch]::StartNew()

function Write-Head([string]$Text) {
    Write-Host ''
    Write-Host "=== $Text ===" -ForegroundColor Cyan
}
function Write-Info([string]$Text) { Write-Host "    $Text" }
function Write-Ok([string]$Text) { Write-Host "[ OK ] $Text" -ForegroundColor Green }
function Write-Warn([string]$Text) { Write-Host "[WARN] $Text" -ForegroundColor Yellow }
function Write-Fail([string]$Text) { Write-Host "[FAIL] $Text" -ForegroundColor Red }

function Get-BundledPythonCandidates {
    $list = New-Object System.Collections.ArrayList
    if ($Python) { [void]$list.Add($Python) }
    if ($env:QF_PYTHON) { [void]$list.Add($env:QF_PYTHON) }
    # 本机 DeepSeek Harness 内置 Python 3.12（存在则优先）
    [void]$list.Add('C:\Users\HUAWEI\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe')
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) { [void]$list.Add($cmd.Source) }
    return $list
}

function Resolve-BasePython {
    foreach ($candidate in (Get-BundledPythonCandidates)) {
        if (-not $candidate) { continue }
        if (Test-Path -LiteralPath $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    throw '找不到可用的 Python 3.12 解释器。请用 -Python <路径> 指定，或把 python 加入 PATH。'
}

function Test-VenvPip {
    param([string]$VenvPy)
    if (-not (Test-Path -LiteralPath $VenvPy)) { return $false }
    & $VenvPy -m pip --version 1>$null 2>$null
    return ($LASTEXITCODE -eq 0)
}

function Test-HasPyInstaller {
    param([string]$VenvPy)
    & $VenvPy -c "import PyInstaller" 1>$null 2>$null
    return ($LASTEXITCODE -eq 0)
}

function Invoke-PipInstall([string]$VenvPython, [string[]]$PipArgs) {
    # 先走标准 pip；受限沙箱下 0o700 临时目录会 PermissionError，再用兼容包装器重试。
    & $VenvPython -m pip @PipArgs
    if ($LASTEXITCODE -eq 0) { return 0 }
    Write-Warn '标准 pip 安装失败，改用受限沙箱兼容包装器（tools\pip_sandbox.py）重试…'
    & $VenvPython (Join-Path $Script:Root 'tools\pip_sandbox.py') @PipArgs
    return $LASTEXITCODE
}

function New-LauncherBat {
    param([string]$DistDir)
    # 启动器内容保持纯 ASCII（文件名中文即可），转调目录版 exe 并透传参数/退出码。
    $batPath = Join-Path $DistDir '启动清风清理.bat'
    $lines = @(
        '@echo off',
        'rem QingFeng Cleaner launcher -- run the onedir build (portable, no self-extract)',
        'setlocal',
        '"%~dp0QingFengCleaner-onedir\QingFengCleaner.exe" %*',
        'set "rc=%ERRORLEVEL%"',
        'exit /b %rc%'
    )
    Set-Content -LiteralPath $batPath -Value $lines -Encoding ASCII
    return $batPath
}

try {
    Set-Location -LiteralPath $Script:Root

    Write-Head '清风清理 QingFeng Cleaner — 打包交付产物（默认目录版）'
    Write-Info "项目根目录 : $Script:Root"
    Write-Info "PowerShell : $($PSVersionTable.PSVersion)"
    Write-Info "单文件实验版 : $(if ($OneFile) { '构建（-OneFile，输出到 dist\_experimental）' } else { '不构建（默认仅目录版）' })"

    # ---- 0. 基础检查 -------------------------------------------------------
    $mainPy = Join-Path $Script:Root 'main.py'
    if (-not (Test-Path -LiteralPath $mainPy)) { throw "缺少 main.py：$mainPy" }

    # 项目内临时目录：pip / PyInstaller 的临时文件都留在这里（已被 .gitignore 忽略）
    $tmpDir = Join-Path $Script:Root '.tmp\pytmp'
    New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null
    $env:TMP = $tmpDir
    $env:TEMP = $tmpDir

    $basePy = Resolve-BasePython
    Write-Info "基础 Python : $basePy"

    # ---- 1. 虚拟环境 -------------------------------------------------------
    Write-Head '1/6 准备项目内虚拟环境 (.venv)'
    $venvDir = Join-Path $Script:Root '.venv'
    $venvPy = Join-Path $venvDir 'Scripts\python.exe'

    if ($ForceRecreateVenv -and (Test-Path -LiteralPath $venvDir)) {
        Remove-Item -LiteralPath $venvDir -Recurse -Force -ErrorAction SilentlyContinue
    }

    if (Test-VenvPip $venvPy) {
        Write-Ok "复用已有 .venv：$venvPy"
    }
    else {
        if (Test-Path -LiteralPath $venvDir) {
            Remove-Item -LiteralPath $venvDir -Recurse -Force -ErrorAction SilentlyContinue
        }
        Write-Info "创建 .venv（$basePy -m venv）…"
        & $basePy -m venv $venvDir
        if (-not (Test-VenvPip $venvPy)) {
            Write-Warn '标准 venv（含 pip 引导）失败，尝试受限沙箱兼容路径：--without-pip + ensurepip 包装器'
            if (Test-Path -LiteralPath $venvDir) {
                Remove-Item -LiteralPath $venvDir -Recurse -Force -ErrorAction SilentlyContinue
            }
            & $basePy -m venv --without-pip $venvDir
            if ($LASTEXITCODE -ne 0) { throw "创建 .venv 失败：$venvDir" }
            & $venvPy (Join-Path $Script:Root 'tools\sandbox_exec.py') -m ensurepip --upgrade --default-pip
            if (-not (Test-VenvPip $venvPy)) { throw '.venv 已创建但 pip 不可用（ensurepip 失败）' }
        }
        Write-Ok "已创建 .venv：$venvPy"
    }

    # ---- 2. PyInstaller ----------------------------------------------------
    Write-Head '2/6 检查 PyInstaller'
    if (Test-HasPyInstaller $venvPy) {
        $ver = (& $venvPy -c "import PyInstaller; print(PyInstaller.__version__)" 2>$null)
        Write-Ok "PyInstaller 已就绪（$ver）"
    }
    elseif ($SkipInstall) {
        Write-Fail '未安装 PyInstaller，且指定了 -SkipInstall。'
        Write-Info '先不带 -SkipInstall 跑一次，或手动：.venv\Scripts\python -m pip install pyinstaller'
        exit 2
    }
    else {
        Write-Info '在 .venv 中安装 PyInstaller（需要网络，约 2~5 MB）…'
        $rc = Invoke-PipInstall $venvPy @(
            'install', '--disable-pip-version-check', '--no-input', '--no-cache-dir', 'pyinstaller'
        )
        if ($rc -ne 0 -or -not (Test-HasPyInstaller $venvPy)) {
            Write-Fail 'PyInstaller 安装失败（网络或权限受限）。'
            Write-Info '替代方案（无需任何安装，功能完全一致）：packaging\run_dev.bat'
            Write-Info '或先手动在项目内安装：.venv\Scripts\python -m pip install pyinstaller'
            exit 2
        }
        Write-Ok 'PyInstaller 安装完成'
    }

    # ---- 通用 ---------------------------------------------------------------
    $distDir = Join-Path $Script:Root 'dist'
    New-Item -ItemType Directory -Force -Path $distDir | Out-Null
    $icon = Join-Path $Script:Root 'assets\app.ico'
    # 入口用 packaging\launcher.py：它在调用 main.main() 前修复窗口化 exe 的
    # sys.stdout / sys.stderr（--noconsole 打包会把它们置为 None，否则
    # --scan-only / --version 会抛 AttributeError 并弹出错误框卡死进程）。
    $entry = Join-Path $Script:Root 'packaging\launcher.py'
    if (-not (Test-Path -LiteralPath $entry)) { throw "缺少打包入口：$entry" }

    # 清理旧产物：单文件 exe 已不再是交付物（中文路径下自解压不可靠），一律删除
    $legacyOnefile = Join-Path $distDir 'QingFengCleaner.exe'
    if (Test-Path -LiteralPath $legacyOnefile) {
        Remove-Item -LiteralPath $legacyOnefile -Force -ErrorAction SilentlyContinue
        Write-Info '已移除旧的 dist\QingFengCleaner.exe（单文件版不再交付）'
    }

    # ---- 3. 打包目录版（唯一默认交付形态） ---------------------------------
    Write-Head '3/6 PyInstaller 打包目录版（--onedir / --windowed）'
    $onedirParent = Join-Path $Script:Root 'build\onedir'
    $onedirDist = Join-Path $onedirParent 'dist'
    $onedirTarget = Join-Path $distDir 'QingFengCleaner-onedir'
    if (Test-Path -LiteralPath $onedirTarget) {
        Remove-Item -LiteralPath $onedirTarget -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $onedirParent) {
        Remove-Item -LiteralPath $onedirParent -Recurse -Force -ErrorAction SilentlyContinue
    }
    $onedirArgs = @(
        '-m', 'PyInstaller',
        '--noconfirm',
        '--onedir',
        '--windowed',
        '--name', 'QingFengCleaner',
        '--paths', $Script:Root,
        '--distpath', $onedirDist,
        '--workpath', (Join-Path $onedirParent 'work'),
        '--specpath', $onedirParent,
        '--add-data', ((Join-Path $Script:Root 'qingfeng\data') + ';qingfeng/data')
    )
    if (-not $NoClean) { $onedirArgs += '--clean' }
    if (Test-Path -LiteralPath $icon) { $onedirArgs += @('--icon', $icon) }
    $onedirArgs += $entry

    Write-Info ("执行：.venv\Scripts\python.exe " + ($onedirArgs -join ' '))
    $onedirOk = $false
    for ($attempt = 1; $attempt -le 3 -and -not $onedirOk; $attempt++) {
        if ($attempt -gt 1) {
            Write-Warn ("目录版打包重试（第 {0} 次）…" -f $attempt)
            Start-Sleep -Seconds 2
            Remove-Item -LiteralPath $onedirParent -Recurse -Force -ErrorAction SilentlyContinue
        }
        & $venvPy @onedirArgs
        if ($LASTEXITCODE -ne 0) {
            Write-Warn '直接 PyInstaller 失败，改用受限沙箱兼容包装器重试…'
            & $venvPy (Join-Path $Script:Root 'tools\sandbox_exec.py') @onedirArgs
        }
        $onedirOk = ($LASTEXITCODE -eq 0) -and
            (Test-Path -LiteralPath (Join-Path $onedirDist 'QingFengCleaner\QingFengCleaner.exe'))
    }
    if (-not $onedirOk) {
        Write-Fail 'PyInstaller 目录版打包失败（见上方日志）。'
        Write-Info '替代方案：packaging\run_dev.bat 直接运行源码，功能完全一致。'
        exit 3
    }
    $builtDir = Join-Path $onedirDist 'QingFengCleaner'
    Move-Item -LiteralPath $builtDir -Destination $onedirTarget -Force
    $onedirFiles = @(Get-ChildItem -LiteralPath $onedirTarget -Recurse -File -ErrorAction SilentlyContinue)
    $onedirMb = [math]::Round((($onedirFiles | Measure-Object -Sum Length).Sum / 1MB), 2)
    Write-Ok "dist\QingFengCleaner-onedir\（$($onedirFiles.Count) 个文件，共 $onedirMb MB）"

    # ---- 4. 生成启动器 + 布置 dist 交付结构 --------------------------------
    Write-Head '4/6 生成启动器（dist\启动清风清理.bat）并布置 dist 交付结构'
    $batPath = New-LauncherBat -DistDir $distDir
    Write-Ok "已生成 dist\启动清风清理.bat（转调目录版 exe，支持透传参数）"
    if (-not (Test-Path -LiteralPath $batPath)) { throw "启动器生成失败：$batPath" }

    # ---- 5. 可选：单文件实验版（-OneFile，输出 dist\_experimental） ---------
    if ($OneFile) {
        Write-Head '5/6 可选：PyInstaller 单文件实验版（dist\_experimental，非交付物）'
        $expDir = Join-Path $distDir '_experimental'
        if (Test-Path -LiteralPath $expDir) {
            Remove-Item -LiteralPath $expDir -Recurse -Force -ErrorAction SilentlyContinue
        }
        New-Item -ItemType Directory -Force -Path $expDir | Out-Null
        $workDir = Join-Path $Script:Root 'build\onefile'
        $onefileArgs = @(
            '-m', 'PyInstaller',
            '--noconfirm',
            '--onefile',
            '--noconsole',
            '--name', 'QingFengCleaner',
            '--paths', $Script:Root,
            '--distpath', $expDir,
            '--workpath', (Join-Path $workDir 'work'),
            '--specpath', $workDir,
            '--add-data', ((Join-Path $Script:Root 'qingfeng\data') + ';qingfeng/data')
        )
        if (-not $NoClean) { $onefileArgs += '--clean' }
        if (Test-Path -LiteralPath $icon) { $onefileArgs += @('--icon', $icon) }
        $onefileArgs += $entry

        Write-Info ("执行：.venv\Scripts\python.exe " + ($onefileArgs -join ' '))
        & $venvPy @onefileArgs
        if ($LASTEXITCODE -ne 0) {
            Write-Warn '直接 PyInstaller 失败，改用受限沙箱兼容包装器重试…'
            & $venvPy (Join-Path $Script:Root 'tools\sandbox_exec.py') @onefileArgs
        }
        $expExe = Join-Path $expDir 'QingFengCleaner.exe'
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $expExe)) {
            Write-Warn '单文件实验版构建失败；目录版交付物不受影响。'
        }
        else {
            $expMb = [math]::Round(((Get-Item -LiteralPath $expExe).Length / 1MB), 2)
            Write-Ok "dist\_experimental\QingFengCleaner.exe（$expMb MB，实验性，非交付物）"
            Write-Warn '注意：单文件版在中文路径下自解压可能失败（见 tools\verify.ps1 的单文件实验探测）。'
        }
    }
    else {
        Write-Head '5/6 单文件实验版（跳过）'
        Write-Info '默认不构建单文件版；需要实验时加 -OneFile，产物会放到 dist\_experimental\（不进交付）。'
        $expDir = Join-Path $distDir '_experimental'
        if (Test-Path -LiteralPath $expDir) {
            Remove-Item -LiteralPath $expDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    # ---- 6. 校验交付结构 ---------------------------------------------------
    Write-Head '6/6 校验交付结构'
    $onedirExe = Join-Path $onedirTarget 'QingFengCleaner.exe'
    if (-not (Test-Path -LiteralPath $onedirExe)) { throw "未找到目录版 exe：$onedirExe" }
    $onedirRules = Join-Path $onedirTarget '_internal\qingfeng\data\rules\system.json'
    $onedirHeuristics = Join-Path $onedirTarget '_internal\qingfeng\data\heuristics.json'
    if ((Test-Path -LiteralPath $onedirRules) -and (Test-Path -LiteralPath $onedirHeuristics)) {
        Write-Ok '_internal 内含规则库 + v2 heuristics.json（--add-data 生效）'
    }
    else {
        throw '目录版 _internal 缺少规则库或 v2 heuristics.json（--add-data 未生效）'
    }
    $batText = Get-Content -LiteralPath $batPath -Raw
    if ($batText -match 'QingFengCleaner-onedir\\QingFengCleaner\.exe') {
        Write-Ok '启动器内容正确（指向目录版 exe）'
    }
    else {
        throw '启动器内容异常：未找到 QingFengCleaner-onedir\QingFengCleaner.exe'
    }
    if (Test-Path -LiteralPath $legacyOnefile) {
        throw 'dist 根目录仍存在单文件 exe，交付结构不符合 v2.1 要求'
    }
    $distTop = @(Get-ChildItem -LiteralPath $distDir -Force | Select-Object -ExpandProperty Name)
    Write-Info ("dist\ 内容 : " + ($distTop -join '  |  '))
    Write-Head '完成'
    Write-Info ("总耗时：{0:N1}s" -f $Script:Sw.Elapsed.TotalSeconds)
    Write-Info '交付：dist\QingFengCleaner-onedir\QingFengCleaner.exe（或双击 dist\启动清风清理.bat）'
    if ($OneFile) { Write-Info '实验：dist\_experimental\QingFengCleaner.exe（不随交付发布）' }
    Write-Info '下一步验证：powershell -ExecutionPolicy Bypass -File tools\verify.ps1'
    Write-Info '源码运行（无需打包）：packaging\run_dev.bat'
    exit 0
}
catch {
    Write-Host ''
    Write-Fail ("打包脚本异常：{0}" -f $_.Exception.Message)
    Write-Info '替代方案：packaging\run_dev.bat（无需打包，直接运行源码）'
    exit 1
}
finally {
    $Script:Sw.Stop()
}
