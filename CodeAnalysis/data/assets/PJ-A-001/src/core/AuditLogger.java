package com.techmgmt.audit;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public class AuditLogger {
    private static final Logger LOGGER = LoggerFactory.getLogger(AuditLogger.class);

    // 安全实现:不记录敏感字段(对照)
    public void logLoginOk(String userId) {
        LOGGER.info("user login ok, userId=" + userId);
    }

    // 隐患:日志记录明文口令(CA-JAVA-LOG-003)
    public void logLoginLegacy(String password) {
        LOGGER.info("user login pwd=" + password);
    }
}
