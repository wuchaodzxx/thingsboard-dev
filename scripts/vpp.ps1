<#
 =====================================================================
 虚拟电厂物联接入验证环境 — 一键脚本（Windows PowerShell 5.1+ / pwsh）
 用法：powershell -ExecutionPolicy Bypass -File scripts\vpp.ps1 <命令> [参数]
   init      初始化：检查依赖 -> 生成 deploy\.env -> 创建 .venv 并安装依赖
   start     启动 TB（compose up -d，等待 healthy）+ 模拟器（宿主机 uvicorn :19000）
   stop      停止模拟器 + TB（保留容器与数据卷）
   restart   重启（= stop + start）
   status    组件状态总览（TB / 模拟器 / 端口 / 测试产物）
   logs      日志跟踪：logs [tb|sim]（默认 tb）
   test      自动化测试：test [规模]（默认 1 全量；>1 仅并发用例组）
   backup    备份 TB 数据卷（自动停/启 TB 保证一致性）到 backups\
 端口/镜像等在 deploy\.env 配置（模板 deploy\.env.example）。
 =====================================================================
#>
param(
    [Parameter(Position = 0)][string]$Cmd = "status",
    [Parameter(Position = 1)][string]$Arg1 = ""
)
$ErrorActionPreference = "Stop"
$Root    = Split-Path -Parent $PSScriptRoot
$Deploy  = Join-Path $Root "deploy"
$EnvFile = Join-Path $Deploy ".env"
$SimLog  = Join-Path $Root "tests\sim_uvicorn.log"
$SimPid  = Join-Path $Root "tests\sim_uvicorn.pid"
$SimPort = 19000
$TbPort  = 18080
# 从 deploy\.env 读取端口覆盖（若存在）
if (Test-Path $EnvFile) {
    foreach ($line in Get-Content $EnvFile | Where-Object { $_ -match '^\s*(SIM_HTTP_PORT|TB_HTTP_PORT)\s*=' }) {
        $k, $v = $line -split '\s*=\s*', 2
        if ($k -eq 'SIM_HTTP_PORT') { $SimPort = [int]$v }
        if ($k -eq 'TB_HTTP_PORT')  { $TbPort  = [int]$v }
    }
}
$Unix = $IsLinux -or $IsMacOS   # pwsh 跨平台时 .venv 路径不同

function Get-Py {
    $pWin = Join-Path $Root ".venv\Scripts\python.exe"
    $pUnix = Join-Path $Root ".venv/bin/python"
    if (Test-Path $pWin)  { return $pWin }
    if (Test-Path $pUnix) { return $pUnix }
    return "python3"
}
function Invoke-Compose {
    $argsList = @("compose", "--project-directory", $Deploy)
    if (Test-Path $EnvFile) { $argsList += @("--env-file", $EnvFile) }
    $argsList += @("-f", (Join-Path $Deploy "docker-compose.yml"))
    & docker @argsList @args
    if ($LASTEXITCODE -ne 0) { throw "docker compose 执行失败（exit=$LASTEXITCODE）" }
}
function Get-PortPid([int]$Port) {
    try {
        $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
        return ($c | Select-Object -First 1).OwningProcess
    } catch {
        if ($Unix) { $out = & lsof -ti "tcp:$Port" -sTCP:LISTEN 2>$null; if ($out) { return [int]($out | Select-Object -First 1) } }
        return $null
    }
}
function Wait-Http([string]$Url, [int]$TimeoutSec, [string]$Msg) {
    $i = 0
    while ($true) {
        try {
            $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500) { Write-Host "OK $Msg"; return }
        } catch { }
        if ($i -ge $TimeoutSec) { throw "超时（${TimeoutSec}s）：$Msg" }
        Start-Sleep -Seconds 2; $i += 2
    }
}
function Start-Simulator {
    if (Get-PortPid $SimPort) { Write-Host "· 端口 $SimPort 已被监听，跳过模拟器启动"; return }
    Write-Host "· 启动模拟器（uvicorn :$SimPort，日志 $SimLog）…"
    $py = Get-Py
    $appDir = Join-Path (Join-Path $Root "simulator") "backend"
    $argList = @("-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "$SimPort", "--app-dir", $appDir)
    # -WindowStyle 仅 Windows 支持（pwsh Unix 版不支持该参数）；
    # Unix 上 Start-Process 子进程会随 pwsh 退出被 SIGHUP 杀死，故走 nohup + sh 后台启动
    if ($Unix) {
        $sh = 'cd "{0}" && nohup "{1}" -m uvicorn app.main:app --host 0.0.0.0 --port {2} --app-dir "{3}" >> "{4}" 2>&1 < /dev/null & echo $! > "{5}"' -f `
            $Root, $py, $SimPort, $appDir, $SimLog, $SimPid
        & /bin/sh -c $sh
    } else {
        $p = Start-Process -FilePath $py -ArgumentList $argList `
            -WorkingDirectory $Root -WindowStyle Hidden `
            -RedirectStandardOutput $SimLog -RedirectStandardError "$SimLog.err" -PassThru
        Set-Content -Path $SimPid -Value $p.Id
    }
    Wait-Http "http://localhost:$SimPort/api/health" 60 "模拟器就绪 http://localhost:$SimPort/"
}
function Stop-Simulator {
    $pid2 = $null
    try { $pid2 = Get-PortPid $SimPort } catch { }
    if (-not $pid2 -and (Test-Path $SimPid)) { $pid2 = [int](Get-Content $SimPid -ErrorAction SilentlyContinue) }
    if ($pid2) {
        try {
            if ($Unix) { & kill $pid2 2>$null } else { & taskkill /PID $pid2 /F 2>$null | Out-Null }
        } catch { }
        Write-Host "OK 模拟器已停止（pid=$pid2）"
    } else { Write-Host "· 模拟器未在运行" }
    Remove-Item $SimPid -ErrorAction SilentlyContinue
}
function Invoke-Init {
    Write-Host "== 初始化 =="
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "未找到 docker（需 Docker Desktop for Windows）" }
    docker compose version 1>$null 2>$null
    if ($LASTEXITCODE -ne 0) { throw "未找到 Docker Compose v2" }
    if (-not (Test-Path $EnvFile)) { Copy-Item (Join-Path $Deploy ".env.example") $EnvFile; Write-Host "OK 已生成 deploy\.env（可按需修改端口/JVM）" }
    else { Write-Host "· deploy\.env 已存在，跳过" }
    $py = Get-Py
    if (-not (Test-Path $py) -or $py -eq "python3") { throw "未找到 Python 3（需 3.11+），请先安装并创建 .venv" }
    if (-not (Test-Path (Join-Path $Root ".venv"))) {
        & python3 -m venv (Join-Path $Root ".venv"); Write-Host "OK 已创建 .venv"
    }
    Push-Location $Root
    & (Get-Py) -m pip install -q --upgrade pip
    & (Get-Py) -m pip install -q -r certmgr/requirements.txt -r simulator/backend/requirements.txt pytest jsonschema
    Pop-Location
    Write-Host "OK 依赖安装完成（certmgr + 模拟器 + pytest/jsonschema）"
    Write-Host "下一步：scripts\vpp.ps1 start"
}
function Invoke-Start {
    Write-Host "== 启动 TB =="
    Invoke-Compose up -d
    Wait-Http "http://localhost:$TbPort/login" 300 "TB 就绪 http://localhost:$TbPort/（首次启动建库约 1~3 分钟）"
    Write-Host "== 启动模拟器 =="
    Start-Simulator
    Invoke-Status
}
function Invoke-Stop {
    Write-Host "== 停止 =="
    Stop-Simulator
    Invoke-Compose stop
    Write-Host "OK TB 已停止（数据卷保留；彻底重置用 docker compose down -v，慎用）"
}
function Invoke-Restart { Invoke-Stop; Invoke-Start }
function Invoke-Status {
    Write-Host "== 组件状态 =="
    $state = (& docker inspect -f "{{.State.Status}}" vpp-thingsboard 2>$null)
    Write-Host "TB 容器    : $(if ($state) { $state } else { '未运行' })"
    try { $code = (Invoke-WebRequest "http://localhost:$TbPort/login" -UseBasicParsing -TimeoutSec 5).StatusCode } catch { $code = "000" }
    Write-Host "TB HTTP    : http://localhost:$($TbPort)/login -> $code"
    $tls = "未监听"
    try {
        $c = New-Object Net.Sockets.TcpClient          # 跨平台（Windows PS5.1 / pwsh-macOS/Linux 通用）
        $ar = $c.BeginConnect("localhost", 18885, $null, $null)
        if ($ar.AsyncWaitHandle.WaitOne(3000) -and $c.Connected) { $tls = "监听中(:18885)" }
        $c.Close()
    } catch { }
    Write-Host "MQTT TLS   : $tls"
    try {
        $h = Invoke-RestMethod "http://localhost:$SimPort/api/health" -TimeoutSec 5
        Write-Host "模拟器     : http://localhost:$SimPort/ 在线（设备数=$($h.devices)）"
    } catch { Write-Host "模拟器     : 离线" }
    Write-Host "-- 测试产物 --"
    foreach ($f in @("tests\results_p6_s1.json", "tests\results_p6_s10.json", "tests\results_p6_s100.json", "tests\report_p6.html")) {
        $full = Join-Path $Root $f
        Write-Host "$(if (Test-Path $full) { '  OK ' + $f } else { '  -- ' + $f + '（无）' })"
    }
}
function Invoke-Logs([string]$Target) {
    if ($Target -eq "sim") { Get-Content $SimLog -Tail 100 -Wait }
    else { Invoke-Compose logs -f --tail=100 }
}
function Invoke-Test([string]$Scale) {
    Write-Host "== 自动化测试（scale=$Scale）=="
    Push-Location $Root
    try {
        if ([int]$Scale -gt 1) { & (Get-Py) -m pytest tests/p6 --scale $Scale -m scale -v }
        else { & (Get-Py) -m pytest tests/p6 --scale $Scale -v }
        if ($LASTEXITCODE -ne 0) { throw "pytest 失败（exit=$LASTEXITCODE）" }
    } finally { Pop-Location }
}
function Invoke-Backup {
    Write-Host "== 备份 TB 数据卷（先停 TB 保证一致性，完成后自动启动）=="
    $out = Join-Path $Root "backups"; New-Item -ItemType Directory -Force -Path $out | Out-Null
    $name = "vpp-tb-data-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".tgz"
    $mount = ($out -replace '\\', '/')
    $wasRunning = $false
    $state = & docker inspect -f "{{.State.Status}}" vpp-thingsboard 2>$null
    $wasRunning = ($state -eq "running")
    Invoke-Compose stop
    & docker run --rm -v "vpp-tb-data:/src:ro" -v "${mount}:/bak" alpine sh -c "cd /src && tar czf /bak/$name ."
    if ($LASTEXITCODE -ne 0) { throw "备份失败（exit=$LASTEXITCODE）" }
    if ($wasRunning) { Invoke-Compose start; Wait-Http "http://localhost:$TbPort/login" 300 "TB 已恢复" }
    Write-Host "OK 备份完成：backups\$name"
}

switch ($Cmd) {
    "init"    { Invoke-Init }
    "start"   { Invoke-Start }
    "stop"    { Invoke-Stop }
    "restart" { Invoke-Restart }
    "status"  { Invoke-Status }
    "logs"    { Invoke-Logs $(if ($Arg1) { $Arg1 } else { "tb" }) }
    "test"    { Invoke-Test $(if ($Arg1) { $Arg1 } else { "1" }) }
    "backup"  { Invoke-Backup }
    default   { Get-Content $PSCommandPath | Select-Object -Skip 2 -First 17 | ForEach-Object { $_ -replace '^\s*#?\s?', '' } ; exit 1 }
}
