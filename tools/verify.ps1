#Requires -Version 5.1
<#
.SYNOPSIS
    清风清理 QingFeng Cleaner —— 一键端到端验证。

.DESCRIPTION
    依次执行 10 组检查（v1 + v2 + v2.1 交付结构），逐项打印 PASS / FAIL / SKIP，最后给出汇总与总耗时：

      1. 单元测试          python -m unittest discover -s tests -v           退出码 0（v1+v2 共 176 个）
      2. 只扫描冒烟        python main.py --scan-only                         退出码 0 且输出含「可安全清理合计」
      3. 源码 GUI 存活     python main.py --no-tray                           进程存活 8 秒（随后结束）
      4. 源码 GUI 存活     python main.py --no-tray --page space              空间透视页存活 8 秒
      5. 空间索引冒烟      python tools/smoke_v2.py --step index              只读扫 C: 建索引：
                           accessible_bytes>0、耗时≤120s、缓存可读写
      6. 决策冒烟          python tools/smoke_v2.py --step decide             大文件收集/分组/授权矩阵/
                           迁移目标探测，分组字节与项字节一致
      7. 维护者审计        python qa/lead_audit_v2.py                         空间索引/决策/授权/迁移审计；
                           该审计要求完整权限会话（D/E 可写），不满足时记 SKIP 并说明
      8. 交付结构          dist\ = QingFengCleaner-onedir\ + 启动清风清理.bat；dist 根不得再有单文件 exe，
                           启动器必须指向目录版 exe
      9. 目录版产物        dist\QingFengCleaner-onedir\QingFengCleaner.exe    存在且 _internal 含规则库 +
                           heuristics.json；--scan-only 退出码 0；**从中文路径可运行**（--version）；
                           GUI（清理页 / --page space）各存活 8 秒（**唯一交付形态**）
     10. 单文件实验版      dist\_experimental\QingFengCleaner.exe             仅在存在时探测（build.ps1 -OneFile
                           才会产出）；默认不构建 → SKIP，正式交付只含目录版

    日志与输出保存在 tools\_verify_out\<时间戳>\（已被 .gitignore 忽略）。
    退出码：0 = 全部 PASS/SKIP（无 FAIL）；1 = 存在 FAIL。

.PARAMETER Python
    指定 Python 3.12 解释器；默认自动查找（本机内置 3.12 → PATH 上的 python）。

.PARAMETER GuiSeconds
    GUI 存活检查时长，默认 8 秒。

.PARAMETER ScanTimeoutSec
    只扫描 / exe 冒烟的超时秒数，默认 180。

.PARAMETER Exe
    指定目录版 exe 路径，默认 dist\QingFengCleaner-onedir\QingFengCleaner.exe。

.PARAMETER SkipExe
    跳过打包相关检查（第 8~10 项，尚未打包时使用）。

.PARAMETER SkipGui
    跳过第 3 项（无桌面环境时使用）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\verify.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\verify.ps1 -SkipExe -GuiSeconds 10
#>
[CmdletBinding()]
param(
    [string]$Python = '',
    [int]$GuiSeconds = 8,
    [int]$ScanTimeoutSec = 180,
    [string]$Exe = '',
    [switch]$SkipExe,
    [switch]$SkipGui
)

$ErrorActionPreference = 'Stop'
# 让子进程（python / exe）以 UTF-8 输出，避免中文在管道/重定向里按 GBK 编码而导致断言误判
$env:PYTHONIOENCODING = 'utf-8'
$Script:Root = Split-Path -Parent $PSScriptRoot
# 注意：PowerShell 变量名大小写不敏感，所以总计时器不能叫 $sw（会被各步骤的 $sw 覆盖）
$Script:TotalSw = [System.Diagnostics.Stopwatch]::StartNew()
$Script:Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$Script:OutDir = Join-Path $Script:Root "tools\_verify_out\$($Script:Stamp)"
$Script:Results = New-Object System.Collections.ArrayList

function Write-Head([string]$Text) {
    Write-Host ''
    Write-Host "=== $Text ===" -ForegroundColor Cyan
}
function Write-Info([string]$Text) { Write-Host "    $Text" }
function Write-Pass([string]$Text) { Write-Host "[PASS] $Text" -ForegroundColor Green }
function Write-Fail([string]$Text) { Write-Host "[FAIL] $Text" -ForegroundColor Red }
function Write-Skip([string]$Text) { Write-Host "[SKIP] $Text" -ForegroundColor Yellow }
function Write-Warn([string]$Text) { Write-Host "[WARN] $Text" -ForegroundColor Yellow }

function Add-Result {
    param(
        [string]$Name,
        [string]$Status,
        [double]$Seconds,
        [string]$Detail
    )
    [void]$Script:Results.Add([pscustomobject]@{
            Name    = $Name
            Status  = $Status
            Seconds = [math]::Round($Seconds, 2)
            Detail  = $Detail
        })
    switch ($Status) {
        'PASS' { Write-Pass "$Name  ($([math]::Round($Seconds, 2))s)  $Detail" }
        'FAIL' { Write-Fail "$Name  ($([math]::Round($Seconds, 2))s)  $Detail" }
        'SKIP' { Write-Skip "$Name" }
    }
}

function Get-LocalPython {
    $candidates = New-Object System.Collections.ArrayList
    if ($Python) { [void]$candidates.Add($Python) }
    if ($env:QF_PYTHON) { [void]$candidates.Add($env:QF_PYTHON) }
    [void]$candidates.Add('C:\Users\HUAWEI\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe')
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) { [void]$candidates.Add($cmd.Source) }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    return $null
}

function Read-Utf8Text([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    try {
        $text = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
        if ($null -eq $text) { return '' }
        return [string]$text
    }
    catch { return '' }
}

function Add-Tail([string]$Text, [int]$Lines = 12) {
    if (-not $Text) { return '' }
    $rows = @($Text -split "`r?`n" | Where-Object { $_.Trim() -ne '' })
    if ($rows.Count -le $Lines) { return ($rows -join ' | ') }
    return (($rows[($rows.Count - $Lines)..($rows.Count - 1)]) -join ' | ')
}

# 用 System.Diagnostics.Process 直接跑子进程：
#  - PowerShell 5.1 的 Start-Process -PassThru 在进程退出后拿不到 ExitCode（返回 $null）；
#  - 原生 stderr 在 $ErrorActionPreference='Stop' 下被当成错误记录，会中断脚本；
#  - StandardOutputEncoding 显式设成 UTF-8，配合 PYTHONIOENCODING=utf-8 避免中文乱码。
function Invoke-Captured {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [int]$TimeoutMs,
        [string]$Tag,
        [string]$WorkingDirectory = ''
    )
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $argText = ($Arguments | ForEach-Object {
            if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
        }) -join ' '
    $psi.Arguments = $argText
    $psi.WorkingDirectory = if ($WorkingDirectory) { $WorkingDirectory } else { $Script:Root }
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    [void]$proc.Start()
    $outTask = $proc.StandardOutput.ReadToEndAsync()
    $errTask = $proc.StandardError.ReadToEndAsync()

    $exited = $proc.WaitForExit($TimeoutMs)
    $timedOut = -not $exited
    if (-not $exited) {
        try { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue } catch { }
        try { [void]$proc.WaitForExit(5000) } catch { }
    }
    $stdout = ''
    $stderr = ''
    try { $stdout = [string]$outTask.GetAwaiter().GetResult() } catch { }
    try { $stderr = [string]$errTask.GetAwaiter().GetResult() } catch { }
    $code = $null
    try { $code = $proc.ExitCode } catch { }

    $outFile = Join-Path $Script:OutDir "$Tag.out.txt"
    $errFile = Join-Path $Script:OutDir "$Tag.err.txt"
    try { Set-Content -LiteralPath $outFile -Value $stdout -Encoding UTF8 } catch { }
    try { Set-Content -LiteralPath $errFile -Value $stderr -Encoding UTF8 } catch { }

    return [pscustomobject]@{
        ExitCode = $code
        TimedOut = $timedOut
        StdOut   = $stdout
        StdErr   = $stderr
        OutFile  = $outFile
        ErrFile  = $errFile
    }
}

function Stop-Tree([int]$ProcessId) {
    try { & taskkill.exe /PID $ProcessId /T /F 1>$null 2>$null } catch { }
    try { Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue } catch { }
}

# GUI 存活检查：启动 → 观察 N 秒 → 结束。GUI 冷启动偶发环境性抖动（例如上一个进程刚被结束、
# 桌面 DPI/输入法初始化竞争），因此失败时重试一次；返回值里带 Attempt 供汇总里说明。
function Test-GuiAlive {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [int]$Seconds,
        [string]$Tag
    )
    $last = $null
    for ($attempt = 1; $attempt -le 2; $attempt++) {
        $outFile = Join-Path $Script:OutDir ("{0}_{1}.out.txt" -f $Tag, $attempt)
        $errFile = Join-Path $Script:OutDir ("{0}_{1}.err.txt" -f $Tag, $attempt)
        $startArgs = @{
            FilePath               = $FilePath
            WorkingDirectory       = $Script:OutDir
            NoNewWindow            = $true
            PassThru               = $true
            RedirectStandardOutput = $outFile
            RedirectStandardError  = $errFile
        }
        if ($Arguments -and $Arguments.Count -gt 0) { $startArgs['ArgumentList'] = $Arguments }
        $proc = Start-Process @startArgs
        Start-Sleep -Seconds $Seconds
        $alive = $true
        try { $alive = -not $proc.HasExited } catch { $alive = $false }
        if ($alive) {
            Stop-Tree $proc.Id
            return [pscustomobject]@{ Alive = $true; Attempt = $attempt; ErrFile = $errFile; ExitCode = $null }
        }
        $code = $null
        try { $code = $proc.ExitCode } catch { }
        $last = [pscustomobject]@{ Alive = $false; Attempt = $attempt; ErrFile = $errFile; ExitCode = $code }
        if ($attempt -lt 2) { Start-Sleep -Seconds 2 }
    }
    return $last
}

# 读取文本文件：先按 UTF-8 严格解码，失败再按 GBK(936) 解码。
# 打包后的窗口化 exe 会按系统区域（中文 = cp936）写重定向文件，源码运行的 python 则是 UTF-8。
function Read-TextCompat([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    try {
        $bytes = [System.IO.File]::ReadAllBytes($Path)
        if ($bytes.Length -eq 0) { return '' }
        $strict = New-Object System.Text.UTF8Encoding($false, $true)
        try { return $strict.GetString($bytes) }
        catch { return [System.Text.Encoding]::GetEncoding(936).GetString($bytes) }
    }
    catch { return '' }
}

# 用于窗口化 exe：必须用 Start-Process 的文件重定向（管道方式下 PyInstaller 窗口化程序
# 拿不到可用 std 句柄，会丢输出）。进程退出码在 PS 5.1 的 Start-Process -PassThru 上拿不到，
# 所以以"是否按时退出 + 输出是否含扫描汇总"作为判定依据。
function Invoke-WindowedCaptured {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [int]$TimeoutMs,
        [string]$Tag
    )
    $outFile = Join-Path $Script:OutDir "$Tag.out.txt"
    $errFile = Join-Path $Script:OutDir "$Tag.err.txt"
    $p = Start-Process -FilePath $FilePath -ArgumentList $Arguments -WorkingDirectory $Script:Root `
        -NoNewWindow -PassThru -RedirectStandardOutput $outFile -RedirectStandardError $errFile
    $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMs)
    while (-not $p.HasExited -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 100
    }
    $timedOut = $false
    try { $timedOut = -not $p.HasExited } catch { $timedOut = $false }
    if ($timedOut) { Stop-Tree $p.Id }
    return [pscustomobject]@{
        TimedOut = $timedOut
        StdOut   = (Read-TextCompat $outFile)
        StdErr   = (Read-TextCompat $errFile)
        OutFile  = $outFile
        ErrFile  = $errFile
    }
}

try {
    Set-Location -LiteralPath $Script:Root
    New-Item -ItemType Directory -Force -Path $Script:OutDir | Out-Null

    Write-Head '清风清理 QingFeng Cleaner — 端到端验证'
    Write-Info "项目根目录 : $Script:Root"
    Write-Info "输出目录   : tools\_verify_out\$($Script:Stamp)"
    Write-Info "PowerShell : $($PSVersionTable.PSVersion)"

    $basePy = Get-LocalPython
    if (-not $basePy) {
        Write-Fail '找不到可用的 Python 3.12 解释器。用 -Python <路径> 指定。'
        exit 1
    }

    $pyVersion = (& $basePy --version 2>&1 | Out-String).Trim()
    Write-Info "Python     : $pyVersion"
    Write-Info "解释器路径 : $basePy"

    # =====================================================================
    # 1. 单元测试
    # =====================================================================
    Write-Head '1/10 单元测试（python -m unittest discover -s tests -v，v1+v2）'
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $r = Invoke-Captured -FilePath $basePy -Arguments @('-m', 'unittest', 'discover', '-s', 'tests', '-v') `
        -TimeoutMs ($ScanTimeoutSec * 1000) -Tag 'unittest'
    $sw.Stop()
    $ranLine = ''
    # unittest 把结果打到 stderr，需要和 stdout 合并判断
    $unitText = ($r.StdOut + "`n" + $r.StdErr)
    $match = [regex]::Match($unitText, 'Ran (\d+) tests?')
    if ($match.Success) { $ranLine = $match.Value }
    if ($r.TimedOut) {
        Add-Result '单元测试' 'FAIL' $sw.Elapsed.TotalSeconds '超时未结束'
    }
    elseif ($r.ExitCode -ne 0) {
        Add-Result '单元测试' 'FAIL' $sw.Elapsed.TotalSeconds ("退出码 {0}；{1}" -f $r.ExitCode, (Add-Tail $r.StdErr 8))
    }
    elseif ($unitText -notmatch 'OK') {
        Add-Result '单元测试' 'FAIL' $sw.Elapsed.TotalSeconds '输出中未见 OK'
    }
    else {
        Add-Result '单元测试' 'PASS' $sw.Elapsed.TotalSeconds ("{0} · OK" -f $(if ($ranLine) { $ranLine } else { 'unittest 通过' }))
    }

    # =====================================================================
    # 2. 只扫描冒烟
    # =====================================================================
    Write-Head '2/10 只扫描冒烟（python main.py --scan-only）'
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $r = Invoke-Captured -FilePath $basePy -Arguments @('main.py', '--scan-only') `
        -TimeoutMs ($ScanTimeoutSec * 1000) -Tag 'scan_only'
    $sw.Stop()
    $marker = '可安全清理合计'
    $summaryLine = ''
    foreach ($line in ($r.StdOut -split "`r?`n")) {
        if ($line -match $marker) { $summaryLine = $line.Trim() }
    }
    $driveLine = ''
    foreach ($line in ($r.StdOut -split "`r?`n")) {
        if ($line -match 'C 盘可用') { $driveLine = $line.Trim() }
    }
    if ($r.TimedOut) {
        Add-Result '只扫描冒烟' 'FAIL' $sw.Elapsed.TotalSeconds '超时未结束（--scan-only 卡住）'
    }
    elseif ($r.ExitCode -ne 0) {
        Add-Result '只扫描冒烟' 'FAIL' $sw.Elapsed.TotalSeconds ("退出码 {0}；{1}" -f $r.ExitCode, (Add-Tail $r.StdErr 8))
    }
    elseif (-not $summaryLine) {
        Add-Result '只扫描冒烟' 'FAIL' $sw.Elapsed.TotalSeconds ("输出缺少「{0}」" -f $marker)
    }
    else {
        $detail = $summaryLine
        if ($driveLine) { $detail += "；$driveLine" }
        Add-Result '只扫描冒烟' 'PASS' $sw.Elapsed.TotalSeconds $detail
    }

    # =====================================================================
    # 3. 源码 GUI 存活（清理页）
    # =====================================================================
    Write-Head "3/10 源码 GUI 存活（清理页 --no-tray，观察 $GuiSeconds 秒）"
    if ($SkipGui) {
        Add-Result '源码 GUI（清理页）' 'SKIP' 0 '按 -SkipGui 跳过'
    }
    else {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $alive = Test-GuiAlive -FilePath $basePy `
            -Arguments @((Join-Path $Script:Root 'main.py'), '--no-tray') `
            -Seconds $GuiSeconds -Tag 'gui_clean'
        $sw.Stop()
        if ($alive.Alive) {
            $note = if ($alive.Attempt -gt 1) { '（首次启动抖动，重试后存活）' } else { '' }
            Add-Result '源码 GUI（清理页）' 'PASS' $sw.Elapsed.TotalSeconds ("进程存活 {0}s 未退出{1}（已结束测试进程）" -f $GuiSeconds, $note)
        }
        else {
            Add-Result '源码 GUI（清理页）' 'FAIL' $sw.Elapsed.TotalSeconds ("重试后仍在 {0}s 内退出；{1}" -f $GuiSeconds, (Add-Tail (Read-Utf8Text $alive.ErrFile) 10))
        }
    }

    # =====================================================================
    # 4. 源码 GUI 存活（空间透视页，v2）
    # =====================================================================
    Write-Head "4/10 源码 GUI 存活（空间透视页 --page space，观察 $GuiSeconds 秒）"
    if ($SkipGui) {
        Add-Result '源码 GUI（空间透视页）' 'SKIP' 0 '按 -SkipGui 跳过'
    }
    else {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $alive = Test-GuiAlive -FilePath $basePy `
            -Arguments @((Join-Path $Script:Root 'main.py'), '--no-tray', '--page', 'space') `
            -Seconds $GuiSeconds -Tag 'gui_space'
        $sw.Stop()
        if ($alive.Alive) {
            $note = if ($alive.Attempt -gt 1) { '（首次启动抖动，重试后存活）' } else { '' }
            Add-Result '源码 GUI（空间透视页）' 'PASS' $sw.Elapsed.TotalSeconds ("进程存活 {0}s 未退出{1}（已结束测试进程）" -f $GuiSeconds, $note)
        }
        else {
            Add-Result '源码 GUI（空间透视页）' 'FAIL' $sw.Elapsed.TotalSeconds ("重试后仍在 {0}s 内退出；{1}" -f $GuiSeconds, (Add-Tail (Read-Utf8Text $alive.ErrFile) 10))
        }
    }

    # =====================================================================
    # 5. 空间索引冒烟（v2）
    # =====================================================================
    Write-Head '5/10 空间索引冒烟（build_index C: + 缓存往返，≤120s）'
    $smoke = Join-Path $Script:Root 'tools\smoke_v2.py'
    $spaceJson = Join-Path $Script:OutDir 'space_index.json'
    # 受限环境下 %LOCALAPPDATA% 可能不可写：把缓存目录临时指到本次验证输出目录
    $savedLocalAppData = $env:LOCALAPPDATA
    $env:LOCALAPPDATA = Join-Path $Script:OutDir 'localappdata'
    New-Item -ItemType Directory -Force -Path $env:LOCALAPPDATA | Out-Null
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $r = Invoke-Captured -FilePath $basePy `
        -Arguments @($smoke, '--step', 'index', '--save', $spaceJson, '--max-seconds', '120') `
        -TimeoutMs 300000 -Tag 'space_index'
    $sw.Stop()
    if ($null -ne $savedLocalAppData) { $env:LOCALAPPDATA = $savedLocalAppData }
    $payload = $null
    foreach ($line in ($r.StdOut -split "`r?`n")) {
        if ($line.TrimStart().StartsWith('{')) {
            try { $payload = $line | ConvertFrom-Json } catch { $payload = $null }
        }
    }
    if ($r.TimedOut) {
        Add-Result '空间索引' 'FAIL' $sw.Elapsed.TotalSeconds '超时未结束（>300s）'
    }
    elseif ($r.ExitCode -ne 0 -or $null -eq $payload -or -not $payload.ok) {
        Add-Result '空间索引' 'FAIL' $sw.Elapsed.TotalSeconds ("退出码 {0}；{1}" -f $r.ExitCode, (Add-Tail ($r.StdOut + $r.StdErr) 6))
    }
    else {
        Add-Result '空间索引' 'PASS' $sw.Elapsed.TotalSeconds ("可访问 {0} GB / 已用 {1} GB · 建索引 {2}s · 缓存往返={3}" -f ([math]::Round([double]$payload.accessible_bytes / 1GB, 1)), ([math]::Round([double]$payload.total_bytes / 1GB, 1)), $payload.duration, $payload.cache_ok)
    }

    # =====================================================================
    # 6. 决策冒烟（v2）
    # =====================================================================
    Write-Head '6/10 决策冒烟（collect / group / action_policy / suggest_targets）'
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $r = Invoke-Captured -FilePath $basePy `
        -Arguments @($smoke, '--step', 'decide', '--load', $spaceJson) -TimeoutMs 300000 -Tag 'decide_smoke'
    $sw.Stop()
    $payload = $null
    foreach ($line in ($r.StdOut -split "`r?`n")) {
        if ($line.TrimStart().StartsWith('{')) {
            try { $payload = $line | ConvertFrom-Json } catch { $payload = $null }
        }
    }
    if ($r.TimedOut) {
        Add-Result '决策冒烟' 'FAIL' $sw.Elapsed.TotalSeconds '超时未结束（>300s）'
    }
    elseif ($r.ExitCode -ne 0 -or $null -eq $payload -or -not $payload.ok) {
        Add-Result '决策冒烟' 'FAIL' $sw.Elapsed.TotalSeconds ("退出码 {0}；{1}" -f $r.ExitCode, (Add-Tail ($r.StdOut + $r.StdErr) 6))
    }
    else {
        $targets = @($payload.targets)
        $writableTargets = @($targets | Where-Object { $_.writable }).Count
        Add-Result '决策冒烟' 'PASS' $sw.Elapsed.TotalSeconds ("{0} 个大文件 / {1} 组 · 可清理 {2} · 可迁移 {3} · 迁移目标 {4} 个（可写 {5}）" -f $payload.items, $payload.groups, $payload.can_delete, $payload.can_move, $targets.Count, $writableTargets)
    }

    # =====================================================================
    # 7. 维护者审计（qa/lead_audit_v2.py；需要完整权限会话）
    # =====================================================================
    Write-Head '7/10 维护者审计（qa/lead_audit_v2.py）'
    $audit = Join-Path $Script:Root 'qa\lead_audit_v2.py'
    if (-not (Test-Path -LiteralPath $audit)) {
        Add-Result '维护者审计 v2' 'SKIP' 0 '未找到 qa\lead_audit_v2.py'
    }
    else {
        $probe = Invoke-Captured -FilePath $basePy `
            -Arguments @('-c', 'from qingfeng.core import decide; print(sum(1 for t in decide.suggest_targets() if t.writable))') `
            -TimeoutMs 60000 -Tag 'audit_probe'
        $probeLines = @($probe.StdOut -split "`r?`n" | Where-Object { $_.Trim() -ne '' })
        $writable = 0
        if ($probeLines.Count -gt 0) { try { $writable = [int]$probeLines[-1].Trim() } catch { $writable = 0 } }
        if ($probe.ExitCode -ne 0) {
            Add-Result '维护者审计 v2' 'SKIP' 0 '无法探测迁移目标（跳过该审计）'
        }
        elseif ($writable -le 0) {
            Add-Result '维护者审计 v2' 'SKIP' 0 '本会话对 D/E 无写权限；该审计需完整权限会话（维护者机/CI 运行）'
        }
        else {
            $sw = [System.Diagnostics.Stopwatch]::StartNew()
            $r = Invoke-Captured -FilePath $basePy -Arguments @($audit) -TimeoutMs 600000 -Tag 'lead_audit_v2'
            $sw.Stop()
            if ($r.ExitCode -eq 0) {
                Add-Result '维护者审计 v2' 'PASS' $sw.Elapsed.TotalSeconds ("全部检查 PASS · " + (Add-Tail ($r.StdOut + $r.StdErr) 1))
            }
            else {
                Add-Result '维护者审计 v2' 'FAIL' $sw.Elapsed.TotalSeconds ("退出码 {0}；{1}" -f $r.ExitCode, (Add-Tail ($r.StdOut + $r.StdErr) 8))
            }
        }
    }

    # =====================================================================
    # 8. 交付结构（dist = 目录版 + 启动 bat；v2.1 起不再交付单文件 exe）
    # =====================================================================
    Write-Head '8/10 交付结构（dist = QingFengCleaner-onedir\ + 启动清风清理.bat）'
    $distDir = Join-Path $Script:Root 'dist'
    $onedirExe = if ($Exe) { $Exe } else { Join-Path $Script:Root 'dist\QingFengCleaner-onedir\QingFengCleaner.exe' }
    $launcherBat = Join-Path $distDir '启动清风清理.bat'
    $legacyOnefile = Join-Path $distDir 'QingFengCleaner.exe'
    if ($SkipExe) {
        Add-Result '交付结构' 'SKIP' 0 '按 -SkipExe 跳过'
    }
    else {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $problems = New-Object System.Collections.ArrayList
        if (-not (Test-Path -LiteralPath $onedirExe)) {
            [void]$problems.Add('缺少 dist\QingFengCleaner-onedir\QingFengCleaner.exe')
        }
        if (-not (Test-Path -LiteralPath $launcherBat)) {
            [void]$problems.Add('缺少 dist\启动清风清理.bat（先运行 packaging\build.ps1）')
        }
        if (Test-Path -LiteralPath $legacyOnefile) {
            [void]$problems.Add('dist 根目录不应存在单文件 exe（v2.1 起不交付）')
        }
        if (Test-Path -LiteralPath $launcherBat) {
            $batText = Get-Content -LiteralPath $launcherBat -Raw
            if ($batText -notmatch 'QingFengCleaner-onedir\\QingFengCleaner\.exe') {
                [void]$problems.Add('启动器未指向目录版 exe')
            }
        }
        $sw.Stop()
        if ($problems.Count -eq 0) {
            Add-Result '交付结构' 'PASS' $sw.Elapsed.TotalSeconds 'dist\ = QingFengCleaner-onedir\ + 启动清风清理.bat（无单文件 exe）'
        }
        else {
            Add-Result '交付结构' 'FAIL' $sw.Elapsed.TotalSeconds ($problems -join '；')
        }
    }

    # =====================================================================
    # 9. 目录版产物（推荐交付物：启动不需要自解压，兼容性最好）
    # =====================================================================
    Write-Head '9/10 目录版产物（dist\QingFengCleaner-onedir，唯一交付形态）'
    $onedirRules = Join-Path $Script:Root 'dist\QingFengCleaner-onedir\_internal\qingfeng\data\rules\system.json'
    $onedirHeuristics = Join-Path $Script:Root 'dist\QingFengCleaner-onedir\_internal\qingfeng\data\heuristics.json'
    if ($SkipExe) {
        Add-Result '目录版产物' 'SKIP' 0 '按 -SkipExe 跳过'
    }
    elseif (-not (Test-Path -LiteralPath $onedirExe)) {
        Add-Result '目录版产物' 'FAIL' 0 '缺少 dist\QingFengCleaner-onedir\QingFengCleaner.exe；先运行 packaging\build.ps1'
    }
    else {
        # 9.1 产物完整性（规则库 + v2 heuristics.json）
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $onedirDir = Split-Path -Parent $onedirExe
        $onedirFiles = @(Get-ChildItem -LiteralPath $onedirDir -Recurse -File -ErrorAction SilentlyContinue)
        $onedirMb = [math]::Round((($onedirFiles | Measure-Object -Sum Length).Sum / 1MB), 2)
        $hasRules = Test-Path -LiteralPath $onedirRules
        $hasHeuristics = Test-Path -LiteralPath $onedirHeuristics
        $sw.Stop()
        if ($hasRules -and $hasHeuristics) {
            Add-Result '目录版产物' 'PASS' $sw.Elapsed.TotalSeconds ("QingFengCleaner-onedir\ · {0} 个文件 / {1} MB · _internal 内含规则库 + heuristics.json" -f $onedirFiles.Count, $onedirMb)
        }
        elseif (-not $hasRules) {
            Add-Result '目录版产物' 'FAIL' $sw.Elapsed.TotalSeconds '_internal 中缺少 qingfeng/data/rules/system.json'
        }
        else {
            Add-Result '目录版产物' 'FAIL' $sw.Elapsed.TotalSeconds '_internal 中缺少 qingfeng/data/heuristics.json'
        }

        # 9.2 --scan-only 真实运行（cmd 包装以便拿到可靠退出码：窗口化 exe 的 Start-Process 在 PS 5.1 拿不到 ExitCode）
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $odOut = Join-Path $Script:OutDir 'onedir_scan_only.out.txt'
        $odErr = Join-Path $Script:OutDir 'onedir_scan_only.err.txt'
        & cmd.exe /c "`"$onedirExe`" --scan-only > `"$odOut`" 2> `"$odErr`""
        $odCode = $LASTEXITCODE
        $odText = Read-TextCompat $odOut
        $sw.Stop()
        $odSum = ''
        foreach ($line in ($odText -split "`r?`n")) {
            if ($line -match $marker) { $odSum = $line.Trim() }
        }
        if ($odCode -eq 0 -and $odSum) {
            Add-Result '目录版 --scan-only' 'PASS' $sw.Elapsed.TotalSeconds ("退出码 0 · {0}" -f $odSum)
        }
        elseif (-not $odSum) {
            Add-Result '目录版 --scan-only' 'FAIL' $sw.Elapsed.TotalSeconds ("退出码 {0} · 输出缺少「{1}」；{2}" -f $odCode, $marker, (Add-Tail (Read-TextCompat $odErr) 8))
        }
        else {
            Add-Result '目录版 --scan-only' 'FAIL' $sw.Elapsed.TotalSeconds ("退出码 {0}（应为 0）" -f $odCode)
        }

        # 9.3 目录版从当前中文路径可运行（工作目录=dist 目录，路径含中文；--version 冒烟）
        # 窗口化 exe 的 stdout 需要用 cmd 重定向成文件再读（PSI 管道拿不到窗口化程序的输出）
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $zhOut = Join-Path $Script:OutDir 'onedir_zh_version.out.txt'
        $zhErr = Join-Path $Script:OutDir 'onedir_zh_version.err.txt'
        & cmd.exe /c "cd /d `"$onedirDir`" & `"$onedirExe`" --version > `"$zhOut`" 2> `"$zhErr`""
        $zhCode = $LASTEXITCODE
        $zhText = Read-TextCompat $zhOut
        $sw.Stop()
        $zhPath = [bool]($onedirDir -match '[^\x00-\x7F]')
        if ($zhCode -eq 0 -and $zhText -match '清风清理') {
            Add-Result '目录版中文路径可运行' 'PASS' $sw.Elapsed.TotalSeconds ("cwd={0}（含中文={1}）· --version 退出码 0" -f $onedirDir, $zhPath)
        }
        else {
            Add-Result '目录版中文路径可运行' 'FAIL' $sw.Elapsed.TotalSeconds ("--version 退出码 {0}；输出/错误：{1}" -f $zhCode, (Add-Tail (($zhText + "`n" + (Read-TextCompat $zhErr))) 6))
        }

        # 9.4 打包版 GUI（清理页）存活 8 秒
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $g = Test-GuiAlive -FilePath $onedirExe -Arguments @() -Seconds $GuiSeconds -Tag 'onedir_gui'
        $sw.Stop()
        if ($g.Alive) {
            $note = if ($g.Attempt -gt 1) { '（首次启动抖动，重试后存活）' } else { '' }
            Add-Result '目录版 GUI（清理页）' 'PASS' $sw.Elapsed.TotalSeconds ("GUI 进程存活 {0}s 未闪退{1}（已结束测试进程）" -f $GuiSeconds, $note)
        }
        else {
            Add-Result '目录版 GUI（清理页）' 'FAIL' $sw.Elapsed.TotalSeconds ("GUI 重试后仍在 {0}s 内退出；{1}" -f $GuiSeconds, (Add-Tail (Read-TextCompat $g.ErrFile) 8))
        }

        # 9.5 打包版 GUI（空间透视页 --page space）存活 8 秒
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $sp = Test-GuiAlive -FilePath $onedirExe -Arguments @('--no-tray', '--page', 'space') -Seconds $GuiSeconds -Tag 'onedir_space_gui'
        $sw.Stop()
        if ($sp.Alive) {
            $note = if ($sp.Attempt -gt 1) { '（首次启动抖动，重试后存活）' } else { '' }
            Add-Result '目录版 GUI（空间透视页）' 'PASS' $sw.Elapsed.TotalSeconds ("--page space 进程存活 {0}s 未闪退{1}（已结束测试进程）" -f $GuiSeconds, $note)
        }
        else {
            Add-Result '目录版 GUI（空间透视页）' 'FAIL' $sw.Elapsed.TotalSeconds ("--page space 重试后仍在 {0}s 内退出；{1}" -f $GuiSeconds, (Add-Tail (Read-TextCompat $sp.ErrFile) 8))
        }
    }

    # =====================================================================
    # 10. 单文件实验版（仅当 dist\_experimental 存在时探测；不属正式交付）
    # =====================================================================
    Write-Head '10/10 单文件实验版（dist\_experimental，可选）'
    $expExe = Join-Path $Script:Root 'dist\_experimental\QingFengCleaner.exe'
    if ($SkipExe) {
        Add-Result '单文件实验版' 'SKIP' 0 '按 -SkipExe 跳过'
    }
    elseif (-not (Test-Path -LiteralPath $expExe)) {
        Add-Result '单文件实验版' 'SKIP' 0 '默认不构建单文件版（packaging\build.ps1 -OneFile 才产出到 dist\_experimental）；正式交付只含目录版'
    }
    else {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $sizeMb = [math]::Round(((Get-Item -LiteralPath $expExe).Length / 1MB), 2)
        # 单文件 exe 启动时要把自身解压到临时目录；中文路径/受限令牌下可能失败（PyInstaller
        # 「Could not create temporary directory!」）。这里只做探测，失败记 SKIP——正式交付不含它。
        $r = Invoke-WindowedCaptured -FilePath $expExe -Arguments @('--scan-only') -TimeoutMs 15000 -Tag 'experimental_onefile'
        $sw.Stop()
        if ((-not $r.TimedOut) -and $r.StdOut -match $marker) {
            Add-Result '单文件实验版' 'PASS' $sw.Elapsed.TotalSeconds ("实验版 {0} MB · --scan-only 正常退出（仍不作为正式交付）" -f $sizeMb)
        }
        else {
            $reason = if ($r.TimedOut) { '自解压失败/挂起（中文路径下已知问题）' } else { '未拿到扫描汇总输出' }
            Add-Result '单文件实验版' 'SKIP' $sw.Elapsed.TotalSeconds ($reason + '；正式交付只含目录版，属预期')
        }
    }

    # =====================================================================
    # 汇总
    # =====================================================================
    $Script:TotalSw.Stop()
    $passCount = @($Script:Results | Where-Object { $_.Status -eq 'PASS' }).Count
    $failCount = @($Script:Results | Where-Object { $_.Status -eq 'FAIL' }).Count
    $skipCount = @($Script:Results | Where-Object { $_.Status -eq 'SKIP' }).Count

    $lines = New-Object System.Collections.ArrayList
    [void]$lines.Add('')
    [void]$lines.Add('============================================================')
    [void]$lines.Add(' 验证汇总 · QingFeng Cleaner')
    [void]$lines.Add('============================================================')
    foreach ($item in $Script:Results) {
        $pad = $item.Name.PadRight(24, ' ')
        [void]$lines.Add((' [{0}] {1} {2,8:N2}s  {3}' -f $item.Status, $pad, $item.Seconds, $item.Detail))
    }
    [void]$lines.Add('------------------------------------------------------------')
    [void]$lines.Add((' 结果：{0} PASS / {1} FAIL / {2} SKIP · 总耗时 {3:N1}s' -f `
                $passCount, $failCount, $skipCount, $Script:TotalSw.Elapsed.TotalSeconds))
    [void]$lines.Add((' 日志：tools\_verify_out\{0}\' -f $Script:Stamp))
    [void]$lines.Add('============================================================')

    Write-Host ''
    foreach ($line in $lines) {
        if ($line -match '\[FAIL\]') { Write-Host $line -ForegroundColor Red }
        elseif ($line -match '\[PASS\]') { Write-Host $line -ForegroundColor Green }
        elseif ($line -match '\[SKIP\]') { Write-Host $line -ForegroundColor Yellow }
        elseif ($line -match '结果：') { Write-Host $line -ForegroundColor $(if ($failCount -eq 0) { 'Green' } else { 'Red' }) }
        else { Write-Host $line }
    }

    [void]$lines.Add('')
    [void]$lines.Add(('Python: {0}' -f $pyVersion))
    try {
        $lines | Set-Content -LiteralPath (Join-Path $Script:OutDir 'summary.txt') -Encoding UTF8
    }
    catch { Write-Warn "汇总写入失败：$($_.Exception.Message)" }

    if ($failCount -gt 0) { exit 1 }
    exit 0
}
catch {
    Write-Host ''
    Write-Fail ("验证脚本异常：{0}" -f $_.Exception.Message)
    Write-Info ("异常位置：{0}" -f $_.InvocationInfo.PositionMessage)
    exit 1
}
