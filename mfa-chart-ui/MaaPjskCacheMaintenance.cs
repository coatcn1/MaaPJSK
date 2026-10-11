using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;

namespace MFAAvalonia.Helper;

public static class MaaPjskCacheMaintenance
{
    public sealed record Result(int Files, long Bytes, IReadOnlyList<string> Errors);
    public static Result Clear(string root)
    {
        root = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar);
        var errors = new List<string>();
        int files = 0;
        long bytes = 0;
        bool Safe(string path)
        {
            var full = Path.GetFullPath(path);
            if (!full.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)
                || (File.GetAttributes(full) & FileAttributes.ReparsePoint) != 0)
            {
                errors.Add($"已拒绝不安全路径：{Path.GetFileName(path)}");
                return false;
            }
            return true;
        }
        void Visit(string path)
        {
            try
            {
                if (!Safe(path)) return;
                if (Directory.Exists(path))
                {
                    foreach (var entry in Directory.EnumerateFileSystemEntries(path)) Visit(entry);
                    if (!Directory.EnumerateFileSystemEntries(path).Any()) Directory.Delete(path);
                }
                else if (!string.Equals(Path.GetFileName(path), "results.csv", StringComparison.OrdinalIgnoreCase))
                {
                    long length = new FileInfo(path).Length;
                    File.Delete(path);
                    files++;
                    bytes += length;
                }
            }
            catch (Exception error) { errors.Add($"{Path.GetFileName(path)}：{error.Message}"); }
        }
        // 只遍历项目诊断目录；配置、校准、谱面及运行环境不在清理范围。
        if ((File.GetAttributes(root) & FileAttributes.ReparsePoint) != 0)
            return new(0, 0, new[] { "根目录是重解析点，已拒绝清理。" });
        foreach (var name in new[] { "debug", "logs", "screencap" })
        {
            var path = Path.Combine(root, name);
            if (Directory.Exists(path)) Visit(path);
        }
        var config = Path.Combine(root, "config");
        if (Directory.Exists(config) && Safe(config))
            foreach (var path in Directory.EnumerateFiles(config, "failure-*.png", SearchOption.TopDirectoryOnly)) Visit(path);
        return new(files, bytes, errors);
    }
}
