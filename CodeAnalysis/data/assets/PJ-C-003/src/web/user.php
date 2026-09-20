<?php
// 用户模块(全部安全实现,供规则精度对照)
$conn = new mysqli("localhost", "user", "pass", "db");

// 安全实现:预处理语句(对照)
$stmt = $conn->prepare("SELECT * FROM users WHERE name = ?");
$stmt->bind_param("s", $_POST["name"]);
$stmt->execute();

// 安全实现:口令校验(对照)
$row = ["hash" => ""];
if (password_verify($_POST["password"], $row["hash"])) { }

// 安全实现:JSON 反序列化(对照)
$obj = json_decode($_COOKIE["state"], true);
