# -*- coding: utf-8 -*-
import time
import requests
import json
import os
import sys
import select
import threading
import termios
import atexit
from datetime import datetime
from web3 import Web3
from dotenv import load_dotenv

load_dotenv('/root/env7')

PRIVATE_KEY    = os.getenv("PRIVATE_KEY", "")
API_KEY        = os.getenv("API_KEY", "")
API_SECRET     = os.getenv("API_SECRET", "")
API_PASSPHRASE = os.getenv("API_PASSPHRASE", "")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")

_missing = [k for k, v in {
    "PRIVATE_KEY": PRIVATE_KEY, "API_KEY": API_KEY,
    "API_SECRET": API_SECRET, "API_PASSPHRASE": API_PASSPHRASE,
    "WALLET_ADDRESS": WALLET_ADDRESS
}.items() if not v]
if _missing:
    raise EnvironmentError(f"[ERROR] 环境变量未设置: {', '.join(_missing)}")

BASE_BET          = 1.0
MARTIN_MULTIPLIER = 3
MAX_LOSS_STREAK   = 10
WIN_PRICE         = 0.80
ENTRY_MIN_PRICE   = 0.65
ENTRY_MIN_DIFF    = 25
MARKET_PERIOD     = 300
POLL_INTERVAL     = 1

CLOB_API     = "https://clob.polymarket.com"
GAMMA_API    = "https://gamma-api.polymarket.com"
POLYGON_RPC  = "https://polygon-mainnet.g.alchemy.com/v2/JWvS9PwN79OdjMaDAc5x3"
CTF_ADDRESS  = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
LOG_FILE     = "/root/btc5m_martin_log.json"

CTF_ABI  = [{"inputs":[{"name":"collateralToken","type":"address"},{"name":"parentCollectionId","type":"bytes32"},{"name":"conditionId","type":"bytes32"},{"name":"indexSets","type":"uint256[]"}],"name":"redeemPositions","outputs":[],"stateMutability":"nonpayable","type":"function"}]
USDC_ABI = [{"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]
SAFE_ABI = [
    {"inputs":[],"name":"nonce","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"to","type":"address"},{"name":"value","type":"uint256"},{"name":"data","type":"bytes"},{"name":"operation","type":"uint8"},{"name":"safeTxGas","type":"uint256"},{"name":"baseGas","type":"uint256"},{"name":"gasPrice","type":"uint256"},{"name":"gasToken","type":"address"},{"name":"refundReceiver","type":"address"},{"name":"signatures","type":"bytes"}],"name":"execTransaction","outputs":[{"type":"bool"}],"stateMutability":"payable","type":"function"},
    {"inputs":[{"name":"to","type":"address"},{"name":"value","type":"uint256"},{"name":"data","type":"bytes"},{"name":"operation","type":"uint8"},{"name":"safeTxGas","type":"uint256"},{"name":"baseGas","type":"uint256"},{"name":"gasPrice","type":"uint256"},{"name":"gasToken","type":"address"},{"name":"refundReceiver","type":"address"},{"name":"_nonce","type":"uint256"}],"name":"getTransactionHash","outputs":[{"type":"bytes32"}],"stateMutability":"view","type":"function"}
]

mode      = "A"
_old_term = None

def mode_label():
    return {"A": "只做UP", "B": "只做DOWN", "C": "UP+DOWN同时"}.get(mode, mode)

def martin_level(current_bet):
    level = 1
    bet   = BASE_BET
    while bet < current_bet - 0.001:
        bet = round(bet * MARTIN_MULTIPLIER, 2)
        level += 1
    return level

def setup_terminal():
    global _old_term
    try:
        fd        = sys.stdin.fileno()
        _old_term = termios.tcgetattr(fd)
        new       = termios.tcgetattr(fd)
        new[3]   &= ~(termios.ICANON | termios.ECHO)
        new[6][termios.VMIN]  = 0
        new[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, new)
    except:
        pass

def restore_terminal():
    global _old_term
    try:
        if _old_term:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, _old_term)
    except:
        pass

def check_keypress():
    global mode
    try:
        r, _, _ = select.select([sys.stdin], [], [], 0)
        if r:
            ch = sys.stdin.read(1)
            if ch in ('m', 'M'):
                if mode == "A":
                    mode, label = "B", "只做DOWN"
                elif mode == "B":
                    mode, label = "C", "UP+DOWN同时"
                else:
                    mode, label = "A", "只做UP"
                print(f"\n[切换] 模式:{mode} ({label})")
            elif ch in ('q', 'Q', '\x03'):
                print("\n[退出] 用户按q退出")
                restore_terminal()
                os._exit(0)
    except:
        pass

def get_current_balance():
    try:
        w3   = Web3(Web3.HTTPProvider(POLYGON_RPC))
        usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=USDC_ABI)
        return usdc.functions.balanceOf(Web3.to_checksum_address(WALLET_ADDRESS)).call() / 1e6
    except:
        return None

def get_market_price(token_id):
    try:
        r = requests.get(CLOB_API + "/midpoint", params={"token_id": token_id}, timeout=3)
        if r.status_code == 200:
            p = float(r.json().get("mid", 0.5))
            return round(p, 3), round(1 - p, 3)
        return None, None
    except:
        return None, None

def get_btc_price():
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=2)
        return float(r.json()["price"]) if r.status_code == 200 else 0.0
    except:
        return 0.0

def get_client():
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds
    creds = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASSPHRASE)
    return ClobClient(host=CLOB_API, chain_id=137, key=PRIVATE_KEY, creds=creds, signature_type=2, funder=WALLET_ADDRESS)

def cancel_all_open_orders():
    try:
        get_client().cancel_all()
        print("[OK] 已取消所有挂单")
    except:
        pass

def place_order(token_id, amount_usd, entry_price, label=""):
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY
    try:
        client   = get_client()
        price    = round(round(entry_price / 0.01) * 0.01, 2)
        size     = round(amount_usd / price, 2)
        order    = OrderArgs(token_id=token_id, price=0.99, size=size, side=BUY)
        signed   = client.create_order(order)
        resp     = client.post_order(signed, OrderType.GTC)
        order_id = resp.get("orderID", "N/A") if isinstance(resp, dict) else "N/A"
        print(f"[买入] {label}: {size}股 @ {price:.3f} = ${amount_usd:.2f} | ID:{order_id}")
        return True
    except Exception as e:
        print(f"[ERROR] 下单失败: {e}")
        return False

def redeem_thread(condition_id):
    print("[Redeem] 35秒后执行...")
    time.sleep(35)
    retry = 0
    while True:
        retry += 1
        try:
            w3       = Web3(Web3.HTTPProvider(POLYGON_RPC))
            account  = w3.eth.account.from_key(PRIVATE_KEY)
            ctf      = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)
            safe     = w3.eth.contract(address=Web3.to_checksum_address(WALLET_ADDRESS), abi=SAFE_ABI)
            cond_b   = bytes.fromhex(condition_id.replace("0x", ""))
            calldata = ctf.encode_abi("redeemPositions", args=[
                Web3.to_checksum_address(USDC_ADDRESS), b'\x00'*32, cond_b, [1, 2]
            ])
            nonce    = safe.functions.nonce().call()
            tx_hash  = safe.functions.getTransactionHash(
                Web3.to_checksum_address(CTF_ADDRESS), 0, calldata, 0, 0, 0, 0,
                "0x0000000000000000000000000000000000000000",
                "0x0000000000000000000000000000000000000000", nonce
            ).call()
            sh        = account.unsafe_sign_hash(tx_hash)
            sig       = sh.signature
            signature = sig[:64] + bytes([sig[64] + 4])
            gas_price = int(w3.eth.gas_price * (1 + retry * 0.2))
            tx = safe.functions.execTransaction(
                Web3.to_checksum_address(CTF_ADDRESS), 0, calldata, 0, 0, 0, 0,
                "0x0000000000000000000000000000000000000000",
                "0x0000000000000000000000000000000000000000", signature
            ).build_transaction({
                "from":     account.address,
                "nonce":    w3.eth.get_transaction_count(account.address),
                "gas":      300000,
                "gasPrice": gas_price
            })
            signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
            tx_hash2  = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            receipt   = w3.eth.wait_for_transaction_receipt(tx_hash2, timeout=180)
            if receipt.status == 1:
                print("[Redeem] 完成")
                return
            else:
                raise Exception("上链失败")
        except Exception as e:
            if "already known" in str(e):
                print("[Redeem] 已提交")
                return
            if retry > 5:
                print("[Redeem] 放弃")
                return
            time.sleep(10)

def get_active_btc_market():
    try:
        now    = int(time.time())
        period = (now // MARKET_PERIOD) * MARKET_PERIOD
        for ts in [period, period + MARKET_PERIOD, period - MARKET_PERIOD]:
            slug = f"btc-updown-5m-{ts}"
            r    = requests.get(GAMMA_API + "/markets", params={"slug": slug}, timeout=5)
            if r.status_code != 200: continue
            markets = r.json()
            if not markets: continue
            m = markets[0]
            if m.get("closed", True): continue
            condition_id = m.get("conditionId", "")
            if not condition_id: continue
            r2 = requests.get(CLOB_API + f"/markets/{condition_id}", timeout=5)
            if r2.status_code != 200: continue
            tokens = r2.json().get("tokens", [])
            up_token = down_token = None
            for t in tokens:
                o = t.get("outcome", "").lower()
                if o in ("up", "yes"):    up_token   = t.get("token_id")
                elif o in ("down", "no"): down_token = t.get("token_id")
            if up_token and down_token:
                return {"market_id": condition_id, "up_token": up_token,
                        "down_token": down_token, "end_ts": ts + MARKET_PERIOD, "start_ts": ts}
        return None
    except:
        return None

def load_stats():
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE) as f:
                s = json.load(f)
            if "history" not in s:
                s["history"] = []
            if "skips" not in s:
                s["skips"] = 0
            if "last_result" not in s:
                s["last_result"] = {"direction": "", "result": "", "pnl": 0.0, "martin_level": 1, "next_bet": BASE_BET}
            return s
        except:
            pass
    return {
        "rounds": 0, "skips": 0, "total_pnl": 0.0,
        "up":   {"wins": 0, "losses": 0, "loss_streak": 0, "current_bet": BASE_BET},
        "down": {"wins": 0, "losses": 0, "loss_streak": 0, "current_bet": BASE_BET},
        "last_result": {"direction": "", "result": "", "pnl": 0.0, "martin_level": 1, "next_bet": BASE_BET},
        "history": []
    }

def save_stats(stats):
    with open(LOG_FILE, "w") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)

def print_stats(stats):
    bal   = get_current_balance()
    u     = stats["up"]
    d     = stats["down"]
    tot_w = u["wins"]   + d["wins"]
    tot_l = u["losses"] + d["losses"]
    total = tot_w + tot_l
    wr    = (tot_w / total * 100) if total > 0 else 0.0
    lr    = stats.get("last_result", {})
    sep   = "=" * 55
    print(f"\n{sep}")
    print(f"  BTC 5M 策略 ({mode}) | 信号源: {mode_label()}")
    print(f"  总轮数: {stats['rounds']}  跳过: {stats.get('skips', 0)}")
    print(f"  胜/负: {tot_w}W {tot_l}L  胜率: {wr:.1f}%")
    print(f"  累计盈亏: ${stats['total_pnl']:.2f}")
    if lr.get("direction"):
        pnl_str = f"+${lr['pnl']:.2f}" if lr['pnl'] >= 0 else f"-${abs(lr['pnl']):.2f}"
        print(f"  上一局: {lr['result']} {pnl_str}  马丁级别:{lr['martin_level']}  下注:${lr['next_bet']:.2f}")
    if bal is not None:
        print(f"  余额: ${bal:.2f}")
    print(f"{sep}\n")

def run_one_cycle(market, stats):
    global mode
    up_token     = market["up_token"]
    down_token   = market["down_token"]
    end_ts       = market["end_ts"]
    condition_id = market["market_id"]
    up_s         = stats["up"]
    down_s       = stats["down"]

    up_p, dn_p = get_market_price(up_token)
    if up_p is None:
        print("[WARNING] 价格获取失败，跳过")
        return False

    btc_open   = get_btc_price()
    do_up      = mode in ("A", "C")
    do_down    = mode in ("B", "C")
    up_level   = martin_level(up_s["current_bet"])
    down_level = martin_level(down_s["current_bet"])
    show_level = up_level if mode in ("A", "C") else down_level
    show_bet   = up_s["current_bet"] if mode in ("A", "C") else down_s["current_bet"]

    sep = "=" * 55
    print(f"\n{sep}")
    print(f"[新周期] {datetime.now().strftime('%H:%M:%S')} | 信号源:{mode_label()} | 马丁级别:{show_level} 下注:${show_bet:.2f}")
    print(f"{sep}")
    print(f"[CL] 开盘价: ${btc_open:.2f}")

    placed_up   = False
    placed_down = False
    entry_up_p  = up_p
    entry_dn_p  = dn_p

    if do_up:
        if up_p > ENTRY_MIN_PRICE:
            placed_up = place_order(up_token, up_s["current_bet"], up_p, label="UP")
        else:
            print(f"[SKIP] UP 价格{up_p:.3f} <= {ENTRY_MIN_PRICE}")

    if do_down:
        if dn_p > ENTRY_MIN_PRICE:
            placed_down = place_order(down_token, down_s["current_bet"], dn_p, label="DOWN")
        else:
            print(f"[SKIP] DOWN 价格{dn_p:.3f} <= {ENTRY_MIN_PRICE}")

    if not placed_up and not placed_down:
        print("[SKIP] 本局无下单")
        stats["skips"] = stats.get("skips", 0) + 1
        return False

    last_up_p = up_p
    last_dn_p = dn_p

    while True:
        check_keypress()
        now       = time.time()
        remaining = int(end_ts - now)

        if remaining <= 5:
            cancel_all_open_orders()

        btc_now  = get_btc_price()
        btc_diff = btc_now - btc_open if btc_open else 0
        diff_str = f"+${btc_diff:.1f}" if btc_diff >= 0 else f"-${abs(btc_diff):.1f}"

        if placed_up and placed_down:
            status = "入场:UP+DOWN"
        elif placed_up:
            status = "入场:UP"
        else:
            status = "入场:DOWN"
        spent   = (up_s["current_bet"] if placed_up else 0) + (down_s["current_bet"] if placed_down else 0)
        win_tag = "[窗口内]" if abs(btc_diff) >= ENTRY_MIN_DIFF else "[窗口外]"

        if remaining <= 1:
            fu, fd = get_market_price(up_token)
            if fu: last_up_p = fu
            if fd: last_dn_p = fd
            print(f"剩余{remaining:>4}秒 | UP:{last_up_p:.3f} DOWN:{last_dn_p:.3f} | 差价:{diff_str} | {status} ${spent:.2f} {win_tag} [{mode_label()}]")
            break

        fu, fd = get_market_price(up_token)
        if fu:
            last_up_p = fu
            last_dn_p = fd

        print(f"剩余{remaining:>4}秒 | UP:{last_up_p:.3f} DOWN:{last_dn_p:.3f} | 差价:{diff_str} | {status} ${spent:.2f} {win_tag} [{mode_label()}]")
        time.sleep(POLL_INTERVAL)

    up_win    = last_up_p >= WIN_PRICE
    down_win  = last_dn_p >= WIN_PRICE
    round_pnl = 0.0
    last_res  = {}

    print(f"\n[结算] UP:{last_up_p:.3f} DOWN:{last_dn_p:.3f} | {'UP赢' if up_win else 'DOWN赢'}")

    if placed_up:
        bet = up_s["current_bet"]
        lvl = martin_level(bet)
        if up_win:
            pnl = round(bet * (1.0 / entry_up_p - 1), 4)
            round_pnl          += pnl
            up_s["wins"]       += 1
            up_s["loss_streak"] = 0
            up_s["current_bet"] = BASE_BET
            next_bet            = BASE_BET
            print(f"[结算] WIN  UP  ${bet:.2f} @ {entry_up_p:.3f} -> +${pnl:.2f}")
            last_res = {"direction": "UP", "result": "win", "pnl": pnl, "martin_level": lvl, "next_bet": next_bet}
        else:
            round_pnl           -= bet
            up_s["losses"]      += 1
            up_s["loss_streak"] += 1
            if up_s["loss_streak"] >= MAX_LOSS_STREAK:
                up_s["current_bet"]  = BASE_BET
                up_s["loss_streak"]  = 0
                next_bet = BASE_BET
                print(f"[结算] LOSE UP  ${bet:.2f} | 连输{MAX_LOSS_STREAK}次重置 -> ${BASE_BET:.2f}")
            else:
                up_s["current_bet"] = round(bet * MARTIN_MULTIPLIER, 2)
                next_bet = up_s["current_bet"]
                print(f"[结算] LOSE UP  ${bet:.2f} @ {entry_up_p:.3f} -> -${bet:.2f} | 下局${next_bet:.2f} (级别{lvl}->{lvl+1})")
            last_res = {"direction": "UP", "result": "loss", "pnl": -bet, "martin_level": lvl, "next_bet": next_bet}

    if placed_down:
        bet = down_s["current_bet"]
        lvl = martin_level(bet)
        if down_win:
            pnl = round(bet * (1.0 / entry_dn_p - 1), 4)
            round_pnl             += pnl
            down_s["wins"]        += 1
            down_s["loss_streak"]  = 0
            down_s["current_bet"]  = BASE_BET
            next_bet               = BASE_BET
            print(f"[结算] WIN  DOWN ${bet:.2f} @ {entry_dn_p:.3f} -> +${pnl:.2f}")
            last_res = {"direction": "DOWN", "result": "win", "pnl": pnl, "martin_level": lvl, "next_bet": next_bet}
        else:
            round_pnl             -= bet
            down_s["losses"]      += 1
            down_s["loss_streak"] += 1
            if down_s["loss_streak"] >= MAX_LOSS_STREAK:
                down_s["current_bet"]  = BASE_BET
                down_s["loss_streak"]  = 0
                next_bet = BASE_BET
                print(f"[结算] LOSE DOWN ${bet:.2f} | 连输{MAX_LOSS_STREAK}次重置 -> ${BASE_BET:.2f}")
            else:
                down_s["current_bet"] = round(bet * MARTIN_MULTIPLIER, 2)
                next_bet = down_s["current_bet"]
                print(f"[结算] LOSE DOWN ${bet:.2f} @ {entry_dn_p:.3f} -> -${bet:.2f} | 下局${next_bet:.2f} (级别{lvl}->{lvl+1})")
            last_res = {"direction": "DOWN", "result": "loss", "pnl": -bet, "martin_level": lvl, "next_bet": next_bet}

    print(f"[结算] 净盈亏: ${round_pnl:+.2f}")

    stats["total_pnl"]   = round(stats["total_pnl"] + round_pnl, 4)
    stats["rounds"]     += 1
    stats["last_result"] = last_res
    stats["history"].append({
        "time":   datetime.now().strftime("%Y-%m-%d %H:%M"),
        "mode":   mode,
        "pnl":    round_pnl,
        "result": last_res.get("result", ""),
    })
    stats["history"] = stats["history"][-100:]

    threading.Thread(target=redeem_thread, args=(condition_id,), daemon=True).start()
    return True

def main():
    global mode
    stats = load_stats()

    atexit.register(restore_terminal)
    setup_terminal()

    print("=" * 55)
    print("  BTC 5M 马丁格尔策略")
    print(f"  基础下注:{BASE_BET}U | 倍数:{MARTIN_MULTIPLIER}x | 连输{MAX_LOSS_STREAK}次重置")
    print(f"  入场条件: 价格>{{ENTRY_MIN_PRICE}} 且 BTC价差>{{ENTRY_MIN_DIFF}}")
    print(f"  判赢: 最后1秒价格>={{WIN_PRICE}}")
    print(f"  [M键] 切换模式A->B->C  [Q键] 退出")
    print("=" * 55)
    print_stats(stats)

    last_market_id = None
    btc_diff       = 0.0

    while True:
        try:
            check_keypress()

            market = get_active_btc_market()
            if not market:
                print("[等待] 寻找市场中...")
                time.sleep(3)
                continue

            if market["market_id"] == last_market_id:
                time.sleep(2)
                continue

            btc_open = get_btc_price()

            # 等待价差满足，直到本局结束，不提前跳过
            while True:
                check_keypress()
                btc_now   = get_btc_price()
                btc_diff  = round(btc_now - btc_open, 1)
                remaining = int(market["end_ts"] - time.time())

                if remaining <= 1:
                    break

                if abs(btc_diff) >= ENTRY_MIN_DIFF:
                    print(f"[OK] 价差满足: {btc_diff:+.1f} | 剩余{remaining}s")
                    break

                up_p, dn_p = get_market_price(market["up_token"])
                up_p     = up_p if up_p else 0.0
                dn_p     = dn_p if dn_p else 0.0
                diff_str = f"+${btc_diff:.1f}" if btc_diff >= 0 else f"-${abs(btc_diff):.1f}"
                win_tag  = "[窗口内]" if abs(btc_diff) >= ENTRY_MIN_DIFF else "[窗口外]"
                print(f"剩余{remaining:>4}秒 | UP:{up_p:.3f} DOWN:{dn_p:.3f} | 差价:{diff_str} | 未入场 $0.00 {win_tag} [{mode_label()}]")
                time.sleep(1)

            remaining = int(market["end_ts"] - time.time())

            if remaining <= 1 or abs(btc_diff) < ENTRY_MIN_DIFF:
                last_market_id = market["market_id"]
                stats["skips"] = stats.get("skips", 0) + 1
                save_stats(stats)
                time.sleep(2)
                continue

            last_market_id = market["market_id"]
            run_one_cycle(market, stats)
            save_stats(stats)
            print_stats(stats)

        except KeyboardInterrupt:
            print("\n[退出] 策略已停止")
            restore_terminal()
            save_stats(stats)
            break
        except Exception as e:
            print(f"[ERROR] 主循环异常: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()