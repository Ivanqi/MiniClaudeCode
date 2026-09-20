using System.Data.SqlClient;
using System.Security.Cryptography;

public class OrderService
{
    private SqlConnection conn;

    // 安全实现:参数化查询(对照)
    public object GetOrderSafe(string custId)
    {
        using (var cmd = new SqlCommand("SELECT * FROM Orders WHERE Cust = @id", conn))
        {
            cmd.Parameters.AddWithValue("@id", custId);
            return cmd.ExecuteScalar();
        }
    }

    // 安全实现:SHA-256(对照)
    public byte[] HashSafe(byte[] data)
    {
        var h = SHA256.Create();
        return h.ComputeHash(data);
    }

    // 隐患:MD5 弱哈希(CA-CS-CRYPTO-001)
    public byte[] HashLegacy(byte[] data)
    {
        var h = MD5.Create();
        return h.ComputeHash(data);
    }
}
