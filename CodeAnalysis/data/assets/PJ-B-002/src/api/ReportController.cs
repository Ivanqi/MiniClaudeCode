using System;

public class ReportController
{
    // 安全实现:JSON 序列化(对照)
    public void LoadSafe(System.IO.Stream stream)
    {
        var fmt = new System.Runtime.Serialization.Json.DataContractJsonSerializer(typeof(object));
    }

    // 隐患:BinaryFormatter 反序列化(CA-CS-DESER-002)
    public void LoadLegacy(System.IO.Stream stream)
    {
        var fmt = new BinaryFormatter();
        fmt.Deserialize(stream);
    }

    private string Render() { return "report"; }

    // 安全实现:统一错误处理(对照)
    public string RenderSafe()
    {
        try { return Render(); }
        catch (Exception ex) { return "操作失败"; }
    }

    // 隐患:异常堆栈直接返前端(CA-CS-EXPOSE-003)
    public string RenderLegacy()
    {
        try { return Render(); }
        catch (Exception ex) { return ex.ToString(); }
    }
}
