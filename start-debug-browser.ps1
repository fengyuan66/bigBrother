param(
    [ValidateSet("chrome", "edge", "brave")]
    [string]$Browser = "chrome"
)

$ErrorActionPreference = "Stop"

$configs = @{
    chrome = @{
        Port = 9222
        Paths = @(
            "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
            "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
            "$env:LocalAppData\Google\Chrome\Application\chrome.exe"
        )
    }
    edge = @{
        Port = 9223
        Paths = @(
            "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
            "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
        )
    }
    brave = @{
        Port = 9224
        Paths = @(
            "$env:ProgramFiles\BraveSoftware\Brave-Browser\Application\brave.exe",
            "${env:ProgramFiles(x86)}\BraveSoftware\Brave-Browser\Application\brave.exe",
            "$env:LocalAppData\BraveSoftware\Brave-Browser\Application\brave.exe"
        )
    }
}

$config = $configs[$Browser]
$browserPath = $config.Paths | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $browserPath) {
    throw "Could not find $Browser. Install it or update this script with the browser path."
}

Write-Host "Starting $Browser with tab export enabled on port $($config.Port)."
Write-Host "If $Browser is already running, close it first, then run this script again."

Start-Process -FilePath $browserPath -ArgumentList @(
    "--remote-debugging-port=$($config.Port)",
    "about:blank"
)

