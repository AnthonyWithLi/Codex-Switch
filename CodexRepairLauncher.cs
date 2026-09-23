using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

internal static class Native {
    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    public static extern int SetCurrentProcessExplicitAppUserModelID(string appId);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int MessageBox(IntPtr hWnd, string text, string caption, uint type);
}

internal static class Program {
    const string Title = "Codex 账号与对话投影";

    static void Alert(string text) {
        Native.MessageBox(IntPtr.Zero, text, Title, 0x00000010);
    }

    [STAThread]
    private static int Main(string[] args) {
        Native.SetCurrentProcessExplicitAppUserModelID("OpenAI.CodexRepair");
        if (args == null || args.Length < 1) {
            Alert("快捷方式参数丢失，启动器不知道该运行哪份脚本。请重新运行 start_codex_desktop.ps1 -InstallShortcut。");
            return 2;
        }
        string ps1 = args[0];
        if (!File.Exists(ps1)) {
            Alert("找不到脚本：\n" + ps1);
            return 3;
        }
        var psi = new ProcessStartInfo();
        psi.FileName = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.System),
            @"WindowsPowerShell\v1.0\powershell.exe");
        psi.Arguments = "-NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File \"" + ps1 + "\"";
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.RedirectStandardError = true;
        psi.RedirectStandardOutput = true;
        // The script pins its console output to UTF-8 (see start_codex_desktop.ps1).
        // Without this, .NET decodes that pipe with the ANSI code page (936 here)
        // and any Chinese text we quote back ends up as mojibake.
        var utf8 = new UTF8Encoding(false);
        psi.StandardOutputEncoding = utf8;
        psi.StandardErrorEncoding = utf8;
        psi.WorkingDirectory = Path.GetDirectoryName(ps1) ?? "";
        var proc = Process.Start(psi);
        if (proc == null) {
            Alert("无法启动 powershell.exe。");
            return 4;
        }
        var stderr = new StringBuilder();
        var stdout = new StringBuilder();
        var errDone = new AutoResetEvent(false);
        var outDone = new AutoResetEvent(false);
        proc.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e) {
            if (e.Data == null) { errDone.Set(); return; }
            stderr.AppendLine(e.Data);
        };
        proc.OutputDataReceived += delegate(object sender, DataReceivedEventArgs e) {
            if (e.Data == null) { outDone.Set(); return; }
            stdout.AppendLine(e.Data);
        };
        proc.BeginErrorReadLine();
        proc.BeginOutputReadLine();
        proc.WaitForExit();
        errDone.WaitOne(1000);
        outDone.WaitOne(1000);
        string combined = (stderr.ToString() + "\n" + stdout.ToString()).Trim();
        if (proc.ExitCode != 0 && LooksLikeStartupFailure(combined, proc.ExitCode)) {
            string detail = combined.Length > 0 ? combined : ("powershell 退出码 " + proc.ExitCode);
            if (detail.Length > 1200) {
                detail = detail.Substring(0, 1200) + "\n...";
            }
            Alert("快捷方式启动失败（隐藏窗口，所以看起来像没反应）：\n\n" + detail);
        }
        return proc.ExitCode;
    }

    static bool LooksLikeStartupFailure(string text, int exitCode) {
        // The script shows its own MessageBox for every problem it can handle
        // (account picker errors, revoked sessions, missing snapshots, ...), and
        // it writes nothing to stdout in those paths. A non-zero exit with no
        // captured output therefore means "the script already explained itself",
        // so alerting here would only produce a second, useless dialog.
        if (string.IsNullOrEmpty(text)) {
            return false;
        }
        // Only alert when PowerShell itself failed before the script could take
        // over: parse errors, or the script/module not being found.
        string low = text.ToLowerInvariant();
        return low.Contains("parsererror")
            || text.IndexOf("表达式") >= 0
            || text.IndexOf("解析") >= 0
            || text.IndexOf("TerminatorExpected") >= 0
            || text.IndexOf("找不到") >= 0;
    }
}
