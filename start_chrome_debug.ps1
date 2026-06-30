# 启动一个“开启远程调试端口”的真实 Chrome，供本程序通过 CDP 接管。
#
# 用法：
#   1) 在 PowerShell 里运行：  .\start_chrome_debug.ps1
#   2) 在弹出的 Chrome 里手动登录好 BOSS 直聘（过安全验证 + 扫码），并搜一下职位确认能看到。
#   3) 这个 Chrome 窗口【保持打开】。
#   4) 在 .env 里设置  CDP_ENDPOINT=http://localhost:9222 ，重启服务，再跑 BOSS 抓取。
#
# 用独立数据目录（data\chrome-debug），因为 Chrome 禁止对“默认目录”开远程调试。

$ErrorActionPreference = "Stop"

$port = 9222
$dataDir = Join-Path $PSScriptRoot "data\chrome-debug"

$candidates = @(
    "C:\Program Files\Google\Chrome\Application\chrome.exe",
    "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe")
)
$chrome = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $chrome) {
    Write-Error "找不到 chrome.exe，请确认已安装 Google Chrome，或手动修改本脚本里的路径。"
    exit 1
}

New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

Write-Host "启动 Chrome（调试端口 $port，数据目录 $dataDir）..." -ForegroundColor Cyan
Write-Host "请在打开的 Chrome 里登录好 BOSS 直聘并确认能看到职位，然后保持窗口不要关。" -ForegroundColor Yellow

# --remote-allow-origins=* 是新版 Chrome 连接 CDP 的必需参数，否则 403
$chromeArgs = @(
    "--remote-debugging-port=$port",
    "--remote-allow-origins=*",
    "--user-data-dir=$dataDir",
    "https://www.zhipin.com/"
)

& $chrome $chromeArgs
