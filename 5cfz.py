# -*- coding: utf-8 -*-
import time
import requests
import json
import os
import threading
import sys
import tty
import termios
import atexit
from datetime import datetime, timezone
from web3 import Web3
from dotenv import load_dotenv

load_dotenv('/root/env4')

PRIVATE_KEY    = os.getenv("PRIVATE_KEY", "")
API_KEY        = os.getenv("API_KEY", "")
API_SECRET     = os.getenv("API_SECRET", "")
API_PASSPHRASE = os.getenv("API_PASSPHRASE", "")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")

_missing = [k for k, v in {
    "PRIVATE_KEY": PRIVATE_KEY, "API_KEY": API_KEY, "API_SECRET": API_SECRET,
    "API_PASSPHRASE": API_PASSPHRASE, "WALLET_ADDRESS": WALLET_ADDRESS
}.items() if not v]
if _missing:
    raise EnvironmentError(f"[ERROR] 环境变量未设置: {', '.join(_missing)}")

# ── 策略参数 ──────────────────────────────────────────
PRICE_LEVELS    = [0.01, 0.02, 0.03, 0.04, 0.05]
BUY_SIZE        = 5
MARKET_PERIOD   = 300
CANCEL_DELAY    = 60
COST_PER_MARKET = sum(PRICE_LEVELS) * BUY_SIZE * 2  # $1.5

CLOB_API     = "https://clob.polymarket.com"
GAMMA_API    = "https://gamma-api.polymarket.com"
POLYGON_RPC  = "https://polygon-mainnet.g.alchemy.com/v2/JWvS9PwN79OdjMaDAc5x3"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
LOG_FILE     = "/root/5cfz.json"

USDC_ABI = [{"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]

market_states   = {}
today_markets   = []
next_market_idx = 0


def getch():
    fd  = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def keyboard_listener():
    while True:
        try:
            key = getch()
            if key in ('q', 'Q'):
                print("\n[退出] 用户按q退出")
                os._exit(0)
        except Exception:
            time.sleep(0.1)


def get_current_balance():
    try:
        w3   = Web3(Web3.HTTPProvider(POLYGON_RPC))
        usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=USDC_ABI)
        return usdc.functions.balanceOf(Web3.to_checksum_address(WALLET_ADDRESS)).call() / 1e6
    except:
        return None


def get_client():
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds
    creds = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASSPHRASE)
    return ClobClient(host=CLOB_API, chain_id=137, key=PRIVATE_KEY, creds=creds, signature_type=2, funder=WALLET_ADDRESS)


def get_all_open_orders():
    try:
        client = get_client()
        orders = client.get_orders()
        if isinstance(orders, list):
            return [o for o in orders if o.get("status") in ("LIVE", "OPEN", None)]
        return []
    except Exception as e:
        print(f"  [WARN] 获取挂单失败: {e}")
        return []


def cancel_order(order_id, label=""):
    try:
        client = get_client()
        client.cancel(order_id)
        print(f"  [取消] {label} ID:{order_id}")
        return True
    except Exception as e:
        print(f"  [WARN] 取消失败 {label} ID:{order_id}: {e}")
        return False


def place_order(token_id, size, price, label=""):
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY
    try:
        client   = get_client()
        order    = OrderArgs(token_id=token_id, price=price, size=size, side=BUY)
        signed   = client.create_order(order)
        resp     = client.post_order(signed, OrderType.GTC)
        order_id = resp.get("orderID", "N/A") if isinstance(resp, dict) else "N/A"
        print(f"  [挂单] {label}: {size}股 @ {price:.2f} = ${round(size*price,4):.4f} | ID:{order_id}")
        return order_id if order_id != "N/A" else None
    except Exception as e:
        print(f"  [ERROR] 挂单失败 {label}: {e}")
        return None


def resolve_market_tokens(condition_id):
    try:
        r = requests.get(CLOB_API + f"/markets/{condition_id}", timeout=5)
        if r.status_code != 200:
            return None, None
        tokens = r.json().get("tokens", [])
        up_token = down_token = None
        for t in tokens:
            o = t.get("outcome", "").lower()
            if o in ("up", "yes"):
                up_token = t.get("token_id")
            elif o in ("down", "no"):
                down_token = t.get("token_id")
        return up_token, down_token
    except:
        return None, None


def fetch_today_markets():
    now = int(time.time())
    today_utc = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    day_start = int(today_utc.timestamp())
    day_end   = day_start + 86400

    slots = []
    ts = day_start
    while ts < day_end:
        slots.append({"start_ts": ts, "end_ts": ts + MARKET_PERIOD,
                      "condition_id": None, "up_token": None, "down_token": None})
        ts += MARKET_PERIOD

    print(f"  [初始化] 今天共{len(slots)}个市场时间槽，查询未结束的...")

    valid = []
    for m in slots:
        if m["end_ts"] <= now:
            continue
        slug = f"btc-updown-5m-{m['start_ts']}"
        try:
            r = requests.get(GAMMA_API + "/markets", params={"slug": slug}, timeout=5)
            if r.status_code != 200:
                time.sleep(0.1)
                continue
            data = r.json()
            if not data:
                time.sleep(0.1)
                continue
            info = data[0]
            if info.get("closed", True):
                time.sleep(0.1)
                continue
            condition_id = info.get("conditionId", "")
            if not condition_id:
                time.sleep(0.1)
                continue
            up_token, down_token = resolve_market_tokens(condition_id)
            if not up_token or not down_token:
                time.sleep(0.1)
                continue
            m["condition_id"] = condition_id
            m["up_token"]     = up_token
            m["down_token"]   = down_token
            valid.append(m)
            time.sleep(0.15)
        except Exception as e:
            print(f"  [WARN] 查询市场失败 ts={m['start_ts']}: {e}")
            time.sleep(0.2)

    print(f"  [初始化] 查到{len(valid)}个有效未结束市场")
    return sorted(valid, key=lambda x: x["start_ts"])


def place_market_orders(m):
    cid   = m["condition_id"]
    slots = {}
    print(f"  [挂单] 市场 {cid[:12]}... start={datetime.fromtimestamp(m['start_ts']).strftime('%H:%M')}")
    for lv in PRICE_LEVELS:
        for direction, token_id in [("UP", m["up_token"]), ("DOWN", m["down_token"])]:
            oid = place_order(token_id, BUY_SIZE, lv, f"{direction}@{int(lv*100)}c")
            slots[(direction, lv)] = oid
            time.sleep(0.3)
    return slots


def cancel_market_orders(cid, slots):
    print(f"  [取消] 市场 {cid[:12]}... 取消未成交挂单")
    all_open = get_all_open_orders()
    open_map = {o.get("id") or o.get("orderID"): o for o in all_open if o.get("id") or o.get("orderID")}

    cancelled = 0
    for (direction, lv), oid in slots.items():
        if oid and oid in open_map:
            if cancel_order(oid, f"{direction}@{int(lv*100)}c"):
                cancelled += 1
            time.sleep(0.2)
    print(f"  [取消完成] 取消了{cancelled}个挂单")


def load_stats():
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE) as f:
                return json.load(f)
        except:
            pass
    return {"rounds": 0, "history": []}


def save_stats(stats):
    with open(LOG_FILE, "w") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)


def main():
    global today_markets, next_market_idx, market_states

    atexit.register(lambda: os.system("stty sane"))
    stats = load_stats()

    print("=" * 60)
    print("  BTC 5M 多档位反转策略 (1c~5c) — 全天滚动挂单版")
    print(f"  挂单价位: {[f'{int(p*100)}c' for p in PRICE_LEVELS]}")
    print(f"  每位各买: {BUY_SIZE}股 (UP+DOWN)  每市场成本: ${COST_PER_MARKET:.2f}")
    print(f"  补单时机: 当前局未挂过则补 | 取消时机: 结束后{CANCEL_DELAY}s")
    print(f"  [KEY] q=退出")
    print("=" * 60 + "\n")

    balance = get_current_balance()
    bal_str = f"${balance:.2f}" if balance is not None else "获取失败"
    print(f"  当前余额: {bal_str}\n")

    kb_thread = threading.Thread(target=keyboard_listener, daemon=True)
    kb_thread.start()

    print("[初始化] 查询今天所有BTC 5分钟市场...")
    today_markets = fetch_today_markets()
    if not today_markets:
        print("[ERROR] 未找到任何市场，退出")
        return

    # 清理残留挂单
    print("\n[初始化] 检查残留挂单...")
    valid_tokens = set()
    for m in today_markets:
        valid_tokens.add(m["up_token"])
        valid_tokens.add(m["down_token"])
    all_open = get_all_open_orders()
    stale = [o for o in all_open if o.get("asset_id") not in valid_tokens]
    if stale:
        print(f"  [清理] 发现{len(stale)}个残留挂单，取消中...")
        for o in stale:
            oid = o.get("id") or o.get("orderID")
            if oid:
                cancel_order(oid, "残留单")
                time.sleep(0.2)
    else:
        print("  [清理] 无残留挂单")

    # 初始挂单
    print(f"\n[初始化] 开始初始挂单（余额:{bal_str}，每市场${COST_PER_MARKET:.2f}）...")
    next_market_idx = 0
    for i, m in enumerate(today_markets):
        balance = get_current_balance()
        if balance is None or balance < COST_PER_MARKET:
            print(f"  [停止] 余额不足${balance:.2f if balance else 0}，已挂{i}个市场")
            next_market_idx = i
            break
        cid   = m["condition_id"]
        slots = place_market_orders(m)
        market_states[cid] = {
            "up_token":    m["up_token"],
            "down_token":  m["down_token"],
            "start_ts":    m["start_ts"],
            "end_ts":      m["end_ts"],
            "slots":       slots,
            "ever_placed": True,
            "cancelled":   False,
        }
        next_market_idx = i + 1
        time.sleep(0.5)
    else:
        next_market_idx = len(today_markets)

    print(f"\n[运行中] 初始挂单完成，共{len(market_states)}个市场 | 开始主循环...")

    cancel_queue = []  # [(cancel_after_ts, condition_id)]

    while True:
        try:
            now = int(time.time())

            # 1. 执行到期取消任务
            still_pending = []
            for (cancel_at, cid) in cancel_queue:
                if now >= cancel_at:
                    if cid in market_states and not market_states[cid]["cancelled"]:
                        cancel_market_orders(cid, market_states[cid]["slots"])
                        market_states[cid]["cancelled"] = True
                        # 取消后滚动挂下一个市场
                        balance = get_current_balance()
                        while (balance is not None and balance >= COST_PER_MARKET
                               and next_market_idx < len(today_markets)):
                            nm   = today_markets[next_market_idx]
                            ncid = nm["condition_id"]
                            if ncid not in market_states:
                                print(f"\n[滚动] 余额${balance:.2f}，挂下一个市场 {ncid[:12]}...")
                                nslots = place_market_orders(nm)
                                market_states[ncid] = {
                                    "up_token":    nm["up_token"],
                                    "down_token":  nm["down_token"],
                                    "start_ts":    nm["start_ts"],
                                    "end_ts":      nm["end_ts"],
                                    "slots":       nslots,
                                    "ever_placed": True,
                                    "cancelled":   False,
                                }
                            next_market_idx += 1
                            balance = get_current_balance()
                else:
                    still_pending.append((cancel_at, cid))
            cancel_queue = still_pending

            # 2. 检查各市场状态
            for m in today_markets:
                cid      = m["condition_id"]
                start_ts = m["start_ts"]
                end_ts   = m["end_ts"]

                # 已结束：加入取消队列
                if now >= end_ts:
                    if cid in market_states and not market_states[cid]["cancelled"]:
                        cancel_at = end_ts + CANCEL_DELAY
                        if not any(c == cid for _, c in cancel_queue):
                            cancel_queue.append((cancel_at, cid))
                    continue

                # 当前局从未挂过单：补挂
                if cid not in market_states and start_ts <= now < end_ts:
                    balance = get_current_balance()
                    if balance is not None and balance >= COST_PER_MARKET:
                        print(f"\n[补挂] 当前局{cid[:12]}...未挂过单，补挂...")
                        slots = place_market_orders(m)
                        market_states[cid] = {
                            "up_token":    m["up_token"],
                            "down_token":  m["down_token"],
                            "start_ts":    start_ts,
                            "end_ts":      end_ts,
                            "slots":       slots,
                            "ever_placed": True,
                            "cancelled":   False,
                        }
                # 当前局挂过单但成交了 → 本局不补

            # 3. 状态打印
            active = [m for m in today_markets if m["start_ts"] <= now < m["end_ts"]]
            if active:
                m         = active[0]
                cid       = m["condition_id"]
                remaining = int(m["end_ts"] - now)
                print(f"[{{datetime.now().strftime('%H:%M:%S')}}] 当前:{{cid[:12]}}... 剩余{{remaining}}s | \
                      已挂:{{len(market_states)}}个 | 待挂:{{max(0,len(today_markets)-next_market_idx)}}个 | \
                      取消队列:{{len(cancel_queue)}}个")

            time.sleep(30)

        except KeyboardInterrupt:
            print("\n[退出] 策略已停止")
            break
        except Exception as e:
            print(f"[ERROR] 主循环异常: {e}")
            time.sleep(10)


if __name__ == "__main__":
    main()