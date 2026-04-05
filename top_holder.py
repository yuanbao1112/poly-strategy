# -*- coding: utf-8 -*-
"""
Polymarket BTC 5-minute market top holder monitoring tool.
Uses curses for in-place terminal table rendering.
"""
import curses
import re
import time
import requests
from datetime import datetime, timezone

headers = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

ALL_NAMES = [
    ("Flowers001", "UP"), ("Billy-Meat-Suit-Boy", "UP"), ("Flubsie", "UP"),
    ("Jamespn1", "UP"), ("jijobella", "UP"), ("Mark-L", "UP"),
    ("warhammer-6k", "UP"), ("Didiaodiao", "UP"), ("konichi", "UP"),
    ("Vigilant-Guerrilla", "DOWN"), ("Same-Success", "DOWN"),
    ("MeridianColdView", "DOWN"), ("babaali29", "DOWN"),
    ("Bonereaper", "DOWN"), ("Eager-Guava", "DOWN"),
    ("YoCoWo", "DOWN"), ("letsgo3", "DOWN"),
]

EXCLUDE = {
    "0xa772de12507bf3fa3b344e8e7826b3bb6d14f88c",
    "0x505da8075db50c4fe971aacf4b56cea1289c87b2",
    "0x8c74b4eef9a894433b8126aa11d1345efb2b0488",
    "0x4b27447b0370371b9e2b25be6845d7f144cec899",
    "0x1d507f92d6577b4ded3e864df93549266d12fb34",
    "0x1b3cb941da6aae2481147e8c177306950ff7be0b",
    "0x0000000000000000000000000000000000000000",
}

KNOWN_NAMES = {name for name, _ in ALL_NAMES}

# Caches populated during init; not re-fetched in the main loop
ADDR_CACHE = {}   # name -> address or None
WIN_CACHE = {}    # name -> {wr, w, l, pnl, streak}

REFRESH_INTERVAL = 20  # seconds between table refreshes


# ── Address lookup ────────────────────────────────────────────────────────────

def get_address(name):
    """Fetch and cache the proxy wallet address for a Polymarket username."""
    if name in ADDR_CACHE:
        return ADDR_CACHE[name]
    try:
        r = requests.get(
            "https://polymarket.com/@" + name,
            headers=headers, timeout=8
        )
        if r.status_code == 200:
            text = r.text
            for pattern in [
                r'"proxyWallet"\s*:\s*"(0x[a-fA-F0-9]{40})"',
                r'"address"\s*:\s*"(0x[a-fA-F0-9]{40})"',
            ]:
                m = re.search(pattern, text)
                if m and m.group(1).lower() not in EXCLUDE:
                    ADDR_CACHE[name] = m.group(1)
                    return m.group(1)
    except Exception:
        pass
    ADDR_CACHE[name] = None
    return None


# ── Historical stats ──────────────────────────────────────────────────────────

def fetch_all_activity(addr):
    """Fetch complete activity history for a wallet address."""
    all_acts = []
    offset = 0
    while True:
        try:
            r = requests.get(
                "https://data-api.polymarket.com/activity",
                params={"user": addr, "limit": 500, "offset": offset},
                headers=headers, timeout=10
            )
            if r.status_code != 200:
                break
            batch = r.json()
            if not batch:
                break
            all_acts.extend(batch)
            if len(batch) < 500:
                break
            offset += 500
            time.sleep(0.1)
        except Exception:
            break
    return all_acts


def calc_stats(addr):
    """
    Compute win rate, W/L, consecutive win streak and total PnL
    from full activity history.

    Returns dict: {wr, w, l, pnl, streak}
    """
    all_acts = fetch_all_activity(addr)

    # Group by conditionId; track chronological order for streak
    markets = {}          # conditionId -> {cost, back, first_ts}
    total_pnl = 0.0

    for a in all_acts:
        cid = a.get("conditionId", "")
        t = a.get("type", "")
        usdc = float(a.get("usdcSize") or 0)
        ts = int(a.get("timestamp") or 0)

        if cid not in markets:
            markets[cid] = {"cost": 0.0, "back": 0.0, "first_ts": ts}
        else:
            if ts > 0:
                markets[cid]["first_ts"] = min(markets[cid]["first_ts"], ts)

        if t == "BUY":
            markets[cid]["cost"] += usdc
            total_pnl -= usdc
        elif t in ("SELL", "REDEEM"):
            markets[cid]["back"] += usdc
            total_pnl += usdc

    # Sort markets newest-first for streak calculation
    sorted_markets = sorted(
        [(cid, m) for cid, m in markets.items() if m["cost"] > 0],
        key=lambda x: x[1]["first_ts"],
        reverse=True
    )

    wins = losses = 0
    streak = 0
    streak_done = False

    for _, m in sorted_markets:
        if m["back"] > m["cost"]:
            wins += 1
            if not streak_done:
                streak += 1
        else:
            losses += 1
            streak_done = True

    total = wins + losses
    wr = "{:.1f}%".format(wins * 100.0 / total) if total > 0 else "N/A"
    return {"wr": wr, "w": wins, "l": losses, "pnl": total_pnl, "streak": streak}


# ── Activity helpers ──────────────────────────────────────────────────────────

def get_recent_activity(addr, limit=100):
    """Fetch recent activity for a wallet address."""
    try:
        r = requests.get(
            "https://data-api.polymarket.com/activity",
            params={"user": addr, "limit": limit, "offset": 0},
            headers=headers, timeout=8
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return []


def get_current_value(addr):
    """Fetch current portfolio value for a wallet address."""
    try:
        r = requests.get(
            "https://data-api.polymarket.com/value",
            params={"user": addr},
            headers=headers, timeout=8
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and data:
                return float(data[0].get("value") or 0)
            if isinstance(data, dict):
                return float(data.get("value") or 0)
    except Exception:
        pass
    return 0.0


# ── BTC round detection ───────────────────────────────────────────────────────

def current_round_ts():
    """Return (round_start_ts, round_end_ts) for the current 5-minute BTC round."""
    now_ts = int(time.time())
    start = (now_ts // 300) * 300
    return start, start + 300


def get_round_position(addr, round_ts):
    """
    Return the user's position in the current BTC 5-minute round.

    Matches activity where:
      - eventSlug == slug_cur, OR
      - "Bitcoin" in title AND timestamp in [round_ts-60, round_ts+360]

    Returns dict with keys: direction, size, usdc, price, result_usdc
    """
    slug_cur = "btc-updown-5m-{}".format(round_ts)
    acts = get_recent_activity(addr, 100)

    direction = None
    size_total = 0.0
    usdc_buy = 0.0
    usdc_back = 0.0
    prices = []

    for a in acts:
        event_slug = a.get("eventSlug") or a.get("slug") or ""
        title = str(a.get("title") or "")
        act_ts = int(a.get("timestamp") or 0)
        t = a.get("type", "")
        outcome = a.get("outcome", "")
        size = float(a.get("size") or 0)
        usdc = float(a.get("usdcSize") or 0)
        price = float(a.get("price") or 0)

        slug_match = (event_slug == slug_cur)
        time_match = (
            "Bitcoin" in title
            and (round_ts - 60) <= act_ts <= (round_ts + 360)
        )

        if not (slug_match or time_match):
            continue

        if t == "BUY":
            if direction is None and outcome in ("Up", "Down"):
                direction = outcome
            size_total += size
            usdc_buy += usdc
            if price > 0:
                prices.append(price)
        elif t in ("SELL", "REDEEM"):
            usdc_back += usdc

    avg_price = sum(prices) / len(prices) if prices else 0.0

    return {
        "direction": direction,       # "Up", "Down", or None
        "size": size_total,
        "usdc": usdc_buy,
        "price": avg_price,
        "result_usdc": usdc_back,     # proceeds received (SELL + REDEEM)
    }


# ── Init phase (plain terminal) ───────────────────────────────────────────────

def run_init():
    """Load addresses and compute historical stats; print progress to terminal."""
    print("=" * 72)
    print("  Polymarket BTC 5分钟 Top Holder 监控")
    print("=" * 72)

    print("\n[1/2] 加载钱包地址...")
    for name, _ in ALL_NAMES:
        addr = get_address(name)
        display = addr if addr else "未找到"
        print("  {:<28s} {}".format(name, display))
        time.sleep(0.25)

    print("\n[2/2] 计算历史统计数据（请稍候）...")
    for name, _ in ALL_NAMES:
        addr = ADDR_CACHE.get(name)
        if addr:
            stats = calc_stats(addr)
            WIN_CACHE[name] = stats
            print(
                "  {:<28s} 胜率:{:<7s} {}W/{}L  连胜:{}  总盈亏:${:.2f}".format(
                    name,
                    stats["wr"],
                    stats["w"],
                    stats["l"],
                    stats["streak"],
                    stats["pnl"],
                )
            )
        else:
            WIN_CACHE[name] = {"wr": "N/A", "w": 0, "l": 0, "pnl": 0.0, "streak": 0}
            print("  {:<28s} 无地址，跳过".format(name))
        time.sleep(0.15)

    print("\n初始化完成，2秒后进入实时监控...\n")
    time.sleep(2)


# ── Curses rendering helpers ──────────────────────────────────────────────────

# Column definitions: (header, width)
COLUMNS = [
    ("方向",        6),
    ("昵称",        22),
    ("钱包地址",    44),
    ("本轮仓位",    10),
    ("份数",        8),
    ("买入$",       9),
    ("均价",        7),
    ("胜率",        7),
    ("盈利率",      7),
    ("连胜",        5),
    ("W/L",         10),
    ("总盈亏$",     12),
    ("本轮结果",    14),
]

# Color pair IDs
CP_TITLE  = 1   # Yellow bold on default
CP_HEADER = 2   # White on Blue
CP_UP     = 3   # Green bold
CP_DOWN   = 4   # Red bold
CP_NEW    = 5   # Yellow on Red bold (new entrant)
CP_PNL_P  = 6   # Green (positive PnL)
CP_PNL_N  = 7   # Red (negative PnL)
CP_STATUS = 8   # White on Blue


def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(CP_TITLE,  curses.COLOR_YELLOW, -1)
    curses.init_pair(CP_HEADER, curses.COLOR_WHITE,  curses.COLOR_BLUE)
    curses.init_pair(CP_UP,     curses.COLOR_GREEN,  -1)
    curses.init_pair(CP_DOWN,   curses.COLOR_RED,    -1)
    curses.init_pair(CP_NEW,    curses.COLOR_YELLOW, curses.COLOR_RED)
    curses.init_pair(CP_PNL_P,  curses.COLOR_GREEN,  -1)
    curses.init_pair(CP_PNL_N,  curses.COLOR_RED,    -1)
    curses.init_pair(CP_STATUS, curses.COLOR_WHITE,  curses.COLOR_BLUE)


def total_table_width():
    return sum(w for _, w in COLUMNS) + len(COLUMNS) + 1  # separators


def draw_hline(stdscr, row, left, mid, right, fill="═"):
    """Draw a horizontal border line."""
    max_y, max_x = stdscr.getmaxyx()
    if row >= max_y - 1:
        return
    parts = [fill * w for _, w in COLUMNS]
    line = left + mid.join(parts) + right
    try:
        stdscr.addstr(row, 0, line[:max_x - 1])
    except curses.error:
        pass


def draw_sep(stdscr, row):
    """Draw a separator row between data rows."""
    draw_hline(stdscr, row, "├", "┼", "┤", "─")


def addstr_safe(stdscr, row, col, text, attr=0):
    """addstr that clips to terminal bounds and suppresses curses errors."""
    max_y, max_x = stdscr.getmaxyx()
    if row >= max_y - 1 or col >= max_x - 1:
        return
    available = max_x - 1 - col
    if available <= 0:
        return
    try:
        stdscr.addstr(row, col, text[:available], attr)
    except curses.error:
        pass


def draw_row(stdscr, row, cells, attrs):
    """Draw a single data row with column separators."""
    max_y, max_x = stdscr.getmaxyx()
    if row >= max_y - 1:
        return
    col = 0
    addstr_safe(stdscr, row, col, "│")
    col += 1
    for i, ((_, width), text, attr) in enumerate(zip(COLUMNS, cells, attrs)):
        cell = text[:width].ljust(width)
        addstr_safe(stdscr, row, col, cell, attr)
        col += width
        addstr_safe(stdscr, row, col, "│")
        col += 1


def draw_table(stdscr, rows_data, round_count, round_ts, round_end, now_ts,
               market_label, summarized_rounds):
    """Erase and redraw the complete table in place."""
    stdscr.erase()
    max_y, max_x = stdscr.getmaxyx()

    remaining = round_end - now_ts
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if remaining > 0:
        time_str = "剩余 {:d}分{:02d}秒".format(remaining // 60, remaining % 60)
    else:
        time_str = "已结束"

    # Title bar
    title = " Round #{} | {} | {} | {} ".format(
        round_count, market_label, now_str, time_str
    )
    title_attr = curses.color_pair(CP_TITLE) | curses.A_BOLD
    addstr_safe(stdscr, 0, 0, title[:max_x - 1], title_attr)

    # Top border
    draw_hline(stdscr, 1, "╔", "╦", "╗")

    # Header row
    header_cells = [h for h, _ in COLUMNS]
    header_attrs = [curses.color_pair(CP_HEADER)] * len(COLUMNS)
    draw_row(stdscr, 2, header_cells, header_attrs)

    # Data rows
    cur_row = 3
    for i, rd in enumerate(rows_data):
        if cur_row >= max_y - 2:
            break
        # Separator between data rows (but not before the first)
        if i > 0:
            draw_sep(stdscr, cur_row)
            cur_row += 1
        if cur_row >= max_y - 2:
            break

        name = rd["name"]
        declared = rd["declared"]
        addr = ADDR_CACHE.get(name) or "未找到"
        pos = rd["pos"]
        stats = WIN_CACHE.get(name, {})

        direction = pos.get("direction")
        usdc = pos.get("usdc", 0.0)
        size = pos.get("size", 0.0)
        price = pos.get("price", 0.0)
        result_usdc = pos.get("result_usdc", 0.0)

        # Direction cell
        dir_str = declared
        if direction == "Up":
            dir_str = "▲ UP"
        elif direction == "Down":
            dir_str = "▼ DOWN"
        elif direction is None and usdc == 0:
            dir_str = declared + " 未开仓"

        # Name cell (new entrant marker)
        is_new = name not in KNOWN_NAMES
        name_str = ("★" + name) if is_new else name

        # PnL cell
        pnl = stats.get("pnl", 0.0)
        pnl_str = "${:.2f}".format(pnl)

        # Round result cell
        if remaining <= 0 and usdc > 0:
            net = result_usdc - usdc
            if result_usdc > usdc * 0.95:
                result_str = "+${:.2f} ✅".format(net)
            else:
                result_str = "-${:.2f} ❌".format(usdc)
        elif usdc > 0:
            result_str = "持仓中"
        else:
            result_str = "-"

        cells = [
            dir_str,
            name_str,
            addr,
            "UP" if declared == "UP" else "DOWN",
            "{:.1f}".format(size) if size > 0 else "-",
            "${:.2f}".format(usdc) if usdc > 0 else "-",
            "{:.3f}".format(price) if price > 0 else "-",
            stats.get("wr", "N/A"),
            stats.get("wr", "N/A"),   # 盈利率 uses win rate as the probability proxy
            str(stats.get("streak", 0)),
            "{}W/{}L".format(stats.get("w", 0), stats.get("l", 0)),
            pnl_str,
            result_str,
        ]

        # Determine row attribute
        if is_new:
            row_attr = curses.color_pair(CP_NEW) | curses.A_BOLD
            row_attrs = [row_attr] * len(COLUMNS)
        elif declared == "UP":
            row_attr = curses.color_pair(CP_UP) | curses.A_BOLD
            row_attrs = [row_attr] * len(COLUMNS)
        else:
            row_attr = curses.color_pair(CP_DOWN) | curses.A_BOLD
            row_attrs = [row_attr] * len(COLUMNS)

        # Override PnL column color
        pnl_attr = (
            curses.color_pair(CP_PNL_P) | curses.A_BOLD
            if pnl >= 0
            else curses.color_pair(CP_PNL_N) | curses.A_BOLD
        )
        row_attrs[11] = pnl_attr

        # Override result column color
        if "✅" in result_str:
            row_attrs[12] = curses.color_pair(CP_PNL_P) | curses.A_BOLD
        elif "❌" in result_str:
            row_attrs[12] = curses.color_pair(CP_PNL_N) | curses.A_BOLD

        draw_row(stdscr, cur_row, cells, row_attrs)
        cur_row += 1

    # Bottom border
    if cur_row < max_y - 2:
        draw_hline(stdscr, cur_row, "╚", "╩", "╝")
        cur_row += 1

    # Round end summary (below table)
    if remaining <= 0 and round_ts not in summarized_rounds:
        summarized_rounds.add(round_ts)
        winners = [rd for rd in rows_data
                   if rd["pos"].get("result_usdc", 0) > rd["pos"].get("usdc", 0) * 0.95
                   and rd["pos"].get("usdc", 0) > 0]
        losers = [rd for rd in rows_data
                  if rd["pos"].get("usdc", 0) > 0
                  and rd["pos"].get("result_usdc", 0) <= rd["pos"].get("usdc", 0) * 0.95]
        summary_row = cur_row
        if summary_row < max_y - 2:
            addstr_safe(stdscr, summary_row, 0,
                        "【Round #{} 结算】获利 {}人 / 亏损 {}人".format(
                            round_count, len(winners), len(losers)),
                        curses.color_pair(CP_TITLE) | curses.A_BOLD)
            summary_row += 1
        if winners and summary_row < max_y - 2:
            w_line = "✅ " + "  ".join(
                "{}(+${:.2f})".format(
                    rd["name"],
                    rd["pos"].get("result_usdc", 0) - rd["pos"].get("usdc", 0)
                )
                for rd in winners
            )
            addstr_safe(stdscr, summary_row, 0, w_line,
                        curses.color_pair(CP_PNL_P) | curses.A_BOLD)
            summary_row += 1
        if losers and summary_row < max_y - 2:
            l_line = "❌ " + "  ".join(
                "{}(-${:.2f})".format(rd["name"], rd["pos"].get("usdc", 0))
                for rd in losers
            )
            addstr_safe(stdscr, summary_row, 0, l_line,
                        curses.color_pair(CP_PNL_N) | curses.A_BOLD)
            summary_row += 1

    # Status bar at the very bottom
    status = " [Q]退出 | 刷新间隔:{}s | {} ".format(
        REFRESH_INTERVAL, datetime.now().strftime("%H:%M:%S")
    )
    status_row = max_y - 1
    addstr_safe(
        stdscr, status_row, 0,
        status.ljust(max_x - 1)[:max_x - 1],
        curses.color_pair(CP_STATUS)
    )

    stdscr.refresh()


# ── Main curses loop ──────────────────────────────────────────────────────────

def curses_main(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    init_colors()

    last_round_ts = 0
    round_count = 0
    last_refresh = 0
    rows_data = []
    summarized_rounds = set()
    market_label = "Bitcoin Up or Down 5m"

    while True:
        # Non-blocking key check
        try:
            key = stdscr.getch()
        except curses.error:
            key = -1
        if key in (ord("q"), ord("Q")):
            break

        now_ts = int(time.time())
        round_ts, round_end = current_round_ts()

        if round_ts != last_round_ts:
            round_count += 1
            last_round_ts = round_ts
            start_dt = datetime.fromtimestamp(round_ts, tz=timezone.utc)
            end_dt = datetime.fromtimestamp(round_end, tz=timezone.utc)
            market_label = "Bitcoin Up or Down - {} {:02d}:{:02d}-{:02d}:{:02d} UTC".format(
                start_dt.strftime("%b %d"),
                start_dt.hour, start_dt.minute,
                end_dt.hour, end_dt.minute,
            )

        # Refresh data every REFRESH_INTERVAL seconds
        if now_ts - last_refresh >= REFRESH_INTERVAL:
            last_refresh = now_ts
            rows_data = []
            for name, declared in ALL_NAMES:
                addr = ADDR_CACHE.get(name)
                if addr:
                    pos = get_round_position(addr, round_ts)
                else:
                    pos = {
                        "direction": None, "size": 0.0,
                        "usdc": 0.0, "price": 0.0, "result_usdc": 0.0,
                    }
                rows_data.append({"name": name, "declared": declared, "pos": pos})
                time.sleep(0.15)

        draw_table(
            stdscr, rows_data, round_count, round_ts, round_end,
            now_ts, market_label, summarized_rounds
        )
        time.sleep(0.5)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    run_init()
    curses.wrapper(curses_main)


if __name__ == "__main__":
    main()
