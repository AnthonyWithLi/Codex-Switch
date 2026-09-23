# Codex-Switch 一键安装与快捷方式部署脚本
[CmdletBinding()]
param(
    [string]$PythonPath
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "=== 正在初始化 Codex-Switch ===" -ForegroundColor Cyan

# 1. 检查或生成 config.json
$ConfigFile = Join-Path $ScriptDir "config.json"
$ExampleFile = Join-Path $ScriptDir "config.example.json"

if (-not (Test-Path -LiteralPath $ConfigFile)) {
    if (Test-Path -LiteralPath $ExampleFile) {
        Copy-Item -LiteralPath $ExampleFile -Destination $ConfigFile
        Write-Host "已创建本地配置文件: config.json" -ForegroundColor Green
    }
}

# 2. 如果提供了自定义 Python 路径，写入 config.json
if ($PythonPath) {
    if (-not (Test-Path -LiteralPath $PythonPath)) {
        Write-Warning "提供的 Python 路径不存在: $PythonPath"
    } else {
        try {
            $cfg = Get-Content -LiteralPath $ConfigFile -Raw -Encoding UTF8 | ConvertFrom-Json
            $cfg.python = $PythonPath
            $cfg | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $ConfigFile -Encoding UTF8
            Write-Host "已更新 Python 路径至 config.json: $PythonPath" -ForegroundColor Green
        } catch {
            Write-Warning "更新 config.json 失败: $_"
        }
    }
}

# 3. 调用主脚本安装快捷方式
$MainScript = Join-Path $ScriptDir "start_codex_desktop.ps1"
if (Test-Path -LiteralPath $MainScript) {
    Write-Host "正在生成桌面快捷方式并预检快照..." -ForegroundColor Yellow
    & powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File $MainScript -InstallShortcut
    Write-Host "=== 安装完成！请在桌面查看「修复 Codex 对话投影」快捷方式 ===" -ForegroundColor Cyan
} else {
    Write-Error "找不到主脚本: $MainScript"
}
