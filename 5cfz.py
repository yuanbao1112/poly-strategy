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
from datetime import datetime
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

ENABLE_BTC_FILTER = False
BTC_DIFF_MAX      = 25.0

CLOB_API     = "https://clob.polymarket.com"
GAMMA_API    = "https://gamma-api.polymarket.com"
POLYGON_RPC  = "https://polygon-mainnet.g.alchemy.com/v2/JWvS9PwN79OdjMaDAc5x3"
CTF_ADDRESS  = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
LOG_FILE     = "/root/5cfz.json"

USDC_ABI = [{"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]

# 全局槽位：跨局持续维护，key=(direction, price), value=order_id or None
global_slots = {}

def getch():
    fd  = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

def keyboard_listener():
    print("[KEY] q=退出")
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

def get_market_price(up_token):
    try:
        r = requests.get(CLOB_API + "/midpoint", params={"token_id": up_token}, timeout=3)
        if r.status_code == 200:
            up_price = float(r.json().get("mid", 0.5))
            return up_price, round(1 - up_price, 4)
        return None, None
    except:
        return None, None

def get_client():
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds
    creds = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASSPHRASE)
    return ClobClient(host=CLOB_API, chain_id=137, key=PRIVATE_KEY, creds=creds, signature_type=2, funder=WALLET_ADDRESS)

def get_open_order_ids():
    """获取当前所有活跃挂单的order_id集合"""
    try:
        client = get_client()
        orders = client.get_orders()
        if isinstance(orders, list):
            return {o.get("id") or o.get("orderID") for o in orders if o.get("status") in ("LIVE", "OPEN", None)}
        return set()
    except Exception as e:
        print(f"  [WARN] 获取挂单列表失败: {e}")
        return set()

def place_order(token_id, size, price, label=""): 
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY
    amount_usd = round(size * price, 4)
    try:
        client   = get_client()
        order    = OrderArgs(token_id=token_id, price=price, size=size, side=BUY)
        signed   = client.create_order(order)
        resp     = client.post_order(signed, OrderType.GTC)
        order_id = resp.get("orderID", "N/A") if isinstance(resp, dict) else "N/A"
        print(f"  [挂单] {label}: {size}股 @ {price:.2f} = ${amount_usd:.4f} | ID:{order_id}")
        return order_id if order_id != "N/A" else None
    except Exception as e:
        print(f"  [ERROR] 挂单失败 {label}: {e}")
        return None

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

def get_active_btc_market():
    try:
        now    = int(time.time())
        period = (now // MARKET_PERIOD) * MARKET_PERIOD
        for ts in [period, period + MARKET_PERIOD, period - MARKET_PERIOD]:
            slug = f"btc-updown-5m-{ts}"
            r    = requests.get(GAMMA_API + "/markets", params={"slug": slug}, timeout=5)
            if r.status_code != 200:
                continue
            markets = r.json()
            if not markets:
                continue
            m = markets[0]
            if m.get("closed", True):
                continue
            condition_id = m.get("conditionId", "")
            if not condition_id:
                continue
            r2 = requests.get(CLOB_API + f"/markets/{condition_id}", timeout=5)
            if r2.status_code != 200:
                continue
            tokens = r2.json().get("tokens", [])
            up_token = down_token = None
            for t in tokens:
                o = t.get("outcome", "").lower()
                if o in ("up", "yes"):
                    up_token = t.get("token_id")
                elif o in ("down", "no"):
                    down_token = t.get("token_id")
            if up_token and down_token:
                return {
                    "market_id":  condition_id,
                    "up_token":   up_token,
                    "down_token": down_token,
                    "end_ts":     ts + MARKET_PERIOD,
                    "start_ts":   ts
                }
        return None
    except:
        return None

def replenish_missing(token_map):
    """检查全局slots，只补缺失的单"""
    open_ids = get_open_order_ids()

    missing = []
    for key, oid in global_slots.items():
        if oid is None or oid not in open_ids:
            missing.append(key)

    if not missing:
        print(f"  [检查] 10个单全部在挂单中，无需补单")
        return

    print(f"  [补单] 发现{len(missing)}个缺失，开始补: {[(d, int(p*100)) for d,p in missing]}")
    for (direction, lv) in missing:
        oid = place_order(token_map[direction], BUY_SIZE, lv, f"补{direction}@{int(lv*100)}c")
        global_slots[(direction, lv)] = oid
        time.sleep(0.3)

def run_one_cycle(market):
    global global_slots

    up_token     = market["up_token"]
    down_token   = market["down_token"]
    end_ts       = market["end_ts"]
    condition_id = market["market_id"]

    token_map = {"UP": up_token, "DOWN": down_token}

    print(f"\n{'='*55}")
    print(f"[新周期] {datetime.now().strftime('%H:%M:%S')} | 市场:{condition_id[:12]}...")
    print(f"{'='*55}")

    up_p, dn_p = get_market_price(up_token)
    if up_p is not None:
        print(f"  当前 UP:{up_p:.3f} DOWN:{dn_p:.3f}")

    # 初始化slots（第一次运行）
    if not global_slots:
        print(f"  [首次] 初始化10个槽位并全部挂单")
        for lv in PRICE_LEVELS:
            for direction in ["UP", "DOWN"]:
                global_slots[(direction, lv)] = None

    # 每局开始检查并补缺失的单
    replenish_missing(token_map)

    placed = sum(1 for v in global_slots.values() if v)
    print(f"  [状态] 当前有效挂单: {placed}/10 | 等待周期结束...")

    # 等待本局结束
    while time.time() < end_ts:
        time.sleep(5)

    print(f"  [周期结束] {datetime.now().strftime('%H:%M:%S')}")

def main():
    atexit.register(lambda: os.system("stty sane"))
    stats = load_stats()

    print("=" * 55)
    print("  BTC 5M 多档位反转策略 (1c~5c)")
    print(f"  挂单价位: {[f'{int(p*100)}c' for p in PRICE_LEVELS]}")
    print(f"  每位各买: {BUY_SIZE}股 (UP+DOWN)")
    print(f"  补单时机: 新一局开始时检查并补缺失单")
    print(f"  [KEY] q=退出")
    print("=" * 55 + "\n")

    balance = get_current_balance()
    if balance is not None:
        print(f"  当前余额: ${balance:.2f}\n")

    kb_thread = threading.Thread(target=keyboard_listener, daemon=True)
    kb_thread.start()

    last_market_id = None

    while True:
        try:
            market = get_active_btc_market()
            if market is None:
                print("[等待] 寻找市场中...")
                time.sleep(5)
                continue

            if market["market_id"] == last_market_id:
                time.sleep(2)
                continue

            last_market_id = market["market_id"]
            stats["rounds"] = stats.get("rounds", 0) + 1
            stats["history"].append({
                "time":   datetime.now().strftime("%Y-%m-%d %H:%M"),
                "market": market["market_id"],
            })
            stats["history"] = stats["history"][-200:]
            save_stats(stats)

            run_one_cycle(market)

        except KeyboardInterrupt:
            print("\n[退出] 策略已停止")
            break
        except Exception as e:
            print(f"[ERROR] 主循环异常: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
