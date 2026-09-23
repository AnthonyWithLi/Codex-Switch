# -*- coding: utf-8 -*-
<#
.SYNOPSIS
    服务器代理渠道动态切换工具 (Server Proxy Channel Switcher)
.DESCRIPTION
    实时识别本机 OKYun 与 Clash 的具体代理端口（无需硬编码），
    并一键切换远程服务器 (server-proxy / server-codex) 所走的代理通道。
    更新 ~/.ssh/config 中的 RemoteForward 端口配置并自动刷新隧道。
#>

[CmdletBinding()]
param(
    [ValidateSet("okyun", "clash", "status")]
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
    if ($clash.Port) {
        $clash.IsListening = Test-PortListening -Port $clash.Port
    }
    $clashAdapter = Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { $_.InterfaceDescription -match "Meta Tunnel|Clash" -and $_.Name -ne "OKYun" -and $_.Status -eq "Up" }
    if ($clashAdapter) {
        $clash.HasTun = $true
    }

    # --- 3. 读取当前 ~/.ssh/config 的转发目标端口 ---
    $currentPort = $null
    if (Test-Path $SshConfigFile) {
        $lines = Get-Content $SshConfigFile -ErrorAction SilentlyContinue
        $inProxyBlock = $false
        foreach ($l in $lines) {
            if ($l -match "^\s*Host\s+server-proxy\s*$") {
                $inProxyBlock = $true
                continue
            }
            if ($inProxyBlock -and $l -match "^\s*Host\s+") {
                $inProxyBlock = $false
            }
            if ($inProxyBlock -and $l -match "RemoteForward\s+\S+:17890\s+\S+:(\d+)") {
                $currentPort = [int]$Matches[1]
                break
            }
        }
    }

    # 识别当前渠道
    $currentChannel = "未知"
    if ($currentPort) {
        if ($okyun.Port -and $currentPort -eq $okyun.Port) {
            $currentChannel = "OKYun"
        } elseif ($clash.Port -and $currentPort -eq $clash.Port) {
            $currentChannel = "Clash"
        } else {
            $currentChannel = "自定义端口 ($currentPort)"
        }
    }

    return @{
        OKYun = $okyun
        Clash = $clash
        CurrentPort = $currentPort
        CurrentChannel = $currentChannel
    }
}

function Switch-ServerProxy {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("OKYun", "Clash")]
        [string]$Target,
        [switch]$SkipVerify
    )

    $status = Get-ProxyStatus
    $targetObj = if ($Target -eq "OKYun") { $status.OKYun } else { $status.Clash }
    $targetPort = $targetObj.Port

    if (-not $targetPort) {
        throw "无法识别到 $($Target) 的代理端口！请先启动 $($Target) 或检查其配置文件。"
    }

    if (-not $targetObj.IsListening) {
        # 端口未监听，但配置有端口，记录提示
        Write-Warning "$Target 代理目前未处于监听状态（端口 $targetPort），流量可能无法立即打通。"
    }

    if (-not (Test-Path $SshConfigFile)) {
        throw "找不到 SSH 配置文件: $SshConfigFile"
    }

    # 1. 更新 ~/.ssh/config 中的 RemoteForward 端口
    $content = Get-Content -Raw -Encoding UTF8 $SshConfigFile
    $newContent = [regex]::Replace(
        $content,
        "(?ms)(Host\s+server-proxy\s*`r?`n(?:(?!Host\s).)*?RemoteForward\s+\S+:17890\s+\S+:)\d+",
        "`${1}$targetPort"
    )
    if ($content -eq $newContent) {
        # 如果没有匹配上具体的 block，做全局替换作为保底
        $newContent = [regex]::Replace(
            $content,
            "(RemoteForward\s+\S+:17890\s+\S+:)\d+",
            "`${1}$targetPort"
        )
    }

    [System.IO.File]::WriteAllText($SshConfigFile, $newContent.TrimEnd() + "`r`n", (New-Object System.Text.UTF8Encoding($false)))

    # 2. 刷新现有的 SSH 隧道（如果有正在运行的 server-proxy 连接）
    $restartedTunnel = $false
    try {
        $sshProcs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            $_.Name -match "^ssh\.exe$" -and $_.CommandLine -match "server-proxy"
        }
        if ($sshProcs) {
            foreach ($p in $sshProcs) {
                Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
            }
            $restartedTunnel = $true
            # 稍等 1.5 秒让 IDE/后台隧道完成重连
            Start-Sleep -Milliseconds 1500
        }
    } catch {}

    # 3. 验证服务器连通性
    $verifyResult = $null
    if (-not $SkipVerify) {
        try {
            $out = & ssh -o ConnectTimeout=4 -o BatchMode=yes server-codex "curl -sI --connect-timeout 4 -x http://127.0.0.1:17890 https://api.openai.com | head -n 1" 2>&1
            $verifyText = ($out -join "`n").Trim()
            if ($verifyText -match "HTTP/\S+\s+(\d+)") {
                $code = $Matches[1]
                if ($code -in @("200", "421", "400", "403")) {
                    $verifyResult = @{ Success = $true; Message = "连通正常 (HTTP $code)" }
                } else {
                    $verifyResult = @{ Success = $false; Message = "返回状态码: $code" }
                }
            } else {
                $verifyResult = @{ Success = $false; Message = if ($verifyText) { $verifyText } else { "连接超时或无响应" } }
            }
        } catch {
            $verifyResult = @{ Success = $false; Message = $_.Exception.Message }
        }
    }

    return @{
        Target = $Target
        Port = $targetPort
        RestartedTunnel = $restartedTunnel
        Verify = $verifyResult
    }
}

function Show-SwitcherUI {
    Add-Type -AssemblyName System.Windows.Forms | Out-Null
    Add-Type -AssemblyName System.Drawing | Out-Null
    [System.Windows.Forms.Application]::EnableVisualStyles()

    $form = New-Object System.Windows.Forms.Form
    $form.Text = "服务器代理渠道切换"
    $form.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 10)
    $form.StartPosition = "CenterScreen"
    $form.FormBorderStyle = "FixedDialog"
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $form.TopMost = $true
    $form.ShowInTaskbar = $true
    $form.ClientSize = New-Object System.Drawing.Size(560, 390)

    # 提取图标
    $launcherExe = Join-Path $env:LOCALAPPDATA "CodexRepair\CodexRepair.exe"
    if (Test-Path $launcherExe) {
        try { $form.Icon = [System.Drawing.Icon]::ExtractAssociatedIcon($launcherExe) } catch {}
    }

    # 当前状态面板
    $panelCurrent = New-Object System.Windows.Forms.Panel
    $panelCurrent.Location = New-Object System.Drawing.Point(20, 16)
    $panelCurrent.Size = New-Object System.Drawing.Size(520, 68)
    $panelCurrent.BackColor = [System.Drawing.Color]::FromArgb(242, 245, 250)
    $panelCurrent.BorderStyle = [System.Windows.Forms.BorderStyle]::FixedSingle
    $form.Controls.Add($panelCurrent)

    $lblCurrentTitle = New-Object System.Windows.Forms.Label
    $lblCurrentTitle.Location = New-Object System.Drawing.Point(12, 10)
    $lblCurrentTitle.Size = New-Object System.Drawing.Size(140, 24)
    $lblCurrentTitle.Text = "当前服务器生效渠道："
    $lblCurrentTitle.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9.5, [System.Drawing.FontStyle]::Bold)
    $panelCurrent.Controls.Add($lblCurrentTitle)

    $lblCurrentVal = New-Object System.Windows.Forms.Label
    $lblCurrentVal.Location = New-Object System.Drawing.Point(155, 9)
    $lblCurrentVal.Size = New-Object System.Drawing.Size(350, 24)
    $lblCurrentVal.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 10.5, [System.Drawing.FontStyle]::Bold)
    $lblCurrentVal.ForeColor = [System.Drawing.Color]::FromArgb(0, 102, 204)
    $lblCurrentVal.Text = "正在扫描检测..."
    $panelCurrent.Controls.Add($lblCurrentVal)

    $lblCurrentSub = New-Object System.Windows.Forms.Label
    $lblCurrentSub.Location = New-Object System.Drawing.Point(12, 38)
    $lblCurrentSub.Size = New-Object System.Drawing.Size(490, 22)
    $lblCurrentSub.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9)
    $lblCurrentSub.ForeColor = [System.Drawing.Color]::Gray
    $lblCurrentSub.Text = "服务器端统一走 127.0.0.1:17890，本机实时桥接到对应代理端口。"
    $panelCurrent.Controls.Add($lblCurrentSub)

    # 代理选择 GroupBox
    $grp = New-Object System.Windows.Forms.GroupBox
    $grp.Location = New-Object System.Drawing.Point(20, 96)
    $grp.Size = New-Object System.Drawing.Size(520, 180)
    $grp.Text = "实时识别到的代理渠道 (自动检测端口，无需写死)"
    $form.Controls.Add($grp)

    # Radio OKYun
    $rbOkyun = New-Object System.Windows.Forms.RadioButton
    $rbOkyun.Location = New-Object System.Drawing.Point(20, 32)
    $rbOkyun.Size = New-Object System.Drawing.Size(480, 28)
    $rbOkyun.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 10.5, [System.Drawing.FontStyle]::Bold)
    $rbOkyun.Text = "OKYun 代理"
    $grp.Controls.Add($rbOkyun)

    $lblOkyunDetail = New-Object System.Windows.Forms.Label
    $lblOkyunDetail.Location = New-Object System.Drawing.Point(42, 60)
    $lblOkyunDetail.Size = New-Object System.Drawing.Size(460, 20)
    $lblOkyunDetail.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9)
    $lblOkyunDetail.ForeColor = [System.Drawing.Color]::FromArgb(60, 60, 60)
    $lblOkyunDetail.Text = "检测中..."
    $grp.Controls.Add($lblOkyunDetail)

    # Radio Clash
    $rbClash = New-Object System.Windows.Forms.RadioButton
    $rbClash.Location = New-Object System.Drawing.Point(20, 96)
    $rbClash.Size = New-Object System.Drawing.Size(480, 28)
    $rbClash.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 10.5, [System.Drawing.FontStyle]::Bold)
    $rbClash.Text = "Clash (Clash Verge) 代理"
    $grp.Controls.Add($rbClash)

    $lblClashDetail = New-Object System.Windows.Forms.Label
    $lblClashDetail.Location = New-Object System.Drawing.Point(42, 124)
    $lblClashDetail.Size = New-Object System.Drawing.Size(460, 20)
    $lblClashDetail.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9)
    $lblClashDetail.ForeColor = [System.Drawing.Color]::FromArgb(60, 60, 60)
    $lblClashDetail.Text = "检测中..."
    $grp.Controls.Add($lblClashDetail)

    # 底部提示与操作按钮
    $lblStatusMsg = New-Object System.Windows.Forms.Label
    $lblStatusMsg.Location = New-Object System.Drawing.Point(20, 288)
    $lblStatusMsg.Size = New-Object System.Drawing.Size(520, 36)
    $lblStatusMsg.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9)
    $lblStatusMsg.ForeColor = [System.Drawing.Color]::FromArgb(80, 80, 80)
    $lblStatusMsg.Text = "请选择目标渠道后点击「立即切换」。切换将自动更新隧道配置。"
    $form.Controls.Add($lblStatusMsg)

    $btnSwitch = New-Object System.Windows.Forms.Button
    $btnSwitch.Location = New-Object System.Drawing.Point(230, 334)
    $btnSwitch.Size = New-Object System.Drawing.Size(120, 38)
    $btnSwitch.Text = "立即切换"
    $btnSwitch.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 10, [System.Drawing.FontStyle]::Bold)
    $btnSwitch.BackColor = [System.Drawing.Color]::FromArgb(0, 120, 215)
    $btnSwitch.ForeColor = [System.Drawing.Color]::White
    $btnSwitch.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
    $btnSwitch.FlatAppearance.BorderSize = 0
    $btnSwitch.Cursor = [System.Windows.Forms.Cursors]::Hand
    $form.Controls.Add($btnSwitch)

    $btnRefresh = New-Object System.Windows.Forms.Button
    $btnRefresh.Location = New-Object System.Drawing.Point(360, 334)
    $btnRefresh.Size = New-Object System.Drawing.Size(85, 38)
    $btnRefresh.Text = "刷新"
    $btnRefresh.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9.5)
    $form.Controls.Add($btnRefresh)

    $btnClose = New-Object System.Windows.Forms.Button
    $btnClose.Location = New-Object System.Drawing.Point(455, 334)
    $btnClose.Size = New-Object System.Drawing.Size(85, 38)
    $btnClose.Text = "关闭"
    $btnClose.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9.5)
    $btnClose.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
    $form.Controls.Add($btnClose)

    # 刷新界面状态函数
    $RefreshUI = {
        $btnRefresh.Enabled = $false
        $lblStatusMsg.Text = "正在实时扫描代理状态与端口..."
        $lblStatusMsg.ForeColor = [System.Drawing.Color]::Gray
        $form.Refresh()

        $s = Get-ProxyStatus

        # 当前生效展示
        $curPortStr = if ($s.CurrentPort) { "端口: $($s.CurrentPort)" } else { "未配置" }
        $lblCurrentVal.Text = "$($s.CurrentChannel)  ($curPortStr)"
        if ($s.CurrentChannel -eq "OKYun") {
            $lblCurrentVal.ForeColor = [System.Drawing.Color]::FromArgb(0, 130, 70)
            $rbOkyun.Checked = $true
        } elseif ($s.CurrentChannel -eq "Clash") {
            $lblCurrentVal.ForeColor = [System.Drawing.Color]::FromArgb(0, 102, 204)
            $rbClash.Checked = $true
        } else {
            $lblCurrentVal.ForeColor = [System.Drawing.Color]::FromArgb(180, 90, 0)
        }

        # OKYun 详情
        $okyunPort = if ($s.OKYun.Port) { $s.OKYun.Port } else { "未检测到" }
        $okyunStatus = if ($s.OKYun.IsListening) {
            "● 正在监听 (就绪)"
        } elseif ($s.OKYun.IsRunning) {
            "▲ 进程运行中但端口未响应"
        } else {
            "○ 未运行"
        }
        $okyunTun = if ($s.OKYun.HasTun) { " | TUN虚拟网卡生效中" } else { "" }
        $lblOkyunDetail.Text = "实时端口: $okyunPort | 状态: $okyunStatus$okyunTun"
        if ($s.OKYun.IsListening) {
            $lblOkyunDetail.ForeColor = [System.Drawing.Color]::FromArgb(0, 120, 60)
        } else {
            $lblOkyunDetail.ForeColor = [System.Drawing.Color]::FromArgb(140, 60, 60)
        }

        # Clash 详情
        $clashPort = if ($s.Clash.Port) { $s.Clash.Port } else { "未检测到" }
        $clashStatus = if ($s.Clash.IsListening) {
            "● 正在监听 (就绪)"
        } elseif ($s.Clash.IsRunning) {
            "▲ 进程运行中但端口未响应"
        } else {
            "○ 未运行"
        }
        $clashTun = if ($s.Clash.HasTun) { " | TUN虚拟网卡生效中" } else { "" }
        $lblClashDetail.Text = "实时端口: $clashPort | 状态: $clashStatus$clashTun"
        if ($s.Clash.IsListening) {
            $lblClashDetail.ForeColor = [System.Drawing.Color]::FromArgb(0, 100, 180)
        } else {
            $lblClashDetail.ForeColor = [System.Drawing.Color]::FromArgb(140, 60, 60)
        }

        $lblStatusMsg.Text = "检测完成。点击「立即切换」应用选中渠道。"
        $lblStatusMsg.ForeColor = [System.Drawing.Color]::FromArgb(80, 80, 80)
        $btnRefresh.Enabled = $true
    }

    # 绑定事件
    $btnRefresh.Add_Click({
        & $RefreshUI
    })

    $btnSwitch.Add_Click({
        $target = if ($rbOkyun.Checked) { "OKYun" } elseif ($rbClash.Checked) { "Clash" } else { $null }
        if (-not $target) {
            [System.Windows.Forms.MessageBox]::Show($form, "请先选择要切换的目标代理渠道。", "提示", "OK", "Warning") | Out-Null
            return
        }

        $btnSwitch.Enabled = $false
        $btnRefresh.Enabled = $false
        $lblStatusMsg.Text = "正在切换配置并重启 SSH 代理转发隧道，请稍候..."
        $lblStatusMsg.ForeColor = [System.Drawing.Color]::FromArgb(0, 102, 204)
        $form.Refresh()

        try {
            $res = Switch-ServerProxy -Target $target
            $targetPort = $res.Port
            $verifyInfo = ""
            if ($res.Verify) {
                if ($res.Verify.Success) {
                    $verifyInfo = "`n`n服务器连通性测试：`n✅ $($res.Verify.Message)"
                } else {
                    $verifyInfo = "`n`n服务器连通性提示：`n⚠️ $($res.Verify.Message) (可能需要稍等2秒隧道建立)"
                }
            }
            $tunnelInfo = if ($res.RestartedTunnel) { "（已自动刷新活动的 SSH 隧道）" } else { "（未检测到活动隧道，新连接将自动生效）" }

            [System.Windows.Forms.MessageBox]::Show(
                $form,
                "已成功将服务器代理渠道切换至：$target`n`n实时绑定端口：$targetPort $tunnelInfo$verifyInfo",
                "切换完成",
                "OK",
                "Information"
            ) | Out-Null

            & $RefreshUI
        } catch {
            [System.Windows.Forms.MessageBox]::Show($form, "切换失败：`n" + $_.Exception.Message, "错误", "OK", "Error") | Out-Null
            $lblStatusMsg.Text = "切换失败：" + $_.Exception.Message
            $lblStatusMsg.ForeColor = [System.Drawing.Color]::Red
        } finally {
            $btnSwitch.Enabled = $true
            $btnRefresh.Enabled = $true
        }
    })

    # 初次加载
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

if ($Channel -in @("okyun", "clash")) {
    $target = if ($Channel -eq "okyun") { "OKYun" } else { "Clash" }
    $res = Switch-ServerProxy -Target $target -SkipVerify:$NoVerify
    if (-not $Silent) {
        Write-Host "已切换服务器代理至 $target (端口: $($res.Port))"
        if ($res.Verify) {
            Write-Host "连通性测试: $($res.Verify.Message)"
        }
    }
    exit 0
}

# 默认弹出图形界面
Show-SwitcherUI
