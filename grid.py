# -*- coding: utf-8 -*-
import time
import requests
import json
import os
import threading
from datetime import datetime
from web3 import Web3
from dotenv import load_dotenv

load_dotenv('/root/env6')

PRIVATE_KEY    = os.getenv("PRIVATE_KEY", "")
API_KEY        = os.getenv("API_KEY", "")
API_SECRET     = os.getenv("API_SECRET", "")
API_PASSPHRASE = os.getenv("API_PASSPHRASE", "")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")

CLOB_API      = "https://clob.polymarket.com"
GAMMA_API     = "https://gamma-api.polymarket.com"
POLYGON_RPC   = "https://polygon-mainnet.g.alchemy.com/v2/JWvS9PwN79OdjMaDAc5x3"
CTF_ADDRESS   = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS  = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
MARKET_PERIOD = 300
LOG_FILE      = "/root/grid.json"
STATE_FILE    = "/root/grid_state.json"

GRID_LOW      = 0.01
GRID_HIGH     = 0.99
GRID_COUNT    = 20
GRID_SIZE     = 5
PROFIT_RATIO  = 1.10
POLL_INTERVAL = 1

CTF_ABI = [{"inputs":[{"name":"collateralToken","type":"address"},{"name":"parentCollectionId","type":"bytes32"},{"name":"conditionId","type":"bytes32"},{"name":"indexSets","type":"uint256[]"}],"name":"redeemPositions","outputs":[],"stateMutability":"nonpayable","type":"function"}]
USDC_ABI = [{"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]
SAFE_ABI = [
    {"inputs":[],"name":"nonce","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"to","type":"address"},{"name":"value","type":"uint256"},{"name":"data","type":"bytes"},{"name":"operation","type":"uint8"},{"name":"safeTxGas","type":"uint256"},{"name":"baseGas","type":"uint256"},{"name":"gasPrice","type":"uint256"},{"name":"gasToken","type":"address"},{"name":"refundReceiver","type":"address"},{"name":"signatures","type":"bytes"}],"name":"execTransaction","outputs":[{"type":"bool"}],"stateMutability":"payable","type":"function"},
    {"inputs":[{"name":"to","type":"address"},{"name":"value","type":"uint256"},{"name":"data","type":"bytes"},{"name":"operation","type":"uint8"},{"name":"safeTxGas","type":"uint256"},{"name":"baseGas","type":"uint256"},{"name":"gasPrice","type":"uint256"},{"name":"gasToken","type":"address"},{"name":"refundReceiver","type":"address"},{"name":"_nonce","type":"uint256"}],"name":"getTransactionHash","outputs":[{"type":"bytes32"}],"stateMutability":"view","type":"function"}
]
ERC1155_ABI = [{"inputs":[{"name":"account","type":"address"},{"name":"id","type":"uint256"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]


def get_client():
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds
    creds = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASSPHRASE)
    return ClobClient(host=CLOB_API, chain_id=137, key=PRIVATE_KEY, creds=creds, signature_type=2, funder=WALLET_ADDRESS)


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
    except Exception as e:
        print(f"[ERROR] get_active_btc_market: {e}")
        return None


def place_buy_order(token_id, price, size, label=""):
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY
    price = round(round(price / 0.01) * 0.01, 2)
    try:
        client   = get_client()
        order    = OrderArgs(token_id=token_id, price=price, size=size, side=BUY)
        signed   = client.create_order(order)
        resp     = client.post_order(signed, OrderType.GTC)
        order_id = resp.get("orderID", "") if isinstance(resp, dict) else ""
        print(f"[BUY ] {label}: {size}股 @ {price:.2f} | ID:{order_id}")
        return order_id
    except Exception as e:
        print(f"[ERROR] place_buy_order {label} @ {price}: {e}")
        return ""


def place_sell_order(token_id, price, size, label=""):
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import SELL
    price = round(round(price / 0.01) * 0.01, 2)
    price = min(price, 0.99)
    try:
        client   = get_client()
        order    = OrderArgs(token_id=token_id, price=price, size=size, side=SELL)
        signed   = client.create_order(order)
        resp     = client.post_order(signed, OrderType.GTC)
        order_id = resp.get("orderID", "") if isinstance(resp, dict) else ""
        print(f"[SELL] {label}: {size}股 @ {price:.2f} | ID:{order_id}")
        return order_id
    except Exception as e:
        print(f"[ERROR] place_sell_order {label} @ {price}: {e}")
        return ""


def cancel_order(order_id):
    try:
        get_client().cancel(order_id)
    except Exception as e:
        print(f"[ERROR] cancel_order {order_id}: {e}")


def cancel_all_open_orders():
    try:
        get_client().cancel_all()
        print("[OK] 已取消所有挂单")
    except Exception as e:
        print(f"[ERROR] cancel_all_open_orders: {e}")


def get_open_orders(token_id):
    try:
        client = get_client()
        resp   = client.get_orders(market=token_id)
        orders = resp if isinstance(resp, list) else resp.get("data", [])
        result = []
        for o in orders:
            status = o.get("status", "")
            if status in ("LIVE", "OPEN", "UNMATCHED", "live", "open", "unmatched"):
                result.append({
                    "order_id": o.get("id", o.get("orderID", "")),
                    "price":    float(o.get("price", 0)),
                    "size":     float(o.get("size", o.get("original_size", 0))),
                    "side":     o.get("side", "BUY"),
                })
        return result
    except Exception as e:
        print(f"[ERROR] get_open_orders: {e}")
        return []


def get_filled_orders(token_id):
    try:
        client = get_client()
        resp   = client.get_orders(market=token_id)
        orders = resp if isinstance(resp, list) else resp.get("data", [])
        result = []
        for o in orders:
            status = o.get("status", "")
            if status in ("MATCHED", "FILLED", "matched", "filled"):
                result.append({
                    "order_id": o.get("id", o.get("orderID", "")),
                    "price":    float(o.get("price", 0)),
                    "size":     float(o.get("size", o.get("original_size", 0))),
                    "side":     o.get("side", "BUY"),
                })
        return result
    except Exception as e:
        print(f"[ERROR] get_filled_orders: {e}")
        return []


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


def load_stats():
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"rounds": 0, "total_buy_filled": 0, "total_sell_filled": 0, "total_profit": 0.0, "history": []}


def save_stats(stats):
    with open(LOG_FILE, "w") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)


def run_grid_cycle(market):
    up_token     = market["up_token"]
    down_token   = market["down_token"]
    end_ts       = market["end_ts"]
    condition_id = market["market_id"]

    # 计算20个网格价格
    grid_prices = [round(GRID_LOW + i * (GRID_HIGH - GRID_LOW) / (GRID_COUNT - 1), 4) for i in range(GRID_COUNT)]

    # 网格状态: {token_id: {price_str: {buy_order_id, sell_order_id}}}
    grid_state = {
        up_token:   {str(p): {"buy_order_id": "", "sell_order_id": ""} for p in grid_prices},
        down_token: {str(p): {"buy_order_id": "", "sell_order_id": ""} for p in grid_prices},
    }

    # 成交统计
    total_buy_filled  = 0
    total_sell_filled = 0
    round_profit      = 0.0

    # 追踪已知的已成交买单，避免重复处理
    processed_filled_buys  = {up_token: set(), down_token: set()}
    processed_filled_sells = {up_token: set(), down_token: set()}

    print(f"[GRID] 开始网格周期 | 市场:{condition_id[:16]}... | 结束:{datetime.fromtimestamp(end_ts).strftime('%H:%M:%S')}")
    print(f"[GRID] 网格价格区间: {grid_prices[0]:.4f} ~ {grid_prices[-1]:.4f} | 共{GRID_COUNT}档")

    # 初始挂单：UP + DOWN 各20单
    for token_id, label in [(up_token, "UP"), (down_token, "DOWN")]:
        for price in grid_prices:
            price_key = str(price)
            oid = place_buy_order(token_id, price, GRID_SIZE, label=f"{label}@{price:.4f}")
            if oid:
                grid_state[token_id][price_key]["buy_order_id"] = oid

    # 主扫描循环
    while True:
        now       = time.time()
        remaining = int(end_ts - now)

        if remaining <= 2:
            print(f"[GRID] 结束前{remaining}秒，取消所有挂单...")
            cancel_all_open_orders()
            break

        # 对两个token各自进行扫描
        for token_id, label in [(up_token, "UP"), (down_token, "DOWN")]:
            try:
                open_orders   = get_open_orders(token_id)
                open_buy_ids  = {o["order_id"] for o in open_orders if o["side"].upper() == "BUY"}
                open_sell_ids = {o["order_id"] for o in open_orders if o["side"].upper() == "SELL"}
                open_buy_prices  = {round(o["price"], 4) for o in open_orders if o["side"].upper() == "BUY"}

                for price in grid_prices:
                    price_key = str(price)
                    slot      = grid_state[token_id][price_key]
                    buy_oid   = slot["buy_order_id"]
                    sell_oid  = slot["sell_order_id"]

                    # 检查买单是否成交
                    if buy_oid and buy_oid not in open_buy_ids and buy_oid not in processed_filled_buys[token_id]:
                        # 买单已成交（不在挂单列表中且之前有记录）
                        processed_filled_buys[token_id].add(buy_oid)
                        total_buy_filled += 1
                        slot["buy_order_id"] = ""
                        print(f"[FILL] {label} 买单成交 @ {price:.4f} | 总成交买:{total_buy_filled}")

                        # 立即补挂买单（原价位）
                        new_buy_oid = place_buy_order(token_id, price, GRID_SIZE, label=f"{label}补@{price:.4f}")
                        if new_buy_oid:
                            slot["buy_order_id"] = new_buy_oid

                        # 挂卖单（买入价 × PROFIT_RATIO）
                        sell_price = round(round(price * PROFIT_RATIO / 0.01) * 0.01, 2)
                        sell_price = min(sell_price, 0.99)
                        if not sell_oid:
                            new_sell_oid = place_sell_order(token_id, sell_price, GRID_SIZE, label=f"{label}卖@{sell_price:.2f}")
                            if new_sell_oid:
                                slot["sell_order_id"] = new_sell_oid

                    # 检查卖单是否成交
                    if sell_oid and sell_oid not in open_sell_ids and sell_oid not in processed_filled_sells[token_id]:
                        processed_filled_sells[token_id].add(sell_oid)
                        total_sell_filled += 1
                        sell_price = round(round(price * PROFIT_RATIO / 0.01) * 0.01, 2)
                        sell_price = min(sell_price, 0.99)
                        profit = round((sell_price - price) * GRID_SIZE, 4)
                        round_profit = round(round_profit + profit, 4)
                        slot["sell_order_id"] = ""
                        print(f"[FILL] {label} 卖单成交 @ {sell_price:.2f} 利润:+${profit:.4f} | 总成交卖:{total_sell_filled} 累计:+${round_profit:.4f}")

                    # 检查网格完整性：买单缺失则补挂
                    if not slot["buy_order_id"] and price not in open_buy_prices:
                        new_buy_oid = place_buy_order(token_id, price, GRID_SIZE, label=f"{label}补缺@{price:.4f}")
                        if new_buy_oid:
                            slot["buy_order_id"] = new_buy_oid

            except Exception as e:
                print(f"[ERROR] 扫描{label}: {e}")

        # 统计当前挂单数
        up_buy_count   = sum(1 for p in grid_prices if grid_state[up_token][str(p)]["buy_order_id"])
        down_buy_count = sum(1 for p in grid_prices if grid_state[down_token][str(p)]["buy_order_id"])
        print(f"[GRID] 剩余{remaining}s | UP买单:{up_buy_count}/{GRID_COUNT} DOWN买单:{down_buy_count}/{GRID_COUNT} | 已成交买:{total_buy_filled} 卖:{total_sell_filled} | 本轮盈利:${round_profit:.2f}")

        time.sleep(POLL_INTERVAL)

    # 启动 redeem
    print(f"[GRID] 周期结束，启动redeem...")
    threading.Thread(target=redeem_thread, args=(condition_id,), daemon=True).start()

    return {
        "total_buy_filled":  total_buy_filled,
        "total_sell_filled": total_sell_filled,
        "round_profit":      round_profit,
    }


def main():
    stats = load_stats()
    print("=" * 55)
    print("  BTC 5M 双向网格做市策略")
    print(f"  价格范围: {GRID_LOW} ~ {GRID_HIGH} | 每边{GRID_COUNT}单 | 每单{GRID_SIZE}股")
    print(f"  利润目标: {PROFIT_RATIO:.0%} | 扫描间隔: {POLL_INTERVAL}秒")
    print("=" * 55 + "\n")

    last_market_id = None

    while True:
        try:
            market = get_active_btc_market()
            if not market:
                print("[等待] 寻找市场中...")
                time.sleep(5)
                continue

            if market["market_id"] == last_market_id:
                time.sleep(2)
                continue

            last_market_id = market["market_id"]
            print(f"[新周期] {datetime.now().strftime('%H:%M:%S')} | 市场:{market['market_id'][:16]}...")

            result = run_grid_cycle(market)

            stats["rounds"]           += 1
            stats["total_buy_filled"] += result["total_buy_filled"]
            stats["total_sell_filled"] += result["total_sell_filled"]
            stats["total_profit"]      = round(stats["total_profit"] + result["round_profit"], 4)
            stats["history"].append({
                "time":       datetime.now().strftime("%Y-%m-%d %H:%M"),
                "buy_filled": result["total_buy_filled"],
                "sell_filled": result["total_sell_filled"],
                "profit":     result["round_profit"],
            })
            stats["history"] = stats["history"][-100:]
            save_stats(stats)

            print(f"[统计] 总轮:{stats['rounds']} 总成交买:{stats['total_buy_filled']} 卖:{stats['total_sell_filled']} 累计盈利:${stats['total_profit']:.4f}")

            time.sleep(3)

        except KeyboardInterrupt:
            print("\n[退出] 策略已停止")
            save_stats(stats)
            break
        except Exception as e:
            print(f"[ERROR] 主循环异常: {e}")
            time.sleep(10)


if __name__ == "__main__":
    main()
