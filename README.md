# chatgptregister

基于 [Regert888/gpt-auto-register](https://github.com/Regert888/gpt-auto-register) 的修改版。保留原项目的 [AGPL-3.0 许可证](LICENSE)；原项目说明见 [README.upstream.md](README.upstream.md)。

当前版本包含：

- 默认获取 AT、ST，不获取 RT；RT 和 2FA 均可独立开关，2FA 默认开启。
- Outlook 的 Microsoft 取 Token、Graph、IMAP 均使用本次注册的 HTTP / SOCKS5 代理；短暂网络错误有限重试，代理失败不回退直连。
- 完整注册 JSON 保存、查看和导出。
- 使用 `accounts` 格式推送账号池，携带已保存的设备、Cookie、CSRF、密码、2FA 密钥和注册日志；不发送 RT、ID Token。
- 批量冷却以分钟设置，支持小数和 0，不再限制为 120 秒。
- 停止时等待当前任务收尾，处理异常退出和状态通知顺序。
- 认证错误诊断及设备标识一致性修正。遇到浏览器安全验证时会停止并报告错误。

## Windows 使用

1. 安装 64 位 Python 3.11、3.12 或 3.13，以及 Node.js 22/24。
2. 点击本仓库 **Code → Download ZIP**，完整解压到可写目录，如 `D:\chatgptregister`。
3. 双击 `一键启动.bat` 或 `start_windows.bat`。首次启动会创建虚拟环境、安装依赖。
4. 浏览器打开 `http://127.0.0.1:8765/`。

前端已编译，不需要自行安装前端依赖。邮箱代理功能需要新增的 PySocks 依赖，升级请使用完整程序，一键启动会自动安装；HTTP 代理需允许 CONNECT 到邮箱 993 端口。完整导入、配置、数据迁移和使用步骤见 [Windows 使用说明](WINDOWS_README.txt)。

也可以通过 Git 下载：

```sh
git clone https://github.com/Huitom666/chatgptregister.git
cd chatgptregister
```

## 本地数据

仓库不包含测试邮箱、账号凭据、管理员密钥、数据库或运行日志。首次启动需要导入自己的邮箱和配置；账号池推送需使用接收端的系统管理员密钥。

升级前先停止任务、退出程序并备份 `webui/webui.db`。迁移时保留数据库，不要提交到 Git；启动环境 `.venv` 应在目标电脑重新创建。
