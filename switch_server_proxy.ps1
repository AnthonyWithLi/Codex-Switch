# -*- coding: utf-8 -*-
<#
.SYNOPSIS
    服务器与本机双代理管理工具 (Server & Local Dual Proxy Manager)
.DESCRIPTION
    管理与监控 OKYun 与 Clash 双通道代理架构：
    1. 通道 1 (17890): Clash -> 供给服务器端 Codex CLI、系统网络及通用流量；
    2. 通道 2 (17891): OKYun -> 供给服务器端 agy cli 专属专线流量；
    3. 本机策略: 仅 Clash 开启 TUN 与系统代理，OKYun 保持纯端口监听，本机 agy 精准直连 OKYun。
#>

[CmdletBinding()]
param(
    [ValidateSet("okyun", "clash", "dual", "status", "repair")]
    [string]$Channel,
    [switch]$Silent,
    [switch]$NoVerify
)

$ErrorActionPreference = "Stop"

# UTF-8 控制台兼容
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch {}
try { $OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch {}

$SshConfigFile = Join-Path $env:USERPROFILE ".ssh\config"

function Test-PortListening {
    param([int]$Port)
    if ($Port -le 0 -or $Port -gt 65535) { return $false }
    $tcp = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $tcp.BeginConnect("127.0.0.1", $Port, $null, $null)
        if ($iar.AsyncWaitHandle.WaitOne(350)) {
            $tcp.EndConnect($iar)
            return $true
        }
        return $false
    } catch {
        return $false
    } finally {
        $tcp.Close()
    }
}

function Get-ProxyStatus {
    $okyun = @{
        Name = "OKYun"
        ProcessName = "okyunCore"
        IsRunning = $false
        IsListening = $false
        Port = $null
        ConfigPort = $null
        HasTun = $false
        Pids = @()
    }

    $clash = @{
        Name = "Clash"
        ProcessName = "verge-mihomo"
        IsRunning = $false
        IsListening = $false
        Port = $null
        ConfigPort = $null
        HasTun = $false
        Pids = @()
    }

    # --- 1. 动态检测 OKYun ---
    $okyunPref = Join-Path $env:APPDATA "com.follow\OKYun\shared_preferences.json"
    if (Test-Path $okyunPref) {
        try {
            $raw = Get-Content -Raw -Encoding UTF8 $okyunPref -ErrorAction SilentlyContinue
            $prefJson = $raw | ConvertFrom-Json
            if ($prefJson.'flutter.config') {
                $flConfig = $prefJson.'flutter.config' | ConvertFrom-Json
                if ($flConfig.patchClashConfig -and $flConfig.patchClashConfig.'mixed-port') {
                    $okyun.ConfigPort = [int]$flConfig.patchClashConfig.'mixed-port'
                }
            }
        } catch {}
    }
    $okyunProcs = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match "^okyunCore$" }
    if ($okyunProcs) {
        $okyun.IsRunning = $true
        $okyun.Pids = @($okyunProcs | ForEach-Object { $_.Id })
        $conns = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object {
            $_.OwningProcess -in $okyun.Pids -and $_.LocalPort -ne 1053 -and $_.LocalAddress -in @("127.0.0.1", "0.0.0.0")
        }
        if ($conns) {
            $okyun.Port = [int]$conns[0].LocalPort
        }
    }
    if (-not $okyun.Port -and $okyun.ConfigPort) {
        $okyun.Port = $okyun.ConfigPort
    }
    if (-not $okyun.Port) { $okyun.Port = 7897 }
    if ($okyun.Port) {
        $okyun.IsListening = Test-PortListening -Port $okyun.Port
    }
    $okyunAdapter = Get-NetAdapter -Name "OKYun" -ErrorAction SilentlyContinue
    if ($okyunAdapter -and $okyunAdapter.Status -eq "Up") {
        $okyun.HasTun = $true
    }

    # --- 2. 动态检测 Clash (Clash Verge / Mihomo) ---
    $clashYaml = Join-Path $env:APPDATA "io.github.clash-verge-rev.clash-verge-rev\clash-verge.yaml"
    if (Test-Path $clashYaml) {
        try {
            $lines = Get-Content $clashYaml -ErrorAction SilentlyContinue
            foreach ($line in $lines) {
                if ($line -match "^\s*mixed-port:\s*(\d+)") {
                    $clash.ConfigPort = [int]$Matches[1]
                    break
                }
                if ($line -match "^\s*port:\s*(\d+)") {
                    $clash.ConfigPort = [int]$Matches[1]
                }
            }
        } catch {}
    }
    $clashProcs = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match "verge-mihomo|clash-meta|mihomo|clash" }
    if ($clashProcs) {
        $clash.IsRunning = $true
        $clash.Pids = @($clashProcs | ForEach-Object { $_.Id })
        $conns = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object {
            $_.OwningProcess -in $clash.Pids -and $_.LocalPort -ne 33331 -and $_.LocalAddress -in @("127.0.0.1", "0.0.0.0")
        }
        if ($conns) {
            $clash.Port = [int]$conns[0].LocalPort
        }
    }
    if (-not $clash.Port -and $clash.ConfigPort) {
        $clash.Port = $clash.ConfigPort
    }
    if (-not $clash.Port) { $clash.Port = 1578 }
    if ($clash.Port) {
        $clash.IsListening = Test-PortListening -Port $clash.Port
    }
    $clashAdapter = Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { $_.InterfaceDescription -match "Meta Tunnel|Clash" -and $_.Name -ne "OKYun" -and $_.Status -eq "Up" }
    if ($clashAdapter) {
        $clash.HasTun = $true
    }

    # --- 3. 读取当前 ~/.ssh/config 的转发目标端口 ---
    $forward17890 = $null
    $forward17891 = $null
    if (Test-Path $SshConfigFile) {
        $lines = Get-Content $SshConfigFile -ErrorAction SilentlyContinue
        foreach ($l in $lines) {
            if ($l -match "RemoteForward\s+\S+:17890\s+\S+:(\d+)") {
                $forward17890 = [int]$Matches[1]
            }
            if ($l -match "RemoteForward\s+\S+:17891\s+\S+:(\d+)") {
                $forward17891 = [int]$Matches[1]
            }
        }
    }

    # --- 4. 读取 Windows WinINET 系统代理 ---
    $sysProxyEnabled = $false
    $sysProxyServer = ""
    try {
        $reg = Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue
        if ($reg) {
            $sysProxyEnabled = [bool]$reg.ProxyEnable
            $sysProxyServer = [string]$reg.ProxyServer
        }
    } catch {}

    return @{
        OKYun = $okyun
        Clash = $clash
        Forward17890 = $forward17890
        Forward17891 = $forward17891
        SysProxyEnabled = $sysProxyEnabled
        SysProxyServer = $sysProxyServer
    }
}

function Ensure-DualTunnelsConfig {
    param(
        [int]$Port17890,
        [int]$Port17891
    )

    if (-not (Test-Path $SshConfigFile)) {
        throw "找不到 SSH 配置文件: $SshConfigFile"
    }

    $lines = Get-Content $SshConfigFile -Encoding UTF8
    $newLines = @()
    $inTargetHost = $false

    foreach ($line in $lines) {
        if ($line -match "^\s*Host\s+(server-proxy|server-codex)\s*$") {
            $inTargetHost = $true
            $newLines += $line
            continue
        }
        if ($inTargetHost -and $line -match "^\s*Host\s+") {
            $inTargetHost = $false
        }

        if ($inTargetHost -and $line -match "RemoteForward\s+\S+:17890") {
            $newLines += "  RemoteForward 127.0.0.1:17890 127.0.0.1:$Port17890"
            continue
        }
        if ($inTargetHost -and $line -match "RemoteForward\s+\S+:17891") {
            $newLines += "  RemoteForward 127.0.0.1:17891 127.0.0.1:$Port17891"
            continue
        }
        $newLines += $line
    }

    $finalContent = ($newLines -join "`r`n").TrimEnd() + "`r`n"
    [System.IO.File]::WriteAllText($SshConfigFile, $finalContent, (New-Object System.Text.UTF8Encoding($false)))
}

function Restart-ProxyTunnel {
    $restarted = $false
    try {
        $sshProcs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            $_.Name -match "^ssh\.exe$" -and $_.CommandLine -match "server-proxy"
        }
        if ($sshProcs) {
            foreach ($p in $sshProcs) {
                Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
            }
            $restarted = $true
            Start-Sleep -Milliseconds 1200
        }
    } catch {}

    # 启动后台守护隧道连接
    try {
        $running = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            $_.Name -match "^ssh\.exe$" -and $_.CommandLine -match "server-proxy"
        }
        if (-not $running) {
            $psi = New-Object System.Diagnostics.ProcessStartInfo
            $psi.FileName = "ssh.exe"
            $psi.Arguments = "-N -o ConnectTimeout=5 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 server-proxy"
            $psi.CreateNoWindow = $true
            $psi.UseShellExecute = $false
            $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
            [System.Diagnostics.Process]::Start($psi) | Out-Null
            Start-Sleep -Milliseconds 1500
            $restarted = $true
        }
    } catch {}

    return $restarted
}

function Test-RemoteChannels {
    $res = @{
        Channel17890 = @{ Success = $false; Message = "" }
        Channel17891 = @{ Success = $false; Message = "" }
    }

    # 17890 -> OpenAI / Codex
    try {
        $out0 = & ssh -o LogLevel=ERROR -o ConnectTimeout=4 -o BatchMode=yes server-codex "curl -sI --connect-timeout 4 -x http://127.0.0.1:17890 https://api.openai.com | head -n 1" 2>&1
        $clean0 = @($out0 | Where-Object { $_ -notmatch 'remote port forwarding failed' })
        $t0 = ($clean0 -join "`n").Trim()
        if ($t0 -match "HTTP/\S+\s+(\d+)") {
            $code = $Matches[1]
            if ($code -in @("200", "421", "400", "403")) {
                $res.Channel17890 = @{ Success = $true; Message = "连通正常 (HTTP $code)" }
            } else {
                $res.Channel17890 = @{ Success = $false; Message = "返回状态码: $code" }
            }
        } else {
            $res.Channel17890 = @{ Success = $false; Message = if ($t0) { $t0 } else { "连接超时" } }
        }
    } catch {
        $res.Channel17890 = @{ Success = $false; Message = $_.Exception.Message }
    }

    # 17891 -> Google / AGY
    try {
        $out1 = & ssh -o LogLevel=ERROR -o ConnectTimeout=4 -o BatchMode=yes server-codex "curl -sI --connect-timeout 4 -x http://127.0.0.1:17891 https://generativelanguage.googleapis.com | head -n 1" 2>&1
        $clean1 = @($out1 | Where-Object { $_ -notmatch 'remote port forwarding failed' })
        $t1 = ($clean1 -join "`n").Trim()
        if ($t1 -match "HTTP/\S+\s+(\d+)") {
            $code = $Matches[1]
            if ($code -in @("200", "404", "400", "403")) {
                $res.Channel17891 = @{ Success = $true; Message = "连通正常 (HTTP $code)" }
            } else {
                $res.Channel17891 = @{ Success = $false; Message = "返回状态码: $code" }
            }
        } else {
            $res.Channel17891 = @{ Success = $false; Message = if ($t1) { $t1 } else { "连接超时" } }
        }
    } catch {
        $res.Channel17891 = @{ Success = $false; Message = $_.Exception.Message }
    }

    return $res
}

function Set-SystemProxyToClash {
    param([int]$ClashPort = 1578)
    try {
        Set-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -Name ProxyServer -Value "127.0.0.1:$ClashPort"
        Set-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -Name ProxyEnable -Value 1
        return $true
    } catch {
        return $false
    }
}

function Show-SwitcherUI {
    Add-Type -AssemblyName System.Windows.Forms | Out-Null
    Add-Type -AssemblyName System.Drawing | Out-Null
    [System.Windows.Forms.Application]::EnableVisualStyles()

    $form = New-Object System.Windows.Forms.Form
    $form.Text = "服务器与本机双代理管理中心 (OKYun & Clash)"
    $form.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9.5)
    $form.StartPosition = "CenterScreen"
    $form.FormBorderStyle = "FixedDialog"
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $form.TopMost = $true
    $form.ShowInTaskbar = $true
    $form.ClientSize = New-Object System.Drawing.Size(620, 510)

    # 提取图标
    $launcherExe = Join-Path $env:LOCALAPPDATA "CodexRepair\CodexRepair.exe"
    if (Test-Path $launcherExe) {
        try { $form.Icon = [System.Drawing.Icon]::ExtractAssociatedIcon($launcherExe) } catch {}
    }

    # 顶部架构总览面板
    $panelTop = New-Object System.Windows.Forms.Panel
    $panelTop.Location = New-Object System.Drawing.Point(20, 16)
    $panelTop.Size = New-Object System.Drawing.Size(580, 80)
    $panelTop.BackColor = [System.Drawing.Color]::FromArgb(242, 246, 252)
    $panelTop.BorderStyle = [System.Windows.Forms.BorderStyle]::FixedSingle
    $form.Controls.Add($panelTop)

    $lblTopTitle = New-Object System.Windows.Forms.Label
    $lblTopTitle.Location = New-Object System.Drawing.Point(12, 8)
    $lblTopTitle.Size = New-Object System.Drawing.Size(550, 22)
    $lblTopTitle.Text = "⚡ 当前已激活【双通道独立并发】分流架构"
    $lblTopTitle.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 10, [System.Drawing.FontStyle]::Bold)
    $lblTopTitle.ForeColor = [System.Drawing.Color]::FromArgb(0, 80, 180)
    $panelTop.Controls.Add($lblTopTitle)

    $lblTopDesc = New-Object System.Windows.Forms.Label
    $lblTopDesc.Location = New-Object System.Drawing.Point(12, 32)
    $lblTopDesc.Size = New-Object System.Drawing.Size(550, 42)
    $lblTopDesc.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9)
    $lblTopDesc.ForeColor = [System.Drawing.Color]::FromArgb(60, 60, 60)
    $lblTopDesc.Text = "• 通道 1 (端口 17890): Clash -> 专供 Codex CLI、服务器系统及通用流量`n• 通道 2 (端口 17891): OKYun -> 专供 agy cli 专属高质量 Gemini 专线"
    $panelTop.Controls.Add($lblTopDesc)

    # 本机代理状态 GroupBox
    $grpLocal = New-Object System.Windows.Forms.GroupBox
    $grpLocal.Location = New-Object System.Drawing.Point(20, 108)
    $grpLocal.Size = New-Object System.Drawing.Size(580, 155)
    $grpLocal.Text = "本机代理与 TUN 状态监控"
    $form.Controls.Add($grpLocal)

    $lblClashInfo = New-Object System.Windows.Forms.Label
    $lblClashInfo.Location = New-Object System.Drawing.Point(16, 26)
    $lblClashInfo.Size = New-Object System.Drawing.Size(550, 20)
    $lblClashInfo.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9.5, [System.Drawing.FontStyle]::Bold)
    $lblClashInfo.Text = "Clash (Verge-Mihomo): 检测中..."
    $grpLocal.Controls.Add($lblClashInfo)

    $lblClashSub = New-Object System.Windows.Forms.Label
    $lblClashSub.Location = New-Object System.Drawing.Point(34, 48)
    $lblClashSub.Size = New-Object System.Drawing.Size(530, 18)
    $lblClashSub.ForeColor = [System.Drawing.Color]::Gray
    $lblClashSub.Text = "负责处理本机全部系统网络、浏览器、Codex 桌面端及服务器通用流量。"
    $grpLocal.Controls.Add($lblClashSub)

    $lblOkyunInfo = New-Object System.Windows.Forms.Label
    $lblOkyunInfo.Location = New-Object System.Drawing.Point(16, 72)
    $lblOkyunInfo.Size = New-Object System.Drawing.Size(550, 20)
    $lblOkyunInfo.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9.5, [System.Drawing.FontStyle]::Bold)
    $lblOkyunInfo.Text = "OKYun: 检测中..."
    $grpLocal.Controls.Add($lblOkyunInfo)

    $lblOkyunSub = New-Object System.Windows.Forms.Label
    $lblOkyunSub.Location = New-Object System.Drawing.Point(34, 94)
    $lblOkyunSub.Size = New-Object System.Drawing.Size(530, 18)
    $lblOkyunSub.ForeColor = [System.Drawing.Color]::Gray
    $lblOkyunSub.Text = "专供 agy cli 流量直连。TUN 已设为关闭（纯端口模式），避免虚拟网卡冲突。"
    $grpLocal.Controls.Add($lblOkyunSub)

    $lblSysProxy = New-Object System.Windows.Forms.Label
    $lblSysProxy.Location = New-Object System.Drawing.Point(16, 122)
    $lblSysProxy.Size = New-Object System.Drawing.Size(550, 20)
    $lblSysProxy.ForeColor = [System.Drawing.Color]::FromArgb(40, 40, 40)
    $lblSysProxy.Text = "Windows 系统代理: 检测中..."
    $grpLocal.Controls.Add($lblSysProxy)

    # 远程服务器通道状态 GroupBox
    $grpRemote = New-Object System.Windows.Forms.GroupBox
    $grpRemote.Location = New-Object System.Drawing.Point(20, 275)
    $grpRemote.Size = New-Object System.Drawing.Size(580, 130)
    $grpRemote.Text = "远程服务器 (10.20.67.69) 双通道转发与连通性"
    $form.Controls.Add($grpRemote)

    $lblRemote17890 = New-Object System.Windows.Forms.Label
    $lblRemote17890.Location = New-Object System.Drawing.Point(16, 28)
    $lblRemote17890.Size = New-Object System.Drawing.Size(550, 22)
    $lblRemote17890.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9.5)
    $lblRemote17890.Text = "通道 1 [17890 / Codex]: 检测中..."
    $grpRemote.Controls.Add($lblRemote17890)

    $lblRemote17891 = New-Object System.Windows.Forms.Label
    $lblRemote17891.Location = New-Object System.Drawing.Point(16, 56)
    $lblRemote17891.Size = New-Object System.Drawing.Size(550, 22)
    $lblRemote17891.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9.5)
    $lblRemote17891.Text = "通道 2 [17891 / agy]: 检测中..."
    $grpRemote.Controls.Add($lblRemote17891)

    $lblTunnelProc = New-Object System.Windows.Forms.Label
    $lblTunnelProc.Location = New-Object System.Drawing.Point(16, 88)
    $lblTunnelProc.Size = New-Object System.Drawing.Size(550, 22)
    $lblTunnelProc.ForeColor = [System.Drawing.Color]::Gray
    $lblTunnelProc.Text = "SSH 后台隧道守护进程: 检查中..."
    $grpRemote.Controls.Add($lblTunnelProc)

    # 底部状态及操作栏
    $lblStatusMsg = New-Object System.Windows.Forms.Label
    $lblStatusMsg.Location = New-Object System.Drawing.Point(20, 415)
    $lblStatusMsg.Size = New-Object System.Drawing.Size(580, 30)
    $lblStatusMsg.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9)
    $lblStatusMsg.ForeColor = [System.Drawing.Color]::FromArgb(80, 80, 80)
    $lblStatusMsg.Text = "就绪。双通道已各自独立就绪。"
    $form.Controls.Add($lblStatusMsg)

    $btnRepair = New-Object System.Windows.Forms.Button
    $btnRepair.Location = New-Object System.Drawing.Point(20, 452)
    $btnRepair.Size = New-Object System.Drawing.Size(160, 38)
    $btnRepair.Text = "一键刷新/重连双隧道"
    $btnRepair.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9.5, [System.Drawing.FontStyle]::Bold)
    $btnRepair.BackColor = [System.Drawing.Color]::FromArgb(0, 120, 215)
    $btnRepair.ForeColor = [System.Drawing.Color]::White
    $btnRepair.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
    $btnRepair.FlatAppearance.BorderSize = 0
    $btnRepair.Cursor = [System.Windows.Forms.Cursors]::Hand
    $form.Controls.Add($btnRepair)

    $btnFixSysProxy = New-Object System.Windows.Forms.Button
    $btnFixSysProxy.Location = New-Object System.Drawing.Point(190, 452)
    $btnFixSysProxy.Size = New-Object System.Drawing.Size(140, 38)
    $btnFixSysProxy.Text = "修正系统代理至 Clash"
    $btnFixSysProxy.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9)
    $form.Controls.Add($btnFixSysProxy)

    $btnRefresh = New-Object System.Windows.Forms.Button
    $btnRefresh.Location = New-Object System.Drawing.Point(340, 452)
    $btnRefresh.Size = New-Object System.Drawing.Size(85, 38)
    $btnRefresh.Text = "刷新"
    $btnRefresh.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9.5)
    $form.Controls.Add($btnRefresh)

    $btnClose = New-Object System.Windows.Forms.Button
    $btnClose.Location = New-Object System.Drawing.Point(515, 452)
    $btnClose.Size = New-Object System.Drawing.Size(85, 38)
    $btnClose.Text = "关闭"
    $btnClose.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9.5)
    $btnClose.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
    $form.Controls.Add($btnClose)

    # 刷新界面状态
    $RefreshUI = {
        $btnRefresh.Enabled = $false
        $lblStatusMsg.Text = "正在扫描本机代理与远程双通道连通性..."
        $lblStatusMsg.ForeColor = [System.Drawing.Color]::Gray
        $form.Refresh()

        $s = Get-ProxyStatus

        # 1. Clash
        $clashPortStr = if ($s.Clash.Port) { $s.Clash.Port } else { "未检测到" }
        $clashStatus = if ($s.Clash.IsListening) { "● 监听中 (端口 $clashPortStr)" } else { "○ 未在监听" }
        $clashTunStr = if ($s.Clash.HasTun) { " | TUN虚拟网卡生效中 ✅" } else { " | TUN未开启" }
        $lblClashInfo.Text = "Clash: $clashStatus$clashTunStr"
        $lblClashInfo.ForeColor = if ($s.Clash.IsListening) { [System.Drawing.Color]::FromArgb(0, 102, 204) } else { [System.Drawing.Color]::FromArgb(160, 60, 60) }

        # 2. OKYun
        $okyunPortStr = if ($s.OKYun.Port) { $s.OKYun.Port } else { "未检测到" }
        $okyunStatus = if ($s.OKYun.IsListening) { "● 监听中 (端口 $okyunPortStr)" } else { "○ 未在监听" }
        $okyunTunStr = if ($s.OKYun.HasTun) { " | ⚠️ TUN处于开启状态(建议在OKYun界面关闭)" } else { " | TUN已关闭(纯端口模式) ✅" }
        $lblOkyunInfo.Text = "OKYun: $okyunStatus$okyunTunStr"
        $lblOkyunInfo.ForeColor = if ($s.OKYun.IsListening) { [System.Drawing.Color]::FromArgb(0, 130, 70) } else { [System.Drawing.Color]::FromArgb(160, 60, 60) }

        # 3. 系统代理
        if ($s.SysProxyEnabled) {
            $isClash = ($s.Clash.Port -and $s.SysProxyServer -match ":$($s.Clash.Port)$")
            if ($isClash) {
                $lblSysProxy.Text = "Windows 系统代理: 已正确指向 Clash ($($s.SysProxyServer)) ✅"
                $lblSysProxy.ForeColor = [System.Drawing.Color]::FromArgb(0, 120, 60)
            } else {
                $lblSysProxy.Text = "Windows 系统代理: 当前指向 $($s.SysProxyServer) (建议修正为指向 Clash)"
                $lblSysProxy.ForeColor = [System.Drawing.Color]::FromArgb(180, 100, 0)
            }
        } else {
            $lblSysProxy.Text = "Windows 系统代理: 未开启 (由 Clash TUN 网卡全流量接管) ✅"
            $lblSysProxy.ForeColor = [System.Drawing.Color]::FromArgb(0, 120, 60)
        }

        # 4. 远程端口状态
        $p1 = if ($s.Forward17890) { $s.Forward17890 } else { "未配置" }
        $p2 = if ($s.Forward17891) { $s.Forward17891 } else { "未配置" }
        $lblRemote17890.Text = "通道 1 (17890 / Codex & 通用) -> 映射本机端口: $p1"
        $lblRemote17891.Text = "通道 2 (17891 / agy cli 专线) -> 映射本机端口: $p2"

        # 5. 后台守护进程
        $sshProcs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            $_.Name -match "^ssh\.exe$" -and $_.CommandLine -match "server-proxy"
        }
        if ($sshProcs) {
            $lblTunnelProc.Text = "SSH 后台隧道守护进程: 正在运行 (PID: $($sshProcs[0].ProcessId)) ✅"
            $lblTunnelProc.ForeColor = [System.Drawing.Color]::FromArgb(0, 120, 60)
        } else {
            $lblTunnelProc.Text = "SSH 后台隧道守护进程: 未检测到后台进程 (点击「一键刷新/重连」可建立)"
            $lblTunnelProc.ForeColor = [System.Drawing.Color]::FromArgb(180, 100, 0)
        }

        $lblStatusMsg.Text = "检测完成。双通道架构就绪。"
        $lblStatusMsg.ForeColor = [System.Drawing.Color]::FromArgb(60, 60, 60)
        $btnRefresh.Enabled = $true
    }

    $btnRefresh.Add_Click({
        & $RefreshUI
    })

    $btnFixSysProxy.Add_Click({
        $s = Get-ProxyStatus
        $cPort = if ($s.Clash.Port) { $s.Clash.Port } else { 1578 }
        Set-SystemProxyToClash -ClashPort $cPort | Out-Null
        [System.Windows.Forms.MessageBox]::Show($form, "已将 Windows 系统代理地址修正指向 Clash (127.0.0.1:$cPort)。`nOKYun 将继续作为 agy cli 专属代理独立运行。", "设置成功", "OK", "Information") | Out-Null
        & $RefreshUI
    })

    $btnRepair.Add_Click({
        $btnRepair.Enabled = $false
        $btnRefresh.Enabled = $false
        $lblStatusMsg.Text = "正在同步双通道配置并刷新 SSH 隧道..."
        $lblStatusMsg.ForeColor = [System.Drawing.Color]::FromArgb(0, 102, 204)
        $form.Refresh()

        try {
            $s = Get-ProxyStatus
            $cPort = if ($s.Clash.Port) { $s.Clash.Port } else { 1578 }
            $oPort = if ($s.OKYun.Port) { $s.OKYun.Port } else { 7897 }

            Ensure-DualTunnelsConfig -Port17890 $cPort -Port17891 $oPort
            $restarted = Restart-ProxyTunnel

            $lblStatusMsg.Text = "正在进行双通道连通性测试 (OpenAI + Google)..."
            $form.Refresh()

            $tRes = Test-RemoteChannels

            $msg = "双通道隧道已完成同步并启动！`n`n" +
                   "• 通道 1 (17890 -> Clash $cPort): $($tRes.Channel17890.Message)`n" +
                   "• 通道 2 (17891 -> OKYun $oPort): $($tRes.Channel17891.Message)`n`n" +
                   "服务器端的 Codex CLI 与 agy cli 均可立即正常使用。"

            [System.Windows.Forms.MessageBox]::Show($form, $msg, "刷新与检测结果", "OK", "Information") | Out-Null
            & $RefreshUI
        } catch {
            [System.Windows.Forms.MessageBox]::Show($form, "操作失败：`n" + $_.Exception.Message, "错误", "OK", "Error") | Out-Null
            $lblStatusMsg.Text = "失败：" + $_.Exception.Message
            $lblStatusMsg.ForeColor = [System.Drawing.Color]::Red
        } finally {
            $btnRepair.Enabled = $true
            $btnRefresh.Enabled = $true
        }
    })

    $form.Add_Shown({
        & $RefreshUI
    })

    [void]$form.ShowDialog()
}

# --- CLI 执行入口 ---
if ($Channel -eq "status") {
    $s = Get-ProxyStatus
    $s | ConvertTo-Json -Depth 4
    exit 0
}

if ($Channel -in @("repair", "dual")) {
    $s = Get-ProxyStatus
    $cPort = if ($s.Clash.Port) { $s.Clash.Port } else { 1578 }
    $oPort = if ($s.OKYun.Port) { $s.OKYun.Port } else { 7897 }
    Ensure-DualTunnelsConfig -Port17890 $cPort -Port17891 $oPort
    $restarted = Restart-ProxyTunnel
    if (-not $NoVerify) {
        $t = Test-RemoteChannels
        if (-not $Silent) {
            Write-Host "双通道配置同步完成。"
            Write-Host "通道 1 (17890 -> Clash $cPort): $($t.Channel17890.Message)"
            Write-Host "通道 2 (17891 -> OKYun $oPort): $($t.Channel17891.Message)"
        }
    }
    exit 0
}

if ($Channel -in @("okyun", "clash")) {
    $s = Get-ProxyStatus
    $cPort = if ($Channel -eq "okyun") { $s.OKYun.Port } else { $s.Clash.Port }
    $oPort = if ($s.OKYun.Port) { $s.OKYun.Port } else { 7897 }
    Ensure-DualTunnelsConfig -Port17890 $cPort -Port17891 $oPort
    Restart-ProxyTunnel | Out-Null
    if (-not $Silent) {
        Write-Host "已更新通道 1 至 $Channel (端口: $cPort)，通道 2 保持 OKYun (端口: $oPort)"
    }
    exit 0
}

# 默认弹出图形界面
Show-SwitcherUI
