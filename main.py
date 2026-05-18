import ccxt
import requests
import time
import os
import logging
from datetime import datetime
import threading
from flask import Flask

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
app = Flask(__name__)

stats = {
    "start_time": datetime.now(),
    "iterations": 0,
    "errors": 0,
    "signals_sent": 0,
    "last_iteration_time": None
}

@app.route('/')
def home():
    uptime = str(datetime.now() - stats["start_time"]).split('.')[0]
    return (f"✅ OK Uptime: {uptime} "
            f"Итераций: {stats['iterations']} "
            f"Ошибок: {stats['errors']} "
            f"Сигналов: {stats['signals_sent']} "
            f"Последняя: {stats['last_iteration_time']}")

@app.route('/health')
def health():
    return "OK", 200

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
THRESHOLD = 7.0 

# Инициализация Binance Futures
exchange = ccxt.binance({
    'enableRateLimit': True, 
    'timeout': 20000, 
    'options': {'defaultType': 'future'} 
})

active_symbols_global = []
sent_signals = {}
cooldowns = {}
last_market_update = 0

def send_msg(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        logging.error(f"Telegram error: {e}")

def send_alert(symbol, tf, percent, price, o, h, l, vol_curr_usdt, vol_rel, label, ts, avg_vol_usdt):
    key = f"{symbol}_{ts}_{tf}"
    if key not in sent_signals:
        # Очищаем тикер для ссылок (например, 'BTC/USDT:USDT' -> 'BTCUSDT')
        clean_symbol = symbol.split(':')[0].replace('/', '')
        
        # Ссылки на графики
        tv = f"https://www.tradingview.com/chart/?symbol=BINANCE:{clean_symbol}.P"
        cg = f"https://www.coinglass.com/tv/Binance_{clean_symbol}"
        
        vol_emoji = "💎 СИЛЬНЫЙ" if vol_rel >= 3.0 else "⚠️ СЛАБЫЙ"
        
        range_hl = h - l if (h - l) > 0 else 0.00000001
        bull_power = ((price - l) / range_hl) * 100
        bear_power = 100 - bull_power
        
        if bull_power > 70:
            bias_text = f"🟩 Быки {bull_power:.0f}% (DOMINANCE)"
        elif bear_power > 70:
            bias_text = f"🟥 Медведи {bear_power:.0f}% (DOMINANCE)"
        else:
            bias_text = f"⚖️ Покупатели {bull_power:.0f}% / Продавцы {bear_power:.0f}%"
            
        peak = h if "ПАМП 🔥" in label else l

        msg = (f"<b>{label} {percent:+.2f}% ({tf})</b>\n"
               f"Монета: <b>{symbol}</b>\n"
               f"Цена: <code>{price}</code> | Пик: <code>{peak}</code>\n"
               f"───────────────────\n"
               f"📊 <b>Объём (USDT):</b> ${vol_curr_usdt:,.0f}\n"
               f"📈 <b>Средний (20 пер):</b> ${avg_vol_usdt:,.0f}\n"
               f"🚀 <b>Превышение:</b> x{vol_rel:.1f} {vol_emoji}\n"
               f"🎯 <b>Дисбаланс:</b> {bias_text}\n"
               f"───────────────────\n"
               f"🔗 <a href='{tv}'>TradingView</a> | 📈 <a href='{cg}'>Coinglass</a>")
        
        send_msg(msg)
        sent_signals[key] = time.time()
        stats["signals_sent"] += 1
        return True
    return False

def process_heavy_logic(symbol):
    try:
        # Запрашиваем 85 свечей по 1H (хватит для склейки и расчета 20 периодов 4H)
        ohlcv = exchange.fetch_ohlcv(symbol, '1h', limit=85)
        total_candles = len(ohlcv)
        if not ohlcv or total_candles < 84: return
        
        price_now = ohlcv[-1][4]
        current_ts = ohlcv[-1][0]
        vols = [x[5] for x in ohlcv] # Массив объемов
        
        # ================= 1H LOGIC =================
        c1 = ohlcv[-1]
        o1, h1, l1, v1 = c1[1], c1[2], c1[3], vols[-1]
        
        avg_vol_1h_20 = sum(vols[-21:-1]) / 20
        v_rel_1h = v1 / avg_vol_1h_20 if avg_vol_1h_20 > 0 else 1.0
        
        s_up_1h = ((h1 - o1) / o1) * 100
        s_down_1h = ((o1 - l1) / o1) * 100
        
        if s_up_1h >= THRESHOLD:
            send_alert(symbol, "1H", s_up_1h, price_now, o1, h1, l1, v1*price_now, v_rel_1h, "ПАМП 🔥", c1[0], avg_vol_1h_20*price_now)
        elif s_down_1h >= THRESHOLD:
            send_alert(symbol, "1H", -s_down_1h, price_now, o1, h1, l1, v1*price_now, v_rel_1h, "ДАМП ❄️", c1[0], avg_vol_1h_20*price_now)

        # ================= 2H LOGIC (С КУЛДАУНОМ) =================
        start_ts_2h = ohlcv[-2][0]
        cd_2h = cooldowns.get(f"{symbol}_2H", 0)
        
        if start_ts_2h > cd_2h:
            curr_2h_vol = sum(vols[-2:])
            avg_vol_2h_20 = sum(vols[-42:-2]) / 20
            v_rel_2h = curr_2h_vol / avg_vol_2h_20 if avg_vol_2h_20 > 0 else 1.0
            
            o2h = ohlcv[-2][1]
            h2h = max(ohlcv[-2][2], ohlcv[-1][2])
            l2h = min(ohlcv[-2][3], ohlcv[-1][3])
            s_up_2h = ((h2h - o2h) / o2h) * 100
            s_down_2h = ((o2h - l2h) / o2h) * 100
            
            if s_up_2h >= THRESHOLD:
                if send_alert(symbol, "2H", s_up_2h, price_now, o2h, h2h, l2h, curr_2h_vol*price_now, v_rel_2h, "ПАМП 🔥", start_ts_2h, avg_vol_2h_20*price_now):
                    cooldowns[f"{symbol}_2H"] = current_ts
            elif s_down_2h >= THRESHOLD:
                if send_alert(symbol, "2H", -s_down_2h, price_now, o2h, h2h, l2h, curr_2h_vol*price_now, v_rel_2h, "ДАМП ❄️", start_ts_2h, avg_vol_2h_20*price_now):
                    cooldowns[f"{symbol}_2H"] = current_ts

        # ================= 4H LOGIC (С КУЛДАУНОМ) =================
        start_ts_4h = ohlcv[-4][0]
        cd_4h = cooldowns.get(f"{symbol}_4H", 0)
        
        if start_ts_4h > cd_4h:
            curr_4h_vol = sum(vols[-4:])
            avg_vol_4h_20 = sum(vols[-84:-4]) / 20
            v_rel_4h = curr_4h_vol / avg_vol_4h_20 if avg_vol_4h_20 > 0 else 1.0
            
            o4h = ohlcv[-4][1]
            h4h = max(s[2] for s in ohlcv[-4:])
            l4h = min(s[3] for s in ohlcv[-4:])
            s_up_4h = ((h4h - o4h) / o4h) * 100
            s_down_4h = ((o4h - l4h) / o4h) * 100
            
            if s_up_4h >= THRESHOLD:
                if send_alert(symbol, "4H", s_up_4h, price_now, o4h, h4h, l4h, curr_4h_vol*price_now, v_rel_4h, "ПАМП 🔥", start_ts_4h, avg_vol_4h_20*price_now):
                    cooldowns[f"{symbol}_4H"] = current_ts
            elif s_down_4h >= THRESHOLD:
                if send_alert(symbol, "4H", -s_down_4h, price_now, o4h, h4h, l4h, curr_4h_vol*price_now, v_rel_4h, "ДАМП ❄️", start_ts_4h, avg_vol_4h_20*price_now):
                    cooldowns[f"{symbol}_4H"] = current_ts
            
    except Exception as e:
        logging.error(f"Error processing {symbol}: {e}")

def update_markets():
    global active_symbols_global, last_market_update
    try:
        exchange.load_markets(reload=True)
        # Динамический сбор всех активных фьючерсов к USDT
        active_symbols_global = [s for s, m in exchange.markets.items() if m.get('active') and m.get('type') == 'swap' and m.get('quote') == 'USDT']
        last_market_update = time.time()
        logging.info(f"Markets updated. Total active pairs: {len(active_symbols_global)}")
    except Exception as e:
        stats["errors"] += 1
        logging.error(f"Market update error: {e}")

def sniper_loop():
    update_markets() 
    while True:
        try:
            # Обновляем список монет каждые 10 минут
            if time.time() - last_market_update > 600:
                update_markets()

            for symbol in active_symbols_global:
                process_heavy_logic(symbol)
            
            stats["iterations"] += 1
            stats["last_iteration_time"] = datetime.now().strftime('%H:%M:%S')
            
            # Очистка памяти
            now = time.time()
            now_ms = now * 1000
            for k in list(sent_signals.keys()):
                if now - sent_signals[k] > 86400: del sent_signals[k]
            for k in list(cooldowns.keys()):
                if now_ms - cooldowns[k] > 86400 * 1000: del cooldowns[k]
            
            time.sleep(10)
        except Exception as e:
            stats["errors"] += 1
            logging.error(f"Loop Error: {e}")
            time.sleep(30)

threading.Thread(target=sniper_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
