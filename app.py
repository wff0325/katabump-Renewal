#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KataBump 服务器自动续期脚本(修复版)
原项目: https://github.com/wff0325/katabump-Renewal (存在结果误判/确认不可靠问题)

修复点:
  1. 续期结果以「Expiry 日期变化」为准，不再读页面第一条 alert
     (原版会把 Change type 区块的常驻警告当成续期结果)
  2. 模态框内为 ALTCHA 验证(auto="onsubmit" 自动解题)，点击 submit 后
     等待表单 POST 完成并校验到期时间，验证未过时自动重点
  3. 登录 Turnstile 用 patchright + 真实浏览器点击，过 CF 盾

用法:
    本地:  .env 写入 KATABUMP_EMAIL/KATABUMP_PASSWORD 后 python app.py
    Actions: 仓库 Secrets 配 KATABUMP_EMAIL / KATABUMP_PASSWORD / NODE_LINK / TG_*
"""

import os, re, sys, time, random

def _load_local_env():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and v and not os.environ.get(k):
                    os.environ[k] = v
    except Exception:
        pass

_load_local_env()

# ==================== CONFIG ====================
EMAIL        = os.environ.get('KATABUMP_EMAIL') or os.environ.get('EMAIL') or ""
PASSWORD     = os.environ.get('KATABUMP_PASSWORD') or os.environ.get('PASSWORD') or ""
SERVER_URL   = os.environ.get('SERVER_URL') or ""      # 可选: 固定服务器页, 留空自动发现
TG_BOT_TOKEN = os.environ.get('TG_BOT_TOKEN') or ""
TG_CHAT_ID   = os.environ.get('TG_CHAT_ID') or ""
IS_PROXY     = os.environ.get('IS_PROXY', 'false').lower() == 'true'
PROXY_SERVER = os.environ.get('PROXY_SERVER') or "http://127.0.0.1:1081"
BROWSER_CHANNEL = os.environ.get('BROWSER_CHANNEL') or ""
SECURITY_TIMEOUT = int(os.environ.get('SECURITY_TIMEOUT') or "240")

REQUESTS_PROXIES = {"http": PROXY_SERVER, "https": PROXY_SERVER} if IS_PROXY else None

BASE_URL = "https://dashboard.katabump.com"
LOGIN_URL = f"{BASE_URL}/auth/login"

try:
    from patchright.sync_api import sync_playwright
    USING_PATCHRIGHT = True
except ImportError:
    from playwright.sync_api import sync_playwright
    USING_PATCHRIGHT = False

import requests

# ==================== 常量 ====================
CF_IFRAME_SELECTOR = 'iframe[src*="challenges.cloudflare.com"]'
SECURITY_TITLE_HINTS = ('security verification', 'just a moment', 'attention required',
                        'checking your browser', 'one more step')
# 服务器页 "Expiry\n2026-10-20"
EXPIRY_RE = re.compile(r'Expiry\s*\n?\s*(\d{4}-\d{2}-\d{2})', re.IGNORECASE)
# 未到续期时间提示中的可续期日期, 如 "as of 19 October"
RENEW_DATE_RE = re.compile(r'as of ([^\(\n]+)', re.IGNORECASE)

def log(message):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)

def get_current_ip():
    try:
        resp = requests.get("https://api.ip.sb/ip", proxies=REQUESTS_PROXIES, timeout=15)
        if resp.status_code == 200:
            return resp.text.strip()
    except Exception as e:
        log(f"获取出口IP失败: {e}")
    return "获取失败"

def send_telegram_notification(status, old_expiry, new_expiry, current_ip="未知"):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log("Telegram 未配置，跳过通知")
        return
    local_time = time.gmtime(time.time() + 8 * 3600)
    now = time.strftime("%Y-%m-%d %H:%M:%S", local_time)
    if '@' in EMAIL:
        name, domain = EMAIL.split('@', 1)
        masked = f"{name[:2]}****{name[-2:]}@{domain}" if len(name) > 4 else f"{name}@{domain}"
    else:
        masked = EMAIL[:2] + '****'
    text = (
        f"KataBump 续期通知\n\n"
        f"{status}\n"
        f"账号: {masked}\n"
        f"续期前到期: {old_expiry}\n"
        f"续期后到期: {new_expiry}\n"
        f"IP: {current_ip}\n"
        f"时间: {now}"
    )
    try:
        resp = requests.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                             json={"chat_id": TG_CHAT_ID, "text": text},
                             timeout=10, proxies=REQUESTS_PROXIES)
        if resp.status_code == 200:
            log("Telegram 通知发送成功")
        else:
            log(f"Telegram 通知失败: {resp.text}")
    except Exception as e:
        log(f"Telegram 通知异常: {e}")


# ==================== Cloudflare 过盾 ====================
def _has_interstitial_iframe(page):
    try:
        for h in page.query_selector_all(CF_IFRAME_SELECTOR):
            if '/turnstile/' not in (h.get_attribute('src') or ''):
                return True
    except Exception:
        pass
    return False

def _is_security_check_page(page):
    try:
        title = (page.title() or '').lower()
        if any(h in title for h in SECURITY_TITLE_HINTS):
            return True
    except Exception:
        return True
    try:
        return page.evaluate("""() => {
            const t = document.body ? document.body.innerText.slice(0,5000).toLowerCase() : '';
            return t.includes('verify you are human') || t.includes('checking your browser')
                || t.includes('security verification');
        }""")
    except Exception:
        return True

def _find_visible_cf_frames(page):
    result = []
    try:
        for h in page.query_selector_all(CF_IFRAME_SELECTOR):
            try:
                box = h.bounding_box()
                if box and box['width'] > 10 and box['height'] > 10:
                    result.append(h)
            except Exception:
                continue
    except Exception:
        pass
    return result

def _click_cf_checkbox(page, frame_el=None):
    for frame in page.frames:
        if 'challenges.cloudflare.com' not in (frame.url or ''):
            continue
        try:
            frame.locator('input[type="checkbox"]').first.click(timeout=2500)
            return True
        except Exception:
            continue
    if frame_el is None:
        return False
    try:
        frame_el.scroll_into_view_if_needed(timeout=3000)
        box = frame_el.bounding_box()
        if not box:
            return False
        x = box['x'] + random.uniform(18, 34)
        y = box['y'] + box['height'] / 2 + random.uniform(-4, 4)
        page.mouse.move(x - random.uniform(30, 60), y + random.uniform(-8, 8))
        time.sleep(random.uniform(0.15, 0.4))
        page.mouse.move(x, y)
        time.sleep(random.uniform(0.05, 0.2))
        page.mouse.down()
        time.sleep(random.uniform(0.04, 0.1))
        page.mouse.up()
        return True
    except Exception:
        return False

def _get_turnstile_token(page):
    try:
        return page.evaluate("""() => {
            if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
                let t = null;
                try { t = window.turnstile.getResponse(); } catch(e) {}
                if (t && t.length > 30) return t;
                for (let i = 0; i < 5; i++) {
                    try { t = window.turnstile.getResponse(String(i)); } catch(e) { t = null; }
                    if (t && t.length > 30) return t;
                }
            }
            const inp = document.querySelector('input[name="cf-turnstile-response"]');
            if (inp && inp.value && inp.value.length > 30) return inp.value;
            return null;
        }""")
    except Exception:
        return None

def handle_cloudflare(page, timeout=None):
    if timeout is None:
        timeout = SECURITY_TIMEOUT
    def active():
        return _has_interstitial_iframe(page) or _is_security_check_page(page)
    if not active():
        return True
    log("检测到 Cloudflare 安全验证...")
    time.sleep(3)
    start = time.time()
    last_click = 0
    clear = 0
    while time.time() - start < timeout:
        if not active():
            clear += 1
            if clear >= 2:
                return True
            time.sleep(1)
            continue
        clear = 0
        if time.time() - last_click > 6:
            frames = _find_visible_cf_frames(page)
            if frames:
                log("点击 Turnstile 验证...")
                if _click_cf_checkbox(page, frames[-1]):
                    last_click = time.time()
                    time.sleep(random.uniform(2, 4))
                    continue
        time.sleep(1)
    log("CF 验证超时")
    return False

def solve_turnstile(page, timeout=90):
    """登录页 Turnstile: 点击复选框直到 token 生成"""
    if _get_turnstile_token(page):
        return True
    frames = _find_visible_cf_frames(page)
    if not frames:
        time.sleep(2)
        if _get_turnstile_token(page) or not _find_visible_cf_frames(page):
            return True
    log("处理 Turnstile 人机验证...")
    start = time.time()
    last_click = 0
    clicks = 0
    while time.time() - start < timeout:
        if _get_turnstile_token(page):
            log("Turnstile 验证通过")
            return True
        frames = _find_visible_cf_frames(page)
        if not frames:
            break
        if time.time() - last_click > 5:
            log(f"点击 Turnstile 复选框 (第 {clicks + 1} 次)...")
            if _click_cf_checkbox(page, frames[-1]):
                clicks += 1
                last_click = time.time()
                time.sleep(random.uniform(2, 4))
                continue
        time.sleep(1)
    return bool(_get_turnstile_token(page))

# ==================== ALTCHA 验证(续期模态框) ====================
def _altcha_state(page):
    """返回模态框内 altcha-widget 的 data-state"""
    try:
        return page.evaluate("""() => {
            const w = document.querySelector('#renew-modal altcha-widget, .modal.show altcha-widget');
            return w ? (w.getAttribute('data-state') || '') : null;
        }""")
    except Exception:
        return None

def _altcha_verified(page):
    try:
        return page.evaluate("""() => {
            const w = document.querySelector('.modal.show altcha-widget, #renew-modal altcha-widget');
            if (w && (w.getAttribute('data-state') || '') === 'verified') return true;
            const inp = document.querySelector('.modal.show input[type="hidden"]');
            return !!(inp && inp.value && inp.value.length > 20);
        }""")
    except Exception:
        return False

def solve_altcha(page, timeout=60):
    """等待 ALTCHA 自动解题(auto="onsubmit" 点击提交后自动验证)；未过则手动点复选框"""
    start = time.time()
    if _altcha_verified(page):
        return True
    # auto="onsubmit": 提交时自动解题, 先等
    while time.time() - start < 8:
        if _altcha_verified(page):
            log("ALTCHA 自动验证通过")
            return True
        time.sleep(1)
    # 手动点复选框
    last_click = 0
    clicks = 0
    while time.time() - start < timeout:
        if _altcha_verified(page):
            log("ALTCHA 验证通过")
            return True
        if time.time() - last_click > 4:
            log(f"点击 ALTCHA 复选框 (第 {clicks + 1} 次)...")
            try:
                cb = page.locator('.modal.show altcha-widget input[type="checkbox"]').first
                cb.click(timeout=3000)
            except Exception:
                try:
                    page.locator('.modal.show altcha-widget label.altcha-label').first.click(timeout=3000)
                except Exception:
                    pass
            clicks += 1
            last_click = time.time()
            time.sleep(3)
            continue
        time.sleep(1)
    return _altcha_verified(page)


# ==================== 浏览器 ====================
def _detect_channel():
    if BROWSER_CHANNEL:
        return BROWSER_CHANNEL
    if sys.platform == 'win32':
        for ch, paths in {
            'msedge': [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"],
            'chrome': [r"C:\Program Files\Google\Chrome\Application\chrome.exe"],
        }.items():
            if any(os.path.exists(p) for p in paths):
                return ch
        return None
    import shutil
    if shutil.which("google-chrome") or shutil.which("google-chrome-stable"):
        return "chrome"
    if shutil.which("microsoft-edge"):
        return "msedge"
    return "chrome"

def open_browser(p):
    proxy_arg = {"server": PROXY_SERVER} if IS_PROXY else None
    channel = _detect_channel()
    if USING_PATCHRIGHT and channel:
        try:
            browser = p.chromium.launch(channel=channel, headless=False,
                                        args=['--disable-infobars'], proxy=proxy_arg)
            ctx = browser.new_context(viewport={'width': 1440, 'height': 900}, proxy=proxy_arg)
            log(f"启动反检测浏览器 (patchright + {channel})")
            return browser, ctx.new_page()
        except Exception as e:
            log(f"patchright 启动失败: {e}")
    browser = p.chromium.launch(headless=False,
        args=['--no-sandbox', '--disable-blink-features=AutomationControlled', '--disable-infobars'])
    ctx = browser.new_context(viewport={'width': 1440, 'height': 900},
                              proxy=proxy_arg)
    page = ctx.new_page()
    page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    return browser, page

# ==================== 登录 ====================
def login(page):
    if not EMAIL or not PASSWORD:
        log("缺少登录凭证 (KATABUMP_EMAIL/KATABUMP_PASSWORD)")
        return False
    try:
        log("打开登录页...")
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)
        time.sleep(3)

        # cookie 弹窗
        for txt in ("Accept", "accept", "Got it", "OK", "I agree"):
            try:
                btn = page.locator(f'button:has-text("{txt}")').first
                if btn.count() > 0:
                    btn.click(timeout=3000)
                    log(f"已关闭 Cookie 弹窗 ({txt})")
                    time.sleep(0.5)
                    break
            except Exception:
                continue

        log("填写账号密码...")
        email_sel = page.locator('input[type="email"], input[name="email"]').first
        pwd_sel = page.locator('input[type="password"], input[name="password"]').first
        email_sel.wait_for(state="visible", timeout=30000)
        email_sel.fill(EMAIL, timeout=10000)
        pwd_sel.fill(PASSWORD, timeout=10000)
        time.sleep(0.5)

        solve_turnstile(page, timeout=90)

        log("提交登录...")
        submitted = False
        for sel in ('button[type="submit"]', 'button:has-text("Sign in")',
                    'button:has-text("Login")', 'button:has-text("Log in")'):
            try:
                btn = page.locator(sel).first
                if btn.count() > 0:
                    btn.click(timeout=5000)
                    submitted = True
                    break
            except Exception:
                continue
        if not submitted:
            pwd_sel.press("Enter")
        time.sleep(5)
        handle_cloudflare(page)

        nav_start = time.time()
        navigated = False
        while time.time() - nav_start < 120:
            try:
                if '/dashboard' in page.url and '/auth/login' not in page.url:
                    navigated = True
                    break
                if _is_security_check_page(page) or _has_interstitial_iframe(page):
                    handle_cloudflare(page, timeout=60)
            except Exception:
                pass
            time.sleep(1)
        if not navigated:
            log(f"登录失败，仍停留在: {page.url}")
            page.screenshot(path="login_fail.png")
            return False
        log(f"登录成功: {page.url}")
        return True
    except Exception as e:
        log(f"登录异常: {e}")
        try:
            page.screenshot(path="login_fail.png")
        except Exception:
            pass
        return False

# ==================== 服务器页 ====================
def get_server_url(page):
    try:
        page.goto(f"{BASE_URL}/dashboard", wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)
        time.sleep(3)
        links = page.evaluate(
            "() => Array.from(document.querySelectorAll('a[href]')).map(a => a.getAttribute('href'))"
        )
        for href in links:
            if href and '/servers/edit' in href:
                url = href if href.startswith('http') else BASE_URL + href
                log(f"发现服务器: {url}")
                return url
        log("未找到服务器链接")
    except Exception as e:
        log(f"获取服务器链接异常: {e}")
    return None

def get_expiry(page):
    """读取服务器页 Expiry 日期 (YYYY-MM-DD)"""
    try:
        text = page.locator("body").inner_text()
        m = EXPIRY_RE.search(text)
        if m:
            return m.group(1).strip()
        # 兜底: Service information 区块
        m2 = re.search(r'Expiry\s*[:\n]\s*(\d{4}-\d{2}-\d{2})', text)
        if m2:
            return m2.group(1)
    except Exception as e:
        log(f"读取到期时间异常: {e}")
    return "未知"

def _visible_alerts(page):
    """页面【可见】的 alert 列表。
    页面存在隐藏的常驻警告(Change type)和FAQ文本, 必须只读可见提示, 否则误判。"""
    try:
        return page.evaluate("""() => {
            const out = [];
            document.querySelectorAll('.alert, [role="alert"]').forEach(a => {
                const r = a.getBoundingClientRect();
                if (r.width > 0 && r.height > 0) {
                    out.push({cls: a.className || '', text: (a.innerText||'').trim()});
                }
            });
            return out;
        }""")
    except Exception:
        return []

# ==================== 续期 ====================
def renew_server(page):
    """打开 Renew 模态框 -> ALTCHA -> 提交 -> 以 Expiry 变化判定结果。
    返回 (True成功/False失败/'NOT_TIME', 新Expiry)"""
    try:
        old_expiry = get_expiry(page)
        log(f"续期前到期: {old_expiry}")

        log("点击 Renew 打开模态框...")
        # 注意: 不能用 button:has-text("Renew") 兜底 —— 大小写不敏感会先匹配到
        # "Easy renewal" 手风琴按钮(点它只展开FAQ)。必须用 data-bs-target 精确定位。
        renew_btn = page.locator('button[data-bs-target="#renew-modal"]')
        if renew_btn.count() == 0:
            renew_btn = page.locator('button.btn.btn-outline-primary:has-text("Renew")')
        if renew_btn.count() == 0:
            log("未找到 Renew 按钮")
            page.screenshot(path="renew_no_button.png")
            return False, old_expiry
        renew_btn.first.scroll_into_view_if_needed()
        renew_btn.first.click(timeout=10000)
        time.sleep(3)

        if page.locator('#renew-modal.show').count() == 0:
            log("模态框未弹出")
            page.screenshot(path="renew_modal_failed.png")
            return False, old_expiry
        log("Renew 模态框已弹出")

        # 模态框内的 Renew 提交按钮(带 data-altcha-wired)
        submit = page.locator('#renew-modal button[type="submit"].btn-primary').first
        log("点击模态框 Renew 提交(ALTCHA 将自动解题)...")
        submit.click(timeout=10000)

        # ALTCHA 自动解题(auto="onsubmit"): 验证通过后表单才真正提交。
        # 点击提交 -> 等 ALTCHA 验证 -> 模态框关闭(表单提交/跳转)为止;
        # 若验证后模态框仍在, 需要再点一次提交。
        altcha_deadline = time.time() + 45
        while time.time() < altcha_deadline:
            time.sleep(2)
            if page.locator('#renew-modal.show').count() == 0:
                break
            if _altcha_verified(page):
                log("ALTCHA 已验证, 再次点击提交...")
                try:
                    page.locator('#renew-modal button[type="submit"].btn-primary').first.click(timeout=8000)
                except Exception:
                    pass
                time.sleep(4)

        # 兜底: 模态框仍未关(ALTCHA 未通过), 手动解题后再提交一次
        if page.locator('#renew-modal.show').count() > 0:
            log("ALTCHA 尚未通过, 尝试手动解题...")
            if solve_altcha(page, timeout=30):
                try:
                    page.locator('#renew-modal button[type="submit"].btn-primary').first.click(timeout=8000)
                    log("已再次提交")
                    time.sleep(5)
                except Exception:
                    pass

        # 等待结果: 以可见 alert + Expiry 变化双重判定
        # (只读可见 alert! 页面有隐藏的常驻警告和FAQ文本, 全文扫描会误判)
        log("等待续期结果...")
        start = time.time()
        last_msg = ""
        new_expiry = old_expiry
        while time.time() - start < 60:
            handle_cloudflare(page, timeout=10)
            time.sleep(2)
            for a in _visible_alerts(page):
                low = a['text'].lower()
                last_msg = a['text']
                if any(k in low for k in ("can't renew", "cannot renew", "as of",
                                          "already renewed", "not yet", "too early")):
                    m = RENEW_DATE_RE.search(a['text'])
                    if m:
                        log(f"未到续期时间(可续期日期: {m.group(1).strip()})")
                    else:
                        log(f"未到续期时间: {last_msg[:120]}")
                    page.screenshot(path="renew_not_yet.png")
                    return "NOT_TIME", old_expiry
                if any(k in low for k in ("renewed", "success", "extended")):
                    # 提示成功, 确认到期时间变化
                    for _ in range(10):
                        time.sleep(3)
                        new_expiry = get_expiry(page)
                        if new_expiry != old_expiry and new_expiry != "未知":
                            log(f"续期成功! 到期 {old_expiry} -> {new_expiry}")
                            return True, new_expiry
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=60000)
                        handle_cloudflare(page)
                        time.sleep(3)
                    except Exception:
                        pass
                    new_expiry = get_expiry(page)
                    if new_expiry != old_expiry and new_expiry != "未知":
                        log(f"续期成功! 到期 {old_expiry} -> {new_expiry}")
                        return True, new_expiry
                    log("提示成功但到期时间未变化")
                    page.screenshot(path="renew_no_change.png")
                    return False, new_expiry
            # 无 alert 但到期时间已变
            try:
                if '/api-client/renew' not in page.url:
                    new_expiry = get_expiry(page)
                    if new_expiry != old_expiry and new_expiry != "未知":
                        log(f"续期成功! 到期 {old_expiry} -> {new_expiry}")
                        return True, new_expiry
            except Exception:
                pass

        new_expiry = get_expiry(page)
        if new_expiry != old_expiry and new_expiry != "未知":
            log(f"续期成功! 到期 {old_expiry} -> {new_expiry}")
            return True, new_expiry
        log(f"未检测到明确结果(最后提示: {last_msg[:100]})")
        page.screenshot(path="renew_no_result.png")
        return False, new_expiry
    except Exception as e:
        log(f"续期异常: {e}")
        try:
            page.screenshot(path="renew_error.png")
        except Exception:
            pass
        return False, "未知"

# ==================== 主流程 ====================
def main():
    log(f"凭证检测: EMAIL={'已配置' if EMAIL else '未配置'}, PASSWORD={'已配置' if PASSWORD else '未配置'}")
    if not EMAIL or not PASSWORD:
        log("缺少登录凭证, 退出")
        sys.exit(1)

    server_url = SERVER_URL
    with sync_playwright() as p:
        browser = None
        try:
            if IS_PROXY:
                log(f"代理已启用: {PROXY_SERVER}")
            current_ip = get_current_ip()
            log(f"当前出口IP: {current_ip}")

            browser, page = open_browser(p)
            page.set_default_timeout(15000)

            if not login(page):
                sys.exit(1)

            if not server_url:
                server_url = get_server_url(page)
                if not server_url:
                    log("无法获取服务器地址, 退出")
                    sys.exit(1)

            page.goto(server_url, wait_until="domcontentloaded", timeout=60000)
            handle_cloudflare(page)
            time.sleep(3)
            old_expiry = get_expiry(page)
            log(f"当前到期: {old_expiry}")

            result, new_expiry = renew_server(page)

            if result == "NOT_TIME":
                status = "未到续期时间"
            elif result is True:
                status = "续期成功"
            else:
                status = "续期失败"

            send_telegram_notification(status, old_expiry, new_expiry, current_ip)
            sys.exit(0 if result is True or result == "NOT_TIME" else 1)
        except Exception as e:
            log(f"运行出错: {e}")
            sys.exit(1)
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass

if __name__ == "__main__":
    main()
