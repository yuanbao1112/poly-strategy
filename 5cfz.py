# -*- coding: utf-8 -*-
import time
import requests
import json
import os
import threading
import websocket
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
    raise EnvironmentError(f"[ERROR] 环境变量未设��: {', '.join(_missing)}")

# ── 策略参数 ──────────────────────────────────────────
ENTRY_PRICE_MAX = 0.05    # 买入价格上限 ≤ 5¢
BTC_DIFF_MAX    = 25.0    # chainlink 价差上限 < 25U
BUY_SIZE        = 5       # 每次买入股数
MARKET_PERIOD   = 300
POLL_INTERVAL   = 1
RTDS_WS_URL     = "wss://ws-live-data.polymarket.com"

CLOB_API     = "https://clob.polymarket.com"
GAMMA_API    = "https://gamma-api.polymarket.com"
POLYGON_RPC  = "https://polygon-mainnet.g.alchemy.com/v2/JWvS9PwN79OdjMaDAc5x3"
CTF_ADDRESS  = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
LOG_FILE     = "/root/5cfz.json"

CTF_ABI  = [{"inputs":[{"name":"collateralToken","type":"address"},{"name":"parentCollectionId","type":"bytes32"},{"name":"conditionId","type":"bytes32"},{"name":"indexSets","type":"uint256[]"}],"name":"redeemPositions","outputs":[],"stateMutability":"nonpayable","type":"function"}]
USDC_ABI = [{"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]
SAFE_ABI = [
    {"inputs":[],"name":"nonce","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"to","type":"address"},{"name":"value","type":"uint256"},{"name":"data","type":"bytes"},{"name":"operation","type":"uint8"},{"name":"safeTxGas","type":"uint256"},{"name":"baseGas","type":"uint256"},{"name":"gasPrice","type":"uint256"},{"name":"gasToken","type":"address"},{"name":"refundReceiver","type":"address"},{"name":"signatures","type":"bytes"}],"name":"execTransaction","outputs":[{"type":"bool"}],"stateMutability":"payable","type":"function"},
    {"inputs":[{"name":"to","type":"address"},{"name":"value","type":"uint256"},{"name":"data","type":"bytes"},{"name":"operation","type":"uint8"},{"name":"safeTxGas","type":"uint256"},{"name":"baseGas","type":"uint256"},{"name":"gasPrice","type":"uint256"},{"name":"gasToken","type":"address"},{"name":"refundReceiver","type":"address"},{"name":"_nonce","type":"uint256"}],"name":"getTransactionHash","outputs":[{"type":"bytes32"}],"stateMutability":"view","type":"function"}
]

manual_order_queue = []
manual_lock        = threading.Lock()
input_mode         = False

# ── 键盘监听 ──────────────────────────────────────────
def getch():
    fd  = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

def keyboard_listener():
    global input_mode
    print("[KEY] u=买UP  d=买DOWN  a=全部卖出  q=退出")
    while True:
        try:
            key = getch()

            if key in ('u', 'U'):
                input_mode = True
                try:
                    sys.stdout.write("\n[手动] 买UP，输入股数: ")
                    sys.stdout.flush()
                    size_str = input("")
                    size     = float(size_str.strip())
                    with manual_lock:
                        manual_order_queue.append({"action": "BUY", "direction": "UP", "size": size})
                    print(f"[OK] 已加入队列: 买UP {size}股")
                except ValueError:
                    print("[警告] 输入无效")
                except Exception as e:
                    print(f"[警告] 输入错误: {e}")
                finally:
                    input_mode = False

            elif key in ('d', 'D'):
                input_mode = True
                try:
                    sys.stdout.write("\n[手动] 买DOWN，输入股数: ")
                    sys.stdout.flush()
                    size_str = input("")
                    size     = float(size_str.strip())
                    with manual_lock:
                        manual_order_queue.append({"action": "BUY", "direction": "DOWN", "size": size})
                    print(f"[OK] 已加入队列: 买DOWN {size}股")
                except ValueError:
                    print("[警告] 输入无效")
                except Exception as e:
                    print(f"[警告] 输入错误: {e}")
                finally:
                    input_mode = False

            elif key in ('a', 'A'):
                with manual_lock:
                    manual_order_queue.append({"action": "SELL_ALL"})
                print("\n[手动] 一键全部卖出已加入队列")

            elif key in ('q', 'Q'):
                print("\n[退出] 用户按q退出")
                os._exit(0)

        except Exception:
            time.sleep(0.1)

# ── Chainlink 价格 ────────────────────────────────────
class ChainlinkPriceFeed:
    def __init__(self):
        self.current_price = None
        self.start_price   = None
        self._ws           = None
        self._thread       = None
        self._running      = False
        self._lock         = threading.Lock()

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            if data.get("topic") == "crypto_prices_chainlink" and data.get("type") == "update":
                payload = data.get("payload", {})
                if "btc" in payload.get("symbol", "").lower():
                    with self._lock:
                        self.current_price = float(payload["value"])
        except:
            pass

    def _on_open(self, ws):
        sub = json.dumps({"action": "subscribe", "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "*", "filters": ""}]})
        ws.send(sub)
        print("[CL] Chainlink 订阅成功")

    def _on_error(self, ws, error):
        print(f"[CL] WS错误: {error}")

    def _on_close(self, ws, code, msg):
        if self._running:
            print("[CL] WS断开，5秒后重连...")
            time.sleep(5)
            self._connect()

    def _connect(self):
        try:
            self._ws = websocket.WebSocketApp(RTDS_WS_URL, on_open=self._on_open,
                on_message=self._on_message, on_error=self._on_error, on_close=self._on_close)
            self._ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            print(f"[CL] 连接错误: {e}")

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._connect, daemon=True)
        self._thread.start()
        print("[CL] 等待Chainlink价格...")
        for _ in range(30):
            with self._lock:
                if self.current_price:
                    break
            time.sleep(1)
        with self._lock:
            if self.current_price:
                print(f"[CL] 连接成功，当前BTC: ${self.current_price:.2f}")
            else:
                print("[CL] 超时，继续运行...")

    def record_start_price(self):
        for _ in range(10):
            with self._lock:
                if self.current_price:
                    self.start_price = self.current_price
                    break
            time.sleep(1)
        if self.start_price:
            print(f"[CL] 开盘价: ${self.start_price:.2f}")
        else:
            print("[CL] 开盘价获取失败，差价将显示0")

    def reset_start_price(self):
        with self._lock:
            self.start_price = None

    def get_prices(self):
        with self._lock:
            return self.current_price, self.start_price

    def stop(self):
        self._running = False
        if self._ws:
            self._ws.close()

price_feed = ChainlinkPriceFeed()

# ── 工具函数 ──────────────────────────────────────────
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

def load_stats():
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE) as f:
                return json.load(f)
        except:
            pass
    return {"rounds": 0, "wins": 0, "losses": 0, "skips": 0,
            "total_pnl": 0.0, "history": []}

def save_stats(stats):
    with open(LOG_FILE, "w") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)

def print_stats(stats):
    balance  = get_current_balance()
    total    = stats["wins"] + stats["losses"]
    win_rate = (stats["wins"] / total * 100) if total > 0 else 0
    print(f"\n{'='*55}")
    print(f"  BTC 5M 5¢反转策略")
    print(f"  总轮数: {stats['rounds']}  跳过: {stats.get('skips', 0)}")
    print(f"  胜/负: {stats['wins']}W {stats['losses']}L  胜率: {win_rate:.1f}%")
    print(f"  累计盈亏: ${stats['total_pnl']:.2f}")
    if balance is not None:
        print(f"  余额: ${balance:.2f}")
    print(f"{'='*55}\n")

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

def place_order_by_size(token_id, size, price, label=""):
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY
    amount_usd = round(size * price, 4)
    balance    = get_current_balance()
    if balance is not None and amount_usd > balance:
        print(f"[警告] 余额不足! 需${amount_usd:.4f} 当前${balance:.2f}")
        return None
    try:
        client   = get_client()
        order    = OrderArgs(token_id=token_id, price=price, size=size, side=BUY)
        signed   = client.create_order(order)
        resp     = client.post_order(signed, OrderType.GTC)
        order_id = resp.get("orderID", "N/A") if isinstance(resp, dict) else "N/A"
        print(f"[买入] {label}: {size}股 @ {price:.3f} = ${amount_usd:.4f} | ID:{order_id}")
        return resp
    except Exception as e:
        print(f"[ERROR] 下单失败: {e}")
        return None

def sell_order_by_size(token_id, size, price, label=""):
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import SELL
    try:
        ERC1155_ABI = [{"inputs":[{"name":"account","type":"address"},{"name":"id","type":"uint256"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]
        w3        = Web3(Web3.HTTPProvider(POLYGON_RPC))
        ctf       = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=ERC1155_ABI)
        real_bal  = ctf.functions.balanceOf(Web3.to_checksum_address(WALLET_ADDRESS), int(token_id)).call()
        real_size = real_bal / 1e6
        if real_size <= 0:
            print(f"[警告] 链上余额为0，无法卖出")
            return None
        actual_size  = round(min(size, real_size) * 0.95, 3)
        actual_price = min(price, 0.99)
        client   = get_client()
        order    = OrderArgs(token_id=token_id, price=actual_price, size=actual_size, side=SELL)
        signed   = client.create_order(order)
        resp     = client.post_order(signed, OrderType.GTC)
        order_id = resp.get("orderID", "N/A") if isinstance(resp, dict) else "N/A"
        amount   = round(actual_size * actual_price, 4)
        print(f"[卖出] {label}: {actual_size}股 @ {actual_price:.3f} = ${amount:.4f} | ID:{order_id}")
        return resp
    except Exception as e:
        print(f"[ERROR] 卖出失败: {e}")
        return None

def redeem_thread(condition_id):
    time.sleep(35)
    retry = 0
    while True:
        retry += 1
        try:
            w3      = Web3(Web3.HTTPProvider(POLYGON_RPC))
            account = w3.eth.account.from_key(PRIVATE_KEY)
            ctf     = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)
            safe    = w3.eth.contract(address=Web3.to_checksum_address(WALLET_ADDRESS), abi=SAFE_ABI)
            condition_bytes = bytes.fromhex(condition_id.replace("0x", ""))
            calldata = ctf.encode_abi("redeemPositions", args=[
                Web3.to_checksum_address(USDC_ADDRESS), b'\x00' * 32, condition_bytes, [1, 2]
            ])
            nonce   = safe.functions.nonce().call()
            tx_hash = safe.functions.getTransactionHash(
                Web3.to_checksum_address(CTF_ADDRESS), 0, calldata, 0, 0, 0, 0,
                "0x0000000000000000000000000000000000000000",
                "0x0000000000000000000000000000000000000000", nonce
            ).call()
            signed_hash = account.unsafe_sign_hash(tx_hash)
            sig         = signed_hash.signature
            signature   = sig[:64] + bytes([sig[64] + 4])
            gas_price   = int(w3.eth.gas_price * (1 + retry * 0.2))
            tx = safe.functions.execTransaction(
                Web3.to_checksum_address(CTF_ADDRESS), 0, calldata, 0, 0, 0, 0,
                "0x0000000000000000000000000000000000000000",
                "0x0000000000000000000000000000000000000000", signature
            ).build_transaction({
                "from": account.address,
                "nonce": w3.eth.get_transaction_count(account.address),
                "gas": 300000, "gasPrice": gas_price
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
            tokens                = r2.json().get("tokens", [])
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

# ── 主循环 ────────────────────────────────────────────
def run_one_cycle(market):
    up_token     = market["up_token"]
    down_token   = market["down_token"]
    end_ts       = market["end_ts"]
    condition_id = market["market_id"]

    print(f"\n{'='*55}")
    print(f"[新周期] {datetime.now().strftime('%H:%M:%S')} | 买入条件: 价格≤{ENTRY_PRICE_MAX} 差价<{BTC_DIFF_MAX}U 买{BUY_SIZE}股")
    print(f"{'='*55}")

    price_feed.record_start_price()

    placed      = False
    entry_dir   = None
    entry_price = None
    entry_size  = 0.0
    total_cost  = 0.0

    while True:
        now       = time.time()
        remaining = int(end_ts - now)

        if now >= end_ts - 1:
            up_p, dn_p = get_market_price(up_token)
            if up_p:
                print(f"剩余{remaining:>4}秒 | UP:{up_p:.3f} DOWN:{dn_p:.3f} | 最终价格")
            break

        if now >= end_ts - 5:
            cancel_all_open_orders()

        up_p, dn_p = get_market_price(up_token)
        if up_p is None:
            time.sleep(POLL_INTERVAL)
            continue

        btc_now, btc_start = price_feed.get_prices()
        diff_val = (btc_now - btc_start) if btc_now and btc_start else 0
        diff_str = f"+${diff_val:.1f}" if diff_val >= 0 else f"-${abs(diff_val):.1f}"
        btc_diff = abs(diff_val)

        if not input_mode:
            status = f"入场:{entry_dir} {entry_size}股 ${total_cost:.4f}" if placed else "未入场"
            diff_color = "\033[1;32m" if btc_diff < BTC_DIFF_MAX else "\033[0;37m"
            print(f"剩余{remaining:>4}秒 | UP:{up_p:.3f} DOWN:{dn_p:.3f} | 差价:{diff_color}{diff_str}\033[0m | {status}")

        # ── 手动操作 ──────────────────────────────────
        with manual_lock:
            if manual_order_queue:
                order_req = manual_order_queue.pop(0)
                action    = order_req.get("action")

                if action == "BUY":
                    m_dir   = order_req["direction"]
                    m_size  = order_req["size"]
                    m_token = up_token if m_dir == "UP" else down_token
                    m_price = up_p if m_dir == "UP" else dn_p
                    print(f"[手动] 买{m_dir} {m_size}股 @ {m_price:.3f}")
                    resp = place_order_by_size(m_token, m_size, m_price, f"手动{m_dir}")
                    if resp:
                        placed      = True
                        entry_dir   = m_dir
                        entry_price = m_price
                        entry_size  = round(entry_size + m_size, 2)
                        total_cost  = round(total_cost + m_size * m_price, 4)

                elif action == "SELL_ALL":
                    print("[手动] 一键全部卖出...")
                    if entry_size > 0 and entry_dir:
                        m_token = up_token if entry_dir == "UP" else down_token
                        m_price = up_p if entry_dir == "UP" else dn_p
                        sell_order_by_size(m_token, entry_size, m_price, f"全部卖{entry_dir}")
                        entry_size  = 0.0
                        total_cost  = 0.0
                        placed      = False
                        entry_dir   = None
                    else:
                        print("[警告] 无持仓记录")

        # ── 自动买入：价格≤5¢ 且 差价<25U ──────────────
        if not placed:
            buy_dir = None
            if up_p <= ENTRY_PRICE_MAX:
                buy_dir = "UP"
            elif dn_p <= ENTRY_PRICE_MAX:
                buy_dir = "DOWN"

            if buy_dir:
                buy_price = up_p if buy_dir == "UP" else dn_p
                cost      = round(BUY_SIZE * buy_price, 4)
                if btc_diff < BTC_DIFF_MAX:
                    buy_token = up_token if buy_dir == "UP" else down_token
                    print(f"\033[1;36m  [信号] {buy_dir} 价格:{buy_price:.3f} 差价:{diff_str} -> 买{BUY_SIZE}股 约${cost:.4f}\033[0m")
                    resp = place_order_by_size(buy_token, BUY_SIZE, buy_price, f"5¢反转{buy_dir}")
                    if resp:
                        placed      = True
                        entry_dir   = buy_dir
                        entry_price = buy_price
                        entry_size  = BUY_SIZE
                        total_cost  = cost
                else:
                    if not input_mode:
                        print(f"\033[0;33m  [等待] {buy_dir}={buy_price:.3f} 满足价格但差价{diff_str}≥${BTC_DIFF_MAX}，等待差价缩小\033[0m")

        time.sleep(POLL_INTERVAL)

    # ── 结算 ──────────────────────────────────────────
    up_p, dn_p = get_market_price(up_token)
    if up_p is None:
        up_p, dn_p = 0.5, 0.5

    final_winner = "UP" if up_p > dn_p else "DOWN"
    print(f"\n[结算] {final_winner}赢 | 投入:${total_cost:.4f}")

    if not placed:
        print("[结算] 本局无信号，未入场")
        price_feed.reset_start_price()
        return "skip", condition_id, 0, 0

    if entry_dir == final_winner:
        # 赢了：5股 @ 入场价，结算拿回5股*1.0
        net = round(entry_size * (1.0 - entry_price), 4)
        print(f"[结算] WIN  {entry_dir} {entry_size}股 @ {entry_price:.3f} -> +${net:.4f} (约{round(net/total_cost, 1)}x)")
        result = "win"
    else:
        net = -total_cost
        print(f"[结算] LOSE {entry_dir} {entry_size}股 @ {entry_price:.3f} -> -${total_cost:.4f}")
        result = "loss"

    print(f"[结算] 净盈亏: ${net:+.4f}")

    threading.Thread(target=redeem_thread, args=(condition_id,), daemon=True).start()
    price_feed.reset_start_price()

    return result, condition_id, total_cost, net

def main():
    atexit.register(lambda: os.system("stty sane"))
    stats = load_stats()

    print("=" * 55)
    print("  BTC 5M 5¢反转策略")
    print(f"  买入条件: 价格 ≤ {ENTRY_PRICE_MAX} 且 差价 < {BTC_DIFF_MAX}U")
    print(f"  每次买入: {BUY_SIZE} 股")
    print(f"  [KEY] u=买UP  d=买DOWN  a=全部卖出  q=退出")
    print("=" * 55 + "\n")

    price_feed.start()

    kb_thread = threading.Thread(target=keyboard_listener, daemon=True)
    kb_thread.start()

    print_stats(stats)

    last_market_id = None
    total_pnl      = stats.get("total_pnl", 0.0)

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

            cycle_result, condition_id, spent, net = run_one_cycle(market)
            save_stats(stats)

            if cycle_result == "skip":
                stats["skips"] = stats.get("skips", 0) + 1
                save_stats(stats)
                continue

            stats["rounds"] += 1
            total_pnl = round(total_pnl + net, 4)

            if cycle_result == "win":
                stats["wins"] += 1
            else:
                stats["losses"] += 1

            stats["total_pnl"] = total_pnl
            stats["history"].append({
                "time":   datetime.now().strftime("%Y-%m-%d %H:%M"),
                "dir":    condition_id,
                "cost":   spent,
                "net":    net,
                "result": cycle_result,
            })
            stats["history"] = stats["history"][-200:]
            save_stats(stats)
            print_stats(stats)

        except KeyboardInterrupt:
            print("\n[退出] 策略已停止")
            price_feed.stop()
            save_stats(stats)
            print_stats(stats)
            break
        except Exception as e:
            print(f"[ERROR] 主循环异常: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()

