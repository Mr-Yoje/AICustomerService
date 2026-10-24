# 一键启动：如同级目录存在前端仓库则先打包，再启动后端（单端口 http://127.0.0.1:8000）
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

Push-Location (Join-Path $root "backend")
$serveArgs = @("scripts/serve.py", "--port", $Port)
if ($Offline) { $serveArgs += "--offline" }
python @serveArgs
Pop-Location
