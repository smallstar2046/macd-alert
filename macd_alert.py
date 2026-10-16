# -*- coding: utf-8 -*-
"""MACD 邮件提醒 —— 本地全自动版（不依赖 WorkBuddy）

功能：按 alert_config.json 的配置抓取现货或合约 K 线（task 里可写 "market": "futures"），
逐根扫描 MACD 事件
（金叉 / 死叉 / 零轴下方金叉 / 柱体变色 / 零轴穿越 / 价格突破），命中即用
SMTP 发邮件；用 alert_state.json 记录「上次已处理 K 线时间」，保证不重不漏。

常用命令：
  python macd_alert.py --status          查看配置、游标、上次运行结果
  python macd_alert.py --dry-run         只扫描、打印将发送的内容，不发邮件
  python macd_alert.py --test            发一封测试邮件到配置的所有收件人
  python macd_alert.py --set-password    填写 SMTP 授权码并立即发测试邮件（推荐用菜单第 7 项）
  python macd_alert.py                   扫描一次（供 Windows 计划任务调用）
  python macd_alert.py --loop            常驻模式，每 pollMinutes 分钟跑一次
  python macd_alert.py --reset           清空游标（下次运行按基线重新开始）

只使用 Python 标准库，无需 pip 安装任何依赖。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import smtplib
import ssl
import sys
import time
import urllib.request
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "alert_config.json"
STATE_PATH = BASE / "alert_state.json"
LOG_PATH = BASE / "macd_alert.log"

# 现货上游：(显示名, 解析器, 地址)。按顺序尝试，任一成功即返回。
# 币安对部分地区 IP 会返回 451，故加入 MEXC（格式与币安一致）与 Gate 作为后备，
# 这样云端（GitHub Actions / 海外服务器）与国内网络都能取到行情。
SPOT_UPSTREAMS = [
    ("binance.vision", "binance", "https://data-api.binance.vision"),
    ("binance.gcp", "binance", "https://api-gcp.binance.com"),
    ("binance.api1", "binance", "https://api1.binance.com"),
    ("binance.api2", "binance", "https://api2.binance.com"),
    ("mexc", "binance", "https://api.mexc.com"),
    ("gate", "gate", "https://api.gateio.ws"),
]

# 合约（U 本位永续）上游。Gate 合约放首位：实测国内网络与云端机房均可访问；
# 币安合约接口（fapi）在海外机房常返回 451，故作为备用。两者 K 线含义一致（合约价）。
FUTURES_UPSTREAMS = [
    ("gate.futures", "gate_futures", "https://api.gateio.ws"),
    ("binance.fapi", "binance_futures", "https://fapi.binance.com"),
    ("binance.fapi1", "binance_futures", "https://fapi1.binance.com"),
    ("binance.fapi2", "binance_futures", "https://fapi2.binance.com"),
]

# 合约代码与现货代码不一致的品种（合约 RAYSOL = 现货 RAY，Raydium）
CONTRACT_ALIAS = {"RAYSOL": "RAY"}
SPOT_EQUIV = {"RAYSOLUSDT": "RAYUSDT"}

# 兼容旧脚本引用（_verify_sources.py 等）
UPSTREAMS = SPOT_UPSTREAMS
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) macd-alert/1.0"}
KLINES_LIMIT = 400
FETCH_TIMEOUT = 12          # 正常取数 <2s；不可达上游不必白等 20s

BAR_MS = {
    "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "6h": 21_600_000, "12h": 43_200_000, "1d": 86_400_000,
}

DEFAULT_ALERTS = {
    "golden": True,          # 金叉（DIF 上穿 DEA）
    "death": True,           # 死叉
    "goldenBelowZero": False,  # 零轴下方金叉（交叉时 DIF<0）
    "deathAboveZero": False,   # 零轴上方死叉（交叉时 DIF>0）
    "histFlip": False,       # 柱体由正转负 / 由负转正
    "zeroCross": False,      # DIF 上穿 / 下穿零轴
    "priceAbove": 0,         # 现价上破该价位（0 = 关闭）
    "priceBelow": 0,         # 现价下破该价位（0 = 关闭）
}

DEFAULT_CONFIG = {
    "smtp": {
        "host": "smtp.163.com",
        "port": 465,
        "ssl": True,
        # 发件邮箱：本地写在 alert_config.json 里；云端用密钥 MACD_SMTP_USER 注入
        # （源码里不写死任何真实邮箱，方便把仓库设为公开）
        "user": "",
        "password": "",
        "fromName": "MACD 监控",
    },
    "pollMinutes": 10,
    "notifyOnStart": True,
    # 参考仓位：总资金（USDT）与单笔基础风险比例（%）。
    # capital 填 0 可整体关闭「参考仓位」；riskPct 是「标准档」的风险，档位会按 ADX/RSI 缩放。
    "capital": 100.0,
    "riskPct": 1.0,
    # 总开关：false 时只扫描不发信（游标照常推进，避免恢复后补发历史信号）
    "mailEnabled": True,
    # 提醒规则矩阵：按「币种 × 信号类型」单独开关，见 apply_symbol_rules 的注释
    "symbolRules": {},
    "tasks": [],
}

log = logging.getLogger("macd_alert")


# ---------------------------------------------------------------- 基础工具
def setup_log(verbose: bool = False) -> None:
    handlers = [logging.FileHandler(LOG_PATH, encoding="utf-8")]
    if verbose:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


def trim_log(max_bytes: int = 3_000_000) -> None:
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > max_bytes:
            lines = LOG_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()
            LOG_PATH.write_text("\n".join(lines[-5000:]) + "\n", encoding="utf-8")
    except Exception:
        pass


def bj(ms: int) -> str:
    """毫秒时间戳 -> 北京时间字符串（不依赖本机时区设置）"""
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ms / 1000.0 + 8 * 3600))


def now_bj() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + 8 * 3600))


def fmt_price(v: float) -> str:
    """价格小数位自适应：贵价币 2 位，低价币最多 6 位"""
    ax = abs(v)
    d = 2 if ax >= 100 else (4 if ax >= 1 else (5 if ax >= 0.01 else 6))
    s = ("%%.%df" % d) % v
    return s


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.warning("读取 %s 失败：%s（改用默认值）", path.name, exc)
        return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _fnum(v, default) -> float:
    """把配置里的数字安全转成 float；空值或非法值回退到 default。"""
    try:
        if v is None or v == "":
            return float(default)
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def load_config() -> dict:
    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        save_json(CONFIG_PATH, DEFAULT_CONFIG)
        log.warning("未找到 %s，已生成模板，请填写 SMTP 授权码后重试", CONFIG_PATH.name)
        cfg = DEFAULT_CONFIG
    out = json.loads(json.dumps(DEFAULT_CONFIG))
    out["smtp"].update(cfg.get("smtp") or {})
    out["pollMinutes"] = int(cfg.get("pollMinutes") or DEFAULT_CONFIG["pollMinutes"])
    out["notifyOnStart"] = bool(cfg.get("notifyOnStart", True))
    out["capital"] = _fnum(cfg.get("capital"), DEFAULT_CONFIG["capital"])
    out["riskPct"] = _fnum(cfg.get("riskPct"), DEFAULT_CONFIG["riskPct"])
    out["tasks"] = cfg.get("tasks") or []
    # 提醒规则矩阵：先规整成便于查表的形式，无法识别的项只告警、不阻断
    out["mailEnabled"] = bool(cfg.get("mailEnabled", True))
    raw_rules = cfg.get("symbolRules")
    out["symbolRules"] = raw_rules if isinstance(raw_rules, dict) else {}
    _rules, _bad = normalize_symbol_rules(raw_rules)
    out["_symbol_rules"] = _rules
    out["_symbol_rules_bad"] = _bad
    if _bad:
        log.warning("symbolRules 里有 %d 处无法识别，已忽略：%s", len(_bad), "；".join(_bad))
    # 云端部署用：授权码可由环境变量注入（GitHub Secrets），无需写进仓库
    env_pwd = (os.environ.get("MACD_SMTP_PASSWORD") or "").strip()
    if env_pwd:
        out["smtp"]["password"] = env_pwd
    env_user = (os.environ.get("MACD_SMTP_USER") or "").strip()
    if env_user:
        out["smtp"]["user"] = env_user
    # 云端部署用：收件人同样可由环境变量注入（GitHub Secrets），仓库里不出现任何邮箱地址
    env_to = (os.environ.get("MACD_MAIL_TO") or "").strip()
    if env_to:
        addrs = [a.strip() for a in env_to.replace(";", ",").replace(" ", ",")
                 .replace("\n", ",").split(",") if a.strip()]
        if addrs:
            for t in out["tasks"]:
                t["to"] = list(addrs)
    return out


# ---------------------------------------------------------------- 行情与指标
def _klines_binance_style(base: str, symbol: str, interval: str,
                          path: str = "/api/v3/klines"):
    """币安（现货/合约）/ MEXC —— 三者 klines 返回格式完全一致。

    每根：[openTime, open, high, low, close, volume, closeTime, ...]
    现货用 /api/v3/klines，合约用 /fapi/v1/klines。
    """
    url = "%s%s?symbol=%s&interval=%s&limit=%d" % (
        base, path, symbol, interval, KLINES_LIMIT)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    if not isinstance(raw, list) or len(raw) < 60:
        raise RuntimeError("返回数据异常（%s 条）" % (len(raw) if isinstance(raw, list) else "非列表"))
    return [
        {"openTime": int(k[0]), "closeTime": int(k[6]),
         "open": float(k[1]), "high": float(k[2]),
         "low": float(k[3]), "close": float(k[4])}
        for k in raw
    ]


def _klines_gate(base: str, symbol: str, interval: str):
    """Gate.io —— 每根：[时间戳(秒), 计价量, close, high, low, open, 基础量, 是否收盘]，按时间升序。"""
    if not symbol.endswith("USDT"):
        raise RuntimeError("Gate 上游仅支持 USDT 交易对")
    step = BAR_MS.get(interval)
    if not step:
        raise RuntimeError("Gate 上游不支持周期 %s" % interval)
    pair = symbol[:-4] + "_USDT"
    url = "%s/api/v4/spot/candlesticks?currency_pair=%s&interval=%s&limit=%d" % (
        base, pair, interval, KLINES_LIMIT)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    if not isinstance(raw, list) or len(raw) < 60:
        raise RuntimeError("返回数据异常（%s 条）" % (len(raw) if isinstance(raw, list) else "非列表"))
    out = []
    for r in raw:
        t = int(float(r[0])) * 1000
        out.append({"openTime": t, "closeTime": t + step - 1,
                    "open": float(r[5]), "high": float(r[3]),
                    "low": float(r[4]), "close": float(r[2])})
    return out


def _klines_gate_futures(base: str, symbol: str, interval: str):
    """Gate 合约（U 本位永续）。

    每根：{"t": 起始秒, "o","h","l","c","v","sum"}，按时间升序。
    合约名 = 基础币 + "_USDT"，RAYSOL 需映射为 RAY。
    """
    if not symbol.endswith("USDT"):
        raise RuntimeError("Gate 合约上游仅支持 USDT 交易对")
    step = BAR_MS.get(interval)
    if not step:
        raise RuntimeError("Gate 合约上游不支持周期 %s" % interval)
    base_asset = symbol[:-4]
    base_asset = CONTRACT_ALIAS.get(base_asset, base_asset)
    contract = base_asset + "_USDT"
    url = "%s/api/v4/futures/usdt/candlesticks?contract=%s&interval=%s&limit=%d" % (
        base, contract, interval, KLINES_LIMIT)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    if not isinstance(raw, list) or len(raw) < 60:
        raise RuntimeError("返回数据异常（%s 条）" % (len(raw) if isinstance(raw, list) else "非列表"))
    out = []
    for r in raw:
        t = int(float(r["t"])) * 1000
        out.append({"openTime": t, "closeTime": t + step - 1,
                    "open": float(r["o"]), "high": float(r["h"]),
                    "low": float(r["l"]), "close": float(r["c"])})
    return out


def _klines_binance_futures(base: str, symbol: str, interval: str):
    return _klines_binance_style(base, symbol, interval, path="/fapi/v1/klines")


_FETCHERS = {
    "binance": _klines_binance_style,
    "gate": _klines_gate,
    "gate_futures": _klines_gate_futures,
    "binance_futures": _klines_binance_futures,
}


# 同一次运行内的取数缓存：多任务会重复取同一个「币种 + 周期」（如 BTCUSDT 4h 被
# BTC-4H 与共振任务各取一次），缓存 90 秒即可在本轮内复用、跨轮自动失效。
_KL_CACHE = {}
KL_CACHE_TTL = 90


def fetch_klines(symbol: str, interval: str, market: str = "spot"):
    """多上游容错抓取（带 90 秒进程内缓存），返回 (source_name, candles)。"""
    m = (market or "spot").lower()
    key = (m, symbol, interval)
    hit = _KL_CACHE.get(key)
    if hit and hit[0] > time.time():
        return hit[1], hit[2]
    name, candles = _fetch_klines(symbol, interval, m)
    _KL_CACHE[key] = (time.time() + KL_CACHE_TTL, name, candles)
    return name, candles


def _fetch_klines(symbol: str, interval: str, market: str = "spot"):
    """多上游容错抓取，返回 (source_name, candles)；全部失败抛 RuntimeError。

    market="futures" 时先走合约上游；合约源全部失败后，用「现货等价物」
    （如合约 RAYSOLUSDT -> 现货 RAYUSDT）兜底，保证信号不会因单一渠道挂掉而中断。
    """
    errs = []
    equiv = SPOT_EQUIV.get(symbol)
    tries = []                                   # 待试上游：(name, kind, base, sym)
    if (market or "spot").lower() == "futures":
        tries += [(n, k, b, symbol) for n, k, b in FUTURES_UPSTREAMS]
    spot_self = [(n, k, b, symbol) for n, k, b in SPOT_UPSTREAMS]
    if equiv and equiv != symbol:
        # 合约代码在现货里往往叫别的名字（如 RAYSOLUSDT -> RAYUSDT）。
        # 若把等价物放在整条链的最后，一旦是「命名不一致」就要白等一轮超时
        # （实测 RAYSOLUSDT × 3 个周期 ≈ 7 分钟）。改为先各试一条主链，再依次回退。
        spot_eq = [(n, k, b, equiv) for n, k, b in SPOT_UPSTREAMS]
        tries += spot_self[:1] + spot_eq[:1] + spot_self[1:] + spot_eq[1:]
    else:
        tries += spot_self

    for name, kind, base, sym in tries:
        try:
            candles = _FETCHERS[kind](base, sym, interval)
            if sym != symbol:
                name = "%s（现货替代 %s）" % (name, sym)
            return name, candles
        except Exception as exc:  # noqa: BLE001
            errs.append("%s -> %s" % (name, exc))
    raise RuntimeError("%s %s 抓取失败：%s" % (symbol, interval, "；".join(errs)))


def ema(vals, span):
    a = 2.0 / (span + 1.0)
    out, prev = [], None
    for v in vals:
        prev = v if prev is None else a * v + (1 - a) * prev
        out.append(prev)
    return out


def macd(closes, fast=12, slow=26, signal=9):
    ef, es = ema(closes, fast), ema(closes, slow)
    dif = [f - s for f, s in zip(ef, es)]
    dea = ema(dif, signal)
    hist = [2.0 * (d - e) for d, e in zip(dif, dea)]
    return dif, dea, hist


IND_PERIOD = 14


def rsi_series(closes, period=IND_PERIOD):
    """Wilder RSI(period)。返回与 closes 等长列表，前 period 个为 None。"""
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    gain = loss = 0.0
    for i in range(1, period + 1):
        ch = closes[i] - closes[i - 1]
        gain += max(ch, 0.0)
        loss += max(-ch, 0.0)
    ag, al = gain / period, loss / period
    for i in range(period, len(closes)):
        if i > period:
            ch = closes[i] - closes[i - 1]
            ag = (ag * (period - 1) + max(ch, 0.0)) / period
            al = (al * (period - 1) + max(-ch, 0.0)) / period
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    return out


def atr_series(candles, period=IND_PERIOD):
    """Wilder ATR(period)。返回与 candles 等长列表，前 period 个为 None。"""
    n = len(candles)
    out = [None] * n
    if n <= period:
        return out
    tr = []
    for i in range(1, n):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(tr[:period]) / period
    out[period] = a
    for k in range(period, len(tr)):
        a = (a * (period - 1) + tr[k]) / period
        out[k + 1] = a
    return out


def adx_series(candles, period=IND_PERIOD):
    """Wilder ADX 与 +DI / -DI。返回 (adx, pdi, mdi) 三个等长列表。"""
    n = len(candles)
    adx, pdi, mdi = [None] * n, [None] * n, [None] * n
    if n <= period * 2:
        return adx, pdi, mdi
    tr, pdm, mdm = [0.0], [0.0], [0.0]
    for i in range(1, n):
        h, l = candles[i]["high"], candles[i]["low"]
        ph, pl, pc = candles[i - 1]["high"], candles[i - 1]["low"], candles[i - 1]["close"]
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
        up, dn = h - ph, pl - l
        pdm.append(up if (up > dn and up > 0) else 0.0)
        mdm.append(dn if (dn > up and dn > 0) else 0.0)

    def wilder(vals):
        out = [None] * n
        s = sum(vals[1:period + 1])
        out[period] = s
        for i in range(period + 1, n):
            s = s - s / period + vals[i]
            out[i] = s
        return out

    str_s, sp, sm = wilder(tr), wilder(pdm), wilder(mdm)
    dxs = []
    for i in range(period, n):
        if not str_s[i]:
            continue
        p = 100.0 * sp[i] / str_s[i]
        m = 100.0 * sm[i] / str_s[i]
        pdi[i], mdi[i] = p, m
        tot = p + m
        dxs.append((i, 0.0 if tot == 0 else 100.0 * abs(p - m) / tot))
    if len(dxs) < period:
        return adx, pdi, mdi
    a = sum(v for _, v in dxs[:period]) / period
    adx[dxs[period - 1][0]] = a
    for k in range(period, len(dxs)):
        a = (a * (period - 1) + dxs[k][1]) / period
        adx[dxs[k][0]] = a
    return adx, pdi, mdi


def cross_dir(a, b):
    out = [0] * len(a)
    for i in range(1, len(a)):
        if a[i - 1] <= b[i - 1] and a[i] > b[i]:
            out[i] = 1
        elif a[i - 1] >= b[i - 1] and a[i] < b[i]:
            out[i] = -1
    return out


# ---------------------------------------------------------------- 事件扫描
def scan_symbol(symbol: str, interval: str, since_ms: int, alerts: dict,
                price_state: dict, market: str = "spot"):
    """逐根扫描区间内所有命中的事件。market：spot（默认）/ futures。

    返回 (events, status, closed_ms, price_state_new)
    events 内每条含 type/label/time/time_ms/close/dif/dea/hist/gap
    """
    host, candles = fetch_klines(symbol, interval, market)
    closes = [c["close"] for c in candles]
    dif, dea, hist = macd(closes)
    x = cross_dir(dif, dea)
    zx = cross_dir(dif, [0.0] * len(dif))
    rsis = rsi_series(closes)
    atrs = atr_series(candles)
    adxs, _pdi, _mdi = adx_series(candles)

    def ind_of(idx):
        """该根 K 线对应的 ADX / RSI / ATR，供信号与状态共用。"""
        return {
            "adx": None if adxs[idx] is None else round(adxs[idx], 2),
            "rsi": None if rsis[idx] is None else round(rsis[idx], 2),
            "atr": None if atrs[idx] is None else round(atrs[idx], 8),
        }

    now_ms = int(time.time() * 1000)
    closed = [i for i, c in enumerate(candles) if c["closeTime"] <= now_ms]
    i = closed[-1]
    live = len(candles) - 1

    if since_ms <= 0:  # 首次：基线取最后一根已收盘 K 线，不回溯历史
        since_ms = candles[i]["closeTime"]

    def ev(kind, label, j):
        t = candles[j]["closeTime"]
        return {
            "type": kind, "label": label, "symbol": symbol, "interval": interval,
            "time": bj(t), "time_ms": t, "close": closes[j],
            "dif": round(dif[j], 8), "dea": round(dea[j], 8),
            "hist": round(hist[j], 8), "gap": round(dif[j] - dea[j], 8),
            **ind_of(j),
        }

    events = []
    for j in range(1, i + 1):
        if candles[j]["closeTime"] <= since_ms:
            continue
        if x[j] == 1:
            if alerts.get("golden"):
                events.append(ev("golden", "金叉（DIF 上穿 DEA）", j))
            if alerts.get("goldenBelowZero") and dif[j] < 0:
                events.append(ev("goldenBelowZero", "零轴下方金叉（DIF<0）", j))
        elif x[j] == -1:
            if alerts.get("death"):
                events.append(ev("death", "死叉（DIF 下穿 DEA）", j))
            if alerts.get("deathAboveZero") and dif[j] > 0:
                events.append(ev("deathAboveZero", "零轴上方死叉（DIF>0）", j))
        if alerts.get("histFlip"):
            if (hist[j] >= 0) != (hist[j - 1] >= 0):
                events.append(ev("histFlip",
                                 "柱体由负转正（绿→红）" if hist[j] >= 0 else "柱体由正转负（红→绿）", j))
        if alerts.get("zeroCross") and zx[j] != 0:
            events.append(ev("zeroCross",
                             "DIF 上穿零轴" if zx[j] == 1 else "DIF 下穿零轴", j))

    # 价格突破：与上一根收盘价比较，只在「首次越线」时触发，避免每根重复报警
    px = closes[live]
    st = dict(price_state or {})
    pa, pb = float(alerts.get("priceAbove") or 0), float(alerts.get("priceBelow") or 0)
    if pa > 0:
        was = bool(st.get("above"))
        now = px >= pa
        if now and not was:
            events.append({"type": "priceAbove", "label": "上破 %s" % fmt_price(pa),
                           "symbol": symbol, "interval": interval, "time": bj(candles[live]["closeTime"]),
                           "time_ms": int(time.time() * 1000), "close": px,
                           "dif": round(dif[live], 8), "dea": round(dea[live], 8),
                           "hist": round(hist[live], 8), "gap": 0.0, **ind_of(live)})
        st["above"] = now
    if pb > 0:
        was = bool(st.get("below"))
        now = px <= pb
        if now and not was:
            events.append({"type": "priceBelow", "label": "下破 %s" % fmt_price(pb),
                           "symbol": symbol, "interval": interval, "time": bj(candles[live]["closeTime"]),
                           "time_ms": int(time.time() * 1000), "close": px,
                           "dif": round(dif[live], 8), "dea": round(dea[live], 8),
                           "hist": round(hist[live], 8), "gap": 0.0, **ind_of(live)})
        st["below"] = now

    status = {
        "symbol": symbol, "interval": interval, "market": market, "source": host,
        "bar_time": bj(candles[i]["closeTime"]),
        "bar_close": closes[i],
        "dif": round(dif[i], 8), "dea": round(dea[i], 8), "hist": round(hist[i], 8),
        "above_zero": dif[i] > 0, "above_dea": dif[i] > dea[i],
        "price": px, "live_dif": round(dif[live], 8), "live_dea": round(dea[live], 8),
        **ind_of(i),
    }
    return events, status, candles[i]["closeTime"], st


# ---------------------------------------------------------------- 多周期共振扫描
def _interval_read(symbol: str, interval: str, window: int, market: str = "spot",
                   now_ms: int = 0):
    """单周期读数 + 「最近 window 根已收盘 K 线内」的零轴反转形态。

    返回 (info, closed_ms, cur_close)；info 为 None 表示该周期未出现目标形态。
    """
    host, candles = fetch_klines(symbol, interval, market)
    closes = [c["close"] for c in candles]
    dif, dea, hist = macd(closes)
    x = cross_dir(dif, dea)
    rsis = rsi_series(closes)
    atrs = atr_series(candles)
    adxs, _pdi, _mdi = adx_series(candles)

    now_ms = int(now_ms) or int(time.time() * 1000)   # now_ms>0 用于历史回放
    closed = [i for i, c in enumerate(candles) if c["closeTime"] <= now_ms]
    i = closed[-1]
    lo = max(1, i - max(1, int(window)) + 1)
    hit = None
    for j in range(i, lo - 1, -1):      # 由新到旧，取最近一次命中的形态
        if x[j] == 1 and dif[j] < 0:
            hit = (RESO_GOLDEN, j)
            break
        if x[j] == -1 and dif[j] > 0:
            hit = (RESO_DEATH, j)
            break

    info = None
    if hit:
        kind, j = hit
        info = {
            "interval": interval, "kind": kind,
            "time": bj(candles[j]["closeTime"]), "time_ms": candles[j]["closeTime"],
            "close": closes[j], "dif": round(dif[j], 8), "dea": round(dea[j], 8),
            "hist": round(hist[j], 8), "gap": round(dif[j] - dea[j], 8),
            "adx": None if adxs[j] is None else round(adxs[j], 2),
            "rsi": None if rsis[j] is None else round(rsis[j], 2),
            "atr": None if atrs[j] is None else round(atrs[j], 8),
            "source": host,
        }
    return info, candles[i]["closeTime"], closes[i]


def _reso_sig(hits, intervals) -> str:
    """共振签名：只有各周期形态完全一致时才非空。

    同一批信号在其「存活期」（仍落在各周期窗口内）内签名保持不变，据此去重；
    信号老化出窗口后签名变为空串 -> 自动解除，之后新出现的共振才会再次提醒。
    """
    if any(h is None for h in hits):
        return ""
    kinds = {h["kind"] for h in hits}
    if len(kinds) != 1:
        return ""
    return "%s@%s" % (kinds.pop(), "|".join(
        "%s:%d" % (iv, hits[k]["time_ms"]) for k, iv in enumerate(intervals)))


def resonance_scan(symbol: str, intervals, window: int, alerts: dict, market: str = "spot",
                   now_ms: int = 0):
    """各周期同时出现同一种零轴反转形态 -> 一个共振事件。

    返回 (event_or_None, status, closed_ms, signature)
    """
    ivs = [str(x).lower() for x in intervals]
    hits, closed_list, curs = [], [], []
    for iv in ivs:
        info, cm, px = _interval_read(symbol, iv, window, market, now_ms)
        hits.append(info)
        closed_list.append(cm)
        curs.append(px)

    sig = _reso_sig(hits, ivs)
    event = None
    if sig and alerts.get(hits[0]["kind"], True):
        kind = hits[0]["kind"]
        iv_txt = "/".join(iv.upper() for iv in ivs)
        main = hits[-1]                       # 用最大周期的读数做展示与解读基准
        newest = max(h["time_ms"] for h in hits)
        zh = "零轴下方金叉" if kind == RESO_GOLDEN else "零轴上方死叉"
        event = {
            "type": kind, "label": "三周期共振·%s（%s 均出现）" % (zh, iv_txt),
            "symbol": symbol, "interval": "/".join(ivs),
            "time": bj(newest), "time_ms": newest,
            "close": main["close"], "dif": main["dif"], "dea": main["dea"],
            "hist": main["hist"], "gap": main["gap"],
            "adx": main["adx"], "rsi": main["rsi"], "atr": main["atr"],
            "ref_interval": ivs[-1],
            "reso": [{"interval": iv.upper(), "kind": h["kind"], "time": h["time"],
                      "dif": h["dif"], "dea": h["dea"], "hist": h["hist"],
                      "adx": h["adx"], "rsi": h["rsi"]}
                     for iv, h in zip(ivs, hits)],
        }

    status = {
        "symbol": symbol, "intervals": list(ivs), "window": int(window),
        "market": market, "resonance": bool(sig),
        "hits": {iv: (None if h is None else {"kind": h["kind"], "time": h["time"],
                                              "dif": h["dif"], "dea": h["dea"],
                                              "hist": h["hist"], "adx": h["adx"],
                                              "rsi": h["rsi"]})
                 for iv, h in zip(ivs, hits)},
        "bar_time": bj(max(closed_list)),
        "price": curs[-1],
    }
    return event, status, max(closed_list), sig


# ---------------------------------------------------------------- 邮件
def test_recipients(cfg: dict):
    """测试邮件收件人：配置中所有任务的收件人去重；若为空则发给发件邮箱自己。"""
    seen, out = set(), []
    for t in cfg.get("tasks") or []:
        for a in (t.get("to") or []):
            a = (a or "").strip()
            if a and a not in seen:
                seen.add(a)
                out.append(a)
    return out or [cfg["smtp"]["user"]]


def send_mail(smtp: dict, to, subject: str, text_body: str, html_body: str) -> None:
    if not smtp.get("user"):
        raise RuntimeError(
            "发件邮箱为空：请在 %s 的 smtp.user 里填写，"
            "或设置环境变量（GitHub 密钥）MACD_SMTP_USER" % CONFIG_PATH.name)
    if not smtp.get("password"):
        raise RuntimeError(
            "SMTP 授权码为空：请在 %s 的 smtp.password 里填写，"
            "或设置环境变量（GitHub 密钥）MACD_SMTP_PASSWORD" % CONFIG_PATH.name)
    to = [a for a in (to or []) if a]
    if not to:
        raise RuntimeError("收件人为空")
    msg = MIMEMultipart("alternative")
    msg["From"] = formataddr((str(Header(smtp.get("fromName") or "MACD 监控", "utf-8")), smtp["user"]))
    msg["To"] = ", ".join(to)
    msg["Subject"] = Header(subject, "utf-8")
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    host, port = smtp["host"], int(smtp["port"])
    if smtp.get("ssl", True):
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=40) as sv:
            sv.login(smtp["user"], smtp["password"])
            sv.sendmail(smtp["user"], to, msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=40) as sv:
            sv.starttls(context=ssl.create_default_context())
            sv.login(smtp["user"], smtp["password"])
            sv.sendmail(smtp["user"], to, msg.as_string())


# ---------------------------------------------------------------- 邮件操作提示
# 信号邮件里附带的一句操作提示，按「周期 + 金叉/死叉」匹配后追加。
# 想改措辞直接改下面的文字；不想要某条就把它删掉，或把值改成 ""。
HINTS = {
    ("4h", "golden"): "结合日K看看是不是要加仓了",
    ("4h", "death"): "可以考虑 合约换现货",
    ("1d", "golden"): "现在现货可以换一下合约了，结合周K考虑考虑",
    ("1d", "death"): "拿现货还是出货好好考虑",
}
GOLDEN_TYPES = ("golden", "goldenBelowZero")   # 金叉类信号
DEATH_TYPES = ("death", "deathAboveZero")      # 死叉类信号

# ---- 多周期共振（零轴反转）：多个周期同时出现同一种零轴反转形态 ----
RESO_GOLDEN = "resoBelowGolden"   # 零轴下方 DIF 上穿 DEA（交叉时 DIF<0）
RESO_DEATH = "resoAboveDeath"     # 零轴上方 DIF 下穿 DEA（交叉时 DIF>0）
RESO_TYPES = (RESO_GOLDEN, RESO_DEATH)
DEFAULT_RESO_ALERTS = {RESO_GOLDEN: True, RESO_DEATH: True}

# ---------------------------------------------------------------- 提醒规则矩阵
# symbolRules：按「币种 × 信号类型」单独覆盖某个币的提醒开关，随时可以只关掉
# 「某个币的某一类提醒」而不用去改任务本身。写法（alert_config.json 顶层）：
#   "symbolRules": {
#       "*":       { "histFlip": false },      // 通配：所有币都关掉柱体变色
#       "BTCUSDT": { "death": false },         // BTC 不发死叉
#       "ARBUSDT": { "golden": true }          // ARB 强制打开金叉
#   }
# 叠加顺序（后者覆盖前者）：默认值 → 任务 alerts → symbolRules["*"] → symbolRules[币种]
# 没有写进 symbolRules 的信号一律「跟随任务原设置」。
RULES_SIGNALS = (
    ("golden", "金叉"),
    ("death", "死叉"),
    ("goldenBelowZero", "零轴下方金叉"),
    ("deathAboveZero", "零轴上方死叉"),
    ("histFlip", "柱体变色"),
    ("zeroCross", "零轴穿越"),
    ("priceAbove", "上破价位"),
    ("priceBelow", "下破价位"),
    (RESO_GOLDEN, "共振·零轴下方金叉"),
    (RESO_DEATH, "共振·零轴上方死叉"),
)
RULES_KEYS = tuple(k for k, _lab in RULES_SIGNALS)
RULES_LABELS = dict(RULES_SIGNALS)
RULE_PRICE_KEYS = ("priceAbove", "priceBelow")


def normalize_symbol_rules(rules):
    """把 symbolRules 规整成 {SYMBOL: {key: bool|float}}；同时收集无法识别的项。"""
    out, bad = {}, []
    if not isinstance(rules, dict):
        if rules:
            bad.append("symbolRules 应为对象")
        return out, bad
    for sym, ov in rules.items():
        key = "*" if str(sym).strip() == "*" else str(sym).strip().upper()
        if not isinstance(ov, dict):
            bad.append("%s（值应为对象）" % sym)
            continue
        clean = {}
        for k, v in ov.items():
            k = str(k).strip()
            if k not in RULES_KEYS:
                bad.append("%s.%s（未知信号）" % (key, k))
                continue
            if k in RULE_PRICE_KEYS and not isinstance(v, bool):
                try:
                    clean[k] = max(0.0, float(v))
                except (TypeError, ValueError):
                    bad.append("%s.%s（价位应为数字）" % (key, k))
                continue
            clean[k] = bool(v)
        if clean:
            out.setdefault(key, {}).update(clean)
    return out, bad


def apply_symbol_rules(alerts: dict, rules: dict, symbol: str) -> dict:
    """把 symbolRules 叠加到任务级 alerts 上（先通配 "*"，再具体币种）。"""
    if not rules:
        return dict(alerts)
    out = dict(alerts)
    for scope in ("*", str(symbol or "").strip().upper()):
        for k, v in (rules.get(scope) or {}).items():
            if k in RULE_PRICE_KEYS:
                if v is True:          # true = 照旧用任务里填的价位
                    out[k] = out.get(k) or 0
                elif v is False:       # false = 关掉这个价位提醒
                    out[k] = 0
                else:
                    out[k] = _fnum(v, 0)
            else:
                out[k] = bool(v)
    return out


def rules_note(base: dict, eff: dict, symbol: str) -> str:
    """任务级 alerts 与最终生效 alerts 的差异，给人看的短句（无差异返回空串）。"""
    off, on = [], []
    for k, _lab in RULES_SIGNALS:
        if k not in eff and k not in base:
            continue
        if k in RULE_PRICE_KEYS:
            if _fnum(eff.get(k), 0) != _fnum(base.get(k), 0):
                (on if _fnum(eff.get(k), 0) > 0 else off).append(RULES_LABELS[k])
            continue
        if not base.get(k) and eff.get(k):
            on.append(RULES_LABELS[k])
        elif base.get(k) and not eff.get(k):
            off.append(RULES_LABELS[k])
    seg = []
    if off:
        seg.append("关掉 " + "、".join(off))
    if on:
        seg.append("打开 " + "、".join(on))
    return "%s：%s" % (symbol, "；".join(seg)) if seg else ""


def rules_overview(cfg: dict) -> list:
    """给 --status 用：只列出被单独改动过的币种规则。"""
    rules = cfg.get("_symbol_rules") or {}
    lines = []
    for sym in sorted(rules):
        items = []
        for k, _lab in RULES_SIGNALS:
            if k not in rules[sym]:
                continue
            v = rules[sym][k]
            if k in RULE_PRICE_KEYS:
                items.append("%s=%s" % (RULES_LABELS[k], fmt_price(v) if v else "关"))
            else:
                items.append("%s=%s" % (RULES_LABELS[k], "开" if v else "关"))
        if items:
            lines.append("%s：%s" % (sym, "，".join(items)))
    return lines
# 共振提醒句（用户指定）。想改措辞只改这里。
HINTS_RESO = {
    RESO_GOLDEN: "结合大周期和日K 看看是不是要出方向了 是不是应该有决定？",
    RESO_DEATH: "结合大周期和日K 看看是不是要出方向了 是不是应该有决定？",
}


def event_hints(events) -> list:
    """按 (周期, 金叉/死叉) 生成操作提示，同一条只出现一次，顺序与事件一致。"""
    out = []
    for e in events:
        t = e.get("type") or ""
        if t in RESO_TYPES:            # 多周期共振：整类共用一条提醒句
            msg = HINTS_RESO.get(t)
            if msg and msg not in out:
                out.append(msg)
            continue
        if t in GOLDEN_TYPES:
            kind = "golden"
        elif t in DEATH_TYPES:
            kind = "death"
        else:
            continue
        msg = HINTS.get(((e.get("interval") or "").lower(), kind))
        if msg and msg not in out:
            out.append(msg)
    return out


def hints_html(events) -> str:
    lines = event_hints(events)
    if not lines:
        return ""
    body = "<br>".join(
        "<span style=\"font-size:19px;font-weight:700;color:#C81E1E\">"
        "· %s</span>" % h for h in lines)
    return ("<div style=\"background:#FFF3D6;border-left:6px solid #FF8A00;"
            "padding:13px 16px;margin:0 0 14px\">"
            "<div style=\"font-size:22px;font-weight:800;color:#E04A00;"
            "letter-spacing:1px;margin:0 0 6px\">⚡ 操作提示</div>"
            "<div style=\"font-size:19px;line-height:2.0;color:#C81E1E\">"
            "%s</div></div>") % body


def hints_text(events) -> list:
    lines = event_hints(events)
    if not lines:
        return []
    return ["【操作提示】"] + ["· " + h for h in lines] + [""]


# ---------------------------------------------------------------- 指标解读与建议
SHORT_LABEL = {
    "golden": "金叉",
    "goldenBelowZero": "零轴下方金叉",
    "death": "死叉",
    "deathAboveZero": "零轴上方死叉",
    RESO_GOLDEN: "三周期共振·零轴下方金叉",
    RESO_DEATH: "三周期共振·零轴上方死叉",
}


def _num(v, nd=1):
    return "-" if v is None else ("%.*f" % (nd, v))


def _adx_txt(v):
    if v is None:
        return "ADX 数据不足"
    if v >= 25:
        return "ADX %.1f（趋势较强）" % v
    if v >= 20:
        return "ADX %.1f（趋势酝酿）" % v
    return "ADX %.1f（震荡为主）" % v


def _rsi_txt(v):
    if v is None:
        return "RSI 数据不足"
    if v >= 70:
        return "RSI %.1f（已超买）" % v
    if v >= 55:
        return "RSI %.1f（偏强）" % v
    if v > 45:
        return "RSI %.1f（中性）" % v
    if v > 30:
        return "RSI %.1f（偏弱）" % v
    return "RSI %.1f（已超卖）" % v


# ---------------------------------------------------------------- 参考仓位
# 思路：先锁定「这笔最多亏多少」，再反推「该买多少」。
#   风险金额 = 总资金 × 基础风险比例 × 档位倍数
#   仓位数量 = 风险金额 ÷ 参考止损距离（1.5 × ATR）
QUOTES = ("USDT", "USDC", "BUSD", "FDUSD", "USD", "BTC", "ETH")
BASE_ALIAS = {"RAYSOL": "RAY"}      # 合约代码 -> 常见叫法，只为邮件里读着顺


def fmt_qty(v: float) -> str:
    """数量（币的个数）小数位自适应：高价币给足位数，山寨币取整。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "-"
    if v <= 0:
        return "0"
    if v >= 1000:
        s = "%.0f" % v
    elif v >= 1:
        s = "%.4f" % v
    elif v >= 0.01:
        s = "%.5f" % v
    else:
        s = "%.8f" % v
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def base_asset(symbol: str) -> str:
    """BTCUSDT -> BTC；RAYSOLUSDT -> RAY。取不到就原样返回。"""
    s = (symbol or "").upper().replace("_", "").replace("-", "").replace("/", "")
    for q in QUOTES:
        if s.endswith(q) and len(s) > len(q):
            b = s[: -len(q)]
            return BASE_ALIAS.get(b, b)
    return symbol


def risk_tier(adx, rsi):
    """按 ADX / RSI 给出（相对基础风险的倍数, 档位名）。"""
    if rsi is not None and rsi >= 70:
        return 0.5, "追高档 0.5倍"
    if adx is None:
        return 1.0, "标准档 1倍"
    if adx >= 30 and (rsi is None or 40 <= rsi <= 65):
        return 2.0, "满档 2倍"
    if adx >= 25:
        return 1.0, "标准档 1倍"
    if adx >= 20:
        return 0.5, "试探档 0.5倍"
    return 0.5, "震荡档 0.5倍"


def stop_distance(e):
    """参考止损距离 = 1.5 × ATR；ATR 缺失时返回 None。"""
    atr = e.get("atr")
    if atr is None or atr <= 0:
        return None
    return 1.5 * atr


def position_hint(e, capital: float = 0.0, risk_pct: float = 1.0) -> str:
    """金叉类信号给出「参考仓位」。死叉是离场信号、不给开仓量；capital<=0 时整体关闭。"""
    capital = _fnum(capital, 0)
    risk_pct = _fnum(risk_pct, 1.0)
    if capital <= 0:
        return ""
    t = e.get("type") or ""
    if t == RESO_GOLDEN:
        # 三周期共振属左侧反转尝试，档位封顶在「试探档」，不在零轴下方重仓
        mult, tier = 0.5, "共振试探档 0.5倍"
    elif t in GOLDEN_TYPES:
        mult, tier = risk_tier(e.get("adx"), e.get("rsi"))
    else:
        return ""
    px = e.get("close")
    dist = stop_distance(e)
    if not px or px <= 0 or not dist:
        return ""
    risk_amt = capital * risk_pct / 100.0 * mult
    qty = risk_amt / dist
    notional = qty * px
    pct = notional / capital * 100.0
    warn = ""
    if notional > capital * 1.001:
        warn = "，需 %.1f 倍杠杆" % (notional / capital)
    elif notional < 5:
        warn = "，低于最小下单额"
    return "参考仓位 %s %s（名义 %.0f USDT，占总资金 %.0f%%，止损价 %s，最大亏损 %.2f USDT，%s%s）" % (
        fmt_qty(qty), base_asset(e.get("symbol")), notional, pct,
        fmt_price(px - dist), risk_amt, tier, warn)


def _reso_reading(e, capital: float = 0.0, risk_pct: float = 1.0) -> str:
    """多周期共振的解读：强调「三周期同步」，但属左侧反转尝试，建议轻仓等确认。"""
    adx, r, atr = e.get("adx"), e.get("rsi"), e.get("atr")
    below = (e.get("type") or "") == RESO_GOLDEN
    seg = []
    for h in e.get("reso") or []:
        t = (h.get("time") or "")
        seg.append("%s %s %s" % (h.get("interval"),
                                 "金叉" if h["kind"] == RESO_GOLDEN else "死叉",
                                 t[11:16] if len(t) >= 16 else t))
    weak = adx is not None and adx < 20
    stop = "" if atr is None else "，参考止损 1.5×ATR ≈ %s" % fmt_price(1.5 * atr)
    if below:
        if weak:
            tip = ("三周期同步转向、但大周期动能仍弱，多半是超跌反弹；先轻仓试探，"
                   "等 ADX 站上 25 或 4H/日K 确认方向后再加") + stop
        elif r is not None and r >= 70:
            tip = "低位反转已启动但动能偏热，别追高，等回踩不破再看" + stop
        else:
            tip = ("短中周期已同步转多，可作底部反转的左侧尝试（轻仓），"
                   "以 4H / 日K 能否站上零轴作为加仓条件") + stop
    else:
        if weak:
            tip = "三周期同步转弱、但大周期动能不强，先减半观察，既别急着清仓也别急着抄底" + stop
        else:
            tip = "短中周期已同步转空，注意减仓、别急着抄底；有合约仓位的优先降杠杆" + stop
    iv_txt = (e.get("interval") or "").upper()
    zh = "零轴下方金叉" if below else "零轴上方死叉"
    line = "%s %s 三周期共振·%s ｜ %s ｜ 4H 读数：%s · %s ｜ 解读：%s" % (
        e.get("symbol"), iv_txt, zh, " · ".join(seg),
        _adx_txt(adx), _rsi_txt(r), tip)
    hint = position_hint(e, capital, risk_pct)
    return line + (" ｜ " + hint if hint else "")


def event_reading(e, capital: float = 0.0, risk_pct: float = 1.0) -> str:
    """把单个信号的「零轴位置 + ADX 趋势强度 + RSI 强弱 + ATR 波幅」拼成一句解读和建议。

    只对金叉/死叉类信号解读；柱体变色、零轴穿越、价格突破不解读。
    金叉类信号若配置了总资金，还会附上「参考仓位」。
    """
    t = e.get("type") or ""
    if t in RESO_TYPES:
        return _reso_reading(e, capital, risk_pct)
    if t in GOLDEN_TYPES:
        kind = "golden"
    elif t in DEATH_TYPES:
        kind = "death"
    else:
        return ""
    adx, r, atr = e.get("adx"), e.get("rsi"), e.get("atr")
    if adx is None and r is None:
        return ""
    strong = adx is not None and adx >= 25
    weak = adx is not None and adx < 20
    stop = "" if atr is None else "，参考止损 1.5×ATR ≈ %s" % fmt_price(1.5 * atr)
    exit_line = "" if atr is None else "，参考离场线 1.5×ATR ≈ %s" % fmt_price(1.5 * atr)
    if kind == "golden":
        if r is not None and r >= 70:
            tip = "动能已有透支迹象，别追高，等回踩不破再看"
        elif strong:
            tip = "趋势与动能配合，可按计划分批进" + stop
        elif weak:
            tip = "多半是震荡中的反弹，先轻仓试探，等 ADX 站上 25 再加"
        else:
            tip = "信号中等，别重仓，等站稳零轴或 ADX 走强再确认"
    else:
        if strong:
            tip = "趋势转弱确认度较高，注意减仓，别急着抄底"
        elif weak:
            tip = "震荡里的回落，未必是趋势反转，可先减半观察"
        else:
            tip = "先减一部分留一部分" + exit_line
    pos = "零轴上方" if (e.get("dif") or 0) > 0 else "零轴下方"
    line = "%s %s %s（%s）→ %s · %s ｜ 解读：%s" % (
        e.get("symbol"), (e.get("interval") or "").upper(),
        SHORT_LABEL.get(t, t), pos, _adx_txt(adx), _rsi_txt(r), tip)
    hint = position_hint(e, capital, risk_pct)
    return line + (" ｜ " + hint if hint else "")


def event_readings(events, capital: float = 0.0, risk_pct: float = 1.0) -> list:
    out = []
    for e in events:
        s = event_reading(e, capital, risk_pct)
        if s and s not in out:
            out.append(s)
    return out


def reads_html(events, capital: float = 0.0, risk_pct: float = 1.0) -> str:
    lines = event_readings(events, capital, risk_pct)
    if not lines:
        return ""
    body = "<br>".join(
        "<span style=\"font-size:16px;font-weight:700;color:#0C447C\">"
        "· %s</span>" % h for h in lines)
    return ("<div style=\"background:#E6F1FB;border-left:6px solid #378ADD;"
            "padding:13px 16px;margin:0 0 14px\">"
            "<div style=\"font-size:22px;font-weight:800;color:#0C447C;"
            "letter-spacing:1px;margin:0 0 6px\">📊 指标解读与建议</div>"
            "<div style=\"font-size:16px;line-height:1.9;color:#185FA5\">"
            "%s</div></div>") % body


def reads_text(events, capital: float = 0.0, risk_pct: float = 1.0) -> list:
    lines = event_readings(events, capital, risk_pct)
    if not lines:
        return []
    return ["【指标解读与建议】"] + ["· " + h for h in lines] + [""]


def event_table(events, title: str, capital: float = 0.0, risk_pct: float = 1.0) -> str:
    rows = []
    for e in events:
        cls = "up" if e["type"] in ("golden", "goldenBelowZero", "priceAbove",
                                    "zeroCross", RESO_GOLDEN) else "down"
        rows.append(
            "<tr>"
            "<td>%s</td><td>%s</td><td class='%s'>%s</td>"
            "<td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
            "<td align='right'>%s</td><td align='right'>%s</td>"
            "</tr>" % (
                e["symbol"], e["interval"].upper(), cls, e["label"],
                e["time"], fmt_price(e["close"]),
                ("%+.6f" % e["dif"]).rstrip("0").rstrip("."),
                ("%+.6f" % e["hist"]).rstrip("0").rstrip("."),
                _num(e.get("adx")), _num(e.get("rsi")),
            ))
    return """<html><body style="font-family:system-ui,'Microsoft YaHei',sans-serif;color:#2C2C2A">
<h3 style="margin:0 0 10px">%s</h3>
%s<table cellspacing="0" cellpadding="7" style="border-collapse:collapse;font-size:13px">
<thead><tr style="background:#f7f6f3;color:#8a8a85">
<th align="left">币种</th><th align="left">周期</th><th align="left">信号</th>
<th align="left">确认时间(北京)</th><th align="right">收盘价</th>
<th align="right">DIF</th><th align="right">MACD柱</th>
<th align="right">ADX</th><th align="right">RSI</th></tr></thead>
<tbody>%s</tbody></table>
<p style="font-size:12px;color:#8a8a85;line-height:1.7;margin-top:12px">
.up{color:#D8453F}.down{color:#12946A}<br>
ADX ≥ 25 视为趋势市（信号更可信），＜ 20 多为震荡市（假信号偏多）；RSI 70 以上超买、30 以下超卖。<br>
参考仓位 = 总资金 × 单笔风险比例 × 档位倍数 ÷（1.5×ATR 止损距离）；档位由 ADX/RSI 决定，仅金叉信号给出，死叉属离场信号不给开仓量。<br>
多周期共振 = 各周期同时出现同一种零轴反转形态；DIF / 柱 / ADX / RSI 取最大周期那根读数，参考仓位封顶在「试探档 0.5 倍」。<br>
信号按 K 线收盘确认，未收盘 K 线不计入。本邮件由本地程序自动发送，仅供参考，不构成投资建议。
</p></body></html>""" % (title, hints_html(events) + reads_html(events, capital, risk_pct),
                        "".join(rows))


def event_text(events, title: str, capital: float = 0.0, risk_pct: float = 1.0) -> str:
    lines = [title, ""]
    lines += hints_text(events)
    lines += reads_text(events, capital, risk_pct)
    for e in events:
        lines.append("[%s] %s %s  %s  收盘 %s  DIF %+.6f  柱 %+.6f  ADX %s  RSI %s" % (
            e["symbol"], e["interval"].upper(), e["label"], e["time"],
            fmt_price(e["close"]), e["dif"], e["hist"],
            _num(e.get("adx")), _num(e.get("rsi"))))
    lines += ["", "参考仓位 = 总资金 × 单笔风险比例 × 档位倍数 ÷（1.5×ATR 止损距离）；仅金叉信号给出。",
              "信号按 K 线收盘确认，未收盘 K 线不计入。",
              "本邮件由本地程序自动发送，仅供参考，不构成投资建议。"]
    return "\n".join(lines)


# ---------------------------------------------------------------- 多周期共振任务
def _run_resonance_task(t: dict, name: str, st: dict, cfg: dict,
                        cap: float, riskp: float, dry_run: bool = False):
    """多周期共振任务的一轮处理。返回 (事件数, 邮件数, 问题列表, 新状态)。"""
    ivs = [str(x).lower() for x in t["intervals"]]
    window = max(1, int(t.get("window") or 3))
    market = (t.get("market") or "spot").lower()
    alerts = dict(DEFAULT_RESO_ALERTS)
    alerts.update(t.get("alerts") or {})
    first_run = not st
    paused = not bool(cfg.get("mailEnabled", True))
    old_sigs = dict(st.get("sigs") or {})
    sigs = dict(old_sigs)
    collected, statuses, closed_list, failed, rule_notes = [], [], [], [], []

    for sym in t.get("symbols") or []:
        sym_alerts = apply_symbol_rules(alerts, cfg.get("_symbol_rules") or {}, sym)
        note = rules_note(alerts, sym_alerts, sym)
        if note:
            rule_notes.append(note)
        try:
            ev, status, closed_ms, sig = resonance_scan(sym, ivs, window, sym_alerts, market)
        except Exception as exc:  # noqa: BLE001
            failed.append("%s: %s" % (sym, exc))
            log.warning("任务[%s] %s 抓取失败：%s", name, sym, exc)
            continue
        statuses.append(status)
        closed_list.append(closed_ms)
        sigs[sym] = sig
        if ev and sig and sig != (old_sigs.get(sym) or ""):
            collected.append(ev)

    collected.sort(key=lambda e: (e["time_ms"], e["symbol"]))
    if rule_notes:
        log.info("任务[%s] 提醒规则生效：%s", name, " ｜ ".join(rule_notes))
    log.info("任务[%s] 共振窗口=最近 %s 根，命中 %d 个事件，成功 %d/%d 币种",
             name, window, len(collected), len(statuses), len(t.get("symbols") or []))

    problems = []
    if dry_run:
        for e in collected:
            log.info("  [DRY] %s %s %s %s", e["symbol"], (e["interval"] or "").upper(),
                     e["label"], e["time"])
        return len(collected), 0, problems, st        # dry-run 不写状态

    if paused:
        if collected:
            log.info("任务[%s] 已暂停（mailEnabled=false）：%d 个共振信号不发信",
                     name, len(collected))
            collected = []

    if first_run and collected:
        log.info("任务[%s] 首次运行：跳过发送（%d 个共振信号仅记为基线）", name, len(collected))
        collected = []

    to = [a for a in (t.get("to") or []) if a]
    sent_ok, mails = True, 0
    if collected and not to:
        sent_ok = False
        problems.append("%s 未配置收件人" % name)
        log.error("任务[%s] 没有收件人：请在 GitHub Secrets 里添加 MACD_MAIL_TO，"
                  "或在 alert_config.json 的 to 里填收件邮箱；本轮不发信", name)
    if collected and to:
        title = "【MACD 共振】%s（%d 个）" % (
            "、".join(sorted({e["symbol"].replace("USDT", "") for e in collected})),
            len(collected))
        try:
            send_mail(cfg["smtp"], to, title,
                      event_text(collected, title, cap, riskp),
                      event_table(collected, title, cap, riskp))
            mails += 1
            log.info("任务[%s] 已发共振邮件至 %s（%d 个事件）", name, ",".join(to), len(collected))
        except Exception as exc:  # noqa: BLE001
            sent_ok = False
            problems.append("%s 发信失败：%s" % (name, exc))
            log.error("任务[%s] 发信失败：%s", name, exc)

    if first_run and cfg.get("notifyOnStart") and sent_ok and to and not paused:
        body = ("监控已启动（多周期共振）。\n\n任务：%s\n币种：%s\n周期：%s\n"
                "共振窗口：各周期最近 %s 根已收盘 K 线\n收件人：%s\n启动时间：%s\n" % (
                    name, ", ".join(t.get("symbols") or []),
                    " / ".join(iv.upper() for iv in ivs), window,
                    ", ".join(to), now_bj()))
        try:
            send_mail(cfg["smtp"], to,
                      "【MACD 共振】已启动（%s）" % " / ".join(iv.upper() for iv in ivs),
                      body, "<pre style='font:13px/1.6 Consolas,monospace'>%s</pre>" % body)
            mails += 1
            log.info("任务[%s] 已发启动通知", name)
        except Exception as exc:  # noqa: BLE001
            sent_ok = False
            problems.append("%s 启动通知失败：%s" % (name, exc))

    if not sent_ok:      # 发信失败不落新签名，下一轮重试
        for e in collected:
            sigs[e["symbol"]] = old_sigs.get(e["symbol"], "")
        log.warning("任务[%s] 保持签名不动（失败项：%s）", name,
                    "；".join(failed) if failed else "邮件未发出")

    advanced = int(st.get("since") or 0)
    if closed_list:
        advanced = max(closed_list)
    new_st = {
        "since": advanced, "since_time": bj(advanced) if advanced else "",
        "last_run": now_bj(), "last_events": len(collected),
        "last_status": statuses, "sigs": sigs, "failed": failed,
    }
    return len(collected), mails, problems, new_st


# ---------------------------------------------------------------- 主流程
def run_once(cfg: dict, dry_run: bool = False, force_test: bool = False) -> int:
    state = load_json(STATE_PATH, {})
    tasks = cfg["tasks"]
    cap = _fnum(cfg.get("capital"), 0)
    riskp = _fnum(cfg.get("riskPct"), 1.0)
    if not tasks:
        log.warning("alert_config.json 里没有任何 task，未做任何事")
        return 0

    paused = not bool(cfg.get("mailEnabled", True))
    if paused:
        log.warning("配置里 mailEnabled=false：本轮只扫描不发信（游标照常推进）")

    total_events, mails, problems = 0, 0, []
    for t in tasks:
        name = t.get("name") or ("%s-%s" % (
            ",".join(t.get("symbols") or []),
            t.get("interval") or "/".join(t.get("intervals") or [])))
        st = state.get(name) or {}
        if isinstance(t.get("intervals"), list) and len(t["intervals"]) >= 2:
            n_ev, n_mail, probs, new_st = _run_resonance_task(
                t, name, st, cfg, cap, riskp, dry_run)
            total_events += n_ev
            mails += n_mail
            problems.extend(probs)
            state[name] = new_st
            continue
        since = int(st.get("since") or 0)
        first_run = not st
        alerts = dict(DEFAULT_ALERTS)
        alerts.update(t.get("alerts") or {})
        price_states = st.get("price") or {}
        market = (t.get("market") or "spot").lower()
        collected, statuses, closed_list, failed = [], [], [], []

        rule_notes = []
        for sym in t.get("symbols") or []:
            sym_alerts = apply_symbol_rules(alerts, cfg.get("_symbol_rules") or {}, sym)
            note = rules_note(alerts, sym_alerts, sym)
            if note:
                rule_notes.append(note)
            try:
                ev, status, closed_ms, pst = scan_symbol(
                    sym, t["interval"], since, sym_alerts, price_states.get(sym) or {}, market)
            except Exception as exc:  # noqa: BLE001
                failed.append("%s: %s" % (sym, exc))
                log.warning("任务[%s] %s 抓取失败：%s", name, sym, exc)
                continue
            price_states[sym] = pst
            collected.extend(ev)
            statuses.append(status)
            closed_list.append(closed_ms)

        collected.sort(key=lambda e: (e["time_ms"], e["symbol"]))
        if rule_notes:
            log.info("任务[%s] 提醒规则生效：%s", name, " ｜ ".join(rule_notes))
        total_events += len(collected)
        log.info("任务[%s] 游标 %s 起，区间事件 %d 个，成功 %d/%d 币种",
                 name, bj(since) if since else "(首次)", len(collected),
                 len(statuses), len(t.get("symbols") or []))

        if dry_run:
            for e in collected:
                log.info("  [DRY] %s %s %s %s 收 %s", e["symbol"], e["interval"],
                         e["label"], e["time"], fmt_price(e["close"]))
            continue

        if paused:
            if collected:
                log.info("任务[%s] 已暂停（mailEnabled=false）：%d 个事件不发信",
                         name, len(collected))
                collected = []

        if first_run and collected:
            # 首次运行只初始化游标，避免把最近一批历史信号全发出来
            log.info("任务[%s] 首次运行：跳过发送（%d 个历史事件仅记录）", name, len(collected))
            collected = []

        sent_ok = True
        to = [a for a in (t.get("to") or []) if a]
        if collected and not to:
            sent_ok = False
            problems.append("%s 未配置收件人" % name)
            log.error("任务[%s] 没有收件人：请在 GitHub Secrets 里添加 MACD_MAIL_TO，"
                      "或在 alert_config.json 的 to 里填收件邮箱；本轮不发信", name)
        if collected and to:
            title = "【MACD 信号】%s（%d 个）" % (
                "、".join(sorted({e["symbol"].replace("USDT", "") for e in collected})), len(collected))
            try:
                send_mail(cfg["smtp"], to, title,
                          event_text(collected, title, cap, riskp),
                          event_table(collected, title, cap, riskp))
                mails += 1
                sent_ok = True
                log.info("任务[%s] 已发邮件至 %s（%d 个事件）", name, ",".join(to), len(collected))
            except Exception as exc:  # noqa: BLE001
                sent_ok = False
                problems.append("%s 发信失败：%s" % (name, exc))
                log.error("任务[%s] 发信失败：%s", name, exc)

        if first_run and cfg.get("notifyOnStart") and sent_ok and to and not paused:
            body = "监控已启动。\n\n任务：%s\n币种：%s\n周期：%s\n收件人：%s\n启动时间：%s\n" % (
                name, ", ".join(t.get("symbols") or []), t["interval"],
                ", ".join(to), now_bj())
            try:
                send_mail(cfg["smtp"], to,
                          "【MACD 监控】已启动（%s %s）" % ("/".join(t.get("symbols") or []), t["interval"]),
                          body, "<pre style='font:13px/1.6 Consolas,monospace'>%s</pre>" % body)
                mails += 1
                log.info("任务[%s] 已发启动通知", name)
            except Exception as exc:  # noqa: BLE001
                sent_ok = False
                problems.append("%s 启动通知失败：%s" % (name, exc))

        # 游标推进规则：所有币种抓取成功、且该发的邮件都发成功，才推进
        advanced = since
        if closed_list and not failed and sent_ok:
            advanced = max(closed_list)
        elif failed or not sent_ok:
            log.warning("任务[%s] 保持游标不动（失败项：%s）", name,
                        "；".join(failed) if failed else "邮件未发出")

        state[name] = {
            "since": advanced,
            "since_time": bj(advanced) if advanced else "",
            "last_run": now_bj(),
            "last_events": len(collected),
            "last_status": statuses,
            "price": price_states,
            "failed": failed,
        }

    save_json(STATE_PATH, state)
    trim_log()
    log.info("本轮结束：事件 %d 个，邮件 %d 封，异常 %d 项", total_events, mails, len(problems))
    return 0


def cmd_set_password(cfg: dict, pwd_arg: str = "") -> int:
    """写入 SMTP 授权码（可交互输入），随即发一封测试邮件验证通道。"""
    user = cfg["smtp"]["user"]
    pwd = (pwd_arg or "").strip()
    if not pwd:
        print("即将为发件邮箱 %s 写入 SMTP 授权码（注意：是授权码，不是邮箱登录密码）。" % user)
        print("获取方式：登录 163 邮箱 → 设置 → POP3/SMTP/IMAP → 开启「SMTP 服务」→ 复制生成的授权码。")
        print("（粘贴时在窗口内点右键即可粘贴；直接回车表示取消）\n")
        try:
            pwd = input("请粘贴授权码后回车：").strip()
        except (EOFError, KeyboardInterrupt):
            pwd = ""
    if not pwd:
        print("已取消，未做任何修改。")
        return 1

    raw = load_json(CONFIG_PATH, {}) or {}
    raw.setdefault("smtp", {})["password"] = pwd
    save_json(CONFIG_PATH, raw)
    print("授权码已写入 %s（共 %d 个字符）。" % (CONFIG_PATH.name, len(pwd)))

    to = test_recipients(cfg)
    try:
        send_mail({**cfg["smtp"], "password": pwd}, to,
                  "【MACD 监控】测试邮件",
                  "这是一封测试邮件，收到即表示本地 Python 程序的邮件通道可用。\n时间：%s" % now_bj(),
                  "<p>这是一封测试邮件，收到即表示本地 Python 程序的邮件通道可用。<br>时间：%s</p>" % now_bj())
        print("测试邮件已发出，收件人：%s，请查收。" % "、".join(to))
        log.info("授权码已更新，测试邮件已发送至 %s", to)
        return 0
    except Exception as exc:  # noqa: BLE001
        print("授权码已保存，但测试邮件发送失败：%s" % exc)
        print("提示：163 邮箱授权码只显示一次，请确认输入无误；若刚开启 SMTP，稍等 1~2 分钟再试。")
        log.error("测试邮件发送失败：%s", exc)
        return 1


def cmd_status(cfg: dict) -> None:
    state = load_json(STATE_PATH, {})
    print("配置文件：%s" % CONFIG_PATH)
    smtp = cfg["smtp"]
    print("SMTP：%s:%s  user=%s  授权码=%s" % (
        smtp["host"], smtp["port"], smtp["user"],
        "已填写" if smtp.get("password") else "**未填写**"))
    print("检查频率：每 %s 分钟" % cfg["pollMinutes"])
    print("参考仓位：总资金 %s USDT，单笔基础风险 %s%%（填 0 关闭）" % (
        cfg.get("capital"), cfg.get("riskPct")))
    print("邮件总开关：%s" % ("开启" if cfg.get("mailEnabled", True) else "**已暂停（只扫描不发信）**"))
    _over = rules_overview(cfg)
    if _over:
        print("提醒规则（仅列被单独改动过的币种，其余跟随任务原设置）：")
        for line in _over:
            print("   · %s" % line)
    else:
        print("提醒规则：未做任何单独改动（全部跟随任务原设置）")
    for _b in (cfg.get("_symbol_rules_bad") or []):
        print("   ⚠ 无法识别的规则：%s" % _b)
    print("任务数：%d" % len(cfg["tasks"]))
    for t in cfg["tasks"]:
        name = t.get("name")
        st = state.get(name) or {}
        ivs = t.get("intervals")
        if isinstance(ivs, list) and len(ivs) >= 2:      # 多周期共振任务
            print("\n■ %s（多周期共振）" % name)
            print("   币种：%s" % ", ".join(t.get("symbols") or []))
            print("   周期：%s   共振窗口：各周期最近 %s 根   收件人：%s" % (
                " / ".join(str(iv).upper() for iv in ivs), t.get("window") or 3,
                ", ".join(t.get("to") or [])))
            print("   上次运行：%s   上次事件：%s" % (
                st.get("last_run") or "-", st.get("last_events", "-")))
            for s in st.get("last_status") or []:
                h = s.get("hits") or {}
                seg = []
                for iv in s.get("intervals") or ivs:
                    one = h.get(iv)
                    seg.append("%s=%s" % (str(iv).upper(), "-" if not one else (
                        "零轴下金叉" if one.get("kind") == RESO_GOLDEN else "零轴上死叉")))
                print("     %-11s %s   共振=%s" % (s["symbol"], "  ".join(seg),
                                                 s.get("resonance")))
            continue
        print("\n■ %s" % name)
        print("   币种：%s   周期：%s   收件人：%s" % (
            ", ".join(t.get("symbols") or []), t.get("interval"), ", ".join(t.get("to") or [])))
        print("   游标：%s (%s)   上次运行：%s" % (
            st.get("since", "(未初始化)"), st.get("since_time") or "-", st.get("last_run") or "-"))
        for s in st.get("last_status") or []:
            print("     %-10s %s 收 %s  DIF %+.6f  DEA %+.6f  柱 %+.6f  零轴上=%s 多头=%s  ADX %s  RSI %s" % (
                s["symbol"], s["bar_time"], fmt_price(s["bar_close"]),
                s["dif"], s["dea"], s["hist"], s["above_zero"], s["above_dea"],
                _num(s.get("adx")), _num(s.get("rsi"))))
    print("\n状态文件：%s\n日志文件：%s" % (STATE_PATH, LOG_PATH))


def main() -> int:
    ap = argparse.ArgumentParser(description="MACD 邮件提醒（本地全自动版）")
    ap.add_argument("--dry-run", action="store_true", help="只扫描打印，不发邮件、不写游标")
    ap.add_argument("--test", action="store_true", help="发送测试邮件后退出")
    ap.add_argument("--set-password", dest="set_password", nargs="?", const="", default=None,
                    help="填写 SMTP 授权码（可跟值；不跟值则交互输入），随即发送测试邮件")
    ap.add_argument("--status", action="store_true", help="打印配置与游标状态")
    ap.add_argument("--reset", action="store_true", help="清空游标与状态")
    ap.add_argument("--loop", action="store_true", help="常驻模式，按 pollMinutes 循环")
    ap.add_argument("-v", "--verbose", action="store_true", help="同时输出到控制台")
    args = ap.parse_args()

    setup_log(args.verbose or args.status or args.dry_run or args.test or args.set_password is not None)
    cfg = load_config()

    if args.status:
        cmd_status(cfg)
        return 0

    if args.set_password is not None:
        return cmd_set_password(cfg, args.set_password)

    if args.reset:
        save_json(STATE_PATH, {})
        print("已清空 %s" % STATE_PATH)
        return 0

    if args.test:
        try:
            to = test_recipients(cfg)
            send_mail(cfg["smtp"], to,
                      "【MACD 监控】测试邮件",
                      "这是一封测试邮件，收到即表示本地 Python 程序的邮件通道可用。\n时间：%s" % now_bj(),
                      "<p>这是一封测试邮件，收到即表示本地 Python 程序的邮件通道可用。<br>时间：%s</p>" % now_bj())
            print("测试邮件已发送，收件人：%s，请查收。" % "、".join(to))
            log.info("测试邮件已发送")
            return 0
        except Exception as exc:  # noqa: BLE001
            print("发送失败：%s" % exc)
            log.error("测试邮件发送失败：%s", exc)
            return 1

    if args.dry_run:
        return run_once(cfg, dry_run=True)

    if args.loop:
        log.info("进入常驻模式，每 %s 分钟检查一次", cfg["pollMinutes"])
        while True:
            try:
                run_once(cfg)
            except Exception as exc:  # noqa: BLE001
                log.exception("本轮运行异常：%s", exc)
            time.sleep(max(60, int(cfg["pollMinutes"]) * 60))

    return run_once(cfg)


if __name__ == "__main__":
    sys.exit(main())
