using System;
using System.Security.Cryptography;

public class BatchJob
{
    // 批量任务模块:仅做日志与哈希校验(安全实现对照)
    public string DigestFile(byte[] data)
    {
        var h = SHA256.Create();
        return Convert.ToHexString(h.ComputeHash(data));
    }

    public string Name() { return "batch"; }
}
