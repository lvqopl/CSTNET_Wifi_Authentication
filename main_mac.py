import logging
import os
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
CHECK_INTERVAL_SECONDS = int(os.getenv("PORTAL_CHECK_INTERVAL", "5"))
TARGET_WIFI_SSID = os.getenv("PORTAL_WIFI_SSID", "wifi_name")
PORTAL_URL = os.getenv("PORTAL_URL", "http://10.10.10.9")
INTERNET_TEST_URL = os.getenv("PORTAL_TEST_URL", "https://www.baidu.com")
REQUEST_TIMEOUT_SECONDS = int(os.getenv("PORTAL_REQUEST_TIMEOUT", "5"))
SELENIUM_TIMEOUT_SECONDS = int(os.getenv("PORTAL_SELENIUM_TIMEOUT", "15"))
LOG_PATH = os.getenv(
    "PORTAL_LOG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "wifi_portal_runner_mac.log"),
)
CHROMEDRIVER_PATH = os.getenv("CHROMEDRIVER_PATH")
CHROME_BINARY_PATH = os.getenv("CHROME_BINARY_PATH")
USERNAME_ENV = "PORTAL_USERNAME"
PASSWORD_ENV = "PORTAL_PASSWORD"
PORTAL_HEADLESS = os.getenv("PORTAL_HEADLESS", "true").strip().lower() in {"1", "true", "yes", "on"}
FAST_DNS_HOST = os.getenv("PORTAL_FAST_DNS_HOST", "223.5.5.5")
FAST_DNS_PORT = int(os.getenv("PORTAL_FAST_DNS_PORT", "53"))
CONNECT_TIMEOUT_MS = int(os.getenv("PORTAL_CONNECT_TIMEOUT_MS", "800"))


def setup_logging() -> None:
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    logging.info("日志初始化完成，输出路径：%s", LOG_PATH)


def _run_cmd(cmd: list[str]) -> Optional[str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return result.stdout
    except Exception as exc:
        logging.debug("执行命令失败 %s：%s", cmd, exc)
        return None


def get_current_ssid() -> Optional[str]:
    """macOS 获取当前 WiFi SSID。

    优先使用 airport -I；失败则回退 networksetup -getairportnetwork。
    """
    # 1) 尝试 airport -I（可读性更好）
    airport_path = "/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport"
    out = _run_cmd([airport_path, "-I"])
    if out:
        for raw in out.splitlines():
            line = raw.strip()
            # 形如：SSID: your_ssid
            if line.lower().startswith("ssid:"):
                ssid = line.split(":", 1)[1].strip()
                logging.debug("airport 检测到 SSID：%s", ssid)
                return ssid or None

    # 2) 回退 networksetup -getairportnetwork en0/en1
    for iface in ("en0", "en1", "en2"):
        out2 = _run_cmd(["networksetup", "-getairportnetwork", iface])
        if out2:
            # 形如：Current Wi-Fi Network: your_ssid
            if ":" in out2:
                ssid = out2.split(":", 1)[1].strip()
                logging.debug("networksetup(%s) 检测到 SSID：%s", iface, ssid)
                if ssid and ssid.lower() != "no network":
                    return ssid
    logging.debug("未从 airport/networksetup 获取到 SSID。")
    return None


def has_internet_connectivity() -> bool:
    try:
        response = requests.get(INTERNET_TEST_URL, timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=False)
        logging.debug("连通性检测返回状态码：%s", response.status_code)
        return response.status_code < 400
    except requests.RequestException as exc:
        logging.info("网络连通性检测失败：%s", exc)
        return False


def has_quick_connectivity() -> bool:
    try:
        with socket.create_connection((FAST_DNS_HOST, FAST_DNS_PORT), CONNECT_TIMEOUT_MS / 1000.0):
            return True
    except OSError:
        return False


def is_online() -> bool:
    return has_quick_connectivity() or has_internet_connectivity()


def create_webdriver() -> webdriver.Chrome:
    chrome_options = ChromeOptions()
    try:
        chrome_options.page_load_strategy = "eager"
    except Exception:
        pass

    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--log-level=2")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-logging"])

    if CHROME_BINARY_PATH:
        chrome_options.binary_location = CHROME_BINARY_PATH
        logging.info("使用指定浏览器：%s", CHROME_BINARY_PATH)

    if PORTAL_HEADLESS:
        chrome_options.add_argument("--headless=new")
        logging.info("以无头模式启动 Chrome/Chromium。")
    else:
        logging.info("以可见模式启动 Chrome/Chromium。")

    prefs = {
        "profile.managed_default_content_settings.images": 2,
        "profile.default_content_setting_values.notifications": 2,
        "profile.default_content_setting_values.popups": 2,
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
    }
    chrome_options.add_experimental_option("prefs", prefs)

    if CHROMEDRIVER_PATH:
        logging.info("使用指定 chromedriver：%s", CHROMEDRIVER_PATH)
        service = ChromeService(CHROMEDRIVER_PATH)
    else:
        logging.info("使用系统/缓存中的 chromedriver。")
        service = ChromeService()

    return webdriver.Chrome(service=service, options=chrome_options)


def try_click(driver: webdriver.Chrome, xpath: str) -> bool:
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
    try:
        tag = element.tag_name.lower()
    except Exception:
        return None

    if tag in {"input", "textarea"}:
        return element

    try:
        descendants = element.find_elements(By.TAG_NAME, "input")
        if descendants:
            return descendants[0]
    except Exception:
        pass

    try:
        sib = element.find_element(By.XPATH, "following-sibling::input[1]")
        return sib
    except Exception:
        pass

    try:
        psib = element.find_element(By.XPATH, "preceding-sibling::input[1]")
        return psib
    except Exception:
        pass

    try:
        parent = element.find_element(By.XPATH, "..")
        maybe = parent.find_elements(By.XPATH, ".//input")
        if maybe:
            return maybe[0]
    except Exception:
        pass

    return None


def fill_field(driver: webdriver.Chrome, xpath: str, value: str) -> bool:
    try:
        element = WebDriverWait(driver, min(SELENIUM_TIMEOUT_SECONDS, 1)).until(
            EC.presence_of_element_located((By.XPATH, xpath))
        )

        target = _locate_nearby_input(element)

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

        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", target)
        except Exception:
            pass
        try:
            target.click()
        except Exception:
            pass

        try:
            target.clear()
        except Exception:
            pass

        try:
            target.send_keys(value)
            current_val = target.get_attribute("value")
            if (current_val or "").strip() == value:
                logging.info("已填充字段（send_keys）：%s", xpath)
                return True
        except Exception as exc:
            logging.debug("send_keys 异常，将尝试 JS 方式：%s", exc)

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
    logout_xpath = "/html/body/div[1]/div[2]/ul/li[2]/span"

    for i in range(max(1, retries)):
        hover_to_reveal(driver, logout_xpath)

        if try_click(driver, logout_xpath):
            logging.info("第 %s 次尝试：已触发注销。", i + 1)
            time.sleep(0.2)
            return True

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
    try:
        driver.execute_script("window.open(arguments[0], '_blank');", PORTAL_URL)
        driver.switch_to.window(driver.window_handles[-1])
        logging.info("已在新标签页打开门户：%s", PORTAL_URL)
    except WebDriverException as exc:
        logging.warning("打开新标签失败，退回到当前标签刷新：%s", exc)
        driver.get(PORTAL_URL)


def wait_for_login_form(driver: webdriver.Chrome, timeout_s: int = 8) -> bool:
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
    return wait_for_login_form(driver, timeout_s=quick_timeout_s)


def is_logged_in(driver: webdriver.Chrome, quick_timeout_s: int = 1) -> bool:
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

        # 优先快速判断是否已登录
        if is_logged_in(driver, quick_timeout_s=1):
            logging.info("检测到已登录状态，开始注销。")
            attempt_logout(driver, retries=3)
            open_portal_fresh_tab(driver)
            if not wait_for_login_form(driver, timeout_s=6):
                logging.error("注销后未见登录页，放弃本次流程。")
                return
        else:
            # 若非已登录，再判断是否已在登录页
            if not is_login_form_present(driver, quick_timeout_s=1):
                open_portal_fresh_tab(driver)
                if not wait_for_login_form(driver, timeout_s=6):
                    attempt_logout(driver, retries=2)
                    open_portal_fresh_tab(driver)
                    if not wait_for_login_form(driver, timeout_s=6):
                        logging.error("未能进入登录页，放弃本次流程。")
                        return

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
    setup_logging()
    logging.info("前台模式启动(macOS)。目标 WiFi：%s，检测间隔：%s 秒，Headless=%s", TARGET_WIFI_SSID, CHECK_INTERVAL_SECONDS, PORTAL_HEADLESS)

    while True:
        try:
            ssid = get_current_ssid()
            if ssid == TARGET_WIFI_SSID:
                logging.debug("当前连接到目标 WiFi：%s", ssid)
                if is_online():
                    logging.info("网络连通性正常，无需操作。")
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
