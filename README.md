# CSTNET_Wifi_Authentication
科技网掉线自动认证

## 环境准备
- 安装 Python 3.10+（Windows/Mac 均可）
- 安装依赖：
```bash
pip install -r requirements.txt
```

## 配置 .env（与脚本同目录）
创建 `.env` 文件，示例：
```ini
# 必填
PORTAL_WIFI_SSID=wifi_name
PORTAL_URL=http://10.10.10.9
PORTAL_USERNAME=your_username
PORTAL_PASSWORD=your_password

# 可选（有默认值）
PORTAL_TEST_URL=https://www.baidu.com
PORTAL_CHECK_INTERVAL=5
PORTAL_REQUEST_TIMEOUT=5
PORTAL_SELENIUM_TIMEOUT=15
PORTAL_LOG_PATH=main.log

# 浏览器/驱动（强烈建议指定，避免无网时自动下载失败）
# 指定 chromedriver 的绝对路径
CHROMEDRIVER_PATH=C:/path/to/chromedriver.exe
# 指定 Chrome/Chromium 的可执行文件路径（当你使用 Chromium 与对应 chromedriver 时很有用）
# 例如 Chromium：C:/Users/you/AppData/Local/Chromium/Application/chrome.exe
# 或 Chrome：C:/Program Files/Google/Chrome/Application/chrome.exe
CHROME_BINARY_PATH=C:/path/to/chrome_or_chromium.exe

# 是否无头模式（true/false）
PORTAL_HEADLESS=true
```

## 运行（前台常驻脚本）
- 脚本：`main.py`
- 行为：每隔 `PORTAL_CHECK_INTERVAL` 秒检测当前 WiFi SSID；只有当连接到 `PORTAL_WIFI_SSID` 且外网不可达时，自动打开门户页面，必要时先“悬停显示+注销”，再在新标签页进入登录页，填入用户名/密码并点击登录。

启动命令：
```bash
python main.py
```
日志位置：`PORTAL_LOG_PATH`（默认 `wifi_portal_runner.log`）

## 注销/登录逻辑说明
- 登录态判断：
  - 若检测到登录表单（两个输入区域），直接登录。
  - 否则尝试判定已登录：先悬停显示隐藏菜单，再查找“注销”按钮。
- 注销实现：
  - 通过 ActionChains 悬停 + JS 触发 mouseover/mousemove/mouseenter，并强制显示样式；
  - 优先常规点击，失败回退 JS `arguments[0].click()`。
- 登录实现：
  - 在新标签页打开 `PORTAL_URL`，定位输入框并填充（多策略定位），点击登录按钮；
  - 之后快速轮询外网连通性（≤10 秒）。

## 浏览器与驱动（重要）
- 强烈建议在 `.env` 指定以下两项，避免在无网情况下自动下载驱动失败：
  - `CHROMEDRIVER_PATH`：指向与你的浏览器主版本一致的 chromedriver。
  - `CHROME_BINARY_PATH`：如使用 Chromium，请显式指向它；或指向 Chrome 安装路径。
- 版本匹配：Chrome/Chromium 主版本号必须与 chromedriver 主版本号一致（例如 136 对 136）。
- 无头模式：`PORTAL_HEADLESS=true`；若遇到兼容性问题，可改为 `false` 先观察页面行为。

## 常见问题排查
- 报错“chrome not reachable / session not created”
  - 多为驱动与浏览器版本不匹配，或无网时无法自动下载驱动。
  - 指定 `CHROMEDRIVER_PATH` 与 `CHROME_BINARY_PATH`，确保主版本一致；结束残留 `chrome.exe/chromedriver.exe` 后重试。
- 填充失败/无输入
  - 已增强输入定位与 JS 回退；请确认 Xpath 未变化，或适当增大 `PORTAL_SELENIUM_TIMEOUT`。
- 注销按钮是隐藏的
  - 已加入“悬停显示+JS 强制显示+JS 点击兜底”的策略；若站点结构变动，请提供新的 Xpath。

## macOS 说明
- 脚本主体可直接运行；不好获取 SSID 
  - 直接设置wifi名称
- 浏览器/驱动：安装 macOS 版 Chrome/Chromium 与对应 chromedriver，并设置上述两项路径。


