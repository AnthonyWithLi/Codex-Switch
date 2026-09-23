# Click the desktop shortcut after quitting Codex Desktop.
#
# Shows a single-select account picker, optionally switches the same named
# account on this PC and server-codex (each machine's own auth.json snapshot;
# tokens are never copied over SSH). On each run it also refreshes the
# current account snapshot from that machine's live auth.json when the
# refresh token changed. Then runs
# handoff/codex_unwedge_projection.py on this PC and over SSH. New freeze
# shapes belong in the Python script, not here.
#
# codex_local_csw.py probes each account's access token before switching and
# while refreshing snapshots. A 401 means the ChatGPT session was probably
# ended server-side, which makes Desktop open the ChatGPT Work page with
# "You don't have access to Work yet" and look like a browser Work/Personal
# bug even though auth.json is fine.
#
# The picker shows each account's tier, workspace and session state inline, and
# a 401 is reported there ("会话探针 401，可试强制切换") instead of in a second
# dialog: a single 401 can be wrong (wrong chatgpt-account-id header, flaky
# proxy, transient server reply) and Codex may still mint a fresh access token
# from the refresh token. Picking an account always switches it. The result
# popup tells you when a switch went ahead despite the warning.
# When a sibling snapshot shares the same ChatGPT workspace and is still alive,
# the warning also names it (e.g. pick team-account instead of personal-account).
#
# ENCODING, do not break this:
#   * This file must keep a UTF-8 BOM. The launcher runs
#     powershell.exe -File, and PS 5.1 decodes a BOM-less script with the ANSI
#     code page (936 here) -> every Chinese string turns to mojibake and the
#     script fails to parse at all. An editor that strips the BOM breaks the
#     shortcut silently.
#   * Child process output is pinned to UTF-8 near the top of the script
#     ([Console]::OutputEncoding). PS 5.1 would otherwise decode python's
#     UTF-8 stdout as GBK and every Chinese popup line becomes garbage.
#
#   powershell -NoProfile -STA -ExecutionPolicy Bypass -File handoff\start_codex_desktop.ps1 -InstallShortcut -Silent
#
# Switch straight to one account, no picker (useful for a one-click shortcut):
#   ... -File handoff\start_codex_desktop.ps1 -Account <account-name>
#
# Keep snapshots fresh in the background (see -SyncOnly):
#   powershell -NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File handoff\start_codex_desktop.ps1 -SyncOnly -Silent
#   Codex rotates the refresh token on every refresh, so a snapshot that is
#   never re-synced against live auth.json eventually holds a dead token. That
#   is the one failure mode this tool can prevent, and -SyncOnly is how a
#   scheduled task prevents it. It pops no UI, switches nothing, repairs
#   nothing, always exits 0 and logs to %LOCALAPPDATA%\CodexRepair\sync.log.
#   The log line ends with dead=<account>=REVOKED for any snapshot whose
#   ChatGPT session the server already ended (or dead=none), which is the only
#   thing you need to look at.
#
# SERVER PROXY, do not remove:
#   The server reaches chatgpt.com only through the SSH RemoteForward declared
#   for server-proxy in ~/.ssh/config (server 127.0.0.1:17890 -> this PC's
#   Clash). http_proxy/https_proxy there are exported by ~/.bashrc only, and a
#   non-interactive `ssh host <cmd>` never sources it, so Invoke-Remote states
#   the proxy on the command line (VAR=val prefix). Without it the server-side
#   token probe cannot connect. If the forward is down the connection is
#   refused immediately and the probe reports "unknown" -- never a long stall.
#   Override the port with -RemoteProxy. The account switch itself needs no
#   network on either side; only the probe does.

[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$StartDesktop,
    [switch]$InstallShortcut,
    [switch]$Silent,
    [switch]$LocalOnly,
    [switch]$SyncOnly,
    [string]$RemoteHost,
    [string]$RemoteProxy,
    [string]$Account
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = $ScriptDir
$Unwedge = Join-Path $ScriptDir "codex_unwedge_projection.py"
$LocalCsw = Join-Path $ScriptDir "codex_local_csw.py"

# --- 读取本地配置（config.json，如存在） ---
$ConfigFile = Join-Path $ScriptDir "config.json"
$Config = $null
if (Test-Path -LiteralPath $ConfigFile) {
    try {
        $Config = Get-Content -LiteralPath $ConfigFile -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        Write-Warning "读取配置文件 config.json 失败: $_"
    }
}

function Find-Python {
    if ($env:CODEX_PYTHON -and (Test-Path -LiteralPath $env:CODEX_PYTHON)) {
        return $env:CODEX_PYTHON
    }
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) { return $cmd.Source }
    $pyCmd = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pyCmd -and $pyCmd.Source) { return $pyCmd.Source }
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe",
        "C:\Python312\python.exe",
        "C:\Python311\python.exe",
        "C:\Python310\python.exe"
    )
    foreach ($p in $candidates) {
        if (Test-Path -LiteralPath $p) { return $p }
    }
    return "python.exe"
}

$Python = if ($Config -and $Config.python) { $Config.python } else { Find-Python }
$CodexDesktopAumid = "OpenAI.Codex_2p2nqsd0c76g0!App"
$RepairAppId = if ($Config -and $Config.app_user_model_id) { $Config.app_user_model_id } else { "OpenAI.CodexRepair" }
$RepairLauncherDir = Join-Path $env:LOCALAPPDATA "CodexRepair"
$RepairLauncherExe = Join-Path $RepairLauncherDir "CodexRepair.exe"
$RepairLauncherCs = Join-Path $ScriptDir "CodexRepairLauncher.cs"
$Vbs = Join-Path $ScriptDir "codex_unwedge_popup.vbs"
$ShortcutName = "修复 Codex 对话投影.lnk"
$Title = "Codex 账号与对话投影"

if (-not $PSBoundParameters.ContainsKey("RemoteHost")) {
    if ($Config -and $Config.remote_host) {
        $RemoteHost = $Config.remote_host
    }
}
if (-not $PSBoundParameters.ContainsKey("RemoteProxy")) {
    if ($Config -and $Config.remote_proxy) {
        $RemoteProxy = $Config.remote_proxy
    }
}
$RemoteCsw = if ($Config -and $Config.remote_csw) { $Config.remote_csw } else { "~/.local/bin/csw" }
$RemoteLocalCsw = if ($Config -and $Config.remote_local_csw) { $Config.remote_local_csw } else { "~/.local/bin/codex_local_csw.py" }

if ([string]::IsNullOrWhiteSpace($RemoteHost)) {
    $LocalOnly = $true
}

$KeepAccount = "__keep__"
$exitCode = 0
$popupText = $null
$popupType = 64
$cancelled = $false

# --- UTF-8 边界 -----------------------------------------------------------
# PS 5.1 用 [Console]::OutputEncoding 解码本机/远程子进程的 stdout。隐藏窗口
# 里它是 GBK(936)，而 python 输出 UTF-8（PYTHONIOENCODING/PYTHONUTF8 已是
# utf-8），于是所有中文提示都变成 "璐﹀彿" 这种乱码 —— 弹窗能弹出来，但读不懂。
# 这里把子进程的输出编码和 python 的 stdout 编码都钉死成 UTF-8。
# 注意：必须用不带 BOM 的 UTF8Encoding，否则 PowerShell 自己的输出会多出 BOM。
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
} catch {
}
try {
    $OutputEncoding = New-Object System.Text.UTF8Encoding($false)
} catch {
}

try {
    if (-not ("CodexRepairAppId" -as [type])) {
        Add-Type -TypeDefinition @"
using System.Runtime.InteropServices;
public static class CodexRepairAppId {
  [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
  public static extern int SetCurrentProcessExplicitAppUserModelID(string appId);
}
"@
    }
    [void][CodexRepairAppId]::SetCurrentProcessExplicitAppUserModelID($RepairAppId)
} catch {
}

if (
    -not $Silent -and
    -not $InstallShortcut -and
    [string]::IsNullOrEmpty($Account) -and
    [Threading.Thread]::CurrentThread.GetApartmentState() -ne "STA"
) {
    $pass = @()
    foreach ($key in $PSBoundParameters.Keys) {
        $val = $PSBoundParameters[$key]
        if ($val -is [switch]) {
            if ($val) { $pass += "-$key" }
        } else {
            $pass += "-$key"
            $pass += "$val"
        }
    }
    $proc = Start-Process -FilePath "powershell.exe" -ArgumentList (
        @("-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", $PSCommandPath) + $pass
    ) -Wait -PassThru -WindowStyle Hidden
    exit $proc.ExitCode
}

function Show-Popup {
    param(
        [Parameter(Mandatory = $true)][string]$Text,
        [int]$Type = 64
    )
    if ($Silent) { return }
    $shell = New-Object -ComObject WScript.Shell
    $null = $shell.Popup($Text, 0, $Title, $Type)
}

$script:SshOpts = @(
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=12"
)

# 服务器上只有 ~/.bashrc / ~/.profile 里 export 了 http(s)_proxy，而非交互式
# `ssh host <cmd>` 走的是不读这些文件的 shell，于是服务器侧的 token 探测会
# 直连 chatgpt.com 并卡到超时。这里把代理显式写在命令行上（POSIX 的
# VAR=val cmd 前缀对该命令生效）。代理落地在 ~/.ssh/config 里 server-proxy
# 那条 RemoteForward 上（服务器 127.0.0.1:17890 → 本机 Clash）。
# 若该转发没开，连接被立即拒绝 -> 探测返回 unknown（"无法确认"），
# 而不是每个账号白等 15 秒。--probe-all 只影响提示，不会阻断切换。
$script:RemoteEnvPrefix = @()
if ($RemoteProxy) {
    $script:RemoteEnvPrefix = @("http_proxy=$RemoteProxy", "https_proxy=$RemoteProxy")
}

function Invoke-Remote {
    param([Parameter(Mandatory = $true)][string[]]$ArgumentList)
    $remoteArgs = @($script:RemoteEnvPrefix + $ArgumentList)
    $output = & ssh @script:SshOpts $RemoteHost @remoteArgs 2>&1 | ForEach-Object { "$_" }
    $code = [int]$LASTEXITCODE
    return @{
        Code = $code
        Text = ($output -join "`n")
    }
}

function Get-RemoteAccountStatus {
    $result = Invoke-Remote -ArgumentList @($RemoteCsw)
    $text = [string]$result.Text
    $accounts = New-Object System.Collections.Generic.List[string]
    $current = $null
    $warning = $null
    if ($text -match "Usage:\s+csw\s+<([^>]+)>") {
        foreach ($name in ($Matches[1] -split "\|")) {
            $item = $name.Trim()
            if ($item) { [void]$accounts.Add($item) }
        }
    }
    if ($text -match "Available:\s+(.+)$") {
        foreach ($name in ($Matches[1].Trim() -split "\s+")) {
            if ($name -and -not $accounts.Contains($name)) { [void]$accounts.Add($name) }
        }
    }
    if ($text -match "Current:\s+(\S+)") {
        $raw = $Matches[1]
        if ($raw -notmatch "^unknown") { $current = $raw }
    }
    if ($result.Code -ne 0 -or $accounts.Count -eq 0) {
        $warning = "未能读取服务器 csw 列表，使用已知账号。"
        if ($Config -and $Config.accounts) {
            foreach ($acc in $Config.accounts) {
                if ($acc.name -and -not $accounts.Contains($acc.name)) {
                    [void]$accounts.Add($acc.name)
                }
            }
        }
    }
    return @{
        Current = $current
        Accounts = $accounts.ToArray()
        Warning = $warning
        Raw = $text
    }
}

function Invoke-LocalPython {
    param([Parameter(Mandatory = $true)][string[]]$ArgumentList)
    $output = & $Python @ArgumentList 2>&1 | ForEach-Object { "$_" }
    return @{
        Code = [int]$LASTEXITCODE
        Text = ($output -join "`n")
    }
}

function ConvertFrom-LocalCswJson {
    param([string]$Text)
    $jsonLine = $null
    foreach ($line in ($Text -split "`r?`n")) {
        $trim = $line.Trim()
        if ($trim.StartsWith("{") -and $trim.EndsWith("}")) {
            $jsonLine = $trim
        }
    }
    if (-not $jsonLine) {
        throw "本机 csw 没有返回 JSON：`n$Text"
    }
    return $jsonLine | ConvertFrom-Json
}

function Invoke-LocalCsw {
    param([Parameter(Mandatory = $true)][string[]]$CswArgs)
    if (-not (Test-Path -LiteralPath $LocalCsw)) {
        throw "找不到本机切号脚本: $LocalCsw"
    }
    # Chinese on the python side has to survive into the popup. PS 5.1 decodes a
    # child process's stderr with the console code page (936 here) while python
    # prints UTF-8, which turns every message into mojibake. The script also
    # mirrors its failure message into a UTF-8 file when CSW_MESSAGE_FILE is
    # set, so read that back explicitly and prefer it. The way python is
    # launched is deliberately unchanged, so exit codes and the JSON contract
    # are unaffected.
    $msgFile = Join-Path $env:TEMP ("csw-msg-{0}.txt" -f ([guid]::NewGuid().ToString("N")))
    $prevMsgFile = $env:CSW_MESSAGE_FILE
    $env:CSW_MESSAGE_FILE = $msgFile
    try {
        $result = Invoke-LocalPython -ArgumentList (@($LocalCsw) + $CswArgs)
    } finally {
        if ($null -eq $prevMsgFile) {
            Remove-Item Env:CSW_MESSAGE_FILE -ErrorAction SilentlyContinue
        } else {
            $env:CSW_MESSAGE_FILE = $prevMsgFile
        }
    }
    $cleanMessage = $null
    if (Test-Path -LiteralPath $msgFile) {
        try {
            $cleanMessage = ([System.IO.File]::ReadAllText($msgFile, [System.Text.Encoding]::UTF8)).Trim()
        } catch {
        }
        Remove-Item -LiteralPath $msgFile -Force -ErrorAction SilentlyContinue
    }
    if ($result.Code -ne 0) {
        $detail = if ($cleanMessage) { $cleanMessage } else { $result.Text }
        throw "本机 csw 失败：`n$detail"
    }
    return ConvertFrom-LocalCswJson $result.Text
}

function Get-SnapshotOk {
    param($Snapshots, [string]$Name)
    if (-not $Snapshots) { return $false }
    $prop = $Snapshots.PSObject.Properties[$Name]
    if (-not $prop -or -not $prop.Value) { return $false }
    return [bool]$prop.Value.ok
}

function Get-LocalSnapshotNames {
    param($Status)
    $missing = New-Object System.Collections.Generic.List[string]
    foreach ($name in @($Status.accounts)) {
        if (-not (Get-SnapshotOk $Status.snapshots $name)) {
            [void]$missing.Add([string]$name)
        }
    }
    return $missing.ToArray()
}

function Test-AccountName {
    param([string]$Name)
    return $Name -match '^[A-Za-z0-9][A-Za-z0-9._-]*$'
}

function Get-LocalCodexProcesses {
    # Block Desktop / CLI / app-server. Do not treat the usage dashboard
    # (codex-dash.exe) or the VS Code / Cursor ChatGPT extension as a lock
    # holder — those are not Codex Desktop, and the extension keeps its own
    # app-server until the editor reloads.
    Get-CimInstance Win32_Process -Filter "Name = 'codex.exe' OR Name = 'codex-app-server.exe' OR Name = 'ChatGPT.exe' OR Name LIKE 'OpenAI.Codex%'" -ErrorAction SilentlyContinue |
        Where-Object {
            $name = [string]$_.Name
            $path = [string]$_.ExecutablePath
            $cmd = [string]$_.CommandLine
            $blob = "$name $path $cmd"
            if ($blob -match '(?i)codex-dash|codex_board|codex-board') { return $false }
            if ($blob -match '(?i)\\extensions\\openai\.chatgpt') { return $false }
            if ($blob -match '(?i)\\.vscode\\extensions\\' -and $name -match '(?i)^codex') { return $false }
            if ($blob -match '(?i)\\.cursor\\extensions\\' -and $name -match '(?i)^codex') { return $false }
            if ($name -match '^(?i)codex\.exe$') { return $true }
            if ($name -match '^(?i)codex-app-server\.exe$') { return $true }
            if ($name -match '^(?i)OpenAI\.Codex') { return $true }
            if ($name -match '^(?i)ChatGPT\.exe$') { return $true }
            if ($path -match 'OpenAI\.Codex_') { return $true }
            return $false
        } |
        Select-Object -ExpandProperty Name -Unique
}

function Format-SnapshotMeta {
    param($Meta, [string]$Name)
    if (-not $Meta) { return "" }
    $item = $Meta.$Name
    if (-not $item) { return "" }
    $bits = New-Object System.Collections.Generic.List[string]
    if ($item.plan_label) { [void]$bits.Add([string]$item.plan_label) }
    if ($item.workspace_short) { [void]$bits.Add("工作区 " + [string]$item.workspace_short) }
    switch ([string]$item.token_status) {
        "ok" { }
        "revoked" { [void]$bits.Add("会话探针 401，可试强制切换") }
        "expired" { [void]$bits.Add("token 待刷新") }
        "missing" { [void]$bits.Add("快照缺失") }
        "unchecked" { }
        "network" { [void]$bits.Add("探针联网失败") }
        default { [void]$bits.Add("状态未知") }
    }
    if ($bits.Count -eq 0) { return "" }
    return " - " + ($bits -join "，")
}

function Show-AccountPicker {
    param(
        [string[]]$Accounts,
        [string]$LocalCurrent,
        [string]$RemoteCurrent,
        [string[]]$MissingLocal,
        [string]$Warning,
        $Meta
    )
    Add-Type -AssemblyName System.Windows.Forms | Out-Null
    Add-Type -AssemblyName System.Drawing | Out-Null
    [System.Windows.Forms.Application]::EnableVisualStyles()

    $uiFont = New-Object System.Drawing.Font("Microsoft YaHei UI", 11)
    $form = New-Object System.Windows.Forms.Form
    $form.Text = $Title
    $form.Font = $uiFont
    $form.StartPosition = "CenterScreen"
    $form.FormBorderStyle = "FixedDialog"
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $form.TopMost = $true
    $form.ShowInTaskbar = $true
    if (Test-Path -LiteralPath $RepairLauncherExe) {
        $form.Icon = [System.Drawing.Icon]::ExtractAssociatedIcon($RepairLauncherExe)
    }
    $formWidth = 760
    $labelHeight = 156
    $rowHeight = 34
    $rowGap = 38
    $pad = 20
    $btnW = 112
    $btnH = 36
    $form.ClientSize = New-Object System.Drawing.Size($formWidth, (220 + (($Accounts.Count + 1) * $rowGap)))

    $label = New-Object System.Windows.Forms.Label
    $label.AutoSize = $false
    $label.Font = $uiFont
    $label.Location = New-Object System.Drawing.Point($pad, 14)
    $label.Size = New-Object System.Drawing.Size(($formWidth - ($pad * 2)), $labelHeight)
    $localText = if ($LocalCurrent) { $LocalCurrent } else { "未知" }
    $remoteText = if ($RemoteCurrent) { $RemoteCurrent } else { "未知" }
    $mismatch = ""
    if ($LocalCurrent -and $RemoteCurrent -and $LocalCurrent -ne $RemoteCurrent) {
        $mismatch = "`n本机与服务器不一致；选一个账号可两边对齐。"
    }
    $warn = if ($Warning) { "`n$Warning" } else { "" }
    $label.Text = "当前本机账号：$localText`n当前服务器账号：$remoteText`n选账号会同时切换两边的 auth.json（各用各的快照，不互拷 token，不改 config.toml）。`n默认是「不切换」；要换账号请点选后再确定。请先完全退出 Codex Desktop。$mismatch$warn"
    $form.Controls.Add($label)

    $radios = New-Object System.Collections.Generic.List[object]
    $top = 14 + $labelHeight + 8
    $radioWidth = $formWidth - ($pad * 2) - 8
    $keep = New-Object System.Windows.Forms.RadioButton
    $keep.Font = $uiFont
    $keep.Location = New-Object System.Drawing.Point(($pad + 8), $top)
    $keep.Size = New-Object System.Drawing.Size($radioWidth, $rowHeight)
    $keep.Text = "不切换，只检查并修复对话投影"
    $keep.Tag = $KeepAccount
    $form.Controls.Add($keep)
    [void]$radios.Add($keep)
    $top += $rowGap

    if (-not $MissingLocal) { $MissingLocal = @() }
    foreach ($name in $Accounts) {
        $radio = New-Object System.Windows.Forms.RadioButton
        $radio.Font = $uiFont
        $radio.Location = New-Object System.Drawing.Point(($pad + 8), $top)
        $radio.Size = New-Object System.Drawing.Size($radioWidth, $rowHeight)
        $marks = New-Object System.Collections.Generic.List[string]
        if ($LocalCurrent -and $name -eq $LocalCurrent) { [void]$marks.Add("本机当前") }
        if ($RemoteCurrent -and $name -eq $RemoteCurrent) { [void]$marks.Add("服务器当前") }
        if ($MissingLocal -contains $name) { [void]$marks.Add("本机快照缺失") }
        $suffix = if ($marks.Count -gt 0) { "（" + ($marks -join "，") + "）" } else { "" }
        $radio.Text = $name + (Format-SnapshotMeta -Meta $Meta -Name $name) + $suffix
        $radio.Tag = $name
        $form.Controls.Add($radio)
        [void]$radios.Add($radio)
        $top += $rowGap
    }
    $keep.Checked = $true

    $btnTop = $top + 16
    $ok = New-Object System.Windows.Forms.Button
    $ok.Font = $uiFont
    $ok.Text = "确定"
    $ok.DialogResult = [System.Windows.Forms.DialogResult]::OK
    $ok.Size = New-Object System.Drawing.Size($btnW, $btnH)
    $ok.Location = New-Object System.Drawing.Point(($formWidth - $pad - ($btnW * 2) - 12), $btnTop)
    $form.AcceptButton = $ok
    $form.Controls.Add($ok)

    $cancel = New-Object System.Windows.Forms.Button
    $cancel.Font = $uiFont
    $cancel.Text = "取消"
    $cancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
    $cancel.Size = New-Object System.Drawing.Size($btnW, $btnH)
    $cancel.Location = New-Object System.Drawing.Point(($formWidth - $pad - $btnW), $btnTop)
    $form.CancelButton = $cancel
    $form.Controls.Add($cancel)
    $form.ClientSize = New-Object System.Drawing.Size($formWidth, ($btnTop + $btnH + $pad))

    $result = $form.ShowDialog()
    $form.Dispose()
    $uiFont.Dispose()
    if ($result -ne [System.Windows.Forms.DialogResult]::OK) {
        return $null
    }
    foreach ($radio in $radios) {
        if ($radio.Checked) { return [string]$radio.Tag }
    }
    return $KeepAccount
}

function Format-Summary {
    param([object]$s, [int]$FallbackCode)
    $nl = [Environment]::NewLine
    $label = $s.host
    if (-not $label) { $label = "unknown" }
    if ($label -eq "local") { $label = "本机" }
    elseif ($label -eq "remote") { $label = "服务器" }
    $code = $FallbackCode
    if ($s.exit_code -ne $null) { $code = [int]$s.exit_code }
    if ($code -eq 2) {
        return "${label}：Codex 仍在运行，未改库。请先完全退出后再点一次。"
    }
    if ($code -ne 0) {
        return "${label}：失败（退出码 $code）。"
    }
    $parts = @()
    if ([int]$s.dup_skip -gt 0) { $parts += ("跳过重复序号 {0} 条" -f $s.dup_skip) }
    if ([int]$s.ordinal_advance -gt 0) { $parts += ("前移序号游标 {0} 条" -f $s.ordinal_advance) }
    if ($s.PSObject.Properties["skip_token_count"] -and [int]$s.skip_token_count -gt 0) {
        $parts += ("跳过 token_count {0} 条" -f $s.skip_token_count)
    }
    if ($s.PSObject.Properties["ordinal_backfill"] -and [int]$s.ordinal_backfill -gt 0) {
        $lineCount = 0
        if ($s.PSObject.Properties["ordinal_backfill_lines"] -and $s.ordinal_backfill_lines -ne $null) {
            $lineCount = [int]$s.ordinal_backfill_lines
        }
        $parts += ("补全 JSONL 缺失序号 {0} 条/{1} 行" -f $s.ordinal_backfill, $lineCount)
        if ([int]$s.catch_up -eq 0 -and $s.PSObject.Properties["catch_up_items"] -and [int]$s.catch_up_items -gt 0) {
            $parts[-1] = $parts[-1] + ("，将投影 {0} 条消息" -f $s.catch_up_items)
        }
    }
    if ([int]$s.catch_up -gt 0) {
        $itemCount = 0
        if ($s.catch_up_items -ne $null) { $itemCount = [int]$s.catch_up_items }
        $parts += ("补全对话投影 {0} 条/{1} 条消息" -f $s.catch_up, $itemCount)
    }
    if ([int]$s.rebuild -gt 0) { $parts += ("重建 token_count 卡死 {0} 条" -f $s.rebuild) }
    if ($parts.Count -eq 0) {
        $msg = "${label}：没有发现可自动修复的投影卡点。"
        if ($s.projection_threads) {
            $msg += (" 已检查 {0} 条" -f $s.projection_threads)
            if ($s.healthy) { $msg += ("，正常 {0} 条" -f $s.healthy) }
            $msg += "。"
        }
        return $msg
    }
    $done = if ($s.dry_run) { "将修复" } else { "已修复" }
    return "${label}：$done " + ($parts -join "；") + "。"
}

function Format-Output {
    param([string]$Raw, [int]$Code, [string]$AccountNote)
    $nl = [Environment]::NewLine
    $blocks = @()
    $anyLocked = $false
    $anyRepair = $false
    foreach ($line in ($Raw -split "`r?`n")) {
        if ($line -match "^SUMMARY\s+(\{.*\})$") {
            try {
                $s = $Matches[1] | ConvertFrom-Json
                $blocks += (Format-Summary $s $Code)
                if ([int]$s.exit_code -eq 2) { $anyLocked = $true }
                if (([int]$s.dup_skip + [int]$s.ordinal_advance + [int]$s.rebuild + [int]$s.catch_up + $(if ($s.PSObject.Properties["ordinal_backfill"]) { [int]$s.ordinal_backfill } else { 0 }) + $(if ($s.PSObject.Properties["skip_token_count"]) { [int]$s.skip_token_count } else { 0 })) -gt 0) { $anyRepair = $true }
            } catch { }
        }
    }
    if ($blocks.Count -eq 0) {
        if ($Code -ne 0) { $msg = "修复失败（退出码 $Code）。$nl$nl$Raw" }
        else { $msg = $Raw }
    } else {
        $msg = $blocks -join $nl
        if ($anyRepair) {
            $msg += $nl + $nl + "请重新打开 Codex Desktop，再看服务器上的对应对话。若刚切过账号，两边都要新开会话才生效。"
        } elseif (-not $anyLocked) {
            $msg += $nl + $nl + "可以打开 Codex Desktop。"
        }
    }
    if ($AccountNote) {
        $msg = $AccountNote + $nl + $nl + $msg
    }
    return $msg
}

function Save-RepairIcon {
    param([string]$IcoPath)
    $extract = @"
using System;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
public static class CodexRepairIconExtract {
  [DllImport("shell32.dll", CharSet = CharSet.Auto)]
  static extern IntPtr ExtractIcon(IntPtr hInst, string lpszExeFileName, int nIconIndex);
  [DllImport("user32.dll", SetLastError = true)]
  static extern bool DestroyIcon(IntPtr hIcon);
  public static void Save(string dll, int index, string icoPath) {
    IntPtr handle = ExtractIcon(IntPtr.Zero, dll, index);
    if (handle == IntPtr.Zero || handle == new IntPtr(1)) {
      throw new InvalidOperationException("ExtractIcon failed");
    }
    try {
      using (Icon src = Icon.FromHandle(handle))
      using (Icon clone = (Icon)src.Clone())
      using (FileStream fs = File.Create(icoPath)) {
        clone.Save(fs);
      }
    } finally {
      DestroyIcon(handle);
    }
  }
}
"@
    if (-not ("CodexRepairIconExtract" -as [type])) {
        Add-Type -TypeDefinition $extract -ReferencedAssemblies System.Drawing.dll
    }
    [CodexRepairIconExtract]::Save("$env:SystemRoot\System32\imageres.dll", 109, $IcoPath)
}

function Set-ShortcutAppUserModelId {
    param(
        [Parameter(Mandatory = $true)][string]$LnkPath,
        [Parameter(Mandatory = $true)][string]$AppId
    )
    $setter = @"
using System;
using System.Runtime.InteropServices;
public static class CodexRepairShortcutAumid {
  [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
  class ShellLink {}
  [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("0000010b-0000-0000-C000-000000000046")]
  interface IPersistFile {
    void GetClassID(out Guid pClassID);
    [PreserveSig] int IsDirty();
    void Load([MarshalAs(UnmanagedType.LPWStr)] string pszFileName, uint dwMode);
    void Save([MarshalAs(UnmanagedType.LPWStr)] string pszFileName, [MarshalAs(UnmanagedType.Bool)] bool fRemember);
    void SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string pszFileName);
    void GetCurFile([MarshalAs(UnmanagedType.LPWStr)] out string ppszFileName);
  }
  [StructLayout(LayoutKind.Sequential, Pack = 4)]
  struct PROPERTYKEY {
    public Guid fmtid;
    public uint pid;
  }
  [StructLayout(LayoutKind.Explicit)]
  struct PROPVARIANT {
    [FieldOffset(0)] public ushort vt;
    [FieldOffset(8)] public IntPtr pszVal;
  }
  [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")]
  interface IPropertyStore {
    uint GetCount(out uint cProps);
    uint GetAt(uint iProp, out PROPERTYKEY pkey);
    uint GetValue(ref PROPERTYKEY key, out PROPVARIANT pv);
    uint SetValue(ref PROPERTYKEY key, ref PROPVARIANT pv);
    uint Commit();
  }
  [DllImport("ole32.dll")]
  static extern int PropVariantClear(ref PROPVARIANT pvar);
  const ushort VT_LPWSTR = 31;
  public static void Set(string lnkPath, string appId) {
    object obj = new ShellLink();
    IPersistFile persist = (IPersistFile)obj;
    persist.Load(lnkPath, 2);
    IPropertyStore store = (IPropertyStore)obj;
    PROPERTYKEY key = new PROPERTYKEY();
    key.fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");
    key.pid = 5;
    PROPVARIANT value = new PROPVARIANT();
    value.vt = VT_LPWSTR;
    value.pszVal = Marshal.StringToCoTaskMemUni(appId);
    try {
      uint hr = store.SetValue(ref key, ref value);
      if (hr != 0) {
        Marshal.ThrowExceptionForHR(unchecked((int)hr));
      }
      hr = store.Commit();
      if (hr != 0) {
        Marshal.ThrowExceptionForHR(unchecked((int)hr));
      }
      persist.Save(lnkPath, true);
    } finally {
      PropVariantClear(ref value);
    }
  }
}
"@
    if (-not ("CodexRepairShortcutAumid" -as [type])) {
        Add-Type -TypeDefinition $setter
    }
    [CodexRepairShortcutAumid]::Set($LnkPath, $AppId)
}

function Ensure-RepairLauncher {
    if (-not (Test-Path -LiteralPath $RepairLauncherCs)) {
        throw "找不到启动器源码: $RepairLauncherCs"
    }
    New-Item -ItemType Directory -Force -Path $RepairLauncherDir | Out-Null
    $ico = Join-Path $RepairLauncherDir "CodexRepair.ico"
    Save-RepairIcon -IcoPath $ico
    $csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
    if (-not (Test-Path -LiteralPath $csc)) {
        $csc = Join-Path $env:WINDIR "Microsoft.NET\Framework\v4.0.30319\csc.exe"
    }
    if (-not (Test-Path -LiteralPath $csc)) {
        throw "找不到 csc.exe，无法编译独立启动器。"
    }
    $tmpExe = Join-Path $RepairLauncherDir "CodexRepair.build.exe"
    $out = & $csc /nologo /target:winexe /platform:anycpu /optimize+ "/win32icon:$ico" "/out:$tmpExe" $RepairLauncherCs 2>&1 | ForEach-Object { "$_" }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $tmpExe)) {
        throw "编译启动器失败：`n$($out -join "`n")"
    }
    Copy-Item -LiteralPath $tmpExe -Destination $RepairLauncherExe -Force
    Remove-Item -LiteralPath $tmpExe -ErrorAction SilentlyContinue
    Write-Host "launcher: $RepairLauncherExe"
}

function Install-RepairShortcut {
    param([string]$Path)
    Ensure-RepairLauncher
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($Path)
    $lnk.TargetPath = $RepairLauncherExe
    $lnk.Arguments = "`"$PSCommandPath`""
    $lnk.WorkingDirectory = $RepoRoot
    $lnk.WindowStyle = 7
    $lnk.Description = "退出 Codex 后点击：同时切换本机与服务器账号、修复对话投影"
    $lnk.IconLocation = "$RepairLauncherExe,0"
    $lnk.Save()
    try {
        Set-ShortcutAppUserModelId -LnkPath $Path -AppId $RepairAppId
    } catch {
        Write-Host "AppUserModelID 写入失败（快捷方式仍可用）：$_"
    }
    Write-Host "shortcut: $Path"
}

function Copy-RepairShortcutPlaces {
    param([string]$DesktopPath)
    $startDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
    $startPath = Join-Path $startDir $ShortcutName
    if (Test-Path -LiteralPath $startDir) {
        Copy-Item -LiteralPath $DesktopPath -Destination $startPath -Force
        Write-Host "start menu: $startPath"
    }
    $pinDir = Join-Path $env:APPDATA "Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar"
    if (Test-Path -LiteralPath $pinDir) {
        $pinPath = Join-Path $pinDir $ShortcutName
        Copy-Item -LiteralPath $DesktopPath -Destination $pinPath -Force
        Write-Host "taskbar pin: $pinPath"
    }
}

function Try-PinRepairShortcut {
    param([string]$Path)
    $did = $false
    try {
        $folder = Split-Path -Parent $Path
        $name = Split-Path -Leaf $Path
        $app = New-Object -ComObject Shell.Application
        $item = $app.NameSpace($folder).ParseName($name)
        if (-not $item) { return $false }
        foreach ($verb in @($item.Verbs())) {
            $label = ([string]$verb.Name).Replace("&", "")
            if ($label -match "任务栏|taskbar") {
                $verb.DoIt()
                Write-Host "pinned via: $label"
                $did = $true
            } elseif ($label -match "固定到" -and $label -match "开始") {
                $verb.DoIt()
                Write-Host "pinned to Start via: $label"
                $did = $true
            }
        }
    } catch {
        Write-Host "pin verb unavailable: $_"
    }
    return $did
}

function Invoke-RemotePython {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [string]$TempName = "codex_remote_probe.py",
        [string]$PythonArgs = ""
    )
    $probe = $Source.Replace("`r`n", "`n")
    $tmp = Join-Path $env:TEMP $TempName
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($tmp, $probe, $utf8)
    try {
        $remoteCmd = ($script:RemoteEnvPrefix -join " ") + " python3 -"
        if ($PythonArgs) { $remoteCmd = ($script:RemoteEnvPrefix -join " ") + " python3 - $PythonArgs" }
        $opt = ($script:SshOpts | ForEach-Object { $_.Replace('"', '\"') }) -join " "
        $out = & cmd.exe /c "ssh $opt $RemoteHost $remoteCmd < `"$tmp`"" 2>&1 | ForEach-Object { "$_" }
        return @{
            Code = [int]$LASTEXITCODE
            Text = ($out -join "`n")
        }
    } finally {
        Remove-Item -LiteralPath $tmp -ErrorAction SilentlyContinue
    }
}

function Invoke-RemoteEnsureSnapshots {
    param([switch]$Dry, [switch]$Probe)
    $remoteArgs = @("python3", $RemoteLocalCsw, "--ensure-snapshots", "--json")
    if ($Probe) { $remoteArgs += "--probe-all" }
    if ($Dry) { $remoteArgs += "--dry-run" }
    $result = Invoke-Remote -ArgumentList $remoteArgs
    if ($result.Code -ne 0) {
        throw "服务器快照刷新失败：`n$($result.Text)"
    }
    return ConvertFrom-LocalCswJson $result.Text
}

function Format-DeadSessions {
    # 把 --probe-all 的结果压成一行，给 sync.log 用。没探测（meta 为空）时
    # 返回 $null，免得日志里出现"一切正常"的假象。
    param($Payload)
    if (-not $Payload) { return $null }
    $meta = $Payload.snapshot_meta
    if (-not $meta) { return $null }
    $dead = New-Object System.Collections.Generic.List[string]
    $unknown = 0
    foreach ($name in @($meta.PSObject.Properties.Name)) {
        $item = $meta.$name
        $st = [string]$item.token_status
        if ($st -eq "revoked") {
            [void]$dead.Add("$name=REVOKED")
        } elseif ($st -eq "unknown" -or $st -eq "network") {
            $unknown++
        }
    }
    if ($dead.Count -gt 0) { return "dead=" + ($dead -join ",") }
    if ($unknown -gt 0) { return "probe-unavailable($unknown)" }
    return "dead=none"
}

function Write-SyncLog {
    # 后台保鲜任务没有窗口，出问题只能靠日志。只保留当前 + 上一次。
    param([string]$Text)
    $path = Join-Path $RepairLauncherDir "sync.log"
    try {
        New-Item -ItemType Directory -Force -Path $RepairLauncherDir | Out-Null
        $item = Get-Item -LiteralPath $path -ErrorAction SilentlyContinue
        if ($item -and $item.Length -gt 262144) {
            Move-Item -LiteralPath $path -Destination "$path.1" -Force
        }
        $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        Add-Content -LiteralPath $path -Value "[$stamp] $Text" -Encoding UTF8
    } catch {
    }
}

function Format-RefreshClause {
    param($Status, [string]$Side)
    if (-not $Status) { return $null }
    $st = [string]$Status.refresh_status
    $acct = [string]$Status.refresh_account
    $prefix = if ($Status.dry_run) { "${Side}将用当前 live 更新" } else { "${Side}已用当前 live 更新" }
    if ($st -eq "updated" -and $acct) {
        return "$prefix $acct 快照"
    }
    if ($st -eq "already_current" -and $acct) {
        return "${Side}$acct 快照已与 live refresh token 一致"
    }
    if ($st -eq "skipped") {
        return "${Side}当前 live 不是已知账号，未刷新快照"
    }
    return $null
}

function Add-RefreshNote {
    param([string]$Base, $LocalStatus, $RemoteStatus)
    $parts = New-Object System.Collections.Generic.List[string]
    $localClause = Format-RefreshClause $LocalStatus "本机"
    $remoteClause = Format-RefreshClause $RemoteStatus "服务器"
    if ($localClause) { [void]$parts.Add($localClause) }
    if ($remoteClause) { [void]$parts.Add($remoteClause) }
    if ($parts.Count -eq 0) { return $Base }
    return ($Base.TrimEnd("。") + "。" + ($parts -join "；") + "。")
}

function Test-RemoteUnlocked {
    $result = Invoke-RemotePython -TempName "codex_unlock_probe.py" -Source @'
import sqlite3
from pathlib import Path
p = Path.home() / ".codex" / "thread_history_1.sqlite"
if not p.exists():
    print("UNLOCKED")
    raise SystemExit(0)
try:
    con = sqlite3.connect("file:%s?mode=rw" % p.as_posix(), uri=True, timeout=0.2)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.rollback()
    finally:
        con.close()
    print("UNLOCKED")
except Exception:
    print("LOCKED")
    raise SystemExit(2)
'@
    return ($result.Code -eq 0 -and $result.Text -match "UNLOCKED")
}

function Stop-RemoteControlAppServer {
    param([string]$SwitchTo, [bool]$Force)
    $switchLiteral = if ([string]::IsNullOrEmpty($SwitchTo)) { '""' } else { ($SwitchTo | ConvertTo-Json -Compress) }
    $forceLiteral = if ($Force) { "True" } else { "False" }
    $cswLiteral = ($RemoteLocalCsw | ConvertTo-Json -Compress)
    $source = @'
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

SWITCH_TO = __SWITCH_TO__
SWITCH_FORCE = __SWITCH_FORCE__
CSW_PATH = __CSW_PATH__
'@
    $source = $source.Replace("__SWITCH_TO__", $switchLiteral).Replace("__SWITCH_FORCE__", $forceLiteral).Replace("__CSW_PATH__", $cswLiteral) + @'

SKIP_MARKERS = ("vscode-server", "cursor-server", ".vscode-server")
KEEP_MARKERS = (
    "/.npm-global/",
    "/.cache/codex-runtimes/",
    "/.codex/",
    "codex-linux-x64",
    "/usr/bin/codex",
    "/usr/local/bin/codex",
)
SOCK = Path.home() / ".codex" / "app-server-control" / "app-server-control.sock"


def cmdline(pid):
    path = Path("/proc/%d/cmdline" % pid)
    try:
        raw = path.read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
    except Exception:
        return ""
    return raw


def exe_path(pid):
    try:
        return os.readlink("/proc/%d/exe" % pid)
    except Exception:
        return ""


def ppid_of(pid):
    try:
        return int(Path("/proc/%d/stat" % pid).read_text().split()[3])
    except Exception:
        return 0


def children_of(pid):
    out = []
    proc = Path("/proc")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        other = int(entry.name)
        if ppid_of(other) == pid:
            out.append(other)
    return out


def is_skipped(text):
    low = text.lower()
    return any(marker in low for marker in SKIP_MARKERS)


def is_cli_or_desktop_runtime(text):
    low = text.lower()
    if is_skipped(low):
        return False
    return any(marker.lower() in low for marker in KEEP_MARKERS)


if SWITCH_TO:
    db = Path.home() / ".codex" / "thread_history_1.sqlite"
    if db.exists():
        try:
            con = sqlite3.connect("file:%s?mode=rw" % db.as_posix(), uri=True, timeout=0.2)
            try:
                con.execute("BEGIN IMMEDIATE")
                con.rollback()
            finally:
                con.close()
        except Exception:
            print("LOCKED")
            raise SystemExit(2)
    csw_argv = ["python3", CSW_PATH, SWITCH_TO, "--json"]
    if SWITCH_FORCE:
        csw_argv.append("--force")
    switched = subprocess.run(
        csw_argv,
        capture_output=True,
        text=True,
    )
    sys.stdout.write(switched.stdout or "")
    if switched.returncode != 0:
        sys.stderr.write(switched.stderr or "")
        raise SystemExit(switched.returncode)
    print("CSW_DONE", SWITCH_TO)

pids = set()
if SOCK.exists():
    try:
        import subprocess
        out = subprocess.check_output(["lsof", "-t", str(SOCK)], text=True, stderr=subprocess.DEVNULL)
        for item in out.split():
            if item.strip().isdigit():
                pids.add(int(item))
    except Exception as exc:
        print("LSOF_FAIL", exc)

for entry in Path("/proc").iterdir():
    if not entry.name.isdigit():
        continue
    pid = int(entry.name)
    text = cmdline(pid) + " " + exe_path(pid)
    if "app-server" not in text.lower() and "code-mode-host" not in text.lower():
        continue
    if is_cli_or_desktop_runtime(text):
        pids.add(pid)

for pid in list(pids):
    parent = ppid_of(pid)
    if parent > 1:
        pids.add(parent)
    for child in children_of(pid):
        pids.add(child)

filtered = set()
for pid in pids:
    text = cmdline(pid) + " " + exe_path(pid)
    if is_skipped(text):
        continue
    if pid <= 1:
        continue
    filtered.add(pid)

print("PIDS", " ".join(str(pid) for pid in sorted(filtered)) or "-")
for pid in filtered:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
time.sleep(0.15)
for pid in filtered:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
if SOCK.exists():
    try:
        SOCK.unlink()
    except OSError:
        pass
print("STOPPED")
'@
    $result = Invoke-RemotePython -TempName "codex_stop_appserver.py" -Source $source
    if ($result.Code -ne 0 -or ($result.Text -notmatch "STOPPED|NO_SOCK")) {
        throw "重启服务器 app-server 失败：`n$($result.Text)"
    }
    return $result.Text
}

try {
    if ($InstallShortcut) {
        $desktopFolder = [Environment]::GetFolderPath([Environment]::SpecialFolder::Desktop)
        $desktop = Join-Path $desktopFolder $ShortcutName
        Install-RepairShortcut $desktop
        Copy-RepairShortcutPlaces $desktop
        $null = Try-PinRepairShortcut $desktop
        if (-not $DryRun) {
            Write-Host "已创建桌面快捷方式。"
            if ((Test-Path -LiteralPath $Python) -and (Test-Path -LiteralPath $LocalCsw)) {
                $env:PYTHONIOENCODING = "utf-8"
                try {
                    $ensured = Invoke-LocalCsw -CswArgs @("--ensure-snapshots", "--json")
                    $miss = @(Get-LocalSnapshotNames $ensured)
                    if ($miss.Count -gt 0) {
                        Write-Host ("本机仍缺快照: " + ($miss -join ", "))
                    } else {
                        $clause = Format-RefreshClause $ensured "本机"
                        if ($clause) { Write-Host $clause } else { Write-Host "本机账号快照已就绪。" }
                    }
                    try {
                        $remoteEnsured = Invoke-RemoteEnsureSnapshots
                        $rclause = Format-RefreshClause $remoteEnsured "服务器"
                        if ($rclause) { Write-Host $rclause }
                    } catch {
                        Write-Host "服务器快照刷新失败（快捷方式已创建）：$_"
                    }
                } catch {
                    Write-Host "本机快照准备失败（快捷方式已创建）：$_"
                }
            }
            return
        }
    }

    if (-not (Test-Path -LiteralPath $Unwedge)) { throw "找不到修复脚本: $Unwedge" }
    if (-not (Test-Path -LiteralPath $Python)) { throw "找不到 Python 解释器: $Python。请在环境变量 PATH 中配置 Python，或在 config.json 中指定 'python' 路径。" }
    if (-not (Test-Path -LiteralPath $LocalCsw)) { throw "找不到本机切号脚本: $LocalCsw" }

    if ($SyncOnly) {
        # 静默保鲜模式，给计划任务用（本机 + 服务器各刷一次快照）。
        #
        # 为什么需要它：Codex 每次刷新 access token 时 refresh_token 也会轮换，
        # 旧的那份立刻作废。快照只在点快捷方式时同步，如果长时间不点，快照就会
        # 停在旧 refresh_token 上——下次切过去才发现是一份死凭证。
        # 这个模式不弹窗、不切号、不修投影，失败只写日志，退出码恒为 0，
        # 免得计划任务每次都弹一个失败框。
        $lines = New-Object System.Collections.Generic.List[string]
        $ok = $true
        try {
            $localPayload = Invoke-LocalCsw -CswArgs @("--ensure-snapshots", "--json", "--probe-all")
            $who = if ($localPayload.current) { $localPayload.current } else { "unknown" }
            $dead = Format-DeadSessions $localPayload
            $extra = if ($dead) { " $dead" } else { "" }
            $lines.Add("local=$who refresh=$($localPayload.refresh_status) session=$($localPayload.current_token_status)$extra")
        } catch {
            $ok = $false
            $lines.Add("local FAILED: $_")
        }
        if (-not $LocalOnly) {
            try {
                $remotePayload = Invoke-RemoteEnsureSnapshots -Probe
                $who = if ($remotePayload.current) { $remotePayload.current } else { "unknown" }
                $dead = Format-DeadSessions $remotePayload
                $extra = if ($dead) { " $dead" } else { "" }
                $lines.Add("server=$who refresh=$($remotePayload.refresh_status)$extra")
            } catch {
                $ok = $false
                $lines.Add("server FAILED: $_")
            }
        }
        $tail = if ($ok) { "[ok]" } else { "[partial]" }
        Write-SyncLog (($lines -join "; ") + " " + $tail)
        exit 0
    }

    $env:PYTHONIOENCODING = "utf-8"
    $accountNote = $null
    $status = $null
    $remoteEnsure = $null
    # Probing costs one round trip per snapshot, so only pay for it when the
    # picker is actually going to be shown and its annotations matter.
    $willShowPicker = (-not $Account) -and (-not $Silent) -and (-not $LocalOnly)
    $ensureArgs = @("--ensure-snapshots", "--json")
    if ($willShowPicker) { $ensureArgs += "--probe-all" }
    if ($DryRun) { $ensureArgs += "--dry-run" }

    $remoteAsync = $null
    if (-not $LocalOnly) {
        # Remote does not need --probe-all for the picker: the picker only displays
        # $localStatus.snapshot_meta, while remote only needs current and accounts.
        # Run remote ensure in parallel with the local snapshot check so the SSH
        # latency is completely hidden.
        $remoteArgs = @("python3", $RemoteLocalCsw, "--ensure-snapshots", "--json")
        if ($DryRun) { $remoteArgs += "--dry-run" }
        $remoteFullCmd = @($script:SshOpts + @($RemoteHost) + @($script:RemoteEnvPrefix + $remoteArgs))
        try {
            $psAsync = [powershell]::Create()
            $null = $psAsync.AddScript({
                param($cmdArgs)
                $output = & ssh @cmdArgs 2>&1 | ForEach-Object { "$_" }
                return @{ Code = [int]$LASTEXITCODE; Text = ($output -join "`n") }
            }).AddArgument($remoteFullCmd)
            $handle = $psAsync.BeginInvoke()
            $remoteAsync = @{ PS = $psAsync; Handle = $handle }
        } catch {
            $remoteAsync = $null
        }
    }

    $localStatus = Invoke-LocalCsw -CswArgs $ensureArgs

    if (-not $LocalOnly) {
        if ($remoteAsync) {
            try {
                $rawResult = $remoteAsync.PS.EndInvoke($remoteAsync.Handle)
                if ($rawResult -and $rawResult.Count -gt 0) {
                    $item = $rawResult[0]
                    if ($item.Code -eq 0) {
                        $remoteEnsure = ConvertFrom-LocalCswJson $item.Text
                    }
                }
            } catch {
            } finally {
                $remoteAsync.PS.Dispose()
            }
        }
        if (-not $remoteEnsure) {
            try {
                $remoteEnsure = Invoke-RemoteEnsureSnapshots -Dry:$DryRun
            } catch {
                $status = Get-RemoteAccountStatus
                $status.Warning = "未能从服务器快照状态读取账号列表，已回退到 csw。"
            }
        }
        if ($remoteEnsure) {
            $status = @{
                Current = [string]$remoteEnsure.current
                Accounts = @($remoteEnsure.accounts)
                Warning = $null
                Raw = ""
            }
            if (-not $status.Current) { $status.Current = $null }
            if ($status.Accounts.Count -eq 0) {
                $status = Get-RemoteAccountStatus
                $status.Warning = "未能从服务器快照状态读取账号列表，已回退到 csw。"
            }
        }
    }

    $choice = $KeepAccount
    if ($Account) {
        if ($Account -eq "keep") { $choice = $KeepAccount }
        else { $choice = $Account }
    } elseif ($Silent) {
        $choice = $KeepAccount
    } elseif (-not $LocalOnly) {
        $missing = @(Get-LocalSnapshotNames $localStatus)
        $warnParts = New-Object System.Collections.Generic.List[string]
        if ($status.Warning) { [void]$warnParts.Add([string]$status.Warning) }
        if ($missing.Count -gt 0) {
            [void]$warnParts.Add("本机缺少有效快照：" + ($missing -join "、") + "。选这些账号会失败。")
        }
        if (-not $localStatus.current) {
            [void]$warnParts.Add("当前本机不是已知账号。选账号会先把现登录另存为 ~/.codex/auth.unknown-时间戳.json，再切到所选账号。")
        }
        $choice = Show-AccountPicker -Accounts $status.Accounts -LocalCurrent $localStatus.current -RemoteCurrent $status.Current -MissingLocal $missing -Warning ($warnParts -join " ") -Meta $localStatus.snapshot_meta
        if ($null -eq $choice) {
            $cancelled = $true
            return
        }
    }

    if ($choice -ne $KeepAccount) {
        if (-not (Test-AccountName $choice)) { throw "非法账号名: $choice" }
        if (-not $LocalOnly -and $status.Accounts -notcontains $choice) { throw "账号不在服务器 csw 列表中: $choice" }
        if (@($localStatus.accounts) -notcontains $choice) { throw "账号不在本机 csw 列表中: $choice" }
        if (-not (Get-SnapshotOk $localStatus.snapshots $choice)) {
            throw "本机没有 $choice 的有效快照。请先用该账号登录一次 Codex Desktop，或检查 ~/.codex/account_backup。"
        }
        $procs = @(Get-LocalCodexProcesses)
        if ($procs.Count -gt 0) {
            throw "本机仍有 Codex 进程（$($procs -join ', ')）。请先完全退出后再点一次。"
        }
        # 选号界面上每个账号已经标了会话状态（"会话探针 401，可试强制切换"）。
        # 用户看过还点它，就是要切 —— 直接切，不再弹第二个确认框。
        # 理由是单个 401 只是"疑似"（account_id 请求头、代理抖动、服务端瞬时 401
        # 都可能误报），refresh token 有时仍能换出有效 access token，只有真切过去
        # 让 Codex 试一次才知道。所以只做告知、不做拦截，切换结果里会写明这次是
        # 强制切过去的。
        $forceSwitch = $true
        if ($DryRun) {
            $scope = if ($LocalOnly) { "本机" } else { "本机和服务器" }
            $accountNote = "将切换${scope}账号为 $choice（dry-run，未改 auth.json / 未重启 app-server）。"
            $accountNote = Add-RefreshNote -Base $accountNote -LocalStatus $localStatus -RemoteStatus $remoteEnsure
        } else {
            $prevLocal = [string]$localStatus.current
            $localDidSwitch = $prevLocal -ne $choice
            $remoteDidSwitch = $false
            $switchArgs = @($choice, "--json")
            if ($forceSwitch) { $switchArgs += "--force" }
            try {
                $afterLocal = Invoke-LocalCsw -CswArgs $switchArgs
                if ($afterLocal.current -ne $choice) {
                    throw "本机 csw 已执行，但当前账号是 $($afterLocal.current)，不是 $choice。"
                }
                $localNote = if ($localDidSwitch) {
                    "本机 auth.json 已切换为 $choice。"
                } else {
                    "本机 auth.json 已是 $choice。"
                }
                if ($afterLocal.notes) {
                    foreach ($note in @($afterLocal.notes)) {
                        $text = [string]$note
                        if ($text -match "save unrecognized live auth to (\S+)") {
                            $localNote += " 原未知登录已另存为 $($Matches[1])。"
                        }
                        if ($text -match "webview stores were locked") {
                            throw "Desktop 的 Work/Personal 会话还被占用。请先完全退出 Codex Desktop 后再点一次。"
                        }
                        if ($text -match "reset Codex Desktop Work/Personal session") {
                            $localNote += " 已清 Desktop 内嵌浏览器的 Work/Personal 会话。"
                        }
                        if ($text -match "forced past a revoked session") {
                            $localNote += " 注意：$choice 的会话探针报 401，已按你的选择强制切过去。" +
                                "若 Desktop 仍显示「没有 Work 权限」，那这份登录确实失效了，需要用该账号重新登录一次。"
                        }
                    }
                }
                $authNote = $localNote
                $stopOut = $null
                if (-not $LocalOnly) {
                    if ($status.Current -ne $choice) {
                        $stopOut = Stop-RemoteControlAppServer -SwitchTo $choice -Force $forceSwitch
                        if ($stopOut -match "LOCKED") {
                            throw "服务器 thread_history 仍被占用。请先完全退出 Codex Desktop / 远程会话后再点一次。"
                        }
                        $verifyCurrent = $null
                        foreach ($line in ($stopOut -split "`r?`n")) {
                            $trim = $line.Trim()
                            if ($trim.StartsWith("{") -and $trim.EndsWith("}")) {
                                try {
                                    $verifyCurrent = [string]($trim | ConvertFrom-Json).current
                                } catch { }
                            }
                        }
                        if ($verifyCurrent -and $verifyCurrent -ne $choice) {
                            throw "服务器 csw 已执行，但当前账号是 $verifyCurrent，不是 $choice。`n$stopOut"
                        }
                        $remoteDidSwitch = $true
                        $authNote = "$localNote 服务器 auth.json 已切换为 $choice。"
                    } else {
                        $stopOut = Stop-RemoteControlAppServer
                        $authNote = "$localNote 服务器 auth.json 已是 $choice。"
                    }
                    $accountNote = "$authNote 已停止服务器上的 CLI/Desktop 远程 app-server（$stopOut）。请重新打开本机 Desktop，并重新运行服务器 ``codex`` / Desktop 远程任务。VS Code 里的 ChatGPT 扩展不会自动切号。"
                } else {
                    $accountNote = "$authNote 请重新打开本机 Desktop。"
                }
            } catch {
                if ($localDidSwitch -and -not $remoteDidSwitch -and $prevLocal) {
                    try {
                        $null = Invoke-LocalCsw -CswArgs @($prevLocal, "--json", "--force")
                    } catch {
                    }
                }
                throw
            }
        }
    } else {
        $localCur = if ($localStatus.current) { $localStatus.current } else { "未知" }
        if ($LocalOnly) {
            $accountNote = "未切换账号（本机：$localCur）。"
        } else {
            $remoteCur = if ($status.Current) { $status.Current } else { "未知" }
            $accountNote = "未切换账号（本机：$localCur；服务器：$remoteCur）。"
        }
        $accountNote = Add-RefreshNote -Base $accountNote -LocalStatus $localStatus -RemoteStatus $remoteEnsure
        if ($localStatus.current_token_status -eq "revoked") {
            $accountNote += "`n注意：本机当前账号 $localCur 的 ChatGPT 会话已被服务端吊销（HTTP 401）。" +
                "这不是浏览器里 Work/Personal 记录的问题——凭据失效时 Desktop 建不起该工作区会话，" +
                "仍会落到 ChatGPT Work 页面并显示无权限。请用该账号运行 codex login 重新登录，再点一次。"
        }
    }

    $pyArgs = @($Unwedge)
    if ($DryRun) { $pyArgs += "--dry-run" }
    if (-not $LocalOnly) {
        $pyArgs += @("--ssh-host", $RemoteHost)
    }
    $raw = & $Python @pyArgs 2>&1 | ForEach-Object { "$_" }
    $exitCode = $LASTEXITCODE
    $joined = ($raw -join "`n")
    $popupText = Format-Output -Raw $joined -Code $exitCode -AccountNote $accountNote
    if ($joined -match '"exit_code": 2') { $popupType = 48; if ($exitCode -eq 0) { $exitCode = 0 } }
    elseif ($exitCode -ne 0) { $popupType = 16 }
    else { $popupType = 64 }

    if ($StartDesktop) {
        Start-Process "shell:AppsFolder\$CodexDesktopAumid"
        $popupText += [Environment]::NewLine + [Environment]::NewLine + "已尝试打开 Codex Desktop。"
    }
} catch {
    $popupType = 16
    $popupText = "操作出错：" + [Environment]::NewLine + "$_"
    $exitCode = 1
} finally {
    if (-not $cancelled -and $popupText) {
        if ($Silent) { Write-Host $popupText }
        else { Show-Popup -Text $popupText -Type $popupType }
    }
    exit $exitCode
}
