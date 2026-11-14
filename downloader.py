
import argparse
import html
import quopri
import random
import re
import string
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

BASE = "https://taptv.ru"
REG_URL = f"{BASE}/reg"

# Только символы, допустимые в URL: кириллица и пробелы рядом со ссылкой не захватываются
CONFIRM_RE = re.compile(r"https://taptv\.ru/confirm_email\?[A-Za-z0-9%_\-.~=&+]+")
PLAYLIST_RE = re.compile(r"https?://taptv\.space/playlist/[A-Za-z0-9]+/playlist\.m3u8")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
DEBUG = False


def rnd(n, alphabet=string.ascii_lowercase + string.digits):
    return "".join(random.choice(alphabet) for _ in range(n))


def flatten(obj):
    """Склеивает все строки из вложенного JSON в один текст."""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return "\n".join(flatten(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return "\n".join(flatten(v) for v in obj)
    return ""


def find_confirm(text):
    """Ищет ссылку подтверждения сначала в исходном тексте, затем после
    раскодировки quoted-printable. Предпочитает ссылку, где есть token=."""
    raw = html.unescape(text)
    candidates = CONFIRM_RE.findall(raw)

    if "=3D" in raw or "=\n" in raw or "=\r\n" in raw:
        try:
            dec = quopri.decodestring(raw.encode("utf-8", "ignore")).decode("utf-8", "ignore")
            candidates += CONFIRM_RE.findall(html.unescape(dec))
        except Exception:
            pass

    for c in candidates:
        if "token=" in c:
            return c
    return candidates[0] if candidates else None


# --------------------------------------------------------------------------
# Базовый класс: цикл ожидания письма
# --------------------------------------------------------------------------
class Provider:
    name = "base"
    address = None

    def create(self):
        raise NotImplementedError

    def new_texts(self, seen):
        """Возвращает тексты новых писем (seen — множество уже виденных id)."""
        raise NotImplementedError

    def wait_confirm_link(self, timeout=180, poll=5):
        deadline = time.time() + timeout
        seen = set()
        while time.time() < deadline:
            try:
                texts = self.new_texts(seen)
            except requests.RequestException as e:
                print(f"[mail] {self.name}: ошибка опроса: {e}")
                texts = []
            for text in texts:
                if DEBUG:
                    print("----- письмо -----")
                    print(text[:1500])
                    print("------------------")
                link = find_confirm(text)
                if link:
                    return link
            time.sleep(poll)
        raise TimeoutError("Письмо с подтверждением не пришло")


# --------------------------------------------------------------------------
# mail.tm и mail.gw (одинаковый REST API)
# --------------------------------------------------------------------------
class MailTm(Provider):
    def __init__(self, api, name):
        self.api, self.name = api, name
        self.s = requests.Session()
        self.password = rnd(16, string.ascii_letters + string.digits)

    def _req(self, method, path, **kw):
        for _ in range(5):
            r = self.s.request(method, self.api + path, timeout=20, **kw)
            if r.status_code == 429:  # лимит ~8 запросов/сек
                time.sleep(2)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError("Слишком много запросов к API почты (429)")

    def create(self):
        domains = self._req("GET", "/domains")["hydra:member"]
        active = [d["domain"] for d in domains if d.get("isActive", True)]
        if not active:
            raise RuntimeError("У провайдера нет активных доменов")
        self.address = f"{rnd(10)}@{random.choice(active)}"
        creds = {"address": self.address, "password": self.password}
        self._req("POST", "/accounts", json=creds)
        token = self._req("POST", "/token", json=creds)["token"]
        self.s.headers["Authorization"] = f"Bearer {token}"
        return self.address

    def new_texts(self, seen):
        out = []
        for m in self._req("GET", "/messages")["hydra:member"]:
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            full = self._req("GET", f"/messages/{m['id']}")
            out.append(flatten([full.get("text"), full.get("html")]))
        return out


# --------------------------------------------------------------------------
# TempMail.lol
# --------------------------------------------------------------------------
class TempMailLol(Provider):
    name = "tempmaillol"
    API = "https://api.tempmail.lol/v2"

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(UA)
        self.token = None

    def create(self):
        r = self.s.post(f"{self.API}/inbox/create", json={}, timeout=20)
        r.raise_for_status()
        data = r.json()
        self.address, self.token = data["address"], data["token"]
        return self.address

    def new_texts(self, seen):
        r = self.s.get(f"{self.API}/inbox", params={"token": self.token}, timeout=20)
        r.raise_for_status()
        out = []
        for i, m in enumerate(r.json().get("emails", [])):
            key = m.get("date") or m.get("subject") or i
            if key in seen:
                continue
            seen.add(key)
            out.append(flatten(m))
        return out


# --------------------------------------------------------------------------
# 1secmail
# --------------------------------------------------------------------------
class OneSecMail(Provider):
    name = "onesecmail"
    API = "https://www.1secmail.com/api/v1/"
    FALLBACK = ["1secmail.com", "1secmail.org", "1secmail.net", "esiix.com",
                "wwjmp.com", "xojxe.com", "yoggm.com", "kzccv.com"]

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(UA)

    def create(self):
        try:
            r = self.s.get(self.API, params={"action": "getDomainList"}, timeout=20)
            r.raise_for_status()
            domains = r.json() or self.FALLBACK
        except (requests.RequestException, ValueError):
            domains = self.FALLBACK
        self.login, self.domain = rnd(10), random.choice(domains)
        self.address = f"{self.login}@{self.domain}"
        return self.address

    def new_texts(self, seen):
        r = self.s.get(self.API, params={"action": "getMessages", "login": self.login,
                                         "domain": self.domain}, timeout=20)
        r.raise_for_status()
        out = []
        for m in r.json():
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            rr = self.s.get(self.API, params={"action": "readMessage", "login": self.login,
                                              "domain": self.domain, "id": m["id"]}, timeout=20)
            rr.raise_for_status()
            out.append(flatten(rr.json()))
        return out


# --------------------------------------------------------------------------
# Guerrilla Mail
# --------------------------------------------------------------------------
class Guerrilla(Provider):
    name = "guerrilla"
    API = "https://api.guerrillamail.com/ajax.php"
    DOMAINS = ["sharklasers.com", "grr.la", "guerrillamail.info", "guerrillamail.biz",
               "guerrillamail.com", "guerrillamail.de", "guerrillamail.net",
               "guerrillamail.org", "spam4.me"]

    def __init__(self, domain=None):
        self.s = requests.Session()
        self.domain = domain
        self.sid = None

    def _call(self, func, **params):
        params["f"] = func
        if self.sid:
            params["sid_token"] = self.sid
        r = self.s.get(self.API, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        if data.get("sid_token"):
            self.sid = data["sid_token"]
        return data

    def create(self):
        self._call("get_email_address", lang="en", ip="127.0.0.1", agent="Mozilla/5.0")
        user = rnd(10)
        self._call("set_email_user", email_user=user, lang="en")
        self.address = f"{user}@{self.domain or random.choice(self.DOMAINS)}"
        return self.address

    def new_texts(self, seen):
        out = []
        for m in self._call("check_email", seq=0).get("list", []):
            if m["mail_id"] in seen:
                continue
            seen.add(m["mail_id"])
            full = self._call("fetch_email", email_id=m["mail_id"])
            out.append(full.get("mail_body") or "")
        return out


# --------------------------------------------------------------------------
# temp-mail.io (внутренний API сайта, неофициальный)
# --------------------------------------------------------------------------
class TempMailIo(Provider):
    name = "tempmailio"
    API = "https://api.internal.temp-mail.io/api/v3"

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(UA)
        self.s.headers.update({"Origin": "https://temp-mail.io",
                               "Referer": "https://temp-mail.io/"})

    def create(self):
        r = self.s.post(f"{self.API}/email/new",
                        json={"min_name_length": 10, "max_name_length": 10}, timeout=20)
        r.raise_for_status()
        self.address = r.json()["email"]
        return self.address

    def new_texts(self, seen):
        r = self.s.get(f"{self.API}/email/{self.address}/messages", timeout=20)
        r.raise_for_status()
        out = []
        for m in r.json():
            if m.get("id") in seen:
                continue
            seen.add(m.get("id"))
            out.append(flatten(m))
        return out


# --------------------------------------------------------------------------
# Maildrop.cc (любой адрес @maildrop.cc, GraphQL)
# --------------------------------------------------------------------------
class Maildrop(Provider):
    name = "maildrop"
    API = "https://api.maildrop.cc/graphql"

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(UA)

    def _gql(self, query):
        r = self.s.post(self.API, json={"query": query}, timeout=20)
        r.raise_for_status()
        return r.json().get("data") or {}

    def create(self):
        self.box = rnd(10)
        self.address = f"{self.box}@maildrop.cc"
        return self.address

    def new_texts(self, seen):
        out = []
        inbox = self._gql('query { inbox(mailbox:"%s") { id subject } }' % self.box)
        for m in inbox.get("inbox") or []:
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            msg = self._gql('query { message(mailbox:"%s", id:"%s") { html data } }'
                            % (self.box, m["id"]))
            out.append(flatten(msg.get("message") or {}))
        return out


# --------------------------------------------------------------------------
# Mailnesia (любой адрес @mailnesia.com, разбор HTML)
# --------------------------------------------------------------------------
class Mailnesia(Provider):
    name = "mailnesia"
    URL = "https://mailnesia.com/mailbox"

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(UA)

    def create(self):
        self.box = rnd(10)
        self.address = f"{self.box}@mailnesia.com"
        return self.address

    def new_texts(self, seen):
        r = self.s.get(f"{self.URL}/{self.box}", timeout=20)
        r.raise_for_status()
        out = []
        for mid in re.findall(rf"/mailbox/{self.box}/(\d+)", r.text):
            if mid in seen:
                continue
            seen.add(mid)
            rr = self.s.get(f"{self.URL}/{self.box}/{mid}", timeout=20)
            rr.raise_for_status()
            out.append(rr.text)
        return out


# --------------------------------------------------------------------------
# temp-mail.org через браузер (официального API нет)
# --------------------------------------------------------------------------
class TempMailOrg(Provider):
    name = "tempmailorg"
    URL = "https://temp-mail.org/en/"

    def __init__(self, ctx):
        self.page = ctx.new_page()

    def create(self):
        self.page.goto(self.URL, wait_until="domcontentloaded")
        self.page.wait_for_function(
            "(() => { const e = document.querySelector('#mail');"
            " return e && /@/.test(e.value); })()", timeout=90000)
        self.address = self.page.input_value("#mail").strip()
        return self.address

    @staticmethod
    def _collect(page):
        parts = [page.content()]
        for f in page.frames:
            try:
                parts.append(f.content())
            except Exception:
                pass
        try:
            parts += page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
        except Exception:
            pass
        return "\n".join(parts)

    def new_texts(self, seen):
        out = [self._collect(self.page)]
        hrefs = self.page.eval_on_selector_all(
            "a[href*='/view/']", "els => els.map(e => e.href)")
        for h in hrefs:
            if h in seen:
                continue
            seen.add(h)
            v = self.page.context.new_page()
            try:
                v.goto(h, wait_until="domcontentloaded")
                v.wait_for_timeout(1500)
                out.append(self._collect(v))
            finally:
                v.close()
        try:
            self.page.click("#click-to-refresh", timeout=2000)
        except Exception:
            pass
        return out


# --------------------------------------------------------------------------
# Реестр провайдеров (порядок = порядок перебора в режиме auto)
# --------------------------------------------------------------------------
PROVIDERS = {
    "tempmaillol": lambda ctx: TempMailLol(),
    "onesecmail":  lambda ctx: OneSecMail(),
    "guerrilla":   lambda ctx: Guerrilla(),
    "tempmailio":  lambda ctx: TempMailIo(),
    "maildrop":    lambda ctx: Maildrop(),
    "mailnesia":   lambda ctx: Mailnesia(),
    "tempmailorg": lambda ctx: TempMailOrg(ctx),   # браузерный
    "mailtm":      lambda ctx: MailTm("https://api.mail.tm", "mailtm"),  # сайт блокирует
    "mailgw":      lambda ctx: MailTm("https://api.mail.gw", "mailgw"),
}


# --------------------------------------------------------------------------
# Капча
# --------------------------------------------------------------------------
def solve_math(question: str) -> int:
    """'18 − 7 =' -> 11. Поддерживает + - − – × x * ÷ /."""
    q = (question.replace("−", "-").replace("–", "-").replace("—", "-")
         .replace("×", "*").replace("х", "*").replace("x", "*")
         .replace("÷", "/").replace(":", "/"))
    m = re.search(r"(-?\d+)\s*([+\-*/])\s*(-?\d+)", q)
    if not m:
        raise ValueError(f"Не удалось разобрать пример: {question!r}")
    a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
    return {"+": a + b, "-": a - b, "*": a * b, "/": a // b if b else 0}[op]


# --------------------------------------------------------------------------
# Основной сценарий
# --------------------------------------------------------------------------
def register(page, email, username, password, attempts=5):
    for attempt in range(1, attempts + 1):
        print(f"[reg] попытка {attempt}: открываю {REG_URL}")
        page.goto(REG_URL, wait_until="domcontentloaded")
        page.wait_for_selector("#registrationForm")

        page.fill("#username", username)
        page.fill("#email", email)
        page.fill("#password", password)
        page.fill("#confirmPassword", password)

        page.wait_for_function(
            "document.querySelector('#captchaQuestion') && "
            "/\\d/.test(document.querySelector('#captchaQuestion').textContent)")
        question = page.inner_text("#captchaQuestion")
        answer = solve_math(question)
        print(f"[reg] капча: {question.strip()} {answer}")
        page.fill("#captcha_answer", str(answer))

        page.check("#agreeCheckbox")
        page.wait_for_function("!document.querySelector('#registerButton').disabled")

        with page.expect_response(lambda r: "verify_register" in r.url, timeout=60000) as ri:
            page.click("#registerButton")
        resp = ri.value
        try:
            data = resp.json()
        except Exception:
            data = {}
        print(f"[reg] ответ сервера: HTTP {resp.status} {data}")

        if data.get("success") or data.get("redirect"):
            return True

        msg = (data.get("message") or "")
        wait = int(data.get("wait_seconds") or 0)
        if wait:
            print(f"[reg] лимит попыток, жду {wait} с")
            time.sleep(wait + 1)
        elif re.search(r"пример|капч", msg, re.I):
            print("[reg] капча не принята, пробую снова")
        else:
            raise RuntimeError(f"Регистрация отклонена: {msg or data}")
    raise RuntimeError("Не удалось зарегистрироваться за отведённое число попыток")


def get_playlist(page):
    print("[lk] жду редирект на /lk ...")
    page.wait_for_url(re.compile(r"https://taptv\.ru/lk"), timeout=60000)
    print(f"[lk] открыт {page.url}")
    page.wait_for_selector("#playlistUrl", state="attached", timeout=60000)
    page.wait_for_function(
        "document.querySelector('#playlistUrl').value.includes('playlist.m3u8')",
        timeout=30000)
    link = page.input_value("#playlistUrl").strip()
    if not PLAYLIST_RE.fullmatch(link):
        m = PLAYLIST_RE.search(page.content())
        if m:
            link = m.group(0)
    return link


def download_playlist(ctx, url, out_path):
    """Скачивает плейлист в файл. Сначала через контекст браузера (с его
    cookie/заголовками), при неудаче — обычным requests. Возвращает Path."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    data = None

    try:
        r = ctx.request.get(url, headers={"Referer": BASE + "/"}, timeout=60000)
        if r.ok:
            data = r.body()
        else:
            print(f"[dl ] браузерный запрос: HTTP {r.status}")
    except Exception as e:
        print(f"[dl ] браузерный запрос не удался: {e}")

    if data is None:
        r = requests.get(url, headers={**UA, "Referer": BASE + "/"}, timeout=60)
        r.raise_for_status()
        data = r.content

    if not data.lstrip().startswith(b"#EXTM3U"):
        print("[dl ] предупреждение: файл не начинается с #EXTM3U, "
              "сохраняю как есть")
    out_path.write_bytes(data)
    print(f"[dl ] сохранено: {out_path.resolve()} ({len(data)} байт)")
    return out_path


def try_provider(kind, ctx, out_path):
    """Полный цикл с одним провайдером. Бросает исключение при неудаче."""
    mail = PROVIDERS[kind](ctx)
    email = mail.create()
    password = rnd(12, string.ascii_letters + string.digits)
    username = "user" + rnd(7)
    print(f"[mail] {kind}: {email}")
    print(f"[acc ] логин: {username}  пароль: {password}")

    page = ctx.new_page()
    page.bring_to_front()
    try:
        register(page, email, username, password)
        print("[mail] жду письмо с подтверждением...")
        link = mail.wait_confirm_link()
        print(f"[mail] ссылка подтверждения: {link}")
        page.bring_to_front()
        page.goto(link, wait_until="domcontentloaded")
        playlist = get_playlist(page)
        saved = download_playlist(ctx, playlist, out_path)
        return email, password, playlist, saved
    except Exception:
        try:
            page.screenshot(path=f"error_{kind}.png", full_page=True)
            print(f"[err ] скриншот: error_{kind}.png (URL: {page.url})")
        except Exception:
            pass
        raise
    finally:
        try:
            page.close()
        except Exception:
            pass


def main():
    global DEBUG
    ap = argparse.ArgumentParser()
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--mail", choices=["auto"] + list(PROVIDERS), default="auto",
                    help="провайдер временной почты (по умолчанию auto — перебор всех)")
    ap.add_argument("--out", default="playlist.m3u",
                    help="путь для сохранения плейлиста (по умолчанию playlist.m3u8)")
    ap.add_argument("--list", action="store_true", help="показать провайдеров и выйти")
    ap.add_argument("--debug", action="store_true", help="печатать начало каждого письма")
    args = ap.parse_args()
    DEBUG = args.debug

    if args.list:
        print("\n".join(PROVIDERS))
        return

    order = list(PROVIDERS) if args.mail == "auto" else [args.mail]
    errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        ctx = browser.new_context(locale="ru-RU", viewport={"width": 1280, "height": 900})
        ctx.set_default_timeout(30000)

        result = None
        for kind in order:
            print(f"\n===== провайдер: {kind} =====")
            try:
                result = try_provider(kind, ctx, args.out)
                break
            except (PWTimeout, TimeoutError, RuntimeError, ValueError,
                    requests.RequestException, KeyError, OSError) as e:
                print(f"[fail] {kind}: {e}")
                errors.append(f"{kind}: {e}")
        browser.close()

    if not result:
        sys.exit("Не получилось ни с одним провайдером:\n  " + "\n  ".join(errors))

    email, password, playlist, saved = result
    print("\n=== РЕЗУЛЬТАТ ===")
    print(f"Email:    {email}")
    print(f"Пароль:   {password}")
    print(f"Ссылка:   {playlist}")
    print(f"Файл:     {saved.resolve()}")


if __name__ == "__main__":
    main()