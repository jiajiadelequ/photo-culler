using System;
using System.Diagnostics;
using System.IO;

internal static class Program
{
    private static int Main(string[] args)
    {
        var baseDir = AppDomain.CurrentDomain.BaseDirectory;
        var cmdPath = Path.Combine(baseDir, "codex.cmd");

        if (!File.Exists(cmdPath))
        {
            Console.Error.WriteLine("未在 codex.exe 同目录下找到 codex.cmd。");
            return 1;
        }

        var psi = new ProcessStartInfo
        {
            FileName = cmdPath,
            UseShellExecute = false,
        };

        foreach (var arg in args)
        {
            psi.ArgumentList.Add(arg);
        }

        using var process = Process.Start(psi);
        if (process == null)
        {
            Console.Error.WriteLine("启动 codex.cmd 失败。");
            return 1;
        }

        process.WaitForExit();
        return process.ExitCode;
    }
}
