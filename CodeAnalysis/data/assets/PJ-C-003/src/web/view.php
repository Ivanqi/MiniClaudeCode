<?php
// 视图模块:植入 XSS 与动态 include 两处隐患

// 安全实现:输出编码(对照)
echo "<div>" . htmlspecialchars($_GET["q"], ENT_QUOTES, "UTF-8") . "</div>";

// 隐患:反射型 XSS(CA-PHP-XSS-001)
echo "<div>" . $_GET["q"] . "</div>";

// 安全实现:固定视图路径(对照)
include __DIR__ . "/views/home.php";

// 隐患:动态 include(CA-PHP-INCLUDE-002)
include($_GET["page"] . ".php");
