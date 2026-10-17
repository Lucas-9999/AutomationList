param([switch]$CheckOnly)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$Host.UI.RawUI.WindowTitle = '外贸工作台 - 后端服务'
$tradeflowUrl = 'http://127.0.0.1:8765'

try {
    Write-Host ''
    Write-Host '外贸工作台 · 后端服务' -ForegroundColor Cyan
    Write-Host ('工作目录：' + $PSScriptRoot)
    Write-Host ('工作台：  ' + $tradeflowUrl)
    Write-Host ('接口文档：' + $tradeflowUrl + '/docs')
    Write-Host ''

    # Reuse a running workbench; never stop an unrelated process on this port.
    $tradeflowSocket = New-Object System.Net.Sockets.TcpClient
    try {
        $tradeflowConnect = $tradeflowSocket.ConnectAsync('127.0.0.1', 8765)
        try { $null = $tradeflowConnect.Wait(700) } catch { }
        $tradeflowPortBusy = $tradeflowSocket.Connected
    } finally {
        $tradeflowSocket.Dispose()
    }
    if ($tradeflowPortBusy) {
        try {
            $tradeflowHealth = Invoke-RestMethod -Uri ($tradeflowUrl + '/health') -TimeoutSec 3
            $tradeflowApi = Invoke-RestMethod -Uri ($tradeflowUrl + '/openapi.json') -TimeoutSec 3
            $tradeflowRoutes = $tradeflowApi.paths.PSObject.Properties.Name
            if ($tradeflowHealth.status -ne 'ok' -or $tradeflowHealth.stage -ne 'local_review_prototype' -or $tradeflowRoutes -notcontains '/document-jobs/preview' -or $tradeflowRoutes -notcontains '/inquiries/upload') {
                throw 'Unexpected service'
            }
        } catch {
            throw '8765端口已被占用，但未确认是本工作台服务。请检查占用程序；此入口不会强行关闭它。'
        }
        Write-Host '后端服务已经运行，无需重复启动。' -ForegroundColor Green
        Write-Host '请在浏览器打开上面的工作台地址；关闭本窗口不会停止已有服务。'
        exit 0
    }

    $tradeflowPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $tradeflowPython -PathType Leaf)) {
        $tradeflowPythonCommand = Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $tradeflowPythonCommand) { throw '未找到Python。请安装Python及项目依赖后再启动。' }
        $tradeflowPython = $tradeflowPythonCommand.Source
    }
    Write-Host ('Python：  ' + $tradeflowPython)
    & $tradeflowPython -c 'import importlib.util, sys; sys.exit(0 if all(importlib.util.find_spec(m) is not None for m in (''uvicorn'', ''fastapi'', ''dotenv'', ''multipart'', ''pydantic'')) else 1)'
    if ($LASTEXITCODE -ne 0) {
        Write-Host ('安装依赖命令：& "' + $tradeflowPython + '" -m pip install -r "' + (Join-Path $PSScriptRoot 'requirements.txt') + '"')
        throw '当前Python缺少项目运行依赖，请按上面的命令安装后重试。'
    }

    $tradeflowEnvFile = Join-Path $PSScriptRoot '.env'
    if (-not (Test-Path -LiteralPath $tradeflowEnvFile -PathType Leaf)) {
        $tradeflowEnvFile = Join-Path $PSScriptRoot '.evn'
    }
    $tradeflowArguments = @('-m', 'uvicorn', 'tradeflow.api:create_app', '--factory', '--host', '127.0.0.1', '--port', '8765')
    if (Test-Path -LiteralPath $tradeflowEnvFile -PathType Leaf) {
        Write-Host ('配置文件：' + [IO.Path]::GetFileName($tradeflowEnvFile) + '（不显示密钥）')
        $tradeflowArguments += @('--env-file', $tradeflowEnvFile)
    } else {
        Write-Host '未找到.env或.evn，使用当前进程环境配置。'
    }
    if ($CheckOnly) {
        Write-Host '启动检查通过；本次没有启动服务。' -ForegroundColor Green
        exit 0
    }
    Write-Host ''
    Write-Host '正在启动，请等待下方出现 Application startup complete。' -ForegroundColor Green
    Write-Host '使用期间请保留此窗口。按 Ctrl+C 停止后端，或关闭此窗口。'
    Write-Host ''
    & $tradeflowPython @tradeflowArguments
    if ($LASTEXITCODE -ne 0) { throw ('后端退出，代码：' + $LASTEXITCODE + '。请查看上方报错信息。') }
    exit 0
} catch {
    Write-Host ''
    Write-Host ('启动未完成：' + $_.Exception.Message) -ForegroundColor Red
    Write-Host '窗口会保留，便于查看报错信息。'
    exit 1
}
