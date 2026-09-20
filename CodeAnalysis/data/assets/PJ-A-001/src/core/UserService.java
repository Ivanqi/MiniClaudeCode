package com.techmgmt.user;

import java.sql.*;
import javax.crypto.Cipher;

public class UserService {
    private Connection con;

    // 安全实现:参数化查询(对照)
    public String findByIdSafe(String uid) throws Exception {
        PreparedStatement ps = con.prepareStatement("SELECT * FROM users WHERE id = ?");
        ps.setString(1, uid);
        return ps.executeQuery().toString();
    }

    // 隐患:SQL 字符串拼接(CA-JAVA-SQLI-001)
    public String findByIdLegacy(String uid) throws Exception {
        Statement st = con.createStatement();
        String sql = "SELECT * FROM users WHERE id = " + uid;
        return st.executeQuery(sql).toString();
    }

    // 安全实现:AES-GCM 强加密(对照)
    private Cipher strongCipher() throws Exception {
        return Cipher.getInstance("AES/GCM/NoPadding");
    }

    // 隐患:DES/ECB 弱加密(CA-JAVA-CRYPTO-002)
    private Cipher weakCipher() throws Exception {
        return Cipher.getInstance("DES/ECB/PKCS5Padding");
    }
}
