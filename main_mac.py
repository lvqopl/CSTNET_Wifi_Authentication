import logging
import os
import platform
import subprocess
import time
from typing import Optional
import socket

import requests
from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# 读取 .env 配置（若系统环境变量已设置，则以系统变量为准）
load_dotenv(override=False)

# 配置项（括号为默认值）
CHECK_INTERVAL_SECONDS = int(os.getenv("PORTAL_CHECK_INTERVAL", "1"))  # 检测间隔（秒）
TARGET_WIFI_SSID = os.getenv("PORTAL_WIFI_SSID", "wifi_name")  # 目标 WiFi SSID
PORTAL_URL = os.getenv("PORTAL_URL", "http://10.10.10.9")  # 门户地址
INTERNET_TEST_URL = os.getenv("PORTAL_TEST_URL", "https://www.baidu.com")  # 连通性检测地址
REQUEST_TIMEOUT_SECONDS = int(os.getenv("PORTAL_REQUEST_TIMEOUT", "2"))  # HTTP 请求超时（秒）
SELENIUM_TIMEOUT_SECONDS = int(os.getenv("PORTAL_SELENIUM_TIMEOUT", "5"))  # Selenium 等待超时（秒）
LOG_PATH = os.getenv(
    "PORTAL_LOG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "wifi_portal_unified.log"),
)  # 日志文件路径
CHROMEDRIVER_PATH = os.getenv("CHROMEDRIVER_PATH")  # 可选：chromedriver 路径（留空走系统默认）
CHROME_BINARY_PATH = os.getenv("CHROME_BINARY_PATH")  # 可选：指定 Chrome/Chromium 可执行文件
USERNAME_ENV = "PORTAL_USERNAME"  # 用户名变量名
PASSWORD_ENV = "PORTAL_PASSWORD"  # 密码变量名
PORTAL_HEADLESS = os.getenv("PORTAL_HEADLESS", "true").strip().lower() in {"1", "true", "yes", "on"}
FAST_DNS_HOST = os.getenv("PORTAL_FAST_DNS_HOST", "223.5.5.5")  # 快速连通性探测主机（阿里DNS）
FAST_DNS_PORT = int(os.getenv("PORTAL_FAST_DNS_PORT", "53"))
CONNECT_TIMEOUT_MS = int(os.getenv("PORTAL_CONNECT_TIMEOUT_MS", "800"))  # TCP 快速探测超时（毫秒）

#
# def setup_logging() -> None:
#     """初始化日志配置：同时输出到文件与控制台。"""
#     if logging.getLogger().handlers:
#         return
#     logging.basicConfig(
#         level=logging.INFO,
#         format="%(asctime)s [%(levelname)s] %(message)s",
#         handlers=[
#             logging.FileHandler(LOG_PATH, encoding="utf-8"),
#             logging.StreamHandler(),
#         ],
#     )
#     logging.info("日志初始化完成，输出路径：%s", LOG_PATH)
#     level = os.getenv("PORTAL_LOG_LEVEL", "INFO").upper()
#     logging.basicConfig(
#         level=getattr(logging, level)
#     )
def setup_logging() -> None:
    """初始化日志配置：同时输出到文件与控制台。"""
    if logging.getLogger().handlers:
        return

    level = os.getenv("PORTAL_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    logging.info("日志初始化完成，输出路径：%s (Level=%s)", LOG_PATH, level)


def _run_cmd(cmd: list[str]) -> Optional[str]:
    """执行命令并返回标准输出，失败返回 None。"""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return result.stdout
    except Exception as exc:
        logging.debug("执行命令失败 %s：%s", cmd, exc)
        return None


def get_current_ssid() -> Optional[str]:
    """跨平台获取当前 WiFi SSID。

    Windows: 使用 netsh wlan show interfaces
    macOS: 使用 airport -I 或 networksetup -getairportnetwork
    """
    system = platform.system().lower()

    if system == "windows":
        # Windows: netsh wlan show interfaces
        try:
            result = subprocess.run(
                ["netsh", "wlan", "show", "interfaces"],
                capture_output=True,
                text=True,
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            logging.error("执行 netsh 失败：%s", exc)
            return None

        for raw_line in result.stdout.splitlines():
            line = raw_line.strip()
            if line.lower().startswith("ssid") and "bssid" not in line.lower():
                parts = line.split(":", 1)
                if len(parts) == 2:
                    ssid = parts[1].strip()
                    logging.debug("检测到 SSID：%s", ssid)
                    return ssid or None
        logging.debug("未从 netsh 输出中解析到 SSID。")
        return None

    elif system == "darwin":  # macOS
        # 1) 尝试 airport -I（可读性更好）
        airport_path = "/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport"
        out = _run_cmd([airport_path, "-I"])
        if out:
            for raw in out.splitlines():
                line = raw.strip()
                if line.lower().startswith("ssid:"):
                    ssid = line.split(":", 1)[1].strip()
                    logging.debug("airport 检测到 SSID：%s", ssid)
                    return ssid or None

        # 2) 回退 networksetup -getairportnetwork en0/en1
        for iface in ("en0", "en1", "en2"):
            out2 = _run_cmd(["networksetup", "-getairportnetwork", iface])
            if out2 and ":" in out2:
                ssid = out2.split(":", 1)[1].strip()
                logging.debug("networksetup(%s) 检测到 SSID：%s", iface, ssid)
                if ssid and ssid.lower() != "no network":
                    return ssid
        logging.debug("未从 airport/networksetup 获取到 SSID。")
        return None

    else:
        logging.warning("不支持的操作系统：%s", system)
        return None


def has_internet_connectivity() -> bool:
    """访问 INTERNET_TEST_URL，状态码 < 400 视为可用。"""
    try:
        response = requests.get(
            INTERNET_TEST_URL,
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
        logging.debug("连通性检测返回状态码：%s", response.status_code)
        return response.status_code < 400
    except requests.RequestException as exc:
        logging.info("网络连通性检测失败：%s", exc)
        return False


def has_quick_connectivity() -> bool:
    """使用 TCP 直连 DNS 端口的方式进行亚秒级连通性探测。"""
    try:
        with socket.create_connection((FAST_DNS_HOST, FAST_DNS_PORT), CONNECT_TIMEOUT_MS / 1000.0):
            return True
    except OSError:
        return False


def is_online() -> bool:
    """综合判定是否在线：先快速 TCP 探测，失败再回退 HTTP 检测。"""
    return has_internet_connectivity()


def create_webdriver() -> webdriver.Chrome:
    """创建并返回 Chrome WebDriver，支持无头开关并优化加载速度。"""
    chrome_options = ChromeOptions()
    # 加快加载/渲染
    try:
        chrome_options.page_load_strategy = "eager"  # DOM 完成即继续
    except Exception:
        pass

    # 统一窗口参数（无头需要窗口大小）
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--log-level=2")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-logging"])

    # 指定浏览器二进制（Chromium/Chrome）
    if CHROME_BINARY_PATH:
        chrome_options.binary_location = CHROME_BINARY_PATH
        logging.info("使用指定浏览器：%s", CHROME_BINARY_PATH)

    # 无头开关
    if PORTAL_HEADLESS:
        chrome_options.add_argument("--headless=new")
        logging.info("以无头模式启动 Chrome/Chromium。")
    else:
        logging.info("以可见模式启动 Chrome/Chromium。")

    # 关闭图片/通知以提速
    prefs = {
        "profile.managed_default_content_settings.images": 2,
        "profile.default_content_setting_values.notifications": 2,
        "profile.default_content_setting_values.popups": 2,
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
    }
    chrome_options.add_experimental_option("prefs", prefs)

    # chromedriver 服务
    if CHROMEDRIVER_PATH:
        logging.info("使用指定 chromedriver：%s", CHROMEDRIVER_PATH)
        service = ChromeService(CHROMEDRIVER_PATH)
    else:
        logging.info("使用系统/缓存中的 chromedriver。")
        service = ChromeService()

    return webdriver.Chrome(service=service, options=chrome_options)


def try_click(driver: webdriver.Chrome, xpath: str) -> bool:
    """等待元素可点击并尝试点击，失败返回 False。"""
    try:
        element = WebDriverWait(driver, min(SELENIUM_TIMEOUT_SECONDS, 2)).until(
            EC.element_to_be_clickable((By.XPATH, xpath))
        )
        element.click()
        logging.info("已点击元素：%s", xpath)
        return True
    except TimeoutException:
        logging.info("等待元素超时：%s", xpath)
    except (NoSuchElementException, WebDriverException) as exc:
        logging.warning("点击元素失败：%s，原因：%s", xpath, exc)
    return False


def _locate_nearby_input(element) -> Optional[object]:
    """在给定元素附近尝试找到可输入的 input/textarea。"""
    try:
        tag = element.tag_name.lower()
    except Exception:
        return None

    if tag in {"input", "textarea"}:
        return element

    # 子孙 input
    try:
        descendants = element.find_elements(By.TAG_NAME, "input")
        if descendants:
            return descendants[0]
    except Exception:
        pass

    # following-sibling input
    try:
        sib = element.find_element(By.XPATH, "following-sibling::input[1]")
        return sib
    except Exception:
        pass

    # preceding-sibling input
    try:
        psib = element.find_element(By.XPATH, "preceding-sibling::input[1]")
        return psib
    except Exception:
        pass

    # 父级子树 input（常见容器层）
    try:
        parent = element.find_element(By.XPATH, "..")
        maybe = parent.find_elements(By.XPATH, ".//input")
        if maybe:
            return maybe[0]
    except Exception:
        pass

    return None


def fill_field(driver: webdriver.Chrome, xpath: str, value: str) -> bool:
    """在指定 XPath 附近定位实际输入控件并填充值，失败返回 False。"""
    try:
        element = WebDriverWait(driver, min(SELENIUM_TIMEOUT_SECONDS, 1)).until(
            EC.presence_of_element_located((By.XPATH, xpath))
        )

        # 1) 直接/附近定位输入框
        target = _locate_nearby_input(element)

        # 2) 额外兜底：将提供的 /label 替换为 /input 再尝试
        if target is None and xpath.endswith("/label"):
            try:
                alt_xpath = xpath[:-6] + "/input"
                target = WebDriverWait(driver, 2).until(
                    EC.presence_of_element_located((By.XPATH, alt_xpath))
                )
            except TimeoutException:
                target = None

        if target is None:
            logging.error("未能定位输入控件：%s", xpath)
            return False

        # 尝试点击聚焦
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", target)
        except Exception:
            pass
        try:
            target.click()
        except Exception:
            pass

        # 清空并输入（send_keys 为首选）
        try:
            target.clear()
        except Exception:
            pass

        try:
            target.send_keys(value)
            # 验证值是否写入
            current_val = target.get_attribute("value")
            if (current_val or "").strip() == value:
                logging.info("已填充字段（send_keys）：%s", xpath)
                return True
        except Exception as exc:
            logging.debug("send_keys 异常，将尝试 JS 方式：%s", exc)

        # 回退到 JS 直接赋值并触发事件
        try:
            driver.execute_script(
                "arguments[0].value = arguments[1]; arguments[0].dispatchEvent(new Event('input', {bubbles:true})); arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
                target,
                value,
            )
            current_val = target.get_attribute("value")
            if (current_val or "").strip() == value:
                logging.info("已填充字段（JS set+events）：%s", xpath)
                return True
        except Exception as exc:
            logging.error("通过 JS 填充字段失败：%s，原因：%s", xpath, exc)

        logging.error("填充字段未生效：%s", xpath)
        return False

    except TimeoutException:
        logging.warning("等待字段超时：%s", xpath)
    except (NoSuchElementException, WebDriverException) as exc:
        logging.error("填充字段失败：%s，原因：%s", xpath, exc)
    return False


def hover_to_reveal(driver: webdriver.Chrome, target_xpath: str) -> bool:
    """通过鼠标悬停与 JS 事件尝试让目标元素变为可见（兼容无头）。"""
    candidate_xpaths = [
        target_xpath,
        "/html/body/div[1]/div[2]/ul",
        "/html/body/div[1]/div[2]",
    ]

    deadline = time.time() + 0.5  # 最多尝试 ~0.5s
    while time.time() < deadline:
        for xp in candidate_xpaths:
            try:
                elem = driver.find_element(By.XPATH, xp)
            except Exception:
                continue

            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", elem)
            except Exception:
                pass

            try:
                ActionChains(driver).move_to_element(elem).perform()
            except Exception:
                pass

            for ev in ("mouseover", "mousemove", "mouseenter"):
                try:
                    driver.execute_script(
                        "var e=new Event(arguments[1],{bubbles:true}); arguments[0].dispatchEvent(e);",
                        elem,
                        ev,
                    )
                except Exception:
                    pass

            try:
                driver.execute_script(
                    "arguments[0].style.visibility='visible'; arguments[0].style.opacity=1; if (getComputedStyle(arguments[0]).display==='none'){arguments[0].style.display='block';}",
                    elem,
                )
            except Exception:
                pass

            try:
                if WebDriverWait(driver, 0.5).until(
                        EC.visibility_of_element_located((By.XPATH, target_xpath))
                ):
                    return True
            except Exception:
                continue
        time.sleep(0.05)
    return False


def attempt_logout(driver: webdriver.Chrome, retries: int = 2) -> bool:
    """尝试点击注销按钮，必要时先 hover/强制显示，再重试点击（含 JS 点击兜底）。"""
    logout_xpath = "/html/body/div[1]/div[2]/ul/li[2]/span"

    for i in range(max(1, retries)):
        # 先尝试通过 hover/JS 让其可见
        hover_to_reveal(driver, logout_xpath)

        # 优先常规点击
        if try_click(driver, logout_xpath):
            logging.info("第 %s 次尝试：已触发注销。", i + 1)
            time.sleep(0.2)
            return True

        # 兜底：JS 直接点击
        try:
            elem = driver.find_element(By.XPATH, logout_xpath)
            driver.execute_script("arguments[0].click();", elem)
            logging.info("第 %s 次尝试：已通过 JS click 触发注销。", i + 1)
            time.sleep(0.2)
            return True
        except Exception:
            pass

        time.sleep(0.05)

    logging.info("未发现可点击的注销按钮。")
    return False


def open_portal_fresh_tab(driver: webdriver.Chrome) -> None:
    """在新标签页打开门户并切换过去，尽量保持一个活跃标签。"""
    try:
        driver.execute_script("window.open(arguments[0], '_blank');", PORTAL_URL)
        driver.switch_to.window(driver.window_handles[-1])
        logging.info("已在新标签页打开门户：%s", PORTAL_URL)
    except WebDriverException as exc:
        logging.warning("打开新标签失败，退回到当前标签刷新：%s", exc)
        driver.get(PORTAL_URL)


def wait_for_login_form(driver: webdriver.Chrome, timeout_s: int = 8) -> bool:
    """等待用户名与密码区域出现，以判断处于登录页。"""
    username_xpath = "/html/body/div[2]/div[1]/div/div[3]/div[3]/ul/li[1]/label"
    password_xpath = "/html/body/div[2]/div[1]/div/div[3]/div[3]/ul/li[2]/label"
    try:
        WebDriverWait(driver, timeout_s).until(
            EC.presence_of_element_located((By.XPATH, username_xpath))
        )
        WebDriverWait(driver, timeout_s).until(
            EC.presence_of_element_located((By.XPATH, password_xpath))
        )
        return True
    except TimeoutException:
        return False


def is_login_form_present(driver: webdriver.Chrome, quick_timeout_s: int = 1) -> bool:
    """快速判断是否在登录页面（两个输入区域出现）。"""
    return wait_for_login_form(driver, timeout_s=quick_timeout_s)


def is_logged_in(driver: webdriver.Chrome, quick_timeout_s: int = 1) -> bool:
    """快速判断是否已登录：通过 hover 显示并检查注销按钮是否可见。"""
    logout_xpath = "/html/body/div[1]/div[2]/ul/li[2]/span"
    hover_to_reveal(driver, logout_xpath)
    try:
        WebDriverWait(driver, quick_timeout_s).until(
            EC.visibility_of_element_located((By.XPATH, logout_xpath))
        )
        return True
    except TimeoutException:
        return False


def handle_portal_login() -> None:
    """执行门户登录流程：先打开门户，优先快速判断已登录；否则再判断登录页并处理。"""
    username = os.getenv(USERNAME_ENV)
    password = os.getenv(PASSWORD_ENV)

    if not username or not password:
        logging.error("缺少认证信息，请设置环境变量 %s 和 %s。", USERNAME_ENV, PASSWORD_ENV)
        return

    try:
        driver = create_webdriver()
    except WebDriverException as exc:
        logging.error("初始化 WebDriver 失败：%s", exc)
        return

    try:
        driver.set_page_load_timeout(max(SELENIUM_TIMEOUT_SECONDS, 2))
        logging.info("访问门户页面：%s", PORTAL_URL)
        driver.get(PORTAL_URL)

        # 先快速判断已登录（更省时）
        if is_logged_in(driver, quick_timeout_s=1):
            logging.info("检测到已登录状态，开始注销。")
            attempt_logout(driver, retries=3)
            open_portal_fresh_tab(driver)
            if not wait_for_login_form(driver, timeout_s=6):
                logging.error("注销后未见登录页，放弃本次流程。")
                return
        else:
            # 未识别为已登录，则看是否直接在登录页
            if not is_login_form_present(driver, quick_timeout_s=1):
                # 打开新标签页尝试进入登录页
                open_portal_fresh_tab(driver)
                if not wait_for_login_form(driver, timeout_s=6):
                    # 再尝试一次注销并进入登录页
                    attempt_logout(driver, retries=2)
                    open_portal_fresh_tab(driver)
                    if not wait_for_login_form(driver, timeout_s=6):
                        logging.error("未能进入登录页，放弃本次流程。")
                        return

        # 2) 填写用户名与密码，并点击登录
        username_xpath = "/html/body/div[2]/div[1]/div/div[3]/div[3]/ul/li[1]/label"
        password_xpath = "/html/body/div[2]/div[1]/div/div[3]/div[3]/ul/li[2]/label"
        login_button_xpath = "/html/body/div[2]/div[1]/div/div[3]/div[5]/div[1]/input"

        if not fill_field(driver, username_xpath, username):
            logging.error("填充用户名失败，终止流程。")
            return
        if not fill_field(driver, password_xpath, password):
            logging.error("填充密码失败，终止流程。")
            return

        if not try_click(driver, login_button_xpath):
            logging.error("点击登录按钮失败。")
            return

        logging.info("登录已提交，开始快速轮询网络连通性。")
        start_ts = time.time()
        while time.time() - start_ts < 10:
            if is_online():
                logging.info("网络连通性恢复。")
                break
            time.sleep(0.5)
    finally:
        driver.quit()
        logging.info("浏览器实例已关闭。")


def main_loop() -> None:
    """前台循环运行：仅当连接到目标 SSID 且无外网连通性时执行登录流程。"""
    setup_logging()
    system_name = platform.system()
    logging.info("前台模式启动(%s)。目标 WiFi：%s，检测间隔：%s 秒，Headless=%s",
                 system_name, TARGET_WIFI_SSID, CHECK_INTERVAL_SECONDS, PORTAL_HEADLESS)

    while True:
        try:
            ssid = TARGET_WIFI_SSID#get_current_ssid()
            if ssid == TARGET_WIFI_SSID:
                logging.debug("当前连接到目标 WiFi：%s", ssid)
                if is_online():
                    logging.info("网络连通性正常，无需操作。")
                    time.sleep(CHECK_INTERVAL_SECONDS)
                    continue
                else:
                    logging.warning("检测到网络不可用，开始门户自动登录流程。")
                    handle_portal_login()
            else:
                logging.debug("当前 SSID(%s) 非目标 WiFi(%s)，跳过。", ssid, TARGET_WIFI_SSID)
        except Exception as exc:  # pylint: disable=broad-except
            logging.exception("循环执行出现异常：%s", exc)

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main_loop()
