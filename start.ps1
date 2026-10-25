# 一键启动：如同级目录存在前端仓库则先打包，再启动后端。
# 监听 0.0.0.0，本机用 http://127.0.0.1:<端口>，同一 WiFi 用本机局域网地址。
param(
    [int]$Port = 8000,
    [switch]$Offline,
    [switch]$SkipWebBuild
)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

$root = $PSScriptRoot
$web = Join-Path (Split-Path $root -Parent) "AICustomerService-web"
if (-not $SkipWebBuild -and (Test-Path (Join-Path $web "package.json"))) {
    Push-Location $web
    if (-not (Test-Path "node_modules")) { npm install }
    npm run build
    Pop-Location
}

Write-Host "本机访问： http://127.0.0.1:$Port"
Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" -and $_.PrefixOrigin -ne "WellKnown" } |
    ForEach-Object { Write-Host "局域网访问： http://$($_.IPAddress):$Port  ($($_.InterfaceAlias))" }

Push-Location (Join-Path $root "backend")
$serveArgs = @("scripts/serve.py", "--host", "0.0.0.0", "--port", $Port)
if ($Offline) { $serveArgs += "--offline" }
python @serveArgs
Pop-Location
