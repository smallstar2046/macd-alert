# -*- coding: utf-8 -*-
"""MACD 邮件提醒 —— 本地全自动版（不依赖 WorkBuddy）

功能：按 alert_config.json 的配置抓取币安现货 K 线，逐根扫描 MACD 事件
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

# 上游列表：(显示名, 解析器, 地址)。按顺序尝试，任一成功即返回。
# 币安对部分地区 IP 会返回 451，故加入 MEXC（格式与币安一致）与 Gate 作为后备，
# 这样云端（GitHub Actions / 海外服务器）与国内网络都能取到行情。
UPSTREAMS = [
    ("binance.vision", "binance", "https://data-api.binance.vision"),
    ("binance.gcp", "binance", "https://api-gcp.binance.com"),
    ("binance.api1", "binance", "https://api1.binance.com"),
    ("binance.api2", "binance", "https://api2.binance.com"),
    ("mexc", "binance", "https://api.mexc.com"),
    ("gate", "gate", "https://api.gateio.ws"),
]
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) macd-alert/1.0"}
KLINES_LIMIT = 400
FETCH_TIMEOUT = 20

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
        "user": "wopanpanha@163.com",
        "password": "",
        "fromName": "MACD 监控",
    },
    "pollMinutes": 10,
    "notifyOnStart": True,
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
    out["tasks"] = cfg.get("tasks") or []
    # 云端部署用：授权码可由环境变量注入（GitHub Secrets），无需写进仓库
    env_pwd = (os.environ.get("MACD_SMTP_PASSWORD") or "").strip()
    if env_pwd:
        out["smtp"]["password"] = env_pwd
    env_user = (os.environ.get("MACD_SMTP_USER") or "").strip()
    if env_user:
        out["smtp"]["user"] = env_user
    return out


# ---------------------------------------------------------------- 行情与指标
def _klines_binance_style(base: str, symbol: str, interval: str):
    """币安 / MEXC —— 两者 klines 返回格式完全一致。

    每根：[openTime, open, high, low, close, volume, closeTime, ...]
    """
    url = "%s/api/v3/klines?symbol=%s&interval=%s&limit=%d" % (
        base, symbol, interval, KLINES_LIMIT)
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


_FETCHERS = {"binance": _klines_binance_style, "gate": _klines_gate}


def fetch_klines(symbol: str, interval: str):
    """多上游容错抓取，返回 (source_name, candles)；全部失败抛 RuntimeError。"""
    errs = []
    for name, kind, base in UPSTREAMS:
        try:
            candles = _FETCHERS[kind](base, symbol, interval)
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
                price_state: dict):
    """逐根扫描区间内所有命中的事件。

    返回 (events, status, closed_ms, price_state_new)
    events 内每条含 type/label/time/time_ms/close/dif/dea/hist/gap
    """
    host, candles = fetch_klines(symbol, interval)
    closes = [c["close"] for c in candles]
    dif, dea, hist = macd(closes)
    x = cross_dir(dif, dea)
    zx = cross_dir(dif, [0.0] * len(dif))

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
                           "hist": round(hist[live], 8), "gap": 0.0})
        st["above"] = now
    if pb > 0:
        was = bool(st.get("below"))
        now = px <= pb
        if now and not was:
            events.append({"type": "priceBelow", "label": "下破 %s" % fmt_price(pb),
                           "symbol": symbol, "interval": interval, "time": bj(candles[live]["closeTime"]),
                           "time_ms": int(time.time() * 1000), "close": px,
                           "dif": round(dif[live], 8), "dea": round(dea[live], 8),
                           "hist": round(hist[live], 8), "gap": 0.0})
        st["below"] = now

    status = {
        "symbol": symbol, "interval": interval, "source": host,
        "bar_time": bj(candles[i]["closeTime"]),
        "bar_close": closes[i],
        "dif": round(dif[i], 8), "dea": round(dea[i], 8), "hist": round(hist[i], 8),
        "above_zero": dif[i] > 0, "above_dea": dif[i] > dea[i],
        "price": px, "live_dif": round(dif[live], 8), "live_dea": round(dea[live], 8),
    }
    return events, status, candles[i]["closeTime"], st


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
    if not smtp.get("password"):
        raise RuntimeError("SMTP 授权码为空：请编辑 %s，把 password 填成邮箱授权码" % CONFIG_PATH.name)
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


def event_table(events, title: str) -> str:
    rows = []
    for e in events:
        cls = "up" if e["type"] in ("golden", "goldenBelowZero", "priceAbove", "zeroCross") else "down"
        rows.append(
            "<tr>"
            "<td>%s</td><td>%s</td><td class='%s'>%s</td>"
            "<td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
            "</tr>" % (
                e["symbol"], e["interval"].upper(), cls, e["label"],
                e["time"], fmt_price(e["close"]),
                ("%+.6f" % e["dif"]).rstrip("0").rstrip("."),
                ("%+.6f" % e["hist"]).rstrip("0").rstrip("."),
            ))
    return """<html><body style="font-family:system-ui,'Microsoft YaHei',sans-serif;color:#2C2C2A">
<h3 style="margin:0 0 10px">%s</h3>
<table cellspacing="0" cellpadding="7" style="border-collapse:collapse;font-size:13px">
<thead><tr style="background:#f7f6f3;color:#8a8a85">
<th align="left">币种</th><th align="left">周期</th><th align="left">信号</th>
<th align="left">确认时间(北京)</th><th align="right">收盘价</th>
<th align="right">DIF</th><th align="right">MACD柱</th></tr></thead>
<tbody>%s</tbody></table>
<p style="font-size:12px;color:#8a8a85;line-height:1.7;margin-top:12px">
.up{color:#D8453F}.down{color:#12946A}<br>
信号按 K 线收盘确认，未收盘 K 线不计入。本邮件由本地程序自动发送，仅供参考，不构成投资建议。
</p></body></html>""" % (title, "".join(rows))


def event_text(events, title: str) -> str:
    lines = [title, ""]
    for e in events:
        lines.append("[%s] %s %s  %s  收盘 %s  DIF %+.6f  柱 %+.6f" % (
            e["symbol"], e["interval"].upper(), e["label"], e["time"],
            fmt_price(e["close"]), e["dif"], e["hist"]))
    lines += ["", "信号按 K 线收盘确认，未收盘 K 线不计入。",
              "本邮件由本地程序自动发送，仅供参考，不构成投资建议。"]
    return "\n".join(lines)


# ---------------------------------------------------------------- 主流程
def run_once(cfg: dict, dry_run: bool = False, force_test: bool = False) -> int:
    state = load_json(STATE_PATH, {})
    tasks = cfg["tasks"]
    if not tasks:
        log.warning("alert_config.json 里没有任何 task，未做任何事")
        return 0

    total_events, mails, problems = 0, 0, []
    for t in tasks:
        name = t.get("name") or ("%s-%s" % (",".join(t.get("symbols") or []), t.get("interval")))
        st = state.get(name) or {}
        since = int(st.get("since") or 0)
        first_run = not st
        alerts = dict(DEFAULT_ALERTS)
        alerts.update(t.get("alerts") or {})
        price_states = st.get("price") or {}
        collected, statuses, closed_list, failed = [], [], [], []

        for sym in t.get("symbols") or []:
            try:
                ev, status, closed_ms, pst = scan_symbol(
                    sym, t["interval"], since, alerts, price_states.get(sym) or {})
            except Exception as exc:  # noqa: BLE001
                failed.append("%s: %s" % (sym, exc))
                log.warning("任务[%s] %s 抓取失败：%s", name, sym, exc)
                continue
            price_states[sym] = pst
            collected.extend(ev)
            statuses.append(status)
            closed_list.append(closed_ms)

        collected.sort(key=lambda e: (e["time_ms"], e["symbol"]))
        total_events += len(collected)
        log.info("任务[%s] 游标 %s 起，区间事件 %d 个，成功 %d/%d 币种",
                 name, bj(since) if since else "(首次)", len(collected),
                 len(statuses), len(t.get("symbols") or []))

        if dry_run:
            for e in collected:
                log.info("  [DRY] %s %s %s %s 收 %s", e["symbol"], e["interval"],
                         e["label"], e["time"], fmt_price(e["close"]))
            continue

        if first_run and collected:
            # 首次运行只初始化游标，避免把最近一批历史信号全发出来
            log.info("任务[%s] 首次运行：跳过发送（%d 个历史事件仅记录）", name, len(collected))
            collected = []

        sent_ok = True
        if collected:
            to = t.get("to") or []
            title = "【MACD 信号】%s（%d 个）" % (
                "、".join(sorted({e["symbol"].replace("USDT", "") for e in collected})), len(collected))
            try:
                send_mail(cfg["smtp"], to, title,
                          event_text(collected, title), event_table(collected, title))
                mails += 1
                sent_ok = True
                log.info("任务[%s] 已发邮件至 %s（%d 个事件）", name, ",".join(to), len(collected))
            except Exception as exc:  # noqa: BLE001
                sent_ok = False
                problems.append("%s 发信失败：%s" % (name, exc))
                log.error("任务[%s] 发信失败：%s", name, exc)

        if first_run and cfg.get("notifyOnStart") and sent_ok:
            body = "监控已启动。\n\n任务：%s\n币种：%s\n周期：%s\n收件人：%s\n启动时间：%s\n" % (
                name, ", ".join(t.get("symbols") or []), t["interval"],
                ", ".join(t.get("to") or []), now_bj())
            try:
                send_mail(cfg["smtp"], t.get("to") or [],
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
    print("任务数：%d" % len(cfg["tasks"]))
    for t in cfg["tasks"]:
        name = t.get("name")
        st = state.get(name) or {}
        print("\n■ %s" % name)
        print("   币种：%s   周期：%s   收件人：%s" % (
            ", ".join(t.get("symbols") or []), t.get("interval"), ", ".join(t.get("to") or [])))
        print("   游标：%s (%s)   上次运行：%s" % (
            st.get("since", "(未初始化)"), st.get("since_time") or "-", st.get("last_run") or "-"))
        for s in st.get("last_status") or []:
            print("     %-10s %s 收 %s  DIF %+.6f  DEA %+.6f  柱 %+.6f  零轴上=%s 多头=%s" % (
                s["symbol"], s["bar_time"], fmt_price(s["bar_close"]),
                s["dif"], s["dea"], s["hist"], s["above_zero"], s["above_dea"]))
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
