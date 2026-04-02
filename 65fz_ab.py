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

load_dotenv('/root/env6')

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

BASE_BET       = 1.1
SIGNAL_SECS    = 5
ENTRY_MAX      = 0.65
ENTRY_WINDOW   = 300
PRICE_DIFF_MIN = 8.0
MARKET_PERIOD  = 300
POLL_INTERVAL  = 1
RTDS_WS_URL    = "wss://ws-live-data.polymarket.com"

CLOB_API     = "https://clob.polymarket.com"
GAMMA_API    = "https://gamma-api.polymarket.com"
POLYGON_RPC  = "https://polygon-mainnet.g.alchemy.com/v2/JWvS9PwN79OdjMaDAc5x3"
CTF_ADDRESS  = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
LOG_FILE     = "/root/65fz_ab.json"
STATE_FILE   = "/root/65fz_ab_state.json"

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
signal_source      = "A"   # A=庄家信号  B=跨周期马丁  C=局内马丁
MARTIN_SEQ         = [5, 15, 45, 135]  # 马丁序列

def getch():
    fd  = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

def keyboard_listener():
    global input_mode, signal_source
    print("[KEY] u=买UP  d=买DOWN  s=卖出  a=全部卖出  m=切换信号源  q=退出")
    while True:
        try:
            key = getch()

            if key in ('u', 'U'):
                input_mode = True
                try:
                    sys.stdout.write("\n[手动] 买UP，输入金额(USDC): ")
                    sys.stdout.flush()
                    amt_str = input("")
                    amount  = float(amt_str.strip())
                    with manual_lock:
                        manual_order_queue.append({"action": "BUY", "direction": "UP", "amount": amount})
                    print(f"[OK] 已加入队列: 买UP ${amount:.2f}")
                except ValueError:
                    print("[警告] 输入无效")
                except Exception as e:
                    print(f"[警告] 输入错误: {e}")
                finally:
                    input_mode = False

            elif key in ('d', 'D'):
                input_mode = True
                try:
                    sys.stdout.write("\n[手动] 买DOWN，输入金额(USDC): ")
                    sys.stdout.flush()
                    amt_str = input("")
                    amount  = float(amt_str.strip())
                    with manual_lock:
                        manual_order_queue.append({"action": "BUY", "direction": "DOWN", "amount": amount})
                    print(f"[OK] 已加入队列: 买DOWN ${amount:.2f}")
                except ValueError:
                    print("[警告] 输入无效")
                except Exception as e:
                    print(f"[警告] 输入错误: {e}")
                finally:
                    input_mode = False

            elif key in ('s', 'S'):
                input_mode = True
                try:
                    sys.stdout.write("\n[手动] 卖出方向 (u=卖UP / d=卖DOWN): ")
                    sys.stdout.flush()
                    dir_key = input("").strip().lower()
                    if dir_key not in ('u', 'd'):
                        print("[警告] 无效方向，请输入 u 或 d")
                    else:
                        sell_dir = "UP" if dir_key == 'u' else "DOWN"
                        with manual_lock:
                            manual_order_queue.append({"action": "SELL", "direction": sell_dir})
                        print(f"[OK] 已加入队列: 卖出{sell_dir} 全部")
                except Exception as e:
                    print(f"[警告] 输入错误: {e}")
                finally:
                    input_mode = False

            elif key in ('a', 'A'):
                with manual_lock:
                    manual_order_queue.append({"action": "SELL_ALL"})
                print("\n[手动] 一键全部卖出已加入队列")

            elif key in ('m', 'M'):
                if signal_source == "A":
                    signal_source = "B"
                    label = "跨周期马丁"
                elif signal_source == "B":
                    signal_source = "C"
                    label = "局内马丁"
                else:
                    signal_source = "A"
                    label = "庄家信号"
                print(f"\n[切换] 信号源: {signal_source} ({label})")

            elif key in ('q', 'Q'):
                print("\n[退出] 用户按q退出")
                os._exit(0)

        except Exception:
            time.sleep(0.1)

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

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except:
            pass
    return {"last_result": None, "last_bet": BASE_BET, "martin_step": 0}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

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

def print_stats(stats, state):
    balance     = get_current_balance()
    total       = stats["wins"] + stats["losses"]
    win_rate    = (stats["wins"] / total * 100) if total > 0 else 0
    last_res    = state.get("last_result", None)
    last_bet    = state.get("last_bet", BASE_BET)
    martin_step = state.get("martin_step", 0)
    sig_label   = {"A": "庄家信号", "B": "跨周期马丁", "C": "局内马丁"}.get(signal_source, signal_source)
    print(f"\n{'='*55}")
    print(f"  BTC 5M 策略 (ab) | 信号源: {signal_source} {sig_label}")
    print(f"  总轮数: {stats['rounds']}  跳过: {stats.get('skips', 0)}")
    print(f"  胜/负: {stats['wins']}W {stats['losses']}L  胜率: {win_rate:.1f}%")
    print(f"  累计盈亏: ${stats['total_pnl']:.2f}")
    print(f"  上一局: {last_res or 'N/A'} ${last_bet:.2f}  马丁级别:{martin_step+1} 下注:${MARTIN_SEQ[min(martin_step, len(MARTIN_SEQ)-1)]}")
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
    price      = round(round(price / 0.01) * 0.01, 2)
    amount_usd = round(size * price, 2)
    balance    = get_current_balance()
    if balance is not None and amount_usd > balance:
        print(f"[警告] 余额不足! 需${amount_usd:.2f} 当前${balance:.2f}")
        return None
    try:
        client   = get_client()
        order    = OrderArgs(token_id=token_id, price=price, size=size, side=BUY)
        signed   = client.create_order(order)
        resp     = client.post_order(signed, OrderType.GTC)
        order_id = resp.get("orderID", "N/A") if isinstance(resp, dict) else "N/A"
        print(f"[买入] {label}: {size}股 @ {price:.3f} = ${amount_usd:.2f} | ID:{order_id}")
        return resp
    except Exception as e:
        print(f"[ERROR] 下单失败: {e}")
        return None

def place_order(token_id, amount_usd, price, label=""):
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY
    price   = round(round(price / 0.01) * 0.01, 2)
    balance = get_current_balance()
    if balance is not None and amount_usd > balance:
        print(f"[警告] 余额不足! 需${amount_usd:.2f} 当前${balance:.2f}")
        return None
    try:
        client   = get_client()
        size     = round(amount_usd / price, 2)
        order    = OrderArgs(token_id=token_id, price=price, size=size, side=BUY)
        signed   = client.create_order(order)
        resp     = client.post_order(signed, OrderType.GTC)
        order_id = resp.get("orderID", "N/A") if isinstance(resp, dict) else "N/A"
        print(f"[买入] {label}: {size}股 @ {price:.3f} = ${amount_usd:.2f} | ID:{order_id}")
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
        actual_price = round(round(min(price, 0.99) / 0.01) * 0.01, 2)
        client   = get_client()
        order    = OrderArgs(token_id=token_id, price=actual_price, size=actual_size, side=SELL)
        signed   = client.create_order(order)
        resp     = client.post_order(signed, OrderType.GTC)
        order_id = resp.get("orderID", "N/A") if isinstance(resp, dict) else "N/A"
        amount   = round(actual_size * actual_price, 2)
        print(f"[卖出] {label}: {actual_size}股 @ {actual_price:.3f} = ${amount:.2f} | ID:{order_id}")
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

def run_one_cycle(market, state):
    up_token     = market["up_token"]
    down_token   = market["down_token"]
    end_ts       = market["end_ts"]
    condition_id = market["market_id"]

    martin_step_cross = state.get("martin_step", 0)
    bet_amount        = MARTIN_SEQ[min(martin_step_cross, len(MARTIN_SEQ) - 1)]

    this_bet  = BASE_BET
    sig_label = {"A": "庄家信号", "B": "跨周期马丁", "C": "局内马丁"}.get(signal_source, signal_source)

    print(f"\n{'='*55}")
    print(f"[新周期] {datetime.now().strftime('%H:%M:%S')} | 信号源:{sig_label} | 马丁级别:{martin_step_cross+1} 下注:${bet_amount}")
    print(f"{'='*55}")

    price_feed.record_start_price()

    placed      = False
    entry_dir   = None
    entry_price = None
    entry_size  = 0.0
    total_spent = 0.0

    c_martin_step = 0
    c_last_dir    = None

    signal_down_count = 0
    signal_up_count   = 0

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

        in_entry_window = remaining <= ENTRY_WINDOW

        if not input_mode:
            status  = f"入场:{entry_dir}" if placed else "未入场"
            win_str = "[窗口内]" if in_entry_window else f"[{remaining}s后入窗]"
            print(f"剩余{remaining:>4}秒 | UP:{up_p:.3f} DOWN:{dn_p:.3f} | 差价:{diff_str} | {status} ${total_spent:.2f} {win_str} [{sig_label}]")

        # ── 手动操作处理 ──────────────────────────────────
        with manual_lock:
            if manual_order_queue:
                order_req = manual_order_queue.pop(0)
                action    = order_req.get("action")

                if action == "BUY":
                    m_dir    = order_req["direction"]
                    m_amount = order_req["amount"]
                    m_token  = up_token if m_dir == "UP" else down_token
                    m_price  = up_p if m_dir == "UP" else dn_p
                    m_size   = round(m_amount / m_price, 2)
                    print(f"[手动] 买{m_dir} ${m_amount:.2f} -> {m_size}股 @ {m_price:.3f}")
                    resp = place_order_by_size(m_token, m_size, m_price, f"手动{m_dir}")
                    if resp:
                        if not placed:
                            placed      = True
                            entry_dir   = m_dir
                            entry_price = m_price
                            entry_size  = m_size
                            total_spent = round(m_size * m_price, 2)
                        else:
                            if m_dir == entry_dir:
                                entry_size  = round(entry_size + m_size, 2)
                                total_spent = round(total_spent + m_size * m_price, 2)

                elif action == "SELL":
                    m_dir   = order_req["direction"]
                    m_token = up_token if m_dir == "UP" else down_token
                    m_price = up_p if m_dir == "UP" else dn_p
                    if entry_dir == m_dir and entry_size > 0:
                        sell_order_by_size(m_token, entry_size, m_price, f"卖出{m_dir}")
                        entry_size  = 0.0
                        total_spent = 0.0
                        placed      = False
                        entry_dir   = None
                    else:
                        print(f"[警告] 无{m_dir}持仓记录")

                elif action == "SELL_ALL":
                    print("[手动] 一键全部卖出...")
                    if entry_size > 0 and entry_dir:
                        m_token = up_token if entry_dir == "UP" else down_token
                        m_price = up_p if entry_dir == "UP" else dn_p
                        sell_order_by_size(m_token, entry_size, m_price, f"全部卖{entry_dir}")
                        entry_size  = 0.0
                        total_spent = 0.0
                        placed      = False
                        entry_dir   = None
                    else:
                        print("[警告] 无持仓记录")

        # ── 庄家信号检测（两种模式都打印，仅A模式下单）──────
        if diff_val > 0 and btc_diff >= PRICE_DIFF_MIN and dn_p > up_p and dn_p < ENTRY_MAX:
            signal_up_count    = 0
            signal_down_count += 1
            if not input_mode:
                arrow = ">>>" if signal_down_count >= SIGNAL_SECS else "   "
                color = "\033[1;97m" if signal_down_count >= SIGNAL_SECS else "\033[0;36m"
                print(f"{color}{arrow}[庄家] ↓DOWN 连续{signal_down_count}秒(需{SIGNAL_SECS}秒) DOWN:{dn_p:.3f} 差价:{diff_str}\033[0m")
            if signal_down_count >= SIGNAL_SECS and not placed and in_entry_window and signal_source == "A":
                print(f"[下单] 庄家信号 -> 买DOWN ${this_bet:.2f}")
                resp = place_order(down_token, this_bet, dn_p, "庄家DOWN")
                if resp:
                    placed      = True
                    entry_dir   = "DOWN"
                    entry_price = dn_p
                    entry_size  = round(this_bet / dn_p, 2)
                    total_spent = round(this_bet, 2)

        elif diff_val < 0 and btc_diff >= PRICE_DIFF_MIN and up_p > dn_p and up_p < ENTRY_MAX:
            signal_down_count  = 0
            signal_up_count   += 1
            if not input_mode:
                arrow = ">>>" if signal_up_count >= SIGNAL_SECS else "   "
                color = "\033[1;97m" if signal_up_count >= SIGNAL_SECS else "\033[0;36m"
                print(f"{color}{arrow}[庄家] ↑UP   连续{signal_up_count}秒(需{SIGNAL_SECS}秒) UP:{up_p:.3f} 差价:{diff_str}\033[0m")
            if signal_up_count >= SIGNAL_SECS and not placed and in_entry_window and signal_source == "A":
                print(f"[下单] 庄家信号 -> 买UP ${this_bet:.2f}")
                resp = place_order(up_token, this_bet, up_p, "庄家UP")
                if resp:
                    placed      = True
                    entry_dir   = "UP"
                    entry_price = up_p
                    entry_size  = round(this_bet / up_p, 2)
                    total_spent = round(this_bet, 2)

        else:
            if (signal_down_count > 0 or signal_up_count > 0) and not input_mode:
                print(f"  [庄家] 信号中断，计数重置")
            signal_down_count = 0
            signal_up_count   = 0

        # ── 跨周期马丁 (B模式，每局只下一单) ──────────────
        if signal_source == "B" and not placed:
            cur_strong = None
            if up_p > 0.65 and btc_diff > 66:
                cur_strong = "UP"
            elif dn_p > 0.65 and btc_diff > 66:
                cur_strong = "DOWN"

            if cur_strong:
                buy_token = up_token if cur_strong == "UP" else down_token
                buy_price = up_p    if cur_strong == "UP" else dn_p
                print(f"  [马丁] 触发: {cur_strong} 价差:{diff_str} 级别:{martin_step_cross+1} 下注:${bet_amount}")
                resp = place_order(buy_token, bet_amount, buy_price, f"马丁{cur_strong}")
                if resp:
                    placed      = True
                    entry_dir   = cur_strong
                    entry_price = buy_price
                    entry_size  = round(bet_amount / buy_price, 2)
                    total_spent = round(bet_amount, 2)

        # ── 局内马丁 (C模式，同局内可多次反转下单) ─────────
        if signal_source == "C":
            cur_strong = None
            if up_p > 0.65 and btc_diff > 66:
                cur_strong = "UP"
            elif dn_p > 0.65 and btc_diff > 66:
                cur_strong = "DOWN"

            if cur_strong and (c_last_dir is None or cur_strong != c_last_dir):
                if c_martin_step < len(MARTIN_SEQ):
                    c_bet     = MARTIN_SEQ[c_martin_step]
                    buy_token = up_token if cur_strong == "UP" else down_token
                    buy_price = up_p    if cur_strong == "UP" else dn_p
                    print(f"  [局内马丁] 触发: {cur_strong} 价差:{diff_str} 级别:{c_martin_step+1} 下注:${c_bet}")
                    resp = place_order(buy_token, c_bet, buy_price, f"局内马丁{cur_strong}")
                    if resp:
                        if c_last_dir is None:
                            entry_price = buy_price
                        entry_dir   = cur_strong
                        entry_size  = round(entry_size + c_bet / buy_price, 2)
                        total_spent = round(total_spent + c_bet, 2)
                        placed      = True
                        c_last_dir  = cur_strong
                        c_martin_step += 1

        time.sleep(POLL_INTERVAL)

    # ── 结算 ──────────────────────────────────────────────
    up_p, dn_p = get_market_price(up_token)
    if up_p is None:
        up_p, dn_p = 0.5, 0.5

    final_winner = "UP" if up_p > dn_p else "DOWN"
    print(f"\n[结算] {final_winner}赢 | 总投:${total_spent:.2f}")

    if not placed:
        print("[结算] 本局无信号，未入场")
        price_feed.reset_start_price()
        return "skip", condition_id, 0, 0, state

    if entry_dir == final_winner:
        net = round(total_spent / entry_price - total_spent, 2)
        print(f"[结算] WIN  {entry_dir} ${total_spent:.2f} @ {entry_price:.3f} -> +${net:.2f}")
        result = "win"
    else:
        net = -total_spent
        print(f"[结算] LOSE {entry_dir} ${total_spent:.2f} @ {entry_price:.3f} -> -${total_spent:.2f}")
        result = "loss"

    print(f"[结算] 净盈亏: ${net:+.2f}")

    # 跨周期马丁step更新
    if signal_source == "B":
        new_martin_step = 0 if result == "win" else min(martin_step_cross + 1, len(MARTIN_SEQ) - 1)
        print(f"[马丁] {'重置->级别1 下注:$5' if result == 'win' else f'升级->级别{new_martin_step+1} 下局下注:${MARTIN_SEQ[new_martin_step]}'}")
    else:
        new_martin_step = martin_step_cross

    new_state = {"last_result": result, "last_bet": total_spent, "martin_step": new_martin_step}

    threading.Thread(target=redeem_thread, args=(condition_id,), daemon=True).start()
    price_feed.reset_start_price()

    return result, condition_id, total_spent, net, new_state

def main():
    atexit.register(lambda: os.system("stty sane"))
    stats = load_stats()
    state = load_state()

    print("=" * 55)
    print("  BTC 5M 策略 (ab)")
    print(f"  信号源A: 庄家信号 | 信号源B: 跨周期马丁 | 信号源C: 局内马丁")
    print(f"  马丁序列: {MARTIN_SEQ}")
    print(f"  [KEY] u=买UP  d=买DOWN  s=卖出  a=全部卖出  m=切换信号源  q=退出")
    print("=" * 55 + "\n")

    price_feed.start()

    kb_thread = threading.Thread(target=keyboard_listener, daemon=True)
    kb_thread.start()

    print_stats(stats, state)

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

            cycle_result, condition_id, spent, net, state = run_one_cycle(market, state)
            save_state(state)

            if cycle_result == "skip":
                stats["skips"] = stats.get("skips", 0) + 1
                save_stats(stats)
                continue

            stats["rounds"] += 1
            total_pnl = round(total_pnl + net, 2)

            if cycle_result == "win":
                stats["wins"] += 1
            else:
                stats["losses"] += 1

            stats["total_pnl"] = total_pnl
            stats["history"].append({
                "time":   datetime.now().strftime("%Y-%m-%d %H:%M"),
                "bet":    spent,
                "net":    net,
                "result": cycle_result,
                "signal": signal_source,
            })
            stats["history"] = stats["history"][-100:]
            save_stats(stats)
            print_stats(stats, state)

        except KeyboardInterrupt:
            print("\n[退出] 策略已停止")
            price_feed.stop()
            save_stats(stats)
            print_stats(stats, state)
            break
        except Exception as e:
            print(f"[ERROR] 主循环异常: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
