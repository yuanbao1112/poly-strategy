# -*- coding: utf-8 -*-
import time
import requests
import json
import os
import threading
import websocket
from datetime import datetime
import sys
import termios
import tty
import select
import argparse
from dotenv import load_dotenv
from web3 import Web3

parser = argparse.ArgumentParser()
parser.add_argument("--id", type=str, default="6", help="副本编号")
args, _ = parser.parse_known_args()

INSTANCE_ID      = args.id
ENV_FILE         = f"/root/env{INSTANCE_ID}"
RUNTIME_CFG_PATH = f"/root/runtime_cfg_{INSTANCE_ID}.json"
STATS_FILE       = f"/root/5sfz_{INSTANCE_ID}_stats.json"

load_dotenv(ENV_FILE)
cfg_lock = threading.Lock()

CTF_ADDRESS  = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

USDC_ABI = [{"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]
CTF_ABI  = [{"inputs":[{"name":"collateralToken","type":"address"},{"name":"parentCollectionId","type":"bytes32"},{"name":"conditionId","type":"bytes32"},{"name":"indexSets","type":"uint256[]"}],"name":"redeemPositions","outputs":[],"stateMutability":"nonpayable","type":"function"}]
SAFE_ABI = [
    {"inputs":[],"name":"nonce","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"to","type":"address"},{"name":"value","type":"uint256"},{"name":"data","type":"bytes"},{"name":"operation","type":"uint8"},{"name":"safeTxGas","type":"uint256"},{"name":"baseGas","type":"uint256"},{"name":"gasPrice","type":"uint256"},{"name":"gasToken","type":"address"},{"name":"refundReceiver","type":"address"},{"name":"signatures","type":"bytes"}],"name":"execTransaction","outputs":[{"type":"bool"}],"stateMutability":"payable","type":"function"},
    {"inputs":[{"name":"to","type":"address"},{"name":"value","type":"uint256"},{"name":"data","type":"bytes"},{"name":"operation","type":"uint8"},{"name":"safeTxGas","type":"uint256"},{"name":"baseGas","type":"uint256"},{"name":"gasPrice","type":"uint256"},{"name":"gasToken","type":"address"},{"name":"refundReceiver","type":"address"},{"name":"_nonce","type":"uint256"}],"name":"getTransactionHash","outputs":[{"type":"bytes32"}],"stateMutability":"view","type":"function"}
]

MARKET_CONFIGS = {
    "5m":  {"period": 300,   "slug_prefix": "btc-updown-5m",  "label": "BTC 5分钟"},
    "15m": {"period": 900,   "slug_prefix": "btc-updown-15m", "label": "BTC 15分钟"},
    "1h":  {"period": 3600,  "slug_prefix": "btc-updown-1h",  "label": "BTC 1小时"},
    "4h":  {"period": 14400, "slug_prefix": "btc-updown-4h",  "label": "BTC 4小时"},
    "1d":  {"period": 86400, "slug_prefix": "btc-updown-1d",  "label": "BTC 1天"},
}

RUNTIME_CFG = {
    "MARKET_TYPE":      "5m",
    "BUY_MODE":         "size",
    "BUY_USD":          50.0,
    "BUY_SHARES":       5.0,
    "ENTRY_LAST_SEC":   2.5,
    "CANCEL_LAST_SEC":  0.2,
    "BTC_GAP_MIN":      25.0,
    "FAST_PRINT_MS":    1000.0,
    "MIN_BUY_PRICE":    0.80,
    "MAX_BUY_PRICE":    0.99,
    "HEDGE_TRIGGER":    0.65,
    "HEDGE_MULTI":      3.0,
    "HEDGE_MAX":        2,
    "HEDGE_CD_SEC":     0.1,
    "HEDGE_CONFIRM_MS": 300.0,
    "TP_PCT":           0.0,
    "SL_PCT":           0.0,
    "ORDER_MODE":       "gtc",
    "MANUAL_HEDGE":     False,
    "SL_HEDGE_ENABLED": True,
    "ENTRY_MODE":       "dominant",
    "COPY_ENABLED":     False,
    "COPY_ADDRESS":     "",
    "COPY_MODE":        "usd",
    "COPY_USD":         10.0,
    "COPY_SHARES":      5.0,
    "COPY_PCT":         0.0,
    "COPY_MIN_USD":     1.0,
    "COPY_MAX_USD":     500.0,
}
manual_order_queue = []
manual_lock        = threading.Lock()

def plog(msg=""):
    sys.stdout.write(str(msg) + "\r\n")
    sys.stdout.flush()

def get_cfg():
    with cfg_lock:
        return dict(RUNTIME_CFG)

def _apply_cfg(d):
    with cfg_lock:
        for k in ("MARKET_TYPE", "BUY_MODE", "BUY_USD", "BUY_SHARES", "ENTRY_LAST_SEC",
                  "CANCEL_LAST_SEC", "BTC_GAP_MIN", "FAST_PRINT_MS",
                  "MIN_BUY_PRICE", "MAX_BUY_PRICE",
                  "HEDGE_TRIGGER", "HEDGE_MULTI", "HEDGE_MAX", "HEDGE_CD_SEC",
                  "HEDGE_CONFIRM_MS", "TP_PCT", "SL_PCT", "ORDER_MODE", "MANUAL_HEDGE", "SL_HEDGE_ENABLED",
                  "ENTRY_MODE", "COPY_ENABLED", "COPY_ADDRESS", "COPY_MODE",
                  "COPY_USD", "COPY_SHARES", "COPY_PCT", "COPY_MIN_USD", "COPY_MAX_USD"):
            if k in d:
                RUNTIME_CFG[k] = d[k]

def load_runtime_cfg(verbose=False):
    try:
        if not os.path.exists(RUNTIME_CFG_PATH):
            return
        with open(RUNTIME_CFG_PATH, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
        if "MARKET_TYPE" in d:
            d["MARKET_TYPE"] = str(d["MARKET_TYPE"]).lower()
            if d["MARKET_TYPE"] not in MARKET_CONFIGS:
                d.pop("MARKET_TYPE", None)
        if "BUY_MODE" in d:
            d["BUY_MODE"] = str(d["BUY_MODE"]).lower()
            if d["BUY_MODE"] not in ("usd", "size"):
                d.pop("BUY_MODE", None)
        if "ORDER_MODE" in d:
            d["ORDER_MODE"] = str(d["ORDER_MODE"]).lower()
            if d["ORDER_MODE"] not in ("fak", "gtc"):
                d.pop("ORDER_MODE", None)
        if "MANUAL_HEDGE" in d:
            if isinstance(d["MANUAL_HEDGE"], str):
                d["MANUAL_HEDGE"] = d["MANUAL_HEDGE"].lower() in ("true", "1", "yes")
        if "SL_HEDGE_ENABLED" in d:
            if isinstance(d["SL_HEDGE_ENABLED"], str):
                d["SL_HEDGE_ENABLED"] = d["SL_HEDGE_ENABLED"].lower() in ("true", "1", "yes")
        if "ENTRY_MODE" in d:
            d["ENTRY_MODE"] = str(d["ENTRY_MODE"]).lower()
            if d["ENTRY_MODE"] not in ("dominant", "reversal"):
                d.pop("ENTRY_MODE", None)
        if "COPY_ENABLED" in d:
            if isinstance(d["COPY_ENABLED"], str):
                d["COPY_ENABLED"] = d["COPY_ENABLED"].lower() in ("true", "1", "yes")
        if "COPY_MODE" in d:
            d["COPY_MODE"] = str(d["COPY_MODE"]).lower()
            if d["COPY_MODE"] not in ("usd", "size", "pct"):
                d.pop("COPY_MODE", None)
        if "COPY_ADDRESS" in d:
            d["COPY_ADDRESS"] = str(d["COPY_ADDRESS"]).strip()
        for k in ("COPY_USD", "COPY_SHARES", "COPY_PCT", "COPY_MIN_USD", "COPY_MAX_USD"):
            if k in d:
                try:
                    d[k] = float(d[k])
                except:
                    d.pop(k, None)
        for k in ("BUY_USD", "BUY_SHARES", "ENTRY_LAST_SEC", "CANCEL_LAST_SEC",
                  "BTC_GAP_MIN", "FAST_PRINT_MS", "MIN_BUY_PRICE", "MAX_BUY_PRICE",
                  "HEDGE_TRIGGER", "HEDGE_MULTI", "HEDGE_CD_SEC", "HEDGE_CONFIRM_MS",
                  "TP_PCT", "SL_PCT"):
            if k in d:
                try:
                    d[k] = float(d[k])
                except:
                    d.pop(k, None)
        for k in ("HEDGE_MAX",):
            if k in d:
                try:
                    d[k] = int(d[k])
                except:
                    d.pop(k, None)
        _apply_cfg(d)
        if verbose:
            cur = get_cfg()
            mc  = MARKET_CONFIGS.get(cur["MARKET_TYPE"], {})
            plog(f"🔧 热更新: MARKET={cur['MARKET_TYPE']}({mc.get('label','')}) "
                 f"BUY_MODE={cur['BUY_MODE']} BUY_USD={cur['BUY_USD']} "
                 f"ENTRY={cur['ENTRY_LAST_SEC']}s CANCEL={cur['CANCEL_LAST_SEC']}s "
                 f"TP={cur['TP_PCT']:.1f}% SL={cur['SL_PCT']:.1f}% "
                 f"HEDGE_MAX={cur['HEDGE_MAX']} SL_HEDGE_ENABLED={cur.get('SL_HEDGE_ENABLED', True)}")
    except Exception as e:
        if verbose:
            plog(f"⚠️  读取配置失败: {e}")

def save_runtime_cfg():
    try:
        with cfg_lock:
            d = dict(RUNTIME_CFG)
        with open(RUNTIME_CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        plog(f"⚠️  保存配置失败: {e}")

def runtime_cfg_watcher():
    last_mtime = None
    while True:
        try:
            mtime = os.path.getmtime(RUNTIME_CFG_PATH)
            if last_mtime is None or mtime != last_mtime:
                last_mtime = mtime
                load_runtime_cfg(verbose=True)
        except:
            pass
        time.sleep(1.0)

stats_lock = threading.Lock()

def load_stats():
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return {"initial_balance": None, "total_bought": 0,
            "wins": 0, "losses": 0, "history": []}

def save_stats(stats):
    with stats_lock:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)

def print_stats(stats, current_balance):
    initial  = stats.get("initial_balance")
    total    = stats["wins"] + stats["losses"]
    win_rate = (stats["wins"] / total * 100) if total > 0 else 0.0
    plog()
    plog("=" * 55)
    plog("  📊 统计")
    plog(f"  初始余额:   ${initial:.2f}" if initial else "  初始余额:   未记录")
    plog(f"  当前余额:   ${current_balance:.2f}" if current_balance is not None else "  当前余额:   获取失败")
    if initial and current_balance is not None:
        delta = current_balance - initial
        sign  = "+" if delta >= 0 else ""
        plog(f"  余额变化:   {sign}${delta:.2f}")
    plog(f"  总买入次数: {stats['total_bought']}")
    plog(f"  胜利次数:   {stats['wins']}")
    plog(f"  失败次数:   {stats['losses']}")
    plog(f"  胜率:       {win_rate:.1f}%")
    plog("=" * 55)

_in_param_panel = threading.Event()

def _restore_terminal(fd, old_settings):
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    except:
        pass

def show_param_panel(fd, old_settings):
    _in_param_panel.set()
    _restore_terminal(fd, old_settings)
    try:
        param_map = {
            "1":  ("MARKET_TYPE",      "str",   list(MARKET_CONFIGS.keys())),
            "2":  ("BUY_MODE",         "str",   ["usd", "size"]),
            "3":  ("BUY_USD",          "float", (0.01, 99999)),
            "4":  ("BUY_SHARES",       "float", (0.01, 99999)),
            "5":  ("ENTRY_LAST_SEC",   "float", (0.001, 99999)),
            "6":  ("CANCEL_LAST_SEC",  "float", (0.001, 60)),
            "7":  ("BTC_GAP_MIN",      "float", (0, 99999)),
            "8":  ("FAST_PRINT_MS",    "float", (50, 5000)),
            "9":  ("MIN_BUY_PRICE",    "float", (0.01, 0.99)),
            "10": ("MAX_BUY_PRICE",    "float", (0.01, 0.99)),
            "11": ("HEDGE_TRIGGER",    "float", (0.5, 0.99)),
            "12": ("HEDGE_MULTI",      "float", (1, 100)),
            "13": ("HEDGE_MAX",        "int",   (0, 99)),
            "14": ("HEDGE_CD_SEC",     "float", (0, 60)),
            "15": ("HEDGE_CONFIRM_MS", "float", (0, 2000)),
            "16": ("TP_PCT",           "float", (0, 999)),
            "17": ("SL_PCT",           "float", (0, 100)),
            "18": ("ORDER_MODE",        "str",   ["fak", "gtc"]),
            "19": ("MANUAL_HEDGE",      "bool",  [True, False]),
            "29": ("SL_HEDGE_ENABLED",   "bool",  [True, False]),
            "28": ("ENTRY_MODE",         "str",   ["dominant", "reversal"]),
            "20": ("COPY_ENABLED",       "bool",  [True, False]),
            "21": ("COPY_ADDRESS",       "str_free", None),
            "22": ("COPY_MODE",          "str",   ["usd", "size", "pct"]),
            "23": ("COPY_USD",           "float", (1.0, 99999)),
            "24": ("COPY_SHARES",        "float", (1.0, 99999)),
            "25": ("COPY_PCT",           "float", (1.0, 500.0)),
            "26": ("COPY_MIN_USD",       "float", (0.1, 9999)),
            "27": ("COPY_MAX_USD",       "float", (1.0, 99999)),
        }
        while True:
            cur = get_cfg()
            mc  = MARKET_CONFIGS.get(cur["MARKET_TYPE"], {})
            print("\n" + "=" * 55)
            print("  ⚙️   参数面板  （机器人后台运行中）")
            print("=" * 55)
            print(f"  1.  MARKET_TYPE      = {cur['MARKET_TYPE']:>8}   ({mc.get('label','')})  可选: 5m/15m/1h/4h/1d")
            print(f"  2.  BUY_MODE         = {cur['BUY_MODE']:>8}   (usd/size)")
            print(f"  3.  BUY_USD          = {cur['BUY_USD']:>8.2f}   USDC")
            print(f"  4.  BUY_SHARES       = {cur['BUY_SHARES']:>8.2f}   股")
            print(f"  5.  ENTRY_LAST_SEC   = {cur['ENTRY_LAST_SEC']:>8.3f}   秒")
            print(f"  6.  CANCEL_LAST_SEC  = {cur['CANCEL_LAST_SEC']:>8.3f}   秒")
            print(f"  7.  BTC_GAP_MIN      = {cur['BTC_GAP_MIN']:>8.1f}   美元(0=不过滤)")
            print(f"  8.  FAST_PRINT_MS    = {cur['FAST_PRINT_MS']:>8.0f}   ms")
            print(f"  9.  MIN_BUY_PRICE    = {cur['MIN_BUY_PRICE']:>8.3f}")
            print(f"  10. MAX_BUY_PRICE    = {cur['MAX_BUY_PRICE']:>8.3f}")
            print(f"  11. HEDGE_TRIGGER    = {cur['HEDGE_TRIGGER']:>8.3f}")
            print(f"  12. HEDGE_MULTI      = {cur['HEDGE_MULTI']:>8.1f}   倍")
            print(f"  13. HEDGE_MAX        = {cur['HEDGE_MAX']:>8}   次(0=不对冲)")
            print(f"  14. HEDGE_CD_SEC     = {cur['HEDGE_CD_SEC']:>8.2f}   秒")
            print(f"  15. HEDGE_CONFIRM_MS = {cur['HEDGE_CONFIRM_MS']:>8.0f}   ms")
            print(f"  ── 止盈止损 ──────────────────────────────")
            print(f"  16. TP_PCT           = {cur['TP_PCT']:>8.1f}   % 止盈(0=不止盈)")
            print(f"  17. SL_PCT           = {cur['SL_PCT']:>8.1f}   % 止损(0=不止损)")
            print(f"  18. ORDER_MODE       = {cur['ORDER_MODE']:>8}   (fak=吃单/gtc=挂单)")
            print(f"  19. MANUAL_HEDGE     = {str(cur['MANUAL_HEDGE']):>8}   手动买入后是否对冲")
            print(f"  29. SL_HEDGE_ENABLED = {str(cur.get('SL_HEDGE_ENABLED', True)):>8}   止损时是否对冲买反向")
            print(f"  28. ENTRY_MODE       = {cur.get('ENTRY_MODE','dominant'):>8}   dominant=占优/reversal=反转")
            print(f"  ── 跟单模式 ──────────────────────────────────")
            print(f"  20. COPY_ENABLED     = {str(cur.get('COPY_ENABLED',False)):>8}   开启跟单")
            print(f"  21. COPY_ADDRESS     = {(cur.get('COPY_ADDRESS','') or '未设置')[:16]:>16}   跟单地址")
            print(f"  22. COPY_MODE        = {cur.get('COPY_MODE','usd'):>8}   usd/size/pct")
            print(f"  23. COPY_USD         = {cur.get('COPY_USD',10.0):>8.2f}   固定金额USDC")
            print(f"  24. COPY_SHARES      = {cur.get('COPY_SHARES',5.0):>8.2f}   固定股数")
            print(f"  25. COPY_PCT         = {cur.get('COPY_PCT',0.0):>8.1f}   跟单百分比%")
            print(f"  26. COPY_MIN_USD     = {cur.get('COPY_MIN_USD',1.0):>8.2f}   最小跟单金额")
            print(f"  27. COPY_MAX_USD     = {cur.get('COPY_MAX_USD',500.0):>8.2f}   最大跟单金额")
            print("=" * 55)
            tp_desc = f"盈利达到买入金额{cur['TP_PCT']:.0f}%时卖出" if cur['TP_PCT'] > 0 else "未启用"
            sl_desc = f"亏损达到买入金额{cur['SL_PCT']:.0f}%时卖出" if cur['SL_PCT'] > 0 else "未启用"
            print(f"  止盈: {tp_desc}")
            print(f"  止损: {sl_desc}")
            print("=" * 55)
            print("  输入数字修改对应参数，q=退出 (1-19)")
            print("-" * 55)

            try:
                choice = input("  选择 > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                break

            if choice == "q":
                print("  ↩️  已退出面板")
                break

            if choice not in param_map:
                print(f"  ⚠️  无效选项，请输入 1-19 或 q")
                continue

            key, typ, constraint = param_map[choice]
            cur_val = cur[key]

            try:
                raw = input(f"  {key} 当前={cur_val}，新值 > ").strip()
            except (EOFError, KeyboardInterrupt):
                break

            if not raw:
                print("  ↩️  未修改")
                continue

            try:
                new_val = None
                if typ == "str":
                    new_val = raw.lower()
                    if new_val not in constraint:
                        print(f"  ⚠️  只能填: {constraint}")
                        continue
                elif typ == "float":
                    new_val = float(raw)
                    lo, hi = constraint
                    if not (lo <= new_val <= hi):
                        print(f"  ⚠️  需在 {lo} ~ {hi} 之间")
                        continue
                elif typ == "int":
                    new_val = int(raw)
                    lo, hi = constraint
                    if not (lo <= new_val <= hi):
                        print(f"  ⚠️  需在 {lo} ~ {hi} 之间")
                        continue
                elif typ == "bool":
                    if raw.lower() in ("true", "1", "yes", "y"):
                        new_val = True
                    elif raw.lower() in ("false", "0", "no", "n"):
                        new_val = False
                    else:
                        print(f"  ⚠️  请输入 true 或 false")
                        continue
                elif typ == "str_free":
                    new_val = raw.strip()
            except ValueError:
                print("  ⚠️  格式错误")
                continue
            if new_val is None:
                continue

            _apply_cfg({key: new_val})
            save_runtime_cfg()
            if key == "MARKET_TYPE":
                mc2 = MARKET_CONFIGS.get(new_val, {})
                print(f"  ✅ {key} = {new_val} ({mc2.get('label','')})  下一局生效")
            elif key == "TP_PCT":
                print(f"  ✅ 止盈 = {new_val:.1f}%  {'已启用' if new_val > 0 else '已关闭'}")
            elif key == "SL_PCT":
                print(f"  ✅ 止损 = {new_val:.1f}%  {'已启用' if new_val > 0 else '已关闭'}")
            else:
                print(f"  ✅ {key} = {new_val}  已保存")

    except Exception as e:
        print(f"\n⚠️  面板异常: {e}")
    finally:
        _in_param_panel.clear()

def keyboard_listener():
    fd = sys.stdin.fileno()
    if not os.isatty(fd):
        plog("⚠️  非TTY环境，键盘监听不可用")
        return
    plog("💡 提示：按 [P]=参数面板 [U]=买UP [D]=选方向下单 [S]=卖出 [A]=全卖 [Q]=退出")
    old_settings = termios.tcgetattr(fd)
    try:
        while True:
            if _in_param_panel.is_set():
                time.sleep(0.1)
                continue
            try:
                tty.setraw(fd)
                r, _, _ = select.select([sys.stdin], [], [], 0.2)
                if r:
                    ch = sys.stdin.read(1)
                    _restore_terminal(fd, old_settings)
                    if ch == "\x03":
                        _restore_terminal(fd, old_settings)
                        plog("\n⛔ Ctrl+C 已停止")
                        price_feed.stop()
                        os._exit(0)
                    elif ch.lower() == "p":
                        show_param_panel(fd, old_settings)
                    elif ch.lower() == "u":
                        _in_param_panel.set()
                        try:
                            sys.stdout.write("\r\n[手动] 买UP，输入股数: ")
                            sys.stdout.flush()
                            size = float(input("").strip())
                            if size < 5: size = 5.0
                            with manual_lock:
                                manual_order_queue.append({"action": "BUY", "direction": "UP", "size": size})
                            plog(f"[OK] 已加入队列: 买UP {size}股")
                        except Exception as e:
                            plog(f"[警告] 输入错误: {e}")
                        finally:
                            _in_param_panel.clear()
                    elif ch.lower() == "d":
                        _in_param_panel.set()
                        try:
                            sys.stdout.write("\r\n[手动] 选择方向 (u=UP / d=DOWN): ")
                            sys.stdout.flush()
                            dir_input = input("").strip().lower()
                            if dir_input not in ("u", "d"):
                                plog("[警告] 无效方向")
                            else:
                                direction = "UP" if dir_input == "u" else "DOWN"
                                sys.stdout.write(f"\r\n[手动] 买{direction}，输入股数: ")
                                sys.stdout.flush()
                                size = float(input("").strip())
                                if size < 5: size = 5.0
                                with manual_lock:
                                    manual_order_queue.append({"action": "BUY", "direction": direction, "size": size})
                                plog(f"[OK] 已加入队列: 买{direction} {size}股")
                        except Exception as e:
                            plog(f"[警告] 输入错误: {e}")
                        finally:
                            _in_param_panel.clear()
                    elif ch.lower() == "s":
                        _in_param_panel.set()
                        try:
                            sys.stdout.write("\r\n[手动] 卖出方向 (u=卖UP / d=卖DOWN): ")
                            sys.stdout.flush()
                            dir_key = input("").strip().lower()
                            if dir_key not in ("u", "d"):
                                plog("[警告] 无效方向")
                            else:
                                sell_dir = "UP" if dir_key == "u" else "DOWN"
                                with manual_lock:
                                    manual_order_queue.append({"action": "SELL", "direction": sell_dir, "size": 0})
                                plog(f"[OK] 已加入队列: 卖出{sell_dir}")
                        except Exception as e:
                            plog(f"[警告] 输入错误: {e}")
                        finally:
                            _in_param_panel.clear()
                    elif ch.lower() == "a":
                        with manual_lock:
                            manual_order_queue.append({"action": "SELL_ALL"})
                        plog("[手动] 一键全部卖出已加入队列")
                    elif ch.lower() == "q":
                        _restore_terminal(fd, old_settings)
                        plog("\n[退出] 用户按q退出")
                        price_feed.stop()
                        os._exit(0)
                else:
                    _restore_terminal(fd, old_settings)
            except Exception:
                _restore_terminal(fd, old_settings)
                time.sleep(0.1)
    except Exception:
        pass
    finally:
        _restore_terminal(fd, old_settings)

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
    raise EnvironmentError(f"❌ 环境变量未设置: {', '.join(_missing)}")

PARAMS_A = {
    "ENTRY_MAX": 0.62, "ENTRY_MIN": 0.38, "PRICE_DIFF_MIN": 15.0,
    "INITIAL_SIZE": 5.0, "HEDGE_ENABLED": True, "HEDGE_RATIO": 1.0,
    "SIGNAL_CONFIRM": 1, "ENTRY_LAST_SEC": 0, "MANUAL_ONLY": False,
    "ORDER_MODE": "size", "ORDER_TYPE": "taker", "TAKER_DISCOUNT": 0.15,
}

LOOP_INTERVAL    = 0.05
RETRY_INTERVAL   = 0.05
SAMPLE_FINAL_SEC = 1.0

CLOB_API    = "https://clob.polymarket.com"
GAMMA_API   = "https://gamma-api.polymarket.com"
CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RTDS_WS_URL = "wss://ws-live-data.polymarket.com"
POLYGON_RPC = "https://polygon-mainnet.g.alchemy.com/v2/JWvS9PwN79OdjMaDAc5x3"

class PriceCache:
    def __init__(self):
        self._lock      = threading.Lock()
        self.up_mid     = None
        self.dn_mid     = None
        self.up_token   = None
        self.dn_token   = None
        self._ws        = None
        self._running   = False
        self._ws_ok     = False
        self._last_http = 0.0
        self._up_bid    = None
        self._up_ask    = None
        self._dn_bid    = None
        self._dn_ask    = None

    def start(self, up_token, dn_token):
        with self._lock:
            self.up_token = up_token
            self.dn_token = dn_token
            self.up_mid   = None
            self.dn_mid   = None
            self._up_bid  = None
            self._up_ask  = None
            self._dn_bid  = None
            self._dn_ask  = None
        self._running = True
        self._ws_ok   = False
        threading.Thread(target=self._ws_loop, daemon=True).start()
        threading.Thread(target=self._http_fallback_loop, daemon=True).start()

    def stop(self):
        self._running = False
        try:
            if self._ws:
                self._ws.close()
        except: pass

    def _calc_mid(self, bid, ask):
        if bid is not None and ask is not None:
            return (float(bid) + float(ask)) / 2.0
        if ask is not None:
            return float(ask) - 0.005
        if bid is not None:
            return float(bid) + 0.005
        return None

    def _on_open(self, ws):
        with self._lock:
            up_t = self.up_token
            dn_t = self.dn_token
        sub_msg = json.dumps({
            "auth":       {},
            "markets":    [],
            "assets_ids": [up_t, dn_t],
            "type":       "market"
        })
        ws.send(sub_msg)
        self._ws_ok = True
        plog("✅ PriceCache WS 订阅成功")

    def _on_message(self, ws, message):
        try:
            events = json.loads(message)
            if not isinstance(events, list):
                events = [events]
            with self._lock:
                up_t = self.up_token
                dn_t = self.dn_token
                for ev in events:
                    asset_id = ev.get("asset_id", "")
                    if not asset_id:
                        continue
                    side  = ev.get("side", "").upper()
                    price = ev.get("price")
                    if price is not None:
                        price = float(price)
                        if asset_id == up_t:
                            if side == "BUY":
                                self._up_bid = price
                            elif side == "SELL":
                                self._up_ask = price
                            mid = self._calc_mid(self._up_bid, self._up_ask)
                            if mid and 0 < mid < 1:
                                self.up_mid = mid
                        elif asset_id == dn_t:
                            if side == "BUY":
                                self._dn_bid = price
                            elif side == "SELL":
                                self._dn_ask = price
                            mid = self._calc_mid(self._dn_bid, self._dn_ask)
                            if mid and 0 < mid < 1:
                                self.dn_mid = mid
                    best_bid = ev.get("best_bid") or ev.get("bid")
                    best_ask = ev.get("best_ask") or ev.get("ask")
                    if best_bid or best_ask:
                        mid = self._calc_mid(best_bid, best_ask)
                        if mid and 0 < mid < 1:
                            if asset_id == up_t:
                                self.up_mid = mid
                            elif asset_id == dn_t:
                                self.dn_mid = mid
                    raw_mid = ev.get("mid") or ev.get("midpoint")
                    if raw_mid is not None:
                        mid = float(raw_mid)
                        if 0 < mid < 1:
                            if asset_id == up_t:
                                self.up_mid = mid
                            elif asset_id == dn_t:
                                self.dn_mid = mid
        except:
            pass

    def _on_error(self, ws, error):
        self._ws_ok = False
        plog(f"⚠️  PriceCache WS 错误: {error}")

    def _on_close(self, ws, code, msg):
        self._ws_ok = False
        if self._running:
            plog("🔌 PriceCache WS 断开，3秒后重连...")
            time.sleep(3)

    def _ws_loop(self):
        while self._running:
            try:
                with self._lock:
                    up_t = self.up_token
                    dn_t = self.dn_token
                if not up_t or not dn_t:
                    time.sleep(0.5)
                    continue
                self._ws = websocket.WebSocketApp(
                    CLOB_WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                plog(f"⚠️  PriceCache WS 异常: {e}")
            time.sleep(3)

    def _fetch_mid_http(self, token_id):
        try:
            r = requests.get(CLOB_API + "/midpoint",
                             params={"token_id": token_id}, timeout=2.0)
            if r.status_code == 200:
                v = float(r.json().get("mid", 0))
                return v if 0 < v < 1 else None
        except: pass
        return None

    def _http_fallback_loop(self):
        while self._running:
            interval = 30.0 if self._ws_ok else 2.0
            now = time.time()
            if now - self._last_http < interval:
                time.sleep(0.2)
                continue
            self._last_http = now
            with self._lock:
                up_t = self.up_token
                dn_t = self.dn_token
            results = [None, None]
            def f0(): results[0] = self._fetch_mid_http(up_t)
            def f1(): results[1] = self._fetch_mid_http(dn_t)
            ts = [threading.Thread(target=fn, daemon=True) for fn in (f0, f1)]
            for t in ts: t.start()
            for t in ts: t.join(timeout=2.5)
            with self._lock:
                if results[0] is not None: self.up_mid = results[0]
                if results[1] is not None: self.dn_mid = results[1]
            if self._ws_ok:
                plog(f"[HTTP校正] UP:{self.up_mid:.3f} DN:{self.dn_mid:.3f}")

    def get(self):
        with self._lock:
            return self.up_mid, self.dn_mid

price_cache = PriceCache()

def get_usdc_balance():
    try:
        w3   = Web3(Web3.HTTPProvider(POLYGON_RPC))
        usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=USDC_ABI)
        bal  = usdc.functions.balanceOf(Web3.to_checksum_address(WALLET_ADDRESS)).call()
        return round(bal / 1e6, 2)
    except: return None

def redeem_thread(condition_id):
    plog("[Redeem] 等待35秒后开始赎回")
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
            nonce   = safe.functions.nonce().call()
            tx_hash = safe.functions.getTransactionHash(
                Web3.to_checksum_address(CTF_ADDRESS), 0, calldata, 0, 0, 0, 0,
                "0x0000000000000000000000000000000000000000",
                "0x0000000000000000000000000000000000000000", nonce
            ).call()
            sh  = account.unsafe_sign_hash(tx_hash)
            sig = sh.signature
            v   = sig[-1]
            if v < 27: v += 27
            signature = bytes(sig[:64]) + bytes([v])
            gas_price = int(w3.eth.gas_price * (1 + retry * 0.2))
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
            plog(f"[Redeem] txHash={tx_hash2.hex()}")
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash2, timeout=180)
            if receipt.status == 1:
                plog("[Redeem] ✅ 完成")
                return
            raise Exception("上链失败")
        except Exception as e:
            if "already known" in str(e):
                plog("[Redeem] 已提交")
                return
            if retry > 5:
                plog(f"[Redeem] 放弃: {e}")
                return
            plog(f"[Redeem] 第{retry}次失败，10秒后重试: {e}")
            time.sleep(10)

class ChainlinkPriceFeed:
    def __init__(self):
        self.current_price = None
        self.start_price   = None
        self._ws           = None
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
        except: pass

    def _on_open(self, ws):
        ws.send(json.dumps({
            "action": "subscribe",
            "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "*", "filters": ""}]
        }))
        plog("✅ Chainlink订阅成功")

    def _on_close(self, ws, code, msg):
        if self._running:
            plog("🔌 Chainlink WS断开，5秒后重连...")
            time.sleep(5)
            self._connect()

    def _connect(self):
        try:
            self._ws = websocket.WebSocketApp(
                RTDS_WS_URL,
                on_open=self._on_open,
                on_message=self._on_message,
                on_close=self._on_close
            )
            self._ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            plog(f"连接错误: {e}")

    def start(self):
        self._running = True
        threading.Thread(target=self._connect, daemon=True).start()
        plog("⏳ 等待Chainlink价格...")
        for _ in range(30):
            if self.current_price: break
            time.sleep(1)
        if self.current_price:
            plog(f"✅ Chainlink连接成功，当前BTC: ${self.current_price:.2f}")
        else:
            plog("⚠️  Chainlink超时，继续运行...")

    def record_start_price(self):
        for _ in range(10):
            with self._lock:
                if self.current_price:
                    self.start_price = self.current_price
                    break
            time.sleep(1)
        plog(f"[CL] 开盘价: ${self.start_price:.2f}" if self.start_price else "[CL] 开盘价获取失败")

    def reset_start_price(self):
        with self._lock:
            self.start_price = None

    def get_prices(self):
        with self._lock:
            return self.current_price, self.start_price

    def stop(self):
        self._running = False
        try:
            if self._ws: self._ws.close()
        except: pass

price_feed = ChainlinkPriceFeed()

def detect_expert_signal(diff_val, btc_diff, up_p, dn_p, p, sdc, suc):
    entry_min = p["ENTRY_MIN"]
    entry_max = p["ENTRY_MAX"]
    diff_min  = p["PRICE_DIFF_MIN"]
    diff_str  = f"+${diff_val:.1f}" if diff_val >= 0 else f"-${abs(diff_val):.1f}"
    if diff_val > 0 and btc_diff >= diff_min and dn_p > up_p and entry_min < dn_p < entry_max:
        suc = 0; sdc += 1
        plog(f"\033[0;32m  [庄家↓] DOWN信号 连续{sdc*200}ms | DOWN:{dn_p:.3f} BTC:{diff_str}\033[0m")
        return sdc, suc, "DOWN"
    elif diff_val < 0 and btc_diff >= diff_min and up_p > dn_p and entry_min < up_p < entry_max:
        sdc = 0; suc += 1
        plog(f"\033[0;36m  [庄家↑] UP信号 连续{suc*200}ms | UP:{up_p:.3f} BTC:{diff_str}\033[0m")
        return sdc, suc, "UP"
    else:
        if sdc > 0 or suc > 0:
            plog("  [庄家] 信号中断 -> 重置")
        return 0, 0, None

def get_client():
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds
    creds = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASSPHRASE)
    return ClobClient(host=CLOB_API, chain_id=137, key=PRIVATE_KEY, creds=creds,
                      signature_type=2, funder=WALLET_ADDRESS)

def cancel_all_open_orders(reason=""):
    try:
        get_client().cancel_all()
        plog(f"🗑️  已取消所有挂单{'（' + reason + '）' if reason else ''}")
    except Exception as e:
        plog(f"⚠️  撤单失败: {e}")

def get_mid(token_id):
    try:
        r = requests.get(CLOB_API + "/midpoint", params={"token_id": token_id}, timeout=1.2)
        if r.status_code == 200:
            mid = float(r.json().get("mid", 0))
            if 0 < mid < 1: return mid
        return None
    except: return None

def calc_order_size(cfg):
    def _fix(s):
        import math
        return max(math.ceil(s), 1)  # 向上取整到整数，避免精度问题
    if cfg["BUY_MODE"] == "usd":
        up_mid, dn_mid = price_cache.get()
        ref_mid = max(up_mid or 0, dn_mid or 0)
        if ref_mid > 0:
            return _fix(cfg["BUY_USD"] / ref_mid)
        return _fix(cfg["BUY_SHARES"])
    return _fix(cfg["BUY_SHARES"])

def taker_buy_once(token_id, shares):
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY
    # 强制股数为0.1的倍数，确保shares*0.99不超4位小数
    import math
    shares = max(math.ceil(shares), 1)  # 向上取整到整数
    shares = float(shares)
    cfg    = get_cfg()
    otype  = OrderType.GTC
    client = get_client()
    order  = OrderArgs(token_id=token_id, price=0.99, size=shares, side=BUY)
    signed = client.create_order(order)
    resp   = client.post_order(signed, otype)
    return resp

def sell_order_by_size(token_id, size, price, label="", max_retry=5):
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import SELL
    for attempt in range(max_retry):
        try:
            cur_price  = get_mid(token_id) or price
            sell_price = 0.01  # FAK卖单实际以买一价成交，0.01确保一定能匹配
            client     = get_client()
            order      = OrderArgs(token_id=token_id, price=sell_price, size=size, side=SELL)
            signed     = client.create_order(order)
            resp       = client.post_order(signed, OrderType.GTC)
            order_id   = resp.get("orderID", "N/A") if isinstance(resp, dict) else "N/A"
            plog(f"[卖出] {label}: {size}股 @ {sell_price:.3f} = ${round(size*sell_price,2):.2f} | ID:{order_id}")
            return resp
        except Exception as e:
            plog(f"[ERROR] 卖出失败(第{attempt+1}/{max_retry}次): {e}")
            if attempt < max_retry - 1:
                time.sleep(0.3)
    plog(f"[ERROR] 卖出彻底失败: {label}，已重试{max_retry}次")
    return None

def get_active_market(market_type):
    mc     = MARKET_CONFIGS[market_type]
    period = mc["period"]
    prefix = mc["slug_prefix"]
    now    = int(time.time())
    base   = (now // period) * period
    for ts in [base, base + period, base - period]:
        slug = f"{prefix}-{ts}"
        try:
            r = requests.get(GAMMA_API + "/markets", params={"slug": slug}, timeout=5)
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
                if o in ("up", "yes", "higher"): up_token = t.get("token_id")
                elif o in ("down", "no", "lower"): down_token = t.get("token_id")
            if up_token and down_token:
                return {
                    "slug":        slug,
                    "market_id":   condition_id,
                    "up_token":    up_token,
                    "down_token":  down_token,
                    "start_ts":    ts,
                    "end_ts":      ts + period,
                    "market_type": market_type,
                    "label":       mc["label"],
                }
        except:
            continue
    return None

def fmt_btc_diff(btc_now, btc_start):
    if btc_now is None or btc_start is None: return "N/A"
    return f"{(btc_now - btc_start):+.1f}"

def get_final_prices(up_token, dn_token):
    return get_mid(up_token), get_mid(dn_token)

def judge_win(entry_dir, up_mid, dn_mid):
    if up_mid is None or dn_mid is None: return None
    if entry_dir == "UP": return up_mid > dn_mid
    if entry_dir == "DN": return dn_mid > up_mid
    return None

def run_one_cycle(market, stats):
    up_token     = market["up_token"]
    dn_token     = market["down_token"]
    end_ts       = market["end_ts"]
    condition_id = market["market_id"]
    label        = market["label"]

    cfg             = get_cfg()
    # entry_last_sec moved to loop
    cancel_last_sec = cfg["CANCEL_LAST_SEC"]
    buy_mode        = cfg["BUY_MODE"]
    buy_usd         = cfg["BUY_USD"]
    buy_shares      = cfg["BUY_SHARES"]
    min_buy_price   = cfg["MIN_BUY_PRICE"]
    max_buy_price   = cfg["MAX_BUY_PRICE"]
    mode_desc       = f"${buy_usd:.2f} USDC" if buy_mode == "usd" else f"{buy_shares:.2f} 股"

    plog()
    plog("=" * 55)
    plog(f"🚀 [{label}] {market['slug']}")
    plog(f"   结束: {datetime.fromtimestamp(end_ts).strftime('%Y-%m-%d %H:%M:%S')}")
    plog(f"策略: {buy_mode.upper()} {mode_desc} | 最后{cfg['ENTRY_LAST_SEC']*1000:.0f}ms触发")
    plog(f"行情: 全程{cfg['FAST_PRINT_MS']:.0f}ms刷新 | 撤单{cancel_last_sec*1000:.0f}ms")
    plog(f"过滤: mid<{min_buy_price} 或 mid>{max_buy_price} 不买")
    tp_desc = f"盈利>{cfg['TP_PCT']:.0f}%止盈" if cfg['TP_PCT'] > 0 else "止盈:关"
    sl_desc = f"亏损>{cfg['SL_PCT']:.0f}%止损" if cfg['SL_PCT'] > 0 else "止损:关"
    plog(f"{tp_desc} | {sl_desc}")
    plog("=" * 55)

    price_cache.start(up_token, dn_token)
    plog("⚡ PriceCache WS 已启动")
    price_feed.record_start_price()

    bought              = False
    is_manual_bought    = False
    last_print_slot     = None
    sdc, suc            = 0, 0
    entry_dir           = None
    entry_size          = 0.0
    entry_cost          = 0.0
    total_spent         = 0.0
    final_up_mid        = None
    final_dn_mid        = None
    sampled_final       = False
    cancelled_already   = False
    tp_sl_triggered     = False
    sl_triggered        = False
    sl_hedge_done       = False
    tp_sell_attempted   = False
    hedge_count         = 0
    last_hedge_dir      = None
    last_hedge_size     = 0.0
    hedge_cooldown      = 0.0
    hedge_confirm_start = None
    up_size             = 0.0
    dn_size             = 0.0
    up_cost             = 0.0
    dn_cost             = 0.0
    reversal_side       = None
    reversal_confirmed  = False
    reversal_prev_dom   = None
    # 🛑日志去重标记
    mid_fail_logged     = False
    equal_logged        = False
    btc_gap_logged      = False
    max_price_logged    = False
    min_price_logged    = False

    while True:
        now       = time.time()
        remaining = end_ts - now
        if remaining <= 0:
            break

        cfg              = get_cfg()
        entry_last_sec   = cfg["ENTRY_LAST_SEC"]
        cancel_last_sec  = cfg["CANCEL_LAST_SEC"]
        fast_print_ms    = cfg["FAST_PRINT_MS"]
        min_buy_price    = cfg["MIN_BUY_PRICE"]
        max_buy_price    = cfg["MAX_BUY_PRICE"]
        hedge_trigger    = cfg["HEDGE_TRIGGER"]
        hedge_multi      = cfg["HEDGE_MULTI"]
        hedge_max        = cfg["HEDGE_MAX"]
        hedge_cd_sec     = cfg["HEDGE_CD_SEC"]
        hedge_confirm_ms = cfg["HEDGE_CONFIRM_MS"]
        tp_pct           = cfg["TP_PCT"]
        sl_pct           = cfg["SL_PCT"]

        if remaining <= SAMPLE_FINAL_SEC and not sampled_final:
            sampled_final = True
            final_up_mid, final_dn_mid = get_final_prices(up_token, dn_token)
            if final_up_mid and final_dn_mid:
                plog(f"[最终价格] UP:{final_up_mid:.3f} DN:{final_dn_mid:.3f} ✅")
            else:
                plog("[最终价格] 获取失败")

        if remaining <= cancel_last_sec:
            if not cancelled_already:
                cancel_all_open_orders(f"最后{cancel_last_sec*1000:.0f}ms撤单")
                cancelled_already = True
            time.sleep(LOOP_INTERVAL)
            continue

        # ── 手动指令 ──
        with manual_lock:
            if manual_order_queue:
                order_req = manual_order_queue.pop(0)
                action    = order_req.get("action")
                if action == "BUY":
                    m_dir   = order_req["direction"]
                    m_size  = order_req["size"]
                    m_token = up_token if m_dir == "UP" else dn_token
                    try:
                        m_size = max(round(int(m_size * 10) / 10, 1), 0.1)  # 精度截断
                        resp   = taker_buy_once(m_token, m_size)
                        oid    = resp.get("orderID", "N/A") if isinstance(resp, dict) else "N/A"
                        plog(f"[OK] 手动买{m_dir} {m_size}股 @0.99 | ID:{oid}")
                        _m_cost = round(m_size * 0.99, 2)
                        if m_dir == "UP":
                            up_size = round(up_size + m_size, 2)
                            up_cost = round(up_cost + _m_cost, 2)
                        else:
                            dn_size = round(dn_size + m_size, 2)
                            dn_cost = round(dn_cost + _m_cost, 2)
                        if not bought:
                            bought           = True
                            is_manual_bought = True
                            entry_dir        = m_dir
                            entry_size       = m_size
                            entry_cost       = _m_cost
                            total_spent      = _m_cost
                            last_hedge_size  = m_size
                            last_hedge_dir   = m_dir
                            if not cfg.get("MANUAL_HEDGE", False):
                                hedge_count = hedge_max
                                plog(f"[手动] 对冲已禁用(MANUAL_HEDGE=False) | 买入成本≈${entry_cost:.2f}")
                            else:
                                plog(f"[手动] 对冲已启用(MANUAL_HEDGE=True) | 买入成本≈${entry_cost:.2f}")
                        elif m_dir == entry_dir:
                            entry_size  = round(entry_size + m_size, 2)
                            entry_cost  = round(entry_cost + _m_cost, 2)
                            total_spent = round(total_spent + _m_cost, 2)
                    except Exception as e:
                        plog(f"[ERROR] 手动买入失败: {e}")
                elif action == "SELL":
                    m_dir   = order_req["direction"]
                    m_token = up_token if m_dir == "UP" else dn_token
                    up_mid_c, dn_mid_c = price_cache.get()
                    m_price = up_mid_c if m_dir == "UP" else dn_mid_c
                    if entry_dir == m_dir and entry_size > 0 and m_price:
                        resp = sell_order_by_size(m_token, entry_size, m_price, f"卖出{m_dir}")
                        if resp is not None:
                            entry_size = 0.0; total_spent = 0.0; bought = False; entry_dir = None
                            tp_sl_triggered = True
                        else:
                            plog(f"[警告] 手动卖出失败，持仓状态保留，对冲继续")
                    else:
                        plog(f"[警告] 无{m_dir}持仓或价格失败")
                elif action == "SELL_ALL":
                    if entry_size > 0 and entry_dir:
                        m_token = up_token if entry_dir == "UP" else dn_token
                        up_mid_c, dn_mid_c = price_cache.get()
                        m_price = up_mid_c if entry_dir == "UP" else dn_mid_c
                        if m_price:
                            resp = sell_order_by_size(m_token, entry_size, m_price, f"全部卖{entry_dir}")
                            if resp is not None:
                                entry_size = 0.0; total_spent = 0.0; bought = False; entry_dir = None
                                tp_sl_triggered = True
                            else:
                                plog("[警告] 手动全部卖出失败，持仓状态保留，对冲继续")
                        else:
                            plog("[警告] 获取价格失败，卖出取消")
                    else:
                        plog("[警告] 无持仓记录")

        # ── 行情刷新 ──
        ticks    = 1000.0 / fast_print_ms if fast_print_ms > 0 else 1.0
        rem_slot = int(remaining * ticks)
        if rem_slot != last_print_slot and not _in_param_panel.is_set():
            last_print_slot = rem_slot
            up_mid_c, dn_mid_c = price_cache.get()
            btc_now, btc_start = price_feed.get_prices()
            diff_val     = (btc_now - btc_start) if (btc_now and btc_start) else 0.0
            btc_diff     = abs(diff_val)
            btc_diff_str = fmt_btc_diff(btc_now, btc_start)
            ws_tag       = "WS" if price_cache._ws_ok else "HTTP"
            if remaining >= 3600:
                h = int(remaining) // 3600
                m = (int(remaining) % 3600) // 60
                s = int(remaining) % 60
                time_tag = f"{h:02d}:{m:02d}:{s:02d}"
            elif remaining >= 60:
                m = int(remaining) // 60
                s = int(remaining) % 60
                time_tag = f"{m:02d}:{s:02d}"
            else:
                time_tag = f"{remaining:>6.2f}s"

            pnl_str = ""
            if bought and total_spent > 0:
                up_mid_c, dn_mid_c = price_cache.get()
                _up_val  = round(up_size * (up_mid_c or 0), 2)
                _dn_val  = round(dn_size * (dn_mid_c or 0), 2)
                cur_val  = round(_up_val + _dn_val, 2)
                pnl      = round(cur_val - total_spent, 2)
                pnl_pct  = round(pnl / total_spent * 100, 1)
                sign     = "+" if pnl >= 0 else ""
                pos_str  = ""
                if up_size > 0: pos_str += f"UP{up_size:.0f}"
                if dn_size > 0: pos_str += f"+DN{dn_size:.0f}" if up_size > 0 else f"DN{dn_size:.0f}"
                pnl_str = f" | {pos_str} PnL:{sign}${pnl:.2f}({sign}{pnl_pct}%)"

            if up_mid_c is not None and dn_mid_c is not None:
                plog(f"[{ws_tag}][{label}] 剩余{time_tag} | UP:{up_mid_c:.3f} DN:{dn_mid_c:.3f} | BTC:{btc_diff_str}{pnl_str}")
                if remaining > entry_last_sec:
                    sdc, suc, _ = detect_expert_signal(diff_val, btc_diff, up_mid_c, dn_mid_c, PARAMS_A, sdc, suc)
            else:
                plog(f"[{ws_tag}][{label}] 剩余{time_tag} | UP:---  DN:---  | BTC:{btc_diff_str}{pnl_str}")

        # ── 止盈止损检查 ──
        if bought and not tp_sl_triggered and entry_cost > 0 and entry_dir:
            up_mid_c, dn_mid_c = price_cache.get()
            cur_mid = (up_mid_c if entry_dir == "UP" else dn_mid_c) if (up_mid_c and dn_mid_c) else None
            if cur_mid:
                cur_val = entry_size * cur_mid
                pnl_pct = (cur_val - entry_cost) / entry_cost * 100
                if tp_pct > 0 and pnl_pct >= tp_pct and not tp_sell_attempted:
                    token_id = up_token if entry_dir == "UP" else dn_token
                    plog(f"🎯 止盈触发! 盈利{pnl_pct:.1f}%>={tp_pct:.0f}% | 卖出{entry_dir} {entry_size}股")
                    tp_sell_attempted = True
                    resp = sell_order_by_size(token_id, entry_size, cur_mid, f"止盈{entry_dir}")
                    if resp is not None:
                        tp_sl_triggered = True
                        bought          = False
                        entry_size      = 0.0
                    else:
                        plog(f"[警告] 止盈卖出失败，对冲继续运行")
                elif sl_pct > 0 and pnl_pct <= -sl_pct and not sl_triggered:
                    sl_triggered   = True
                    sl_hedge_enabled = cfg.get("SL_HEDGE_ENABLED", True)
                    if not sl_hedge_enabled:
                        # 不对冲，直接卖出亏损方
                        token_id = up_token if entry_dir == "UP" else dn_token
                        plog(f"🛡️ 止损触发! 亏损{abs(pnl_pct):.1f}%>={sl_pct:.0f}% | SL_HEDGE_ENABLED=False，直接卖出{entry_dir}")
                        sell_resp = sell_order_by_size(token_id, entry_size, cur_mid, f"止损{entry_dir}")
                        if sell_resp is not None:
                            tp_sl_triggered = True
                            bought          = False
                            entry_cost      = 0.0
                            entry_size      = 0.0
                            total_spent     = 0.0
                            plog(f"[止损] 卖出成功")
                        else:
                            plog(f"[警告] 止损卖出失败")
                            sl_triggered = False  # 允许下次重试
                    else:
                        sl_hedge_dir   = "UP" if entry_dir == "DN" else "DN"
                        sl_hedge_token = up_token if sl_hedge_dir == "UP" else dn_token
                        sl_hedge_mid   = up_mid_c if sl_hedge_dir == "UP" else dn_mid_c
                        import math
                        sl_hedge_shares = max(math.ceil(last_hedge_size * hedge_multi), 1)
                        actual_sl_hedge_shares = max(math.ceil(last_hedge_size * hedge_multi * 1.1), 1)  # 多买10%
                        plog(f"🛡️ 止损触发! 亏损{abs(pnl_pct):.1f}%>={sl_pct:.0f}% | "
                             f"先对冲买入{sl_hedge_dir} {sl_hedge_shares}股")
                        try:
                            from py_clob_client.clob_types import OrderArgs, OrderType
                            from py_clob_client.order_builder.constants import BUY
                            _client    = get_client()
                            _order     = OrderArgs(token_id=sl_hedge_token, price=0.99,
                                                   size=actual_sl_hedge_shares, side=BUY)
                            _signed    = _client.create_order(_order)
                            _resp      = _client.post_order(_signed, OrderType.GTC)
                            _oid       = _resp.get("orderID", "") if isinstance(_resp, dict) else ""
                            _cost      = round(sl_hedge_shares * sl_hedge_mid, 4)
                            plog(f"[止损对冲✅] {sl_hedge_dir} {sl_hedge_shares}股 ≈${_cost:.2f} | ID:{_oid}")
                            hedge_count    += 1
                            total_spent     = round(total_spent + _cost, 4)
                            if sl_hedge_dir == "UP":
                                up_size = round(up_size + sl_hedge_shares, 2)
                                up_cost = round(up_cost + _cost, 4)
                            else:
                                dn_size = round(dn_size + sl_hedge_shares, 2)
                                dn_cost = round(dn_cost + _cost, 4)
                            last_hedge_dir  = sl_hedge_dir
                            last_hedge_size = sl_hedge_shares
                            sl_hedge_done   = True
                            token_id = up_token if entry_dir == "UP" else dn_token
                            plog(f"[止损] 对冲成功，卖出亏损方 {entry_dir} {entry_size}股")
                            sell_resp = sell_order_by_size(token_id, entry_size, cur_mid, f"止损{entry_dir}")
                            if sell_resp is not None:
                                total_spent = round(total_spent - entry_cost, 4)
                                entry_cost  = 0.0
                                entry_size  = 0.0
                                plog(f"[止损] 卖出成功，对冲继续运行")
                            else:
                                plog(f"[警告] 止损卖出失败，对冲继续运行")
                        except Exception as _e:
                            plog(f"[止损对冲❌] 买入失败: {_e}，下次重试")
                            sl_triggered = False

        # ── 首次买入 ──
        if (not bought) and (not tp_sl_triggered) and remaining <= entry_last_sec + 0.001:
            cfg           = get_cfg()
            entry_last_sec = cfg["ENTRY_LAST_SEC"]
            entry_mode    = cfg.get("ENTRY_MODE", "dominant")
            btc_gap_min   = cfg["BTC_GAP_MIN"]
            min_buy_price = cfg["MIN_BUY_PRICE"]
            max_buy_price = cfg["MAX_BUY_PRICE"]

            _mid_results = [None, None]
            def _f_up(): _mid_results[0] = get_mid(up_token)
            def _f_dn(): _mid_results[1] = get_mid(dn_token)
            _t1 = threading.Thread(target=_f_up, daemon=True)
            _t2 = threading.Thread(target=_f_dn, daemon=True)
            _t1.start(); _t2.start()
            _t1.join(timeout=1.5); _t2.join(timeout=1.5)
            up_mid_c, dn_mid_c = _mid_results[0], _mid_results[1]

            if up_mid_c is None or dn_mid_c is None:
                if not mid_fail_logged:
                    mid_fail_logged = True
                    plog("🛑 mid获取失败，等待...")
                time.sleep(RETRY_INTERVAL)
                continue

            # 当前占优方向
            if up_mid_c > dn_mid_c:
                cur_dom = "UP"
            elif dn_mid_c > up_mid_c:
                cur_dom = "DN"
            else:
                cur_dom = None

            if entry_mode == "dominant":
                # ── 原逻辑：占优买 ──
                if cur_dom is None:
                    if not equal_logged:
                        equal_logged = True
                        plog("🛑 UP==DN，等待...")
                    time.sleep(RETRY_INTERVAL)
                    continue
                side     = cur_dom
                side_mid = up_mid_c if side == "UP" else dn_mid_c
                token_id = up_token if side == "UP" else dn_token

            else:
                # ── reversal模式：检测反转才买 ──
                if cur_dom is None:
                    time.sleep(RETRY_INTERVAL)
                    continue
                if reversal_prev_dom is None:
                    # 记录初始占优方向，等待反转
                    reversal_prev_dom = cur_dom
                    plog(f"[反转模式] 初始占优={cur_dom}，等待反转...")
                    time.sleep(RETRY_INTERVAL)
                    continue
                if cur_dom == reversal_prev_dom:
                    # 未反转，继续等待
                    time.sleep(RETRY_INTERVAL)
                    continue
                # 反转发生！买新占优方向
                plog(f"[反转模式🔀] {reversal_prev_dom} → {cur_dom}，触发买入!")
                reversal_side      = cur_dom
                reversal_prev_dom  = cur_dom
                side     = cur_dom
                side_mid = up_mid_c if side == "UP" else dn_mid_c
                token_id = up_token if side == "UP" else dn_token

            if btc_gap_min > 0:
                btc_now_f, btc_start_f = price_feed.get_prices()
                btc_gap = abs(btc_now_f - btc_start_f) if (btc_now_f and btc_start_f) else 0
                if btc_gap <= btc_gap_min:
                    if not btc_gap_logged:
                        btc_gap_logged = True
                        plog(f"🛑 BTC价差${btc_gap:.1f}<={btc_gap_min:.0f}，等待...")
                    time.sleep(RETRY_INTERVAL)
                    continue

            if side_mid > max_buy_price:
                if not max_price_logged:
                    max_price_logged = True
                    plog(f"🛑 mid={side_mid:.3f}>{max_buy_price:.2f}，不买")
                time.sleep(RETRY_INTERVAL)
                continue

            if side_mid < min_buy_price:
                if not min_price_logged:
                    min_price_logged = True
                    plog(f"🛑 mid={side_mid:.3f}<{min_buy_price:.2f}，不买")
                time.sleep(RETRY_INTERVAL)
                continue

            shares   = calc_order_size(cfg)
            shares   = max(round(int(shares * 10) / 10, 1), 0.1)  # 精度保证
            actual_buy_shares = max(round(int(shares * 1.1 * 10) / 10, 1), 0.1)  # 多买10%
            cost_est = round(shares * side_mid, 4)
            plog(f"🎯 {remaining*1000:.0f}ms | {side} UP:{up_mid_c:.3f} DN:{dn_mid_c:.3f} "
                 f"@0.99市价 {shares:.2f}股 ≈${cost_est:.2f}")
            try:
                resp     = taker_buy_once(token_id, actual_buy_shares)
                order_id = resp.get("orderID", "") if isinstance(resp, dict) else ""
                plog(f"✅ 下单成功：{side} {shares:.2f}股 @0.99 FAK | orderID={order_id}")
                bought           = True
                is_manual_bought = False
                entry_dir        = side
                entry_size       = shares
                entry_cost       = cost_est
                total_spent      = cost_est
                last_hedge_size  = shares
                last_hedge_dir   = side
                if side == "UP":
                    up_size = round(up_size + shares, 2)
                    up_cost = round(up_cost + cost_est, 2)
                else:
                    dn_size = round(dn_size + shares, 2)
                    dn_cost = round(dn_cost + cost_est, 2)
                stats["total_bought"] += 1
                save_stats(stats)
            except Exception as e:
                plog(f"⚠️  下单失败: {e}")
                time.sleep(RETRY_INTERVAL)
                continue

        # ── 对冲监控 ──
        if bought and (not is_manual_bought or cfg.get('MANUAL_HEDGE', False)) and hedge_count < hedge_max and now >= hedge_cooldown:
            up_mid_c, dn_mid_c = price_cache.get()
            if up_mid_c is not None and dn_mid_c is not None:
                reverse_dir   = "UP" if last_hedge_dir == "DN" else "DN"
                reverse_mid   = up_mid_c if reverse_dir == "UP" else dn_mid_c
                reverse_token = up_token if reverse_dir == "UP" else dn_token
                if reverse_mid >= hedge_trigger:
                    if hedge_confirm_start is None:
                        hedge_confirm_start = now
                        plog(f"⏳ 对冲确认中... 反向{reverse_dir} mid={reverse_mid:.3f} 需持续{hedge_confirm_ms:.0f}ms")
                    elif (now - hedge_confirm_start) * 1000 >= hedge_confirm_ms:
                        import math
                        raw_hs = last_hedge_size * hedge_multi
                        hedge_shares = max(math.ceil(raw_hs), 1)  # 向上取整到整数
                        actual_hedge_shares = max(math.ceil(raw_hs * 1.1), 1)  # 多买10%
                        plog(f"🔄 对冲触发! 反向{reverse_dir} | 第{hedge_count+1}次 "
                             f"| {last_hedge_size:.2f}×{hedge_multi:.0f}={hedge_shares:.2f}股")
                        try:
                            from py_clob_client.clob_types import OrderArgs, OrderType
                            from py_clob_client.order_builder.constants import BUY
                            client  = get_client()
                            order   = OrderArgs(token_id=reverse_token, price=0.99, size=actual_hedge_shares, side=BUY)
                            signed  = client.create_order(order)
                            resp    = client.post_order(signed, OrderType.GTC)
                            oid     = resp.get("orderID", "") if isinstance(resp, dict) else ""
                            cost    = round(hedge_shares * reverse_mid, 4)
                            plog(f"[对冲✅] {reverse_dir} {hedge_shares:.2f}股 ≈${cost:.2f} | ID:{oid}")
                            hedge_count        += 1
                            total_spent         = round(total_spent + cost, 4)
                            if reverse_dir == "UP":
                                up_size = round(up_size + hedge_shares, 2)
                                up_cost = round(up_cost + cost, 4)
                            else:
                                dn_size = round(dn_size + hedge_shares, 2)
                                dn_cost = round(dn_cost + cost, 4)
                            last_hedge_dir      = reverse_dir
                            last_hedge_size     = hedge_shares
                            hedge_cooldown      = now + hedge_cd_sec
                            hedge_confirm_start = None
                        except Exception as e:
                            plog(f"[对冲❌] 下单失败: {e}")
                            hedge_cooldown      = now + hedge_cd_sec
                            hedge_confirm_start = None
                else:
                    if hedge_confirm_start is not None:
                        plog(f"  对冲确认取消，mid={reverse_mid:.3f} 跌回阈值以下")
                    hedge_confirm_start = None

        time.sleep(LOOP_INTERVAL)

    price_cache.stop()
    price_feed.reset_start_price()

    if not sampled_final or final_up_mid is None or final_dn_mid is None:
        final_up_mid, final_dn_mid = get_final_prices(up_token, dn_token)

    if bought or tp_sl_triggered:
        if not tp_sl_triggered:
            threading.Thread(target=redeem_thread, args=(condition_id,), daemon=True).start()
        manual_tag = "手动" if is_manual_bought else "自动"
        plog(f"\n🧾 [{label}] 本局买入({manual_tag}) | 初始:{entry_dir} | "
             f"成本≈${entry_cost:.2f} | 对冲:{hedge_count}次 | 累计花费≈${total_spent:.2f}")
        if tp_sl_triggered:
            plog("[结算] 已触发止盈/止损提前卖出")
        else:
            final_dir = last_hedge_dir
            won = judge_win(final_dir, final_up_mid, final_dn_mid)
            if won is True:
                stats["wins"] += 1
                plog(f"[结算] ✅ 胜利！UP:{final_up_mid:.3f} DN:{final_dn_mid:.3f} | 最终方向:{final_dir}")
            elif won is False:
                stats["losses"] += 1
                plog(f"[结算] ❌ 失败。UP:{final_up_mid:.3f} DN:{final_dn_mid:.3f} | 最终方向:{final_dir}")
            else:
                plog("[结算] ⚠️  无法判断胜负")
        cur_bal = get_usdc_balance()
        stats["history"].append({
            "time":        datetime.now().strftime("%Y-%m-%d %H:%M"),
            "market_type": market["market_type"],
            "label":       label,
            "dir":         entry_dir,
            "final_dir":   last_hedge_dir,
            "cost":        entry_cost,
            "spent":       total_spent,
            "hedge_count": hedge_count,
            "final_up":    final_up_mid,
            "final_dn":    final_dn_mid,
            "won":         judge_win(last_hedge_dir, final_up_mid, final_dn_mid),
            "manual":      is_manual_bought,
            "tp_sl":       tp_sl_triggered,
        })
        stats["history"] = stats["history"][-200:]
        save_stats(stats)
        print_stats(stats, cur_bal)
        return "bought"
    else:
        plog(f"\n⚠️  [{label}] 本局未买入")
        return "no_trade"

def main():
    threading.Thread(target=runtime_cfg_watcher, daemon=True).start()
    threading.Thread(target=keyboard_listener, daemon=True).start()
    plog("=" * 55)
    plog(f"📁 副本ID={INSTANCE_ID}  ENV={ENV_FILE}")
    plog(f"📁 日志={STATS_FILE}")
    plog(f"📁 配置={RUNTIME_CFG_PATH}")
    plog("=" * 55)
    plog(" BTC 多周期 最后N秒占优吃单策略")
    plog(" 市场: 5m/15m/1h/4h/1d  按P切换")
    plog(" [P]=参数 [U]=买UP [D]=选方向 [S]=卖出 [A]=全卖 [Q]=退出")
    plog("=" * 55)
    load_runtime_cfg(verbose=True)
    price_feed.start()
    for _ in range(20):
        if price_feed.current_price is not None: break
        time.sleep(0.5)
    plog(f"✅ BTC: ${price_feed.current_price:.1f}" if price_feed.current_price else "⚠️ BTC价格等待超时")
    stats    = load_stats()
    init_bal = get_usdc_balance()
    if init_bal is not None:
        stats["initial_balance"] = init_bal
        save_stats(stats)
        plog(f"📌 初始余额: ${init_bal:.2f}")
    print_stats(stats, init_bal)
    last_market_id = None
    while True:
        try:
            cfg         = get_cfg()
            market_type = cfg["MARKET_TYPE"]
            market      = get_active_market(market_type)
            if market is None:
                mc = MARKET_CONFIGS.get(market_type, {})
                plog(f"⏳ 寻找 {mc.get('label', market_type)} 市场...")
                time.sleep(5)
                continue
            if market["market_id"] == last_market_id:
                time.sleep(1)
                continue
            last_market_id = market["market_id"]
            status = run_one_cycle(market, stats)
            plog(f"🏁 本局: {status}")
            time.sleep(1)
        except KeyboardInterrupt:
            plog("\n⛔ 已停止")
            price_feed.stop()
            print_stats(stats, get_usdc_balance())
            break
        except Exception as e:
            plog(f"❌ 异常: {e}")
            time.sleep(2)

if __name__ == "__main__":
    main()
