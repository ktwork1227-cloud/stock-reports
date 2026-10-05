#!/usr/bin/env python3
"""
股票分析報告 v10（大進化版）
變更（vs v9）：
- 🆕 盈餘品質評分（earnings_quality 模組整合）
  · 5 維度評分（A.營運槓桿 / B.費用率趨勢 / C.R&D 強度 / D.業外佔比 / E.毛利率穩定）
  · 評級 A+/A/B/C/D（0~10 正規化）
  · 補 sell-side 不拆解的「常規 vs 一次性」盈餘結構
  · 受張明輝會計師 KOL 啟發（2026-07-23）
- 🆕 TG 自動推播（不再依賴總管）
  · 報告完成後直接用 Telegram Bot API 推送
  · 推播內容含盈餘品質評分 + 結論 + 持倉建議（如有）
  · 使用 stock_report_v9.py 的 telegram() 函式
- 🆕 報告命名：<stock>_v10_<timestamp>.html
- 🆕 HTML 模板：stock-reports-template/index_v10.html（已預備 2026-06-29）
- 保留 v9 全部邏輯：DB 為主、API 為輔、集保訊號、Buffett 8問、6 指標、結論判定
- 保留 v9 HTML 發佈：GitHub Pages

v9 → v10 邏輯差異：
- v9：盈餘品質未拆解、TG 由總管執行
- v10：盈餘品質自動評分 + TG 自動推播（端到端一條龍）

v10 SOP：見 ~/Bot/SOP-stock-analysis.md
  · 觸發：「分析/體檢/看一下/研究 + 代碼」
  · 流程：跑 v10 → 自動推 TG → 整合結論
  · 備份：v9 腳本保留於 ~/Bot/scripts/stock_report_v9.py
"""

import subprocess, json, base64, requests
import numpy as np
from datetime import datetime

# ===== 設定 =====
GITHUB_TOKEN = 'YOUR_GITHUB_TOKEN_HERE'
REPO_OWNER = 'ktwork1227-cloud'
REPO_NAME = 'stock-reports'
BRANCH = 'main'
TELEGRAM_BOT_TOKEN = 'YOUR_TELEGRAM_BOT_TOKEN_HERE'
TELEGRAM_CHAT_ID = 'YOUR_TELEGRAM_CHAT_ID_HERE'

# ===== v9 新增：DB / CSV 路徑 =====
DB_PATH = '/Users/kt/Python/stock_project/data/stock_data.db'
REALTIME_CSV = '/Users/kt/Python/stock_project/data/realtime_now.csv'
REALTIME_SCHEDULE = '09:30 / 10:00 / 10:30 / 11:00 / 12:00 / 15:30'  # 另一個 AI 管的抓取時間

# ===== v9 新增：資料源優先順序 =====
# 1️⃣ DB 唯讀 → 2️⃣ 即時 CSV → 3️⃣ FinMind API → 4️⃣ Yahoo Finance
DATA_SOURCE_PRIORITY = 'DB_FIRST'  # v9 新設計

# ===== v9 新增：DB 唯讀讀取（取代 FinMind 主要資料源） =====
def db_query(sql, params=()):
    """唯讀查詢 stock_data.db，回傳 list[dict]。DB 缺資料時回傳 []"""
    import sqlite3
    try:
        # mode=ro 唯讀，uri=True 必填
        conn = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        print(f'  [DB 讀取失敗] {e}')
        return []

def db_get_stock_basic(stock):
    """從 stock_daily 拿最後交易日的基本面（股價、量、PE、PB、外資比）"""
    code = stock.replace('.TW','').replace('.TWO','')
    rows = db_query('''
        SELECT date, name, price, change_percent, volume,
               pe_ratio, pb_ratio, foreign_holding_ratio,
               margin_financing_balance, margin_short_balance
        FROM stock_daily
        WHERE code = ?
        ORDER BY date DESC
        LIMIT 1
    ''', (code,))
    return rows[0] if rows else None

def db_get_kline_60d(stock):
    """從 stock_daily 拿近 60 個交易日的收價、量（K 線資料）"""
    code = stock.replace('.TW','').replace('.TWO','')
    rows = db_query('''
        SELECT date, price AS close, volume
        FROM stock_daily
        WHERE code = ?
        ORDER BY date DESC
        LIMIT 60
    ''', (code,))
    # 逆轉為時間正序
    return list(reversed(rows))

def db_get_roe_roa_de(stock):
    """v9.2 補：從 DB 算 ROE / ROA / 負債比（D/A）
       ROE = 季 EPS × 4（年化）/ BVPS
       ROA = 季淨利 × 4（年化）/ 總資產
       DE  = 總負債 / 總資產 × 100（負債比%，= 巴菲特 Q6 的指標）
    """
    code = stock.replace('.TW','').replace('.TWO','')
    # 最新一季 損益表（EPS 與淨利）
    eps_row = db_query('''
        SELECT eps, net_income, net_income_parent FROM stock_income_statement
        WHERE code = ? AND eps IS NOT NULL
        ORDER BY year DESC, season DESC LIMIT 1
    ''', (code,))
    # 最新一季 資負表（BVPS、總資產、總負債、總權益）
    bs_row = db_query('''
        SELECT book_value_per_share, total_assets, total_liabilities, total_equity
        FROM stock_balance_sheet
        WHERE code = ?
        ORDER BY year DESC, season DESC LIMIT 1
    ''', (code,))
    out = {'roe': None, 'roa': None, 'de': None, 'roic': None, 'da_ratio': None}
    if eps_row and bs_row:
        eps   = eps_row[0].get('eps')
        # 淨利優先用母公司（歸屬業主），fallback 合併
        ni    = eps_row[0].get('net_income_parent') or eps_row[0].get('net_income')
        bvps  = bs_row[0].get('book_value_per_share')
        ta    = bs_row[0].get('total_assets')
        tl    = bs_row[0].get('total_liabilities')
        te    = bs_row[0].get('total_equity')
        # 季 EPS / 淨利 年化（×4）
        if eps is not None and bvps and bvps > 0:
            out['roe'] = round(eps * 4 / bvps, 4)
        if ni is not None and ta and ta > 0:
            out['roa'] = round(ni * 4 / ta, 4)
        if tl is not None and ta and ta > 0:
            # v9.2 修：巴菲特 Q6 要的是負債比（D/A = 總負債/總資產）
            out['de']  = round(tl / ta * 100, 2)
            out['da_ratio'] = round(tl / ta, 4)
        # v9.2 補：ROIC = ROE × (1 - D/A)（杜邦拆解邏輯）
        if out['roe'] is not None and out['da_ratio'] is not None:
            out['roic'] = round(out['roe'] * (1 - out['da_ratio']), 4)
    return out

def db_get_price_hl_2y(stock):
    """從 stock_daily 算近 2 年高低點（各年 high/low）
       v9.2 修：用 max/min 盤中極值（不用 price 收盤，與 52 週一致）
    """
    code = stock.replace('.TW','').replace('.TWO','')
    rows = db_query('''
        SELECT date, max, min
        FROM stock_daily
        WHERE code = ?
          AND date >= date('now', '-2 years')
        ORDER BY date ASC
    ''', (code,))
    yd = {}
    for r in rows:
        yr = r['date'][:4]
        hi = r.get('max'); lo = r.get('min')
        if hi is None or lo is None: continue
        if yr not in yd: yd[yr] = {'high': hi, 'low': lo}
        else:
            yd[yr]['high'] = max(yd[yr]['high'], hi)
            yd[yr]['low']  = min(yd[yr]['low'], lo)
    return {yr: {'high': int(round(v['high'])), 'low': int(round(v['low']))} for yr, v in yd.items()}

def db_get_month_revenue(stock, start_year=2022):
    """從 stock_monthly_revenue 拿月營收"""
    code = stock.replace('.TW','').replace('.TWO','')
    rows = db_query('''
        SELECT date, revenue, revenue_year, revenue_month
        FROM stock_monthly_revenue
        WHERE code = ? AND revenue_year >= ?
        ORDER BY date ASC
    ''', (code, start_year))
    result = {}
    for r in rows:
        yr = str(r.get('revenue_year', 0))
        mn = str(r.get('revenue_month', 0))
        rv = r.get('revenue')
        if yr and mn and rv:
            if yr not in result: result[yr] = {}
            result[yr][mn] = float(rv)
    return result

def db_get_eps_4q(stock):
    """v9.1：從 stock_income_statement 取最近 4 季 EPS 加總"""
    code = stock.replace('.TW','').replace('.TWO','')
    rows = db_query('''
        SELECT year, season, eps
        FROM stock_income_statement
        WHERE code = ? AND eps IS NOT NULL
        ORDER BY year DESC, season DESC
        LIMIT 4
    ''', (code,))
    total = 0.0
    cnt = 0
    for r in rows:
        try:
            v = float(r.get('eps') or 0)
            if v:
                total += v
                cnt += 1
        except: pass
    return {'sum': round(total, 2), 'quarters': cnt} if cnt else None


def db_get_price_52w(stock):
    """v9.1：從 stock_daily 取近 52 週（252 個交易日）股價最高/最低
    v9.1.1：用 max/min 欄位（盤中極值）而非 price（收盤）"""
    code = stock.replace('.TW','').replace('.TWO','')
    rows = db_query('''
        SELECT date, max, min
        FROM stock_daily
        WHERE code = ?
          AND date >= date('now', '-370 days')
        ORDER BY date DESC
        LIMIT 252
    ''', (code,))
    highs = [r['max'] for r in rows if r.get('max') is not None]
    lows  = [r['min'] for r in rows if r.get('min') is not None]
    if not highs or not lows:
        return None
    return {
        'high': round(max(highs), 2),
        'low': round(min(lows), 2),
        'count': len(highs),
    }


def db_get_financials(stock, start_year=2021):
    """從 stock_income_statement 拿季損益表（回傳原始 rows）"""
    code = stock.replace('.TW','').replace('.TWO','')
    return db_query('''
        SELECT year, season, revenue, operating_costs, gross_profit,
               operating_income, net_income_parent, eps,
               gross_margin, operating_margin, net_margin
        FROM stock_income_statement
        WHERE code = ? AND year >= ?
        ORDER BY year DESC, season DESC
    ''', (code, start_year))

def db_get_balance_sheet(stock, start_year=2021):
    code = stock.replace('.TW','').replace('.TWO','')
    return db_query('''
        SELECT year, season
        FROM stock_balance_sheet
        WHERE code = ? AND year >= ?
        ORDER BY year DESC, season DESC
    ''', (code, start_year))

def db_get_cash_flow(stock, start_year=2021):
    """
    v9 修：拿 DB 完整現金流並轉成 FinMind 格式 (date, type) → value
    讓 calc_six() 可以直接使用，不需要修改六指標計算邏輯
    """
    code = stock.replace('.TW','').replace('.TWO','')
    rows = db_query('''
        SELECT year, season,
               operating_cash_flow, investing_cash_flow, financing_cash_flow,
               free_cash_flow, net_cash_flow
        FROM stock_cash_flow
        WHERE code = ? AND year >= ?
        ORDER BY year DESC, season DESC
    ''', (code, start_year))
    # 轉成 FinMind 格式
    season_to_month = {1: '03-31', 2: '06-30', 3: '09-30', 4: '12-31'}
    result = []
    for r in rows:
        yr = r.get('year'); se = r.get('season')
        if not yr or not se: continue
        date_str = f'{yr}-{season_to_month.get(se, "12-31")}'
        if r.get('operating_cash_flow') is not None:
            result.append({'date': date_str, 'type': 'NetCashInflowFromOperatingActivities',
                           'value': float(r['operating_cash_flow']) * 1000})  # 千元 → 元
        if r.get('free_cash_flow') is not None:
            result.append({'date': date_str, 'type': 'FreeCashFlow',
                           'value': float(r['free_cash_flow']) * 1000})  # 千元 → 元
        if r.get('investing_cash_flow') is not None:
            result.append({'date': date_str, 'type': 'NetCashOutflowFromInvestingActivities',
                           'value': float(r['investing_cash_flow']) * 1000})  # 千元 → 元
    return result

# ===== v9 新增：集保訊號（主力出貨給散戶） =====
def db_get_weekly_shareholding(stock, weeks=4):
    """從 stock_weekly_shareholding 拿近 N 週集保"""
    code = stock.replace('.TW','').replace('.TWO','')
    rows = db_query('''
        SELECT date, total_shareholders, total_shares,
               holders_under_10, shares_under_10,
               holders_over_1000, shares_over_1000
        FROM stock_weekly_shareholding
        WHERE code = ?
        ORDER BY date DESC
        LIMIT ?
    ''', (code, weeks))
    return list(reversed(rows))  # 時間正序

def analyze_mainboard_distribution(weekly_data):
    """
    v9 新增：主力出貨/集貨訊號
    輸入：近 4 週集保資料（時間正序）
    輸出：訊號 dict
    """
    if not weekly_data or len(weekly_data) < 2:
        return {'signal': 'N/A', 'reason': '集保資料不足'}
    
    first = weekly_data[0]   # 4 週前
    last = weekly_data[-1]   # 最近一週
    
    # 散戶（< 10 張）變化
    retail_shares_chg = (last['shares_under_10'] or 0) - (first['shares_under_10'] or 0)
    retail_holders_chg = (last['holders_under_10'] or 0) - (first['holders_under_10'] or 0)
    
    # 大戶（> 1000 張）變化
    big_shares_chg = (last['shares_over_1000'] or 0) - (first['shares_over_1000'] or 0)
    big_holders_chg = (last['holders_over_1000'] or 0) - (first['holders_over_1000'] or 0)
    
    # 訊號判定
    threshold = 100_000  # 10 萬股門檻（區分「明顯變化」與「變化不明顯」）
    retail_obvious = abs(retail_shares_chg) >= threshold
    big_obvious = abs(big_shares_chg) >= threshold
    
    if retail_obvious and big_obvious:
        # 兩者都明顯變化
        if retail_shares_chg > 0 and big_shares_chg < 0:
            signal = '🔴 主力出貨給散戶'
            reason = f'散戶增加 {retail_shares_chg:+,} 股 / 大戶減少 {big_shares_chg:+,} 股（量級約略一致）'
            risk = '高'
        elif retail_shares_chg < 0 and big_shares_chg > 0:
            signal = '🟢 主力集貨'
            reason = f'散戶出貨 {-retail_shares_chg:+,} 股 / 大戶集貨 {big_shares_chg:+,} 股（量級約略一致）'
            risk = '低'
        else:
            signal = '🟡 同向變化（跟著股價走）'
            reason = f'散戶 {retail_shares_chg:+,} 股、大戶 {big_shares_chg:+,} 股（兩者同方向）'
            risk = '中'
    elif retail_obvious and not big_obvious:
        # 散戶明顯變、大戶不動
        if retail_shares_chg > 0:
            signal = '🔴 主力出貨給散戶'
            reason = f'散戶增加 {retail_shares_chg:+,} 股 / 大戶不動（{big_shares_chg:+,} 股）'
            risk = '高'
        else:
            signal = '🟢 主力集貨'
            reason = f'散戶減少 {retail_shares_chg:+,} 股 / 大戶不動（{big_shares_chg:+,} 股）'
            risk = '中'
    elif not retail_obvious and big_obvious:
        if big_shares_chg > 0:
            signal = '🟢 主力集貨'
            reason = f'大戶增加 {big_shares_chg:+,} 股 / 散戶不動（{retail_shares_chg:+,} 股）'
            risk = '低'
        else:
            signal = '🔴 主力出貨'
            reason = f'大戶減少 {big_shares_chg:+,} 股 / 散戶不動（{retail_shares_chg:+,} 股）'
            risk = '高'
    else:
        # 兩者都變化不明顯
        signal = '⚪ 盤整（散戶大戶皆不動）'
        reason = f'散戶 {retail_shares_chg:+,} 股、大戶 {big_shares_chg:+,} 股（變化都不明顯）'
        risk = '中'
    
    return {
        'signal': signal,
        'reason': reason,
        'risk': risk,
        'retail_shares_chg': retail_shares_chg,
        'retail_holders_chg': retail_holders_chg,
        'big_shares_chg': big_shares_chg,
        'big_holders_chg': big_holders_chg,
        'weeks': len(weekly_data),
        'first_date': first['date'],
        'last_date': last['date'],
    }

# ===== v9 新增：即時價量（讀 CSV） =====
def read_realtime_csv(stock_code=None):
    """
    v9 新增：讀另一個 AI 抓的即時報價 CSV
    不指定 stock_code：回傳 dict[code] = row
    指定 stock_code：回傳該檔的 row 或 None
    """
    import csv
    from pathlib import Path
    import os
    p = Path(REALTIME_CSV)
    if not p.exists():
        return None if stock_code else {}
    
    # 取得檔案 mtime（讓分析報告標註抓取時間）
    mtime = datetime.fromtimestamp(p.stat().st_mtime)
    csv_mtime_str = mtime.strftime('%Y-%m-%d %H:%M:%S')
    
    rows = {}
    with open(p, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            code = row.get('code', '').strip()
            if code:
                row['__csv_mtime'] = csv_mtime_str
                rows[code] = row
    
    if stock_code:
        return rows.get(stock_code)
    return rows

# ===== FinMind =====
def finmind_get(dataset, data_id='', start_date='', end_date='', retries=3, wait_sec=3):
    params = [f'dataset={dataset}']
    if data_id: params.append(f'data_id={data_id}')
    if start_date: params.append(f'start_date={start_date}')
    if end_date: params.append(f'end_date={end_date}')
    url = 'https://api.finmindtrade.com/api/v4/data?' + '&'.join(params)
    import time
    for attempt in range(1, retries + 1):
        r = subprocess.run(['curl', '-s', '--max-time', '20', url], capture_output=True, text=True)
        try:
            data = json.loads(r.stdout).get('data', [])
            if data:
                return data
            if attempt < retries:
                print(f'  [FinMind] {dataset} 空資料，重試 {attempt}/{retries-1}...')
                time.sleep(wait_sec)
            else:
                print(f'  [FinMind] {dataset} 無資料（已重試{retries}次）')
                return []
        except:
            if attempt < retries:
                print(f'  [FinMind] {dataset} 解析失敗，重試 {attempt}/{retries-1}...')
                time.sleep(wait_sec)
            else:
                return []
    return []

# ===== Yahoo Finance =====
def yf_chart(symbol, range_='60d'):
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range_}&interval=1d'
    for attempt in range(1, 4):
        r = subprocess.run(['curl', '-s', '--max-time', '25', '-A', 'Mozilla/5.0', url],
                          capture_output=True, text=True)
        try:
            raw = r.stdout.strip()
            if not raw:
                if attempt < 3:
                    import time; time.sleep(2)
                    continue
                raise ValueError('empty response')
            result = json.loads(raw)['chart']['result']
            if result is None:
                if symbol.endswith('.TW'):
                    alt = symbol.replace('.TW', '.TWO')
                    return yf_chart(alt, range_)
                return None
            return result[0]
        except Exception as e:
            if attempt < 3:
                import time; time.sleep(2)
                continue
            if symbol.endswith('.TW'):
                alt = symbol.replace('.TW', '.TWO')
                return yf_chart(alt, range_)
            return None
    return None

# ===== 六大指標計算（新版）=====
# v10.2 修正（2026-10-05，Boss 驗證）：黃國華六大指標，每項 AA=4、A=3、BB=2、B=1、C=0，滿分 24
GRADE_SCORES = {'AA': 4, 'A': 3, 'BB': 2, 'B': 1, 'C': 0}
GRADE_ICONS = {'AA': '🟢', 'A': '🟡', 'BB': '🟠', 'B': '🔴', 'C': '⚫'}
GRADE_CLASS = {'AA': 'aa', 'A': 'a', 'BB': 'bb', 'B': 'b', 'C': 'c'}

def grade_eps(eps_sum):
    """① EPS 等級（近4季累積，元）— v10.2 改用黃國華規則（2026-10-05）
    黃國華規則：近四季累積 > 5 元 = AA、> 3 = A、> 1.5 = BB、> 0 = B、<= 0 = C"""
    if eps_sum is None: return None, 'C'
    if eps_sum >= 5: return eps_sum, 'AA'
    if eps_sum >= 3: return eps_sum, 'A'
    if eps_sum >= 1.5: return eps_sum, 'BB'
    if eps_sum > 0: return eps_sum, 'B'
    return eps_sum, 'C'

def grade_rev(yoy_avg):
    """② 營收 YoY 等級（近6月平均，%）"""
    if yoy_avg is None: return None, 'C'
    if yoy_avg >= 25: return yoy_avg, 'AA'
    if yoy_avg >= 15: return yoy_avg, 'A'
    if yoy_avg >= 5: return yoy_avg, 'BB'
    if yoy_avg >= 0: return yoy_avg, 'B'
    return yoy_avg, 'C'

def grade_inv(inv_trend_pct):
    """③ 存貨周轉率「趨勢」等級（近4季 YoY 變化率，%）— v10.2 改為黃國華定義（2026-10-05）
    原 grade_inv 誤用「絕對值」（次），黃國華規則看的是「趨勢」（週轉是否加速）
    輸入：inv_trend_pct = (最新週轉率 - 去年同期) / 去年同期 × 100
    負值 = 週轉加速（好）、正值 = 週轉放緩（差）
    注意：機械業週轉慢（絕對值低），用趨勢才公平"""
    if inv_trend_pct is None: return None, 'C'
    if inv_trend_pct <= -10: return inv_trend_pct, 'AA'   # 週轉加速 >= 10%
    if inv_trend_pct <= -3: return inv_trend_pct, 'A'    # 週轉加速 3-10%
    if inv_trend_pct <= 5: return inv_trend_pct, 'BB'    # 持平到微緩
    if inv_trend_pct <= 15: return inv_trend_pct, 'B'    # 略緩
    return inv_trend_pct, 'C'                             # 週轉明顯放緩

def grade_fcf(fcf):
    """④ 自由現金流量等價（NTD元）"""
    if fcf is None: return None, 'C'
    if fcf >= 0: return fcf, 'AA'
    return fcf, 'C'

def grade_opm(opm_avg):
    """⑤ 營業利益率等價（近4季平均，%）"""
    if opm_avg is None: return None, 'C'
    if opm_avg >= 20: return opm_avg, 'AA'
    if opm_avg >= 15: return opm_avg, 'A'
    if opm_avg >= 5: return opm_avg, 'BB'
    if opm_avg >= 0: return opm_avg, 'B'
    return opm_avg, 'C'

def grade_npm(ni_yoy_avg):
    """⑥ 稅後淨利年增率等級（近4季 YoY 平均，%）— v10.2 改為黃國華定義（2026-10-05）
    原 grade_npm 誤用「稅後淨利率」，黃國華六大指標的 ⑥ 是「稅後淨利年增率」
    注意：呼叫端需將淨利率值改成「年增率」（注意基期效應，Boss 已標示）"""
    if ni_yoy_avg is None: return None, 'C'
    if ni_yoy_avg >= 30: return ni_yoy_avg, 'AA'
    if ni_yoy_avg >= 15: return ni_yoy_avg, 'A'
    if ni_yoy_avg >= 5: return ni_yoy_avg, 'BB'
    if ni_yoy_avg >= 0: return ni_yoy_avg, 'B'
    return ni_yoy_avg, 'C'

def calc_six(fin_rows, bs_rows, cf_rows, mr_raw):
    """
    計算六大指標
    fin_rows: TaiwanStockFinancialStatements raw rows
    bs_rows:  TaiwanStockBalanceSheet raw rows
    cf_rows:  TaiwanStockCashFlowsStatement raw rows
    mr_raw:   TaiwanStockMonthRevenue raw rows
    """
    now = datetime.now()
    cy = now.year

    # ── 建立 fs{(date,type): value} ─────────────────────────────
    fs = {}
    for row in fin_rows:
        d = row.get('date', '')
        t = row.get('type')
        v = row.get('value')
        if d and t and v is not None:
            fs[(d, t)] = float(v)

    # ── 建立 bs{(date,type): value} ─────────────────────────────
    bs = {}
    for row in bs_rows:
        d = row.get('date', '')
        t = row.get('type')
        v = row.get('value')
        if d and t and v is not None:
            bs[(d, t)] = float(v)

    # ── 建立 cf{(date,type): value} ─────────────────────────────
    cf = {}
    for row in cf_rows:
        d = row.get('date', '')
        t = row.get('type')
        v = row.get('value')
        if d and t and v is not None:
            cf[(d, t)] = float(v)

    # ── 建立 mr{(year,month): revenue}（同月份取最新日期）────────
    mr = {}
    mr_date = {}
    for row in mr_raw:
        yr_key = int(row.get('revenue_year', 0))
        mo_key = int(row.get('revenue_month', 0))
        rv = row.get('revenue')
        d_str = row.get('date', '')
        if yr_key and mo_key and rv is not None:
            key = (yr_key, mo_key)
            # 同月份取最新日期
            if key not in mr_date or d_str > mr_date[key]:
                mr[key] = float(rv)
                mr_date[key] = d_str

    # ── 動態取近4季日期（收集近3年所有季報，取最新4筆）──────────────
    all_quarters = []
    for y in range(cy - 2, cy + 1):
        for m, end in [(3,'31'), (6,'30'), (9,'30'), (12,'31')]:
            d = f"{y}-{m:02d}-{end}"
            if fs.get((d, 'Revenue')) is not None:
                all_quarters.append(d)
    all_quarters.sort(reverse=True)  # 最新日期在前
    QUARTERS = all_quarters[:4]

    # ── 近6月月份（用於營收YoY）──────────────────────────────────
    RECENT6 = []
    for offset in range(5, -1, -1):
        mo = now.month - offset
        yr = cy
        while mo <= 0:
            mo += 12; yr -= 1
        RECENT6.append((yr, mo))
    # 若當月資料不足，向前取
    while len(RECENT6) < 6:
        mo = RECENT6[0][1] - 1
        yr = RECENT6[0][0]
        if mo <= 0:
            mo += 12; yr -= 1
        RECENT6.insert(0, (yr, mo))

    # ── ① EPS（近4季累積，元）──────────────────────────────────
    eps_list = [fs.get((q, 'EPS')) for q in QUARTERS]
    eps_sum = sum(e for e in eps_list if e)
    ep_v, ep_g = grade_eps(eps_sum if eps_sum else None)

    # ── ② 營收 YoY（近6月平均，%）───────────────────────────────
    # 月營收是YTD累計值，直接同期比較
    yoys = []
    for yr, mo in RECENT6:
        cur = mr.get((yr, mo))
        ly = mr.get((yr - 1, mo))
        if cur and ly and ly != 0:
            yoys.append((cur - ly) / ly * 100)
    rev_avg = sum(yoys) / len(yoys) if yoys else None
    # v9.2 補：rev_avg 塞到 result['__v92_rev_growth']（Q2 要用，單位為小數）
    rg_v, rg_g = grade_rev(rev_avg)

    # ── ③ 存貨周轉率（近4季平均，次）──────────────────────────
    # 期初存貨映射：Q1←去年Q4, Q2←Q1, Q3←Q2, Q4←Q3
    inv_prev_map = {
        f'{cy}-03-31': bs.get((f'{cy-1}-12-31', 'Inventories')),
        f'{cy}-06-30': bs.get((f'{cy}-03-31', 'Inventories')),
        f'{cy}-09-30': bs.get((f'{cy}-06-30', 'Inventories')),
        f'{cy}-12-31': bs.get((f'{cy}-09-30', 'Inventories')),
    }
    # 動態對應QUARTERS中的實際日期
    q_inv_prev = {}
    q_dates_sorted = sorted(QUARTERS)
    for i, q in enumerate(q_dates_sorted):
        if i == 0:
            prev_d = f'{int(q[:4])-1}-12-31'
        else:
            prev_d = q_dates_sorted[i - 1]
        q_inv_prev[q] = bs.get((prev_d, 'Inventories'))

    turns = []
    for q in QUARTERS:
        cogs = fs.get((q, 'CostOfGoodsSold'))
        inv = bs.get((q, 'Inventories'))
        inv_p = q_inv_prev.get(q)
        if cogs and inv and inv_p and inv_p != 0:
            turns.append(cogs / ((inv + inv_p) / 2))
    inv_avg = sum(turns) / len(turns) if turns else None
    inv_v, inv_g = grade_inv(inv_avg)

    # ── ④ 自由現金流量 FCF（年累計 OP CF YTD Q4）───────────────
    # v9 修：拿「最近一個有完整 Q4 資料的年度」而不是「最新年份」
    # 例：2026-12-31 還沒到，所以不能選 2026
    available_years = sorted(set(k[0][:4] for k in cf if 'NetCashInflowFromOperatingActivities' in k[1]))
    fcf_year = available_years[-1] if available_years else str(cy)
    # 遞減檢查，找出第一個有 Q4 (12-31) 資料的年
    for candidate in reversed(available_years):
        if cf.get((f'{candidate}-12-31', 'NetCashInflowFromOperatingActivities')) is not None:
            fcf_year = candidate
            break
    fcf = cf.get((f'{fcf_year}-12-31', 'NetCashInflowFromOperatingActivities'), 0)
    fcf_v, fcf_g = grade_fcf(fcf if fcf != 0 else None)

    # ── ⑤ 營業利益率（近4季平均，%）───────────────────────────
    opms = []
    for q in QUARTERS:
        oi = fs.get((q, 'OperatingIncome'))
        rv = fs.get((q, 'Revenue'))
        if oi is not None and rv and rv != 0:
            opms.append(oi / rv * 100)
    opm_avg = sum(opms) / len(opms) if opms else None
    om_v, om_g = grade_opm(opm_avg)

    # ── ⑥ 稅後淨利年增率（近4季 YoY 平均，%）─────────────────
    # v10.2 修正（2026-10-05，Boss 驗證）：黃國華定義是「淨利年增率」非「淨利率」
    # 同一季淨利 YoY：（本期 - 去年同期） / 去年同期 × 100
    # 注意：需警示基期效應（2025Q2 低基期可能造成 YoY +692% 失真）
    yoys_ni = []
    for q in QUARTERS:
        ni = fs.get((q, 'IncomeAfterTaxes'))
        q_year = int(q[:4])
        q_month = q[5:7]
        # 去年同期日：Q1->去年Q4, Q2->去年Q4, Q3->去年Q3, Q4->去年Q4
        if q_month == '09':
            prev_q = f'{q_year - 1}-09-30'
        else:
            prev_q = f'{q_year - 1}-12-31'
        ni_prev = fs.get((prev_q, 'IncomeAfterTaxes'))
        if ni is not None and ni_prev and ni_prev != 0:
            yoy_pct = (ni - ni_prev) / abs(ni_prev) * 100
            yoys_ni.append(yoy_pct)
    npm_avg = sum(yoys_ni) / len(yoys_ni) / 100 if yoys_ni else None  # 轉小數
    # npm_avg 為小數（如 0.6926 = +69.26%），但 grade_npm 接受 % 數值（如 69.26）
    npm_avg_pct = sum(yoys_ni) / len(yoys_ni) if yoys_ni else None
    npm_v, npm_g = grade_npm(npm_avg_pct)
    # 註記基期效應警示（如果最近一季 YoY > 100% 視為異常）
    if yoys_ni and any(y > 200 for y in yoys_ni[-2:]):
        npm_v = {'v': npm_avg_pct, 'g': npm_g, 'note': '⚠️ 基期效應：YoY > 200% 失真，建議看絕對值'}

    # ── 總分與評語 ──────────────────────────────────────────────
    total = (GRADE_SCORES.get(om_g, 1) + GRADE_SCORES.get(rg_g, 1) +
             GRADE_SCORES.get(npm_g, 1) + GRADE_SCORES.get(ep_g, 1) +
             GRADE_SCORES.get(inv_g, 1) + GRADE_SCORES.get(fcf_g, 1))

    # v10.2 修正（2026-10-05）：黃國華六大指標，每項 AA=4、A=3、BB=2、B=1、C=0，滿分 24
    # 分母從 30 改為 24，門檻同步調整
    if total >= 20: assessment = '極佳'
    elif total >= 16: assessment = '優'
    elif total >= 12: assessment = '良好'
    elif total >= 8: assessment = '普通'
    else: assessment = '待改善'

    return {
        'om':   {'v': om_v,   'g': om_g},
        'rg':   {'v': rg_v,   'g': rg_g},
        'npm':  {'v': npm_v,  'g': npm_g},
        'ep':   {'v': ep_v,   'g': ep_g},
        'inv':  {'v': inv_v,  'g': inv_g},
        'fcf':  {'v': fcf_v,  'g': fcf_g},
        'total': total,
        'assessment': assessment,
        'rev_growth': (rev_avg / 100) if rev_avg is not None else None,  # v9.2 補：小數，給 Q2 用
    }

# ===== 巴菲特紅旗（expanded — 框架 A/B/C/D）=====
def buffett_flags(roe, roa, rev_growth, de, eps_dict, pe, fin_data, ocf=None, operating_margin=None, gross_margin=None, roic_annual=None):
    """
    Buffett 8問快速篩選 + 護城河評估 + 結論判定
    框架 A: 8問表格
    框架 B: 護城河評估
    框架 C: 結論判定
    框架 D: Python 資料結構
    v10.1 B1+ 修正（2026-09-15，Gemini 驗證）：
      - 新增 gross_margin 參數（從 DB 真實抓）
      - moat 區段的「毛利率」note 改用 gross_margin，避免 FinMind 欄位錯位污染
    v10.1 B3 修正（2026-09-15，Gemini 驗證）：
      - 新增 roic_annual 參數（從 DB 4 季 EPS 加總 / bvps × (1-D/A) 算的真實年化 ROIC）
      - Q3 優先用 roic_annual，避免 collect() 內「eps × 4 / bvps」單季沒年化算錯
    """
    """
    Buffett 8問快速篩選 + 護城河評估 + 結論判定
    框架 A: 8問表格
    框架 B: 護城河評估
    框架 C: 結論判定
    框架 D: Python 資料結構
    """
    # ── Helpers ──────────────────────────────────────────────────
    # v9.2 修：ROIC = ROE × (1 - 負債比) 杜邦拆解（假設 de 是 D/A 負債比%）
    roic = (roe * (1 - (de or 0) / 100) if roe is not None and de is not None else None)

    ops_list = [row.get('value') for row in fin_data if row.get('type') == 'OperatingIncome']
    rev_list = [row.get('value') for row in fin_data if row.get('type') == 'Revenue']

    # 計算毛利率趨勢（近4季）
    margins = []
    paired = list(zip(ops_list[-4:], rev_list[-4:])) if len(ops_list) >= 4 and len(rev_list) >= 4 else []
    for o, r in paired:
        if r and r > 0: margins.append(o / r)

    # 當前營益率 proxy（使用 available 的 operating_margin 或計算）
    op_m = operating_margin
    if op_m is None and len(ops_list) >= 1 and len(rev_list) >= 1:
        if rev_list[-1] and rev_list[-1] > 0:
            op_m = ops_list[-1] / rev_list[-1]

    # 淨利
    # v9 修：取「最新一季」淨利（fin_data 由 FinMind 回傳，順序依 API 而定）
    # 用 -1 確保取到「最新」資料（配合 OCF 最新的取法）
    ni_rows = [row.get('value') for row in fin_data if row.get('type') == 'EquityAttributableToOwnersOfParent']
    net_income = ni_rows[-1] if ni_rows else None

    # EBITDA / Debt
    ebitda_rows = [row.get('value') for row in fin_data if row.get('type') == 'EBITDA']
    debt_rows   = [row.get('value') for row in fin_data if row.get('type') == 'TotalDebt']

    # ── 框架 A: 8問快速篩選 ──────────────────────────────────────────
    eight_q = []

    # Q1: 能否用一句話說明這家公司如何賺錢？
    if roa is not None and op_m is not None:
        if roa < 0 and op_m < 0:
            q1_ans = f'ROA {roa*100:.1f}% + 營益率 {op_m*100:.1f}%，雙重虧損'
            q1_rf  = True
        elif roa < 0:
            q1_ans = f'ROA {roa*100:.1f}%，資產報酬為負'
            q1_rf  = True
        elif op_m < 0:
            q1_ans = f'營益率 {op_m*100:.1f}%，本業虧損中'
            q1_rf  = True
        else:
            q1_ans = f'ROA {roa*100:.1f}%，營益率 {op_m*100:.1f}%，基本合格'
            q1_rf  = False
    elif roa is not None:
        q1_ans = f'ROA {roa*100:.1f}%，資料有限'
        q1_rf  = roa < 0
    else:
        q1_ans = '資料不足，無法判斷'
        q1_rf  = False
    eight_q.append({'q': '能否用一句話說明這家公司如何賺錢？', 'answer': q1_ans, 'red_flag': q1_rf})

    # Q2: 10年後這公司仍會存在且更具競爭力？
    if rev_growth is not None:
        if rev_growth < -0.05:
            q2_ans = f'營收年增率 {rev_growth*100:.1f}%，持續衰退'
            q2_rf  = True
        elif rev_growth < 0:
            q2_ans = f'營收年增率 {rev_growth*100:.1f}%，輕微衰退'
            q2_rf  = True
        elif rev_growth >= 0.15:
            q2_ans = f'營收年增率 {rev_growth*100:.1f}%，成長中'
            q2_rf  = False
        else:
            q2_ans = f'營收年增率 {rev_growth*100:.1f}%，溫和成長'
            q2_rf  = False
    else:
        q2_ans = '無營收資料'
        q2_rf  = False
    eight_q.append({'q': '10年後這公司仍會存在且更具競爭力？', 'answer': q2_ans, 'red_flag': q2_rf})

    # Q3: 競爭對手能否複製核心優勢？
    # v10.1 B3 修正（2026-09-15，Gemini 驗證）：優先用 DB 年化 ROIC（從 4 季 EPS 加總 / bvps × (1-D/A)）
    # v10.2 修正（2026-10-05，Boss 驗證）：Q3 與 moat 評估統一，加入毛利率條件
    # 護城河標準：ROIC >= 15% 或 毛利率 >= 30%（任一即視為有護城河）
    if roic_annual is not None:
        roic_ok = roic_annual >= 0.15
        margin_ok = gross_margin is not None and (gross_margin/100 if gross_margin > 1 else gross_margin) >= 0.30
        if roic_ok or margin_ok:
            detail = []
            if roic_ok: detail.append(f'ROIC {roic_annual*100:.1f}%')
            if margin_ok: detail.append(f'毛利率 {gross_margin/100 if gross_margin > 1 else gross_margin:.1%}')
            q3_ans = f'具基本護城河（{"+".join(detail)}，年化）'
            q3_rf  = False
        elif roic_annual < 0.08 and (gross_margin is None or (gross_margin/100 if gross_margin > 1 else gross_margin) < 0.15):
            q3_ans = f'ROIC {roic_annual*100:.1f}% + 毛利率 {gross_margin/100 if gross_margin and gross_margin > 1 else (gross_margin or 0):.1%}，無護城河 (年化)'
            q3_rf  = True
        elif roic_annual < 0.12:
            q3_ans = f'ROIC {roic_annual*100:.1f}% + 毛利率 {gross_margin/100 if gross_margin and gross_margin > 1 else (gross_margin or 0):.1%}，護城河薄弱 (年化)'
            q3_rf  = False
        else:
            q3_ans = f'ROIC {roic_annual*100:.1f}% + 毛利率 {gross_margin/100 if gross_margin and gross_margin > 1 else (gross_margin or 0):.1%}，有基本護城河 (年化)'
            q3_rf  = False
    elif roic is not None:
        # fallback：用舊算法（單季 ROE × (1-D/A)）
        # v10.2 修正（2026-10-05）：單季 fallback 也與毛利率雙條件統一
        roic_ok = roic >= 0.15
        margin_ok = gross_margin is not None and (gross_margin/100 if gross_margin > 1 else gross_margin) >= 0.30
        gm_str = f'{gross_margin/100 if gross_margin and gross_margin > 1 else (gross_margin or 0):.1%}'
        if roic_ok or margin_ok:
            detail = []
            if roic_ok: detail.append(f'ROIC {roic*100:.1f}%')
            if margin_ok: detail.append(f'毛利率 {gm_str}')
            q3_ans = f'具基本護城河（{"+".join(detail)}，單季）'
            q3_rf  = False
        elif roic < 0.08 and (gross_margin is None or (gross_margin/100 if gross_margin > 1 else gross_margin) < 0.15):
            q3_ans = f'ROIC {roic*100:.1f}% + 毛利率 {gm_str}，無護城河保護 (單季)'
            q3_rf  = True
        elif roic < 0.12:
            q3_ans = f'ROIC {roic*100:.1f}% + 毛利率 {gm_str}，護城河薄弱 (單季)'
            q3_rf  = False
        else:
            q3_ans = f'ROIC {roic*100:.1f}% + 毛利率 {gm_str}，有基本護城河 (單季)'
            q3_rf  = False
    else:
        q3_ans = '無法計算ROIC'
        q3_rf  = False
    eight_q.append({'q': '競爭對手能否複製核心優勢？', 'answer': q3_ans, 'red_flag': q3_rf})

    # Q4: 能否漲價5-10%而不流失客戶？
    # v10.1 B2 修正（2026-09-15，Gemini 驗證）：優先用 DB 真實毛利率 gross_margin
    # fallback 才用 FinMind 算的 margins（營益率，可能因欄位錯位污染）
    # 製造業門檻：30%+ 強定價權、20-30% 有基本、10-20% 弱、<10% 商品型
    if gross_margin is not None:
        # v10.2 修正（2026-10-05）：容忍 DB 回傳 % 或小數兩種格式
        gm_v = gross_margin / 100 if gross_margin > 1 else gross_margin
        if gm_v >= 0.30:
            q4_ans = f'毛利率 {gm_v*100:.1f}%，具備強定價權 (DB)'
            q4_rf  = False
        elif gm_v >= 0.20:
            q4_ans = f'毛利率 {gm_v*100:.1f}%，有基本定價權 (DB)'
            q4_rf  = False
        elif gm_v >= 0.15:
            q4_ans = f'毛利率 {gm_v*100:.1f}%，定價權弱 (DB)'
            q4_rf  = True
        else:
            q4_ans = f'毛利率 {gm_v*100:.1f}% < 15%，商品型 (DB)'
            q4_rf  = True
    elif margins:
        avg_m = np.mean(margins[-4:]) if len(margins) >= 4 else margins[-1]
        if avg_m < 0.05:
            q4_ans = f'營益率 {avg_m*100:.1f}%，無定價權，商品型'
            q4_rf  = True
        elif avg_m < 0.10:
            q4_ans = f'營益率 {avg_m*100:.1f}%，定價權弱'
            q4_rf  = True
        elif avg_m >= 0.20:
            q4_ans = f'營益率 {avg_m*100:.1f}%，具備強定價權'
            q4_rf  = False
        else:
            q4_ans = f'營益率 {avg_m*100:.1f}%，有基本定價權'
            q4_rf  = False
    else:
        q4_ans = '資料不足'
        q4_rf  = False
    eight_q.append({'q': '能否漲價5-10%而不流失客戶？', 'answer': q4_ans, 'red_flag': q4_rf})

    # Q5: 獲利真正轉換為現金？
    if ocf is not None and net_income is not None and net_income > 0:
        ratio = ocf / net_income if net_income else 0
        if ratio < 0.5:
            q5_ans = f'OCF/淨利 {ratio*100:.0f}%，現金轉化率低'
            q5_rf  = True
        elif ratio < 0.8:
            q5_ans = f'OCF/淨利 {ratio*100:.0f}%，需關注'
            q5_rf  = False
        else:
            q5_ans = f'OCF/淨利 {ratio*100:.0f}%，現金品質佳'
            q5_rf  = False
    elif ocf is not None and net_income is not None and net_income <= 0:
        q5_ans = '虧損中，無法評估'
        q5_rf  = False
    else:
        q5_ans = 'OCF資料不足'
        q5_rf  = False
    eight_q.append({'q': '獲利真正轉換為現金？', 'answer': q5_ans, 'red_flag': q5_rf})

    # Q6: 營收-30%能否存活？
    if de is not None:
        if de > 80:
            q6_ans = f'負債比 {de:.0f}%，景氣下行可能違約'
            q6_rf  = True
        elif de > 60:
            q6_ans = f'負債比 {de:.0f}%，-30%營收有壓力'
            q6_rf  = True
        elif de <= 30:
            q6_ans = f'負債比 {de:.0f}%，現金足以撐過寒冬'
            q6_rf  = False
        else:
            q6_ans = f'負債比 {de:.0f}%，尚在安全範圍'
            q6_rf  = False
    else:
        q6_ans = '無負債資料'
        q6_rf  = False
    eight_q.append({'q': '營收-30%能否存活？', 'answer': q6_ans, 'red_flag': q6_rf})

    # Q7: 管理層誠信？（不自動打紅旗）
    auditor_rows = [row for row in fin_data if row.get('type') == 'AuditorOpinion']
    if auditor_rows:
        opinion = str(auditor_rows[0].get('value', ''))
        if '保留' in opinion or '無法' in opinion or '否定' in opinion:
            q7_ans = f'會計師出具：{opinion[:20]}'
            q7_rf  = True
        else:
            q7_ans = '無異常跡象'
            q7_rf  = False
    else:
        q7_ans = '無資料（不自動打紅旗）'
        q7_rf  = False
    eight_q.append({'q': '管理層誠信？', 'answer': q7_ans, 'red_flag': q7_rf})

    # Q8: 股價與內在價值差距夠大？
    if pe is not None:
        if pe > 40:
            q8_ans = f'PE {pe:.0f}x，估值偏高'
            q8_rf  = True
        elif pe > 25:
            q8_ans = f'PE {pe:.0f}x，評價合理偏高'
            q8_rf  = False
        elif pe < 15:
            q8_ans = f'PE {pe:.0f}x，股價低於內在價值'
            q8_rf  = False
        else:
            q8_ans = f'PE {pe:.0f}x，評價合理'
            q8_rf  = False
    else:
        q8_ans = 'PE資料不足'
        q8_rf  = False
    eight_q.append({'q': '股價與內在價值差距夠大？', 'answer': q8_ans, 'red_flag': q8_rf})

    # ── 框架 B: 護城河評估 ──────────────────────────────────────────
    moat = []

    # 1. 無形資產（品牌/專利/執照）
    # v10.1 B1+ 修正（2026-09-15，Gemini 驗證）：改用 DB 真實毛利率 gross_margin
    if gross_margin is not None:
        gm_v = gross_margin / 100 if gross_margin > 1 else gross_margin  # 容忍 % 或 小數
        if gm_v >= 0.30:
            m1_strength = '強'; m1_note = f'毛利率 {gm_v*100:.1f}%，品牌定價權強 (DB)'
        elif gm_v >= 0.15:
            m1_strength = '中'; m1_note = f'毛利率 {gm_v*100:.1f}%，有品牌溢價 (DB)'
        elif gm_v >= 0.08:
            m1_strength = '弱'; m1_note = f'毛利率 {gm_v*100:.1f}%，溢價有限 (DB)'
        else:
            m1_strength = '無'; m1_note = f'毛利率 {gm_v*100:.1f}%，無差異化 (DB)'
    elif margins:
        # 備援：DB 沒資料時用 FinMind 計算的營益率（保留舊邏輯）
        avg_m = np.mean(margins[-4:]) if len(margins) >= 4 else margins[-1]
        if avg_m >= 0.30:
            m1_strength = '強'; m1_note = f'營益率 {avg_m*100:.1f}%（FinMind備援）'
        elif avg_m >= 0.15:
            m1_strength = '中'; m1_note = f'營益率 {avg_m*100:.1f}%（FinMind備援）'
        elif avg_m >= 0.08:
            m1_strength = '弱'; m1_note = f'營益率 {avg_m*100:.1f}%（FinMind備援）'
        else:
            m1_strength = '無'; m1_note = f'營益率 {avg_m*100:.1f}%（FinMind備援）'
    else:
        m1_strength = '無'; m1_note = '資料不足'
    moat.append({'type': '無形資產（品牌/專利）', 'strength': m1_strength, 'note': m1_note})

    # 2. 成本優勢
    if roic is not None:
        if roic >= 0.15:
            m2_strength = '強'; m2_note = f'ROIC {roic*100:.1f}%，成本優勢明顯'
        elif roic >= 0.10:
            m2_strength = '中'; m2_note = f'ROIC {roic*100:.1f}%，有成本優勢'
        elif roic >= 0.06:
            m2_strength = '弱'; m2_note = f'ROIC {roic*100:.1f}%，優勢薄弱'
        else:
            m2_strength = '無'; m2_note = f'ROIC {roic*100:.1f}%，無結構優勢'
    else:
        m2_strength = '無'; m2_note = '資料不足'
    moat.append({'type': '成本優勢', 'strength': m2_strength, 'note': m2_note})

    # 3. 轉換成本（proxy: 無精確認量指標，以ROE/ROIC穩定性代替）
    if roe is not None and roe >= 0.15:
        m3_strength = '中'; m3_note = f'ROE {roe*100:.1f}%，客戶黏著度尚可'
    elif roe is not None and roe >= 0.08:
        m3_strength = '弱'; m3_note = f'ROE {roe*100:.1f}%，轉換成本低'
    elif roe is not None:
        m3_strength = '無'; m3_note = f'ROE {roe*100:.1f}%，無黏著度'
    else:
        m3_strength = '無'; m3_note = '資料不足'
    moat.append({'type': '轉換成本', 'strength': m3_strength, 'note': m3_note})

    # 4. 網路效應（無直接proxy，結合規模與ROIC綜合判斷）
    if roic is not None and de is not None and roic >= 0.12 and de < 40:
        m4_strength = '中'; m4_note = '規模與ROIC支持網路效應'
    elif roic is not None and roic >= 0.15:
        m4_strength = '弱'; m4_note = '可能具初步網路效應'
    else:
        m4_strength = '無'; m4_note = '無明顯網路效應跡象'
    moat.append({'type': '網路效應', 'strength': m4_strength, 'note': m4_note})

    # 5. 規模經濟（結合營收規模與成本結構）
    # proxy: 營收規模 proxy - 以ROE維持能力代替
    if roic is not None and roic >= 0.15 and margins and np.mean(margins[-2:]) >= 0.10:
        m5_strength = '強'; m5_note = '規模化運營，成本結構佳'
    elif roic is not None and roic >= 0.10:
        m5_strength = '中'; m5_note = '有規模效益'
    else:
        m5_strength = '弱'; m5_note = '規模效益有限'
    moat.append({'type': '規模經濟', 'strength': m5_strength, 'note': m5_note})

    # 6. 商品型企業判定
    # v10.1 B1+ 修正（2026-09-15，Gemini 驗證）：改用 DB 真實毛利率 gross_margin
    if gross_margin is not None:
        gm_v = gross_margin / 100 if gross_margin > 1 else gross_margin  # 容忍 % 或 小數
        if gm_v < 0.10:
            m6_is_commodity = True; m6_note = f'毛利率 {gm_v*100:.1f}% < 10%，商品型 (DB)'
        elif gm_v < 0.15:
            m6_is_commodity = True; m6_note = f'毛利率 {gm_v*100:.1f}% < 15%，接近商品型 (DB)'
        else:
            m6_is_commodity = False; m6_note = f'毛利率 {gm_v*100:.1f}%，非商品型 (DB)'
    elif margins:
        # 備援：DB 沒資料時用 FinMind 計算的營益率（保留舊邏輯）
        avg_m = np.mean(margins[-4:]) if len(margins) >= 4 else margins[-1]
        if avg_m < 0.10:
            m6_is_commodity = True; m6_note = f'營益率 {avg_m*100:.1f}% < 10%，商品型（FinMind備援）'
        elif avg_m < 0.15:
            m6_is_commodity = True; m6_note = f'營益率 {avg_m*100:.1f}% < 15%，接近商品型（FinMind備援）'
        else:
            m6_is_commodity = False; m6_note = f'營益率 {avg_m*100:.1f}%，非商品型（FinMind備援）'
    else:
        m6_is_commodity = False; m6_note = '資料不足'
    moat.append({'type': '商品型企業', 'strength': '是' if m6_is_commodity else '否', 'note': m6_note})

    # ── 框架 C: 結論判定 ──────────────────────────────────────────
    # 收集所有紅旗（除了管理誠信）
    flag_items = []
    for item in eight_q:
        if item['red_flag'] and '管理層誠信' not in item['q']:
            flag_items.append(f"🚩 Q{item['q'].split('？')[0]}：{item['answer']}")

    # 管理誠信紅旗（單獨處理）
    mgmt_flag = any(item['red_flag'] for item in eight_q if '管理層誠信' in item['q'])
    if mgmt_flag:
        for item in eight_q:
            if '管理層誠信' in item['q'] and item['red_flag']:
                flag_items.append(f"🚨 {item['q']}：{item['answer']}")

    # 額外財務紅旗（增收應款、存貨等）
    ar_rows = [row for row in fin_data if row.get('type') == 'AccountReceivable']
    if len(ar_rows) >= 2 and rev_growth is not None:
        ar0 = ar_rows[0].get('value', 0) or 0
        ar1 = ar_rows[1].get('value', 1) or 1
        ar_growth = (ar0 - ar1) / ar1 if ar1 else 0
        if ar_growth > rev_growth + 0.10:
            flag_items.append(f"🚩 應收帳款異常：成長 {ar_growth*100:.0f}% 持續超越營收")

    inv_rows = [row for row in fin_data if row.get('type') == 'Inventory']
    if len(inv_rows) >= 2 and rev_growth is not None:
        inv0 = inv_rows[0].get('value', 0) or 0
        inv1 = inv_rows[1].get('value', 1) or 1
        inv_growth = (inv0 - inv1) / inv1 if inv1 else 0
        if inv_growth > rev_growth + 0.10:
            flag_items.append(f"🚩 存貨異常：成長 {inv_growth*100:.0f}% 超越營收")

    # 利息保障
    int_rows = [row for row in fin_data if row.get('type') == 'InterestExpense']
    if int_rows and int_rows[0].get('value') and ebitda_rows and ebitda_rows[0].get('value') and int_rows[0]['value'] > 0:
        cov = ebitda_rows[0]['value'] / int_rows[0]['value']
        if cov < 3:
            flag_items.append(f"🚩 利息保障不足：{cov:.1f}x < 3x")

    # 計算紅旗數（排除管理誠信）
    red_count = sum(1 for i, item in enumerate(eight_q) if item['red_flag'] and '管理層誠信' not in item['q'])

    # 判定結論
    if mgmt_flag:
        verdict     = '🚨 自動否決'
        verdict_cl  = 'fail'
        verdict_txt = '管理層誠信問題，原則上不建议投资'
    elif red_count >= 3:
        verdict     = '❌ 不通過'
        verdict_cl  = 'fail'
        verdict_txt = f'存在{red_count}項紅旗，投資風險較高，觀望為宜'
    elif red_count == 2:
        verdict     = '⚠️ 需分析'
        verdict_cl  = 'warn'
        verdict_txt = f'存在{red_count}項紅旗，需進一步研究風險與回報'
    else:
        verdict     = '✅ 通過'
        verdict_cl  = 'pass'
        verdict_txt = '紅旗數量少，具備投資基本條件'

    return {
        'flags':       flag_items,
        'eight_q':     eight_q,
        'moat':        moat,
        'verdict':     verdict,
        'verdict_cl':  verdict_cl,
        'verdict_text': verdict_txt,
        'red_count':   red_count,
        'mgmt_flag':   mgmt_flag,
    }

# ===== V6 新增：放空評估 =====
def short_sell_assessment(result, bf):
    """
    當 Buffett 8問不通過（3+紅旗）時，計算放空適合度評分
    權重：護城河缺失(20%), ROA(25%), 負債率(20%), 營業利益率(25%), 產業趨勢(10%)
    """
    moat = bf.get('moat', [])
    moat_no = sum(1 for m in moat if m.get('strength') == '無')
    moat_score = min(10, moat_no * 3)

    roa = result.get('roa')
    if roa is None or roa < 0: roa_score = 10
    elif roa < 0.03: roa_score = 7
    elif roa < 0.08: roa_score = 4
    else: roa_score = 1

    de = result.get('de')
    if de is None: de_score = 5
    elif de > 100: de_score = 10
    elif de > 80: de_score = 8
    elif de > 50: de_score = 5
    elif de > 30: de_score = 2
    else: de_score = 0

    om = result.get('operating_margin')
    if om is None: om_score = 5
    elif om < 0: om_score = 10
    elif om < 0.05: om_score = 7
    elif om < 0.10: om_score = 4
    else: om_score = 1

    rg = result.get('revenue_growth')
    if rg is None: ind_score = 5
    elif rg < -0.10: ind_score = 10
    elif rg < 0: ind_score = 7
    elif rg < 0.05: ind_score = 4
    else: ind_score = 1

    total = (moat_score * 0.20 + roa_score * 0.25 + de_score * 0.20 +
             om_score * 0.25 + ind_score * 0.10)

    if total >= 70:
        recommendation = '🟢 適合放空'
        short_target = round(result.get('current_price', 0) * 0.80, 2) if result.get('current_price') else 'N/A'
    elif total >= 50:
        recommendation = '⚠️ 觀望放空'
        short_target = 'N/A'
    else:
        recommendation = '🔴 不適合放空'
        short_target = 'N/A'

    return {
        'moat_score': moat_score, 'roa_score': roa_score,
        'de_score': de_score, 'om_score': om_score,
        'ind_score': ind_score, 'total': round(total, 1),
        'recommendation': recommendation, 'short_target': short_target,
    }

# ===== V6 新增：技術關鍵價位 =====
def technical_levels(result):
    price = result.get('current_price', 0)
    hist = result.get('kline_60d', [])
    hl = result.get('price_hl', {})
    p52w = result.get('price_52w', {})  # v9.1：52 週高/低 以 DB 為準
    now = datetime.now()
    cy = now.year

    # v9.1：52 週高/低改用 stock_daily DB 的近 252 個交易日（與投資數據總覽一致）
    high52 = p52w.get('high') if p52w else None
    low52 = p52w.get('low') if p52w else None
    # 備援：若 DB 沒資料，fallback 到 yfinance 2y chart 的 cy + cy-1
    if high52 is None or low52 is None:
        for yr_s in [str(cy), str(cy-1)]:
            d = hl.get(yr_s, {})
            if d.get('high') and (high52 is None or d['high'] > high52): high52 = d['high']
            if d.get('low') and (low52 is None or d['low'] < low52): low52 = d['low']

    if not hist or len(hist) < 20: return {}
    # v9.1.2: 過濾 None 防止 DB 缺值時崩潰
    closes = [d['close'] for d in hist if d.get('close') is not None]
    if len(closes) < 20:
        return {'high52': high52, 'low52': low52, 'price': price,
                'ma20': None, 'ma60': None, 'm20_high': None, 'm20_low': None,
                'drop_from_high': ((price - high52) / high52 * 100) if high52 else None}
    ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else sum(closes) / len(closes)
    ma60 = sum(closes) / len(closes) if len(closes) < 60 else sum(closes[-60:]) / 60
    m20_high = max(closes[-20:]) if len(closes) >= 20 else max(closes)
    m20_low = min(closes[-20:]) if len(closes) >= 20 else min(closes)
    drop_fh = ((price - high52) / high52 * 100) if high52 else None

    return {
        'high52': high52, 'low52': low52,
        'ma20': round(ma20, 2), 'ma60': round(ma60, 2),
        'm20_high': round(m20_high, 2), 'm20_low': round(m20_low, 2),
        'price': price, 'drop_from_high': round(drop_fh, 1) if drop_fh else None,
    }

# ===== 主收集 =====
def collect(stock):
    now = datetime.now()
    cy = now.year
    result = {
        'stock_code': stock, 'stock_name': stock,
        'fetch_time': now.strftime('%Y-%m-%d %H:%M:%S'),
        'current_price': None, 'volume': None,
        'ma5_vol': None, 'ma10_vol': None, 'ma20_vol': None,
        'kline_60d': [], 'eps': {}, 'monthly_revenue': {},
        'price_hl': {}, 'pe_hl': {},
        'financials_raw': [],
        'six': {
            'om': {'v': None, 'g': None},
            'rg': {'v': None, 'g': None},
            'ep': {'v': None, 'g': None},
            'nig': {'v': None, 'g': None},
            'inv': {'v': None, 'g': 'C'},
            'fcf': {'v': None, 'g': 'C'},
        },
        'buffett_flags': [],
        'roe': None, 'roa': None, 'revenue_growth': None,
        'de': None, 'pe_ratio': None, 'forward_pe': None,
        'dividend_yield': None, 'operating_margin': None,
        # ===== v10.1 B1 修正（2026-09-15，Gemini 驗證）=====
        # 新增 gross_margin / net_margin 欄位，從 DB 直接抓，避免 yfinance 欄位錯位
        'gross_margin': None, 'net_margin': None,
        'gross_margin_db_source': None,  # 標記資料源是 DB（用於 sanity check）
        'industry': None, 'sector': None,
        # ===== v9 新增欄位 =====
        'data_source': 'DB+C API',  # 資料源標註
        'shareholding_signal': None,  # 主力出貨/集貨訊號
        'realtime_price': None,       # 即時 CSV 抓取價
        'realtime_volume': None,      # 即時 CSV 抓取量
        'realtime_csv_mtime': None,   # CSV 檔案 mtime
    }

    # === v9 新增：DB 增強資料（讀於 FinMind / yfinance 之前） ===
    code = stock.replace('.TW','').replace('.TWO','')
    v9_db_basic = db_get_stock_basic(stock)
    if v9_db_basic:
        result['data_source'] = 'DB+C API (v10)'
        # 補上 DB 裡有的、yfinance 可能漏的
        if not result.get('industry') and v9_db_basic.get('name'):
            result['industry'] = v9_db_basic.get('name')
        # DB 已有最新PE、融資券、外資持股比（v8 透過 yfinance 抓不一定有）
        if v9_db_basic.get('pe_ratio') and not result.get('pe_ratio'):
            result['pe_ratio'] = v9_db_basic['pe_ratio']
        if v9_db_basic.get('margin_financing_balance') is not None:
            result['margin_financing_balance'] = v9_db_basic['margin_financing_balance']
        if v9_db_basic.get('margin_short_balance') is not None:
            result['margin_short_balance'] = v9_db_basic['margin_short_balance']
        if v9_db_basic.get('foreign_holding_ratio') is not None:
            result['foreign_holding_ratio_v9'] = v9_db_basic['foreign_holding_ratio']
        result['last_db_date'] = v9_db_basic.get('date')
        print(f'  [v9 DB] {code}: 價 {v9_db_basic.get("price")} / PE {v9_db_basic.get("pe_ratio")} / 融資 {v9_db_basic.get("margin_financing_balance")} (date={v9_db_basic.get("date")})')

    # === v9 新增：即時 CSV 讀取 ===
    realtime = read_realtime_csv(stock_code=code)
    if realtime:
        try:
            result['realtime_price']  = float(realtime.get('price', 0) or 0)
            result['realtime_volume'] = int(realtime.get('volume', 0) or 0)
            result['realtime_csv_mtime'] = realtime.get('__csv_mtime')
            # 即時價 vs DB 昨日收價 的變動
            if result.get('current_price') and result['realtime_price']:
                chg = result['realtime_price'] - result['current_price']
                result['realtime_chg_pct'] = round(chg / result['current_price'] * 100, 2)
            print(f'  [v9 CSV] {code}: 即時 {result["realtime_price"]} / 量 {int(round(result["realtime_volume"]/1000)):,}張 (CSV 時間 {result["realtime_csv_mtime"]})')
        except Exception as e:
            print(f'  [v9 CSV 解析失敗] {e}')

    # === v9.2 修：K線 60 天優先用 DB，yfinance 為 fallback ===
    db_kl = db_get_kline_60d(stock)
    if db_kl and len(db_kl) >= 20:
        result['kline_60d'] = db_kl
        result['current_price'] = db_kl[-1]['close']
        result['volume']        = db_kl[-1]['volume']
        vlist = [d['volume'] for d in db_kl if d.get('volume')]
        if len(vlist) >= 5:  result['ma5_vol']  = int(np.mean(vlist[-5:]))
        if len(vlist) >= 10: result['ma10_vol'] = int(np.mean(vlist[-10:]))
        if len(vlist) >= 20: result['ma20_vol'] = int(np.mean(vlist[-20:]))
        print(f'  [v9.2 DB K線] {len(db_kl)} 天 / 收 {db_kl[-1]["close"]} / 量 {int(db_kl[-1]["volume"]/1000):,}張')
    # === Yahoo Finance 60天 (fallback) ===
    ch = yf_chart(stock, '60d')
    if ch and not result.get('kline_60d'):
        meta = ch.get('meta', {})
        ts_list = ch.get('timestamp', [])
        q = ch.get('indicators', {}).get('quote', [{}])[0]
        closes = q.get('close', []); vols = q.get('volume', [])
        result['current_price'] = meta.get('regularMarketPrice')
        result['volume'] = meta.get('regularMarketVolume')
        vlist = [v for v in vols if v]
        if len(vlist) >= 5:  result['ma5_vol']  = int(np.mean(vlist[-5:]))
        if len(vlist) >= 10: result['ma10_vol'] = int(np.mean(vlist[-10:]))
        if len(vlist) >= 20: result['ma20_vol'] = int(np.mean(vlist[-20:]))
        for i, ts in enumerate(ts_list):
            if closes[i] is not None:
                result['kline_60d'].append({
                    'date': datetime.fromtimestamp(ts).strftime('%Y-%m-%d'),
                    'close': round(closes[i], 2),
                    'volume': int(vols[i]) if vols[i] else 0
                })

    # === v9.2 修：2 年高低點優先用 DB，yfinance 為 fallback ===
    db_hl = db_get_price_hl_2y(stock)
    if db_hl:
        for yr_s, vals in db_hl.items():
            result['price_hl'][yr_s] = vals
        print(f'  [v9.2 DB 2y 高低] {len(db_hl)} 年: {list(db_hl.keys())}')
    # === Yahoo Finance 2年 (fallback) ===
    ch2 = yf_chart(stock, '2y')
    if ch2 and not result.get('price_hl'):
        ts2 = ch2.get('timestamp', [])
        q2 = ch2.get('indicators', {}).get('quote', [{}])[0]
        high2 = q2.get('high', []); low2 = q2.get('low', [])
        yd = {}
        for i, ts in enumerate(ts2):
            yr = datetime.fromtimestamp(ts).year
            if yr not in yd: yd[yr] = {'high': [], 'low': []}
            if high2[i]: yd[yr]['high'].append(high2[i])
            if low2[i]:  yd[yr]['low'].append(low2[i])
        for yr in range(cy-4, cy+1):
            d = yd.get(yr, {})
            if d.get('high'):
                result['price_hl'][str(yr)] = {
                    'high': int(round(max(d['high']), 0)),
                    'low': int(round(min(d['low']), 0)),
                }

    # === FinMind 月營收（end_date 設為明年同期以取到完整年度）===
    mr_raw = finmind_get('TaiwanStockMonthRevenue',
                          data_id=stock.replace('.TW',''),
                          start_date='2022-01-01',
                          end_date=f'{cy + 1}-12-31')
    for row in mr_raw:
        yr = str(int(row.get('revenue_year', 0)))
        mn = str(int(row.get('revenue_month', 0)))
        rv = row.get('revenue')
        if yr and mn and rv:
            if yr not in result['monthly_revenue']:
                result['monthly_revenue'][yr] = {}
            result['monthly_revenue'][yr][mn] = float(rv)

    # === FinMind 季財報 ===
    fin = finmind_get('TaiwanStockFinancialStatements',
                       data_id=stock.replace('.TW',''),
                       start_date='2021-01-01',
                       end_date=f'{cy + 1}-12-31')

    # === FinMind 資產負債表 ===
    bs_rows = finmind_get('TaiwanStockBalanceSheet',
                           data_id=stock.replace('.TW',''),
                           start_date='2021-01-01',
                           end_date=f'{cy + 1}-12-31')

    # === v9 修：DB 現金流量表（取代 FinMind，無資料才備援） ===
    cf_rows = db_get_cash_flow(stock)
    if not cf_rows:
        print(f'  [v9 現金流] DB 無資料，改用 FinMind 備援')
        cf_rows = finmind_get('TaiwanStockCashFlowsStatement',
                           data_id=stock.replace('.TW',''),
                           start_date='2021-01-01',
                           end_date=f'{cy + 1}-12-31')

    result['financials_raw'] = fin
    result['balance_sheet_raw'] = bs_rows
    result['cash_flows_raw'] = cf_rows
    result['month_revenue_raw'] = mr_raw

    # ===== v10.1 B1 修正（2026-09-15，Gemini 驗證）=====
    # 從 DB stock_income_statement 抓最新一季真實毛利率 / 營益率 / 淨利率
    # 這是「資料源真相」，避免被 yfinance 欄位錯位污染
    fin_db_rows = db_get_financials(stock)
    if fin_db_rows:
        # fin_db_rows 已 ORDER BY year DESC, season DESC，第一筆 = 最新一季
        latest = fin_db_rows[0]
        gm_db  = latest.get('gross_margin')    # 真實毛利率
        om_db  = latest.get('operating_margin')  # 真實營益率
        npm_db = latest.get('net_margin')      # 真實淨利率
        eps_db = latest.get('eps')             # 真實 EPS
        yr_db  = latest.get('year')
        se_db  = latest.get('season')
        # 寫入 result（DB 真實值優先於 yfinance）
        if gm_db is not None:
            result['gross_margin'] = float(gm_db)
            result['gross_margin_db_source'] = f'DB Q{se_db}/{yr_db}'
            print(f'  [v10.1 B1] {code}: 毛利率 {gm_db:.2f}% (DB 真實，Q{se_db}/{yr_db})')
        if om_db is not None:
            result['operating_margin'] = float(om_db)
            print(f'  [v10.1 B1] {code}: 營益率 {om_db:.2f}% (DB 真實)')
        if npm_db is not None:
            result['net_margin'] = float(npm_db)
            print(f'  [v10.1 B1] {code}: 淨利率 {npm_db:.2f}% (DB 真實)')
        # Sanity check：毛利率 < 15% → 警告（可能是欄位錯或異常公司）
        if gm_db is not None and gm_db < 15:
            print(f'  ⚠️  [v10.1 B1] 毛利率 {gm_db:.2f}% < 15% → 自動警告「可能欄位錯」')
    else:
        print(f'  [v10.1 B1] {code}: DB 損益表無資料，fallback 到 yfinance')

    # Annual EPS from quarterly accumulation
    eps_by_yr = {}
    for row in fin:
        if row.get('type') == 'EPS':
            yr = int(row.get('date','0000-00-00')[:4])
            v = row.get('value')
            if yr and v is not None:
                eps_by_yr[yr] = eps_by_yr.get(yr, 0) + float(v)
    for yr, v in eps_by_yr.items():
        result['eps'][str(yr)] = round(v, 2)

    # 歷史 PE
    for yr_s in list(result['price_hl'].keys()):
        yr_i = int(yr_s)
        hl = result['price_hl'].get(yr_s)
        ep = result['eps'].get(yr_s)
        if hl and ep and ep > 0:
            result['pe_hl'][yr_s] = {
                'high_pe': round(hl['high'] / ep, 1),
                'low_pe': round(hl['low'] / ep, 1),
            }

    # === 六大指標（新公式）===
    six_data = calc_six(fin, bs_rows, cf_rows, mr_raw)
    # v9.2 補：將 calc_six 算的 rev_growth 塞到 result（給 Q2 用）
    if six_data.get('rev_growth') is not None:
        result['revenue_growth'] = six_data['rev_growth']
    result['six'] = six_data

    # === v9.1：近 52 週資料（4 季 EPS + 52 週高低 + 高低 PE）===
    eps_4q = db_get_eps_4q(stock)
    price_52w = db_get_price_52w(stock)
    if eps_4q and price_52w and eps_4q['sum'] > 0:
        result['eps_4q'] = eps_4q
        result['price_52w'] = price_52w
        result['pe_52w'] = {
            'high_pe': round(price_52w['high'] / eps_4q['sum'], 1),
            'low_pe':  round(price_52w['low']  / eps_4q['sum'], 1),
        }
        print(f'  [v9.1 52週] {code}: 4Q EPS {eps_4q["sum"]} / 52w 高 {price_52w["high"]} / 52w 低 {price_52w["low"]} / 高PE {result["pe_52w"]["high_pe"]} / 低PE {result["pe_52w"]["low_pe"]}')

    # === 基本面（Yahoo Finance）===
    try:
        import yfinance as _yf
        tk = _yf.Ticker(stock)
        info = {}
        try: info = tk.info or {}
        except: pass
        fi = {}
        try: fi = tk.fast_info or {}
        except: pass
        result['roe'] = (fi.get('returnOnEquity') or info.get('returnOnEquity')) or result.get('roe')
        result['roa'] = (fi.get('returnOnAssets') or info.get('returnOnAssets')) or result.get('roa')
        result['revenue_growth'] = info.get('revenueGrowth') or result.get('revenue_growth')
        result['operating_margin'] = info.get('operatingMargins') or result.get('operating_margin')
        # ===== v10.1 B1 修正（2026-09-15，Gemini 驗證）=====
        # 避免 yfinance 覆蓋 DB 真實營益率（DB 優先）
        if result.get('gross_margin_db_source'):
            # DB 已有真實營益率 → 取消 yfinance 覆蓋，還原 DB 值
            # 從 fin_db_rows 拿 om_db（前面 Step 2 已寫進 result['operating_margin']）
            print(f'  [v10.1 B1] {code}: 保護 DB 營益率不被 yfinance 覆蓋')
            # 不重新賦值，保留 Step 2 寫入的 DB 值
        else:
            # DB 沒資料 → 用 yfinance fallback（原本邏輯保留）
            result['operating_margin'] = info.get('operatingMargins') or result.get('operating_margin')
        result['pe_ratio'] = (fi.get('trailingPE') or info.get('trailingPE')) or result.get('pe_ratio')
        result['forward_pe'] = (fi.get('forwardPE') or info.get('forwardPE')) or result.get('forward_pe')
        result['de'] = info.get('debtToEquity')
        dy = info.get('dividendYield') or fi.get('dividendYield')
        if dy and dy > 1: dy = dy / 100
        result['dividend_yield'] = dy
        # v10.2 修正（2026-10-05，Boss 驗證）：DB 股息欄位是「配發率」不是「殖利率」
        # yfinance 回傳值混雜，需判斷：殖利率合理 0~15%，配發率 0~100%
        # DB 股利發放資料若寫到 stock_daily，可能誤用為殖利率
        if dy is not None and dy > 0.15:
            result['dividend_yield_warning'] = f'殖利率 {dy*100:.1f}% 異常高，可能誤抓配發率'
        result['industry'] = fi.get('industry') or info.get('industry')
        result['sector'] = fi.get('sector') or info.get('sector')
    except: pass

    # === v9.2 補：DB 算 ROE/ROA/DE/ROIC（在 yfinance 之後，避免被 None 覆蓋）===
    db_ratio = db_get_roe_roa_de(stock)
    if db_ratio.get('roe') is not None:
        result['roe'] = db_ratio['roe']
    if db_ratio.get('roa') is not None:
        result['roa'] = db_ratio['roa']
    if db_ratio.get('de') is not None:
        result['de']  = db_ratio['de']
    if db_ratio.get('roic') is not None:
        result['roic'] = db_ratio['roic']
    if any(v is not None for v in db_ratio.values()):
        roic_s = f'{db_ratio["roic"]*100:.1f}%' if db_ratio.get('roic') is not None else 'N/A'
        print(f'  [v9.2 DB 比率] ROE {db_ratio["roe"]*100:.1f}% / ROA {db_ratio["roa"]*100:.1f}% / 負債比 {db_ratio["de"]:.1f}% / ROIC {roic_s}')

    # === v10.1 B3 修正（2026-09-15，Gemini 驗證）===
    # 直接從 DB 抓最近 4 季 EPS 加總 / bvps，算真正的年化 ROE & ROIC
    # 避免 collect() 內「eps × 4 / bvps」因 FinMind 欄位定義問題算錯（跑出 8.32% 應該 33.3%）
    four_q_eps_rows = db_query('''
        SELECT year, season, eps FROM stock_income_statement
        WHERE code = ? AND eps IS NOT NULL
        ORDER BY year DESC, season DESC LIMIT 4
    ''', (code,))
    if four_q_eps_rows and bs_rows:
        total_eps_4q = sum(r.get('eps', 0) or 0 for r in four_q_eps_rows)
        bvps_db = bs_rows[0].get('book_value_per_share')
        tl_db   = bs_rows[0].get('total_liabilities')
        ta_db   = bs_rows[0].get('total_assets')
        if total_eps_4q > 0 and bvps_db and bvps_db > 0:
            roe_annual_real = round(total_eps_4q / bvps_db, 4)
            if tl_db and ta_db and ta_db > 0:
                da_real = tl_db / ta_db
                roic_annual_real = round(roe_annual_real * (1 - da_real), 4)
                result['roic_annual_real'] = roic_annual_real
                result['roe_annual_real'] = roe_annual_real
                print(f'  [v10.1 B3] {code}: 4Q EPS {total_eps_4q:.2f} / 年化 ROE {roe_annual_real*100:.1f}% / 年化 ROIC {roic_annual_real*100:.1f}%')

    # === 巴菲特紅旗 ===
    # v9 修：OCF 從 cf_rows 取（不在 fin 裡）
    ocf_row = [row for row in cf_rows if row.get('type') == 'NetCashInflowFromOperatingActivities']
    ocf = ocf_row[0].get('value') if ocf_row else None
    bflags = buffett_flags(
        result.get('roe'), result.get('roa'),
        result.get('revenue_growth'), result.get('de'),
        result.get('eps'), result.get('pe_ratio'),
        fin, ocf, result.get('operating_margin'),
        # v10.1 B1+ 修正（2026-09-15）：傳入 DB 真實毛利率給 moat 區段判斷
        gross_margin=result.get('gross_margin'),
        # v10.1 B3 修正（2026-09-15）：傳入 DB 年化 ROIC 給 Q3 判斷護城河
        roic_annual=result.get('roic_annual_real')
    )
    # 完整 dict（含 eight_q, moat, verdict 等）
    result['buffett_flags'] = bflags

    # === 結論 ===
    bf = result['buffett_flags']
    flags = bf.get('flags', [])
    mgmt_flag = bf.get('mgmt_flag', False)
    verdict_cl = bf.get('verdict_cl', '')
    verdict_text = bf.get('verdict_text', '')
    red_count = bf.get('red_count', 0)
    # 評估六大指標達標數（v8：6 指標僅作否決權，不再要求全過）
    six_keys = ['om', 'rg', 'npm', 'ep', 'inv', 'fcf']
    six_pass_cnt = sum(
        1 for k in six_keys
        if result['six'].get(k, {}).get('g') in ['AA', 'A', 'BB']
    )
    result['six_pass_cnt'] = six_pass_cnt  # 給 HTML 顯示用
    pe = result.get('pe_ratio') or 0
    curr_p = result.get('current_price') or 0

    risks = list(flags)
    if pe > 40: risks.append(f'PE {pe:.0f}倍，評價偏高')
    if result.get('revenue_growth') and result['revenue_growth'] < 0:
        risks.append(f'營收年增率 {result["revenue_growth"]*100:.1f}% 衰退')
    result['risks'] = risks if risks else ['無重大風險']

    # 結論使用 buffett_flags 自身的 verdict
    result['verdict']       = bf.get('verdict', 'N/A')
    result['verdict_cl']    = verdict_cl
    result['verdict_text']  = verdict_text
    result['red_count']    = red_count

    # === V6 新增：放空評估（8問不通過時）===
    if red_count >= 3:
        result['short_sell'] = short_sell_assessment(result, bf)
    else:
        result['short_sell'] = None

    # === V6 新增：技術關鍵價位 ===
    result['tech_levels'] = technical_levels(result)

    # 投資結論（v8：Buffett verdict 為主，6 指標為否決權）
    # 規則：buffett pass (0-1紅旗) + 6 指標 ≥4 達標 → ✅ 可考慮買入
    # v10.2 修正（2026-10-05，Boss 驗證）：集保「主力出貨」判斷移到下方 v9 整合結論後
    # 原因：shareholding_signal 是在此區塊後才被設定，順序問題
    # 先設「初步結論」，v9 整合結論完成後再依集保覆寫
    ep_v = result['six'].get('ep', {}).get('v')

    if mgmt_flag:
        result['conclusion'] = '🚨 不建議買入（管理誠信問題）'
        result['risk_level'] = '高'
        result['buy_price'] = 'N/A'; result['target_price'] = 'N/A'
    elif red_count >= 3:
        result['conclusion'] = f'❌ 不建議買入（{red_count}項紅旗）'
        result['risk_level'] = '高'
        result['buy_price'] = 'N/A'; result['target_price'] = 'N/A'
    elif verdict_cl == 'pass' and six_pass_cnt >= 4:
        # ✅ Buffett 通過 + 體質達標
        result['conclusion'] = '✅ 可考慮買入'
        result['risk_level'] = '低'
        if curr_p > 0 and ep_v:
            result['buy_price'] = round(curr_p * 0.85)
            result['target_price'] = round(ep_v * 20) if ep_v else 'N/A'
        else:
            result['buy_price'] = 'N/A'; result['target_price'] = 'N/A'
    elif verdict_cl == 'pass':
        # ⚠️ Buffett 通過但 6 指標 < 4 → 體質偏弱
        result['conclusion'] = f'⚠️ 觀望（6 指標僅 {six_pass_cnt}/6 達標）'
        result['risk_level'] = '中'
        result['buy_price'] = 'N/A'; result['target_price'] = 'N/A'
    else:
        # verdict_cl == 'warn'（紅旗剛好 2 個）
        result['conclusion'] = '⚠️ 觀望（2 項紅旗，需進一步分析）'
        result['risk_level'] = '中'
        result['buy_price'] = 'N/A'; result['target_price'] = 'N/A'

    # === v9 新增：整合結論（Buffett + 6 指標 + 集保訊號三為一體）==
    # v9 修：移到集保段「之後」生成（需要讀 shareholding）
    # 此處只保留「占位」，實際生成在 line ~1396

    # === 營收 YoY（顯示全部已公布月份）==============================
    # 計算並回傳每個已公布月份的 YoY，給 HTML 直接顯示
    # 建立 mr_with_yoy = [(month, yoy_pct or None), ...] 供 HTML 使用
    mr = result.get('monthly_revenue', {})
    cy_s = str(datetime.now().year)
    all_months = [m for m in range(1, 13) if mr.get(cy_s, {}).get(str(m)) is not None]
    yoy_all = []
    for m in all_months:
        m_s = str(m)
        cur = mr.get(cy_s, {}).get(m_s)
        ly = mr.get(str(int(cy_s) - 1), {}).get(m_s)
        if cur and ly and ly > 0:
            yoy_all.append((m, round((cur - ly) / ly * 100, 1)))
        else:
            yoy_all.append((m, None))
    result['rev_yoy_all'] = yoy_all  # [(month, yoy or None), ...]

    # === v9 新增：集保「主力出貨給散戶」訊號 ===
    weekly = db_get_weekly_shareholding(stock, weeks=4)
    if weekly:
        result['shareholding_weekly'] = weekly  # 近 4 週原始資料
        result['shareholding_signal'] = analyze_mainboard_distribution(weekly)
        # v9 修：設 result['shareholding'] 讓整合結論邏輯可以使用
        result['shareholding'] = result['shareholding_signal']
        sig = result['shareholding_signal']
        print(f'  [v9 集保] {sig["signal"]} | {sig["reason"]}')
    else:
        # 查無集保也設空 dict，避免整合結論 KeyError
        result['shareholding'] = {}
        result['shareholding_signal'] = None
        print(f'  [v9 集保] {code}: DB 查無集保資料（4 週內）')

    # === v9 新增：整合結論（Buffett + 6 指標 + 集保訊號三為一體）==
    # v9 修：移到集保段「之後」生成（需要讀 shareholding）
    sh = result.get('shareholding', {})
    sh_signal = sh.get('signal', '')
    sh_risk = sh.get('risk', '')
    is_distribution = '出貨' in sh_signal
    is_accumulation = '集貨' in sh_signal
    sh_dir = sh_signal.replace('🔴 ', '').replace('🟢 ', '').replace('⚪ ', '')

    buffett_pass = (verdict_cl == 'pass')
    body_strong = (six_pass_cnt >= 4)

    lines = []
    # 1) 基本面結語
    if buffett_pass and body_strong:
        lines.append(f'基本面「{result["six"]["assessment"]}」（巴菲特 {red_count} 紅旗，6 指標 {six_pass_cnt}/6 達標）')
    elif buffett_pass:
        lines.append(f'基本面「普通」（巴菲特 {red_count} 紅旗，但 6 指標僅 {six_pass_cnt}/6 達標，體質偏弱）')
    else:
        lines.append(f'基本面「{result["six"]["assessment"]}」（巴菲特 {red_count} 紅旗，需進一步分析）')

    # 2) 集保訊號結語
    if is_distribution:
        big_chg = sh.get('big_shares_chg', 0)
        retail_chg = sh.get('retail_shares_chg', 0)
        lines.append(f'集保訊號「{sh_dir}」（大戶 {big_chg:,} 股 / 散戶 +{retail_chg:,} 股）')
    elif is_accumulation:
        big_chg = sh.get('big_shares_chg', 0)
        retail_chg = sh.get('retail_shares_chg', 0)
        lines.append(f'集保訊號「{sh_dir}」（大戶 +{big_chg:,} 股 / 散戶 {retail_chg:,} 股）')
    else:
        lines.append(f'集保訊號「{sh_dir}」（未見明顯主力動態）')

    # 3) 整合判斷（三為一體）──────────────────────────────────────
    if buffett_pass and body_strong and is_distribution:
        big_mag = abs(sh.get('big_shares_chg', 0)) / 1e6
        if big_mag >= 5:
            lines.append('🟡 整合判斷：基本面極佳 + 主力嚴重出貨 = 「利多兌現」pattern，建議等拉回再進場，不追高')
        else:
            lines.append('🟡 整合判斷：基本面可買 + 主力出貨 = 「利多兌現」疑慮，建議小量試單或觀望')
    elif buffett_pass and body_strong and is_accumulation:
        lines.append('🟢 整合判斷：基本面佳 + 主力集貨 = 「雙重驗證」，是進場好時機')
    elif buffett_pass and body_strong:
        lines.append('🟢 整合判斷：基本面佳 + 集保中性 = 可進場，但留意主力後續動態')
    elif not buffett_pass and is_distribution:
        lines.append('🔴 整合判斷：基本面有疑慮 + 主力出貨 = 建議觀望或避開')
    elif is_accumulation:
        lines.append('🟡 整合判斷：雖然基本面有紅旗，但主力集貨中 → 可能是逆勢布局，値得深入研究')
    else:
        lines.append('⚪ 整合判斷：建議以巴菲特 + 體質結論為主，集保為輔助參考')

    result['integrated_verdict'] = '\n'.join(lines)
    result['integrated_verdict_lines'] = lines

    # === v10.2 修正（2026-10-05，Boss 驗證）===
    # 最終結論集成集保訊號（避免「主力出貨仍建議買入」矛盾）
    # 規範：
    #   集保🔴出貨 + 基本面佳 → 降為「⚠️ 觀望（等集保翻正）」
    #   集保🔴出貨 + 基本面弱 → 「❌ 不建議買入（+ 主力出貨）」
    #   集保🟢集貨 + 基本面佳 → 維持「✅ 可考慮買入」
    #   集保🟢集貨 + 基本面弱 → 不額外加分（保持原結論）
    if is_distribution and '✅' in result.get('conclusion', ''):
        # 基本面佳但主力出貨 → 降為觀望
        result['conclusion'] = '⚠️ 觀望（基本面佳但主力出貨，等集保翻正再說）'
        result['risk_level'] = '中'
        result['buy_price'] = 'N/A'
        result['target_price'] = 'N/A'
        # 在 lines 末尾補註
        lines.append('🔻 v10.2 最終覆寫：避免「利多兌現」後被套牢，集保轉好再進場')
    elif is_distribution and '⚠️' in result.get('conclusion', ''):
        # 原本就是觀望，集保出貨使「觀望」理由更重，但不改語氣
        result['conclusion'] = result['conclusion'].replace('⚠️ 觀望', '⚠️ 避開')
        lines.append('🔻 v10.2 最終覆寫：集保出貨 + 本來就觀望 → 直接升級為「避開」')
    elif is_distribution and '❌' not in result.get('conclusion', '') and '🚨' not in result.get('conclusion', ''):
        # 其他狀況（如體質普通） + 集保出貨 → 不建議買入
        result['conclusion'] = '❌ 不建議買入（主力出貨 + ' + result['conclusion'].replace('⚠️ ', '').replace('✅ ', '') + '）'
        lines.append('🔻 v10.2 最終覆寫：集保出貨是獨立紅旗，獨立升級結論')
    # is_accumulation 不覆寫（不額外加分，避免過度樂觀）

    # 🆕 v10：盈餘品質評分（5 維度，A+/A/B/C/D）
    try:
        from earnings_quality import analyze as eq_analyze
        code_clean = stock.replace('.TW', '').replace('.TWO', '')
        eq_result = eq_analyze(code_clean, verbose=False)
        result['earnings_quality'] = eq_result
        eq_grade = eq_result.get('grade', 'N/A')
        eq_score = eq_result.get('total_score', 0)
        print(f'  [v10 盈餘品質] {eq_grade}（{eq_score}/10）')
    except Exception as e:
        result['earnings_quality'] = {'error': str(e)}
        print(f'  ⚠️ 盈餘品質計算失敗: {e}')

    return result


# ===== HTML =====
def build_html(data):
    with open('/Users/kt/Bot/stock-reports-template/index_v10.html', 'r', encoding='utf-8') as f:
        h = f.read()

    h = h.replace('{STOCK_NAME}', data.get('stock_name', data['stock_code']))
    h = h.replace('{REPORT_DATE}', data['fetch_time'])

    # 🆕 v10：盈餘品質評分渲染區塊
    eq = data.get('earnings_quality', {})
    if eq and not eq.get('error'):
        eq_grade_icon = eq.get('grade_icon', '⚪')
        eq_grade = eq.get('grade', 'N/A')
        eq_total = eq.get('total_score', 0)
        eq_quarter = eq.get('quarter', '')
        eq_prev_q = eq.get('prev_year_quarter', '')

        # 5 維度評分卡
        eq_cards_html = []
        labels_map = [
            ('A', '營運槓桿', 'A_operating_leverage', '淨利 YoY vs 營收 YoY'),
            ('B', '費用率趨勢', 'B_opex_trend', '營業費用率 YoY'),
            ('C', 'R&D 強度', 'C_rd_intensity', '研發費佔營收比'),
            ('D', '業外佔比', 'D_nonop_ratio', '業外損益佔淨利比'),
            ('E', '毛利率穩定', 'E_margin_stability', '近 4 季標準差'),
        ]
        for letter, label, key, desc in labels_map:
            s = eq.get('scores', {}).get(key, {})
            score = s.get('score', 0)
            note = s.get('note', '')
            color = '#10b981' if score > 0 else ('#ef4444' if score < 0 else '#9ca3af')
            eq_cards_html.append(f'''
                <div class="eq-card" style="border-left: 4px solid {color}; padding: 10px; margin: 6px 0; background: rgba(255,255,255,0.04);">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <strong style="color: {color};">{letter}. {label}（{score:+d}）</strong>
                        <small style="color: #9ca3af;">{desc}</small>
                    </div>
                    <div style="color: #d1d5db; margin-top: 4px; font-size: 13px;">{note}</div>
                </div>
            ''')
        eq_cards = ''.join(eq_cards_html)
        h = h.replace('{EQ_GRADE_ICON}', eq_grade_icon)
        h = h.replace('{EQ_GRADE}', eq_grade)
        h = h.replace('{EQ_TOTAL}', str(eq_total))
        h = h.replace('{EQ_QUARTER}', eq_quarter)
        h = h.replace('{EQ_PREV_QUARTER}', eq_prev_q)
        h = h.replace('{EQ_CARDS}', eq_cards)
        # 🆕 B-1 (2026-08-11)：earnings_quality.py 改用 None 表示缺資料，
        # 這裡渲染時加 `or 0` fallback 避免 f-string 格式化 None 爆 TypeError
        h = h.replace('{EQ_REV_YOY}', f"{eq.get('revenue_yoy_pct') or 0:+.2f}")
        h = h.replace('{EQ_NI_YOY}', f"{eq.get('ni_yoy_pct') or 0:+.2f}")
        h = h.replace('{EQ_OPEX_RATE_CURR}', f"{eq.get('opex_rate_curr') or 0:.2f}")
        h = h.replace('{EQ_OPEX_RATE_PREV}', f"{eq.get('opex_rate_prev') or 0:.2f}")
        h = h.replace('{EQ_NONOP_RATIO}', f"{eq.get('nonop_ratio_curr') or 0:.2f}")
        h = h.replace('{EQ_GM_CURR}', f"{eq.get('gross_margin_curr') or 0:.2f}")
        h = h.replace('{EQ_GM_PREV}', f"{eq.get('gross_margin_prev') or 0:.2f}")
    else:
        # 沒資料時隱藏區塊
        for tag in ['{EQ_GRADE_ICON}', '{EQ_GRADE}', '{EQ_TOTAL}', '{EQ_QUARTER}',
                    '{EQ_PREV_QUARTER}', '{EQ_CARDS}', '{EQ_REV_YOY}', '{EQ_NI_YOY}',
                    '{EQ_OPEX_RATE_CURR}', '{EQ_OPEX_RATE_PREV}', '{EQ_NONOP_RATIO}',
                    '{EQ_GM_CURR}', '{EQ_GM_PREV}']:
            h = h.replace(tag, 'N/A')

    # 最新報價（v9.2 修：fallback 用 realtime_price → DB 最後一筆收盤）
    p = data.get('current_price') or 0
    if not p:
        p = data.get('realtime_price') or 0
    if not p:
        # 最後 fallback：kline_60d 最後一筆
        kl_fb = data.get('kline_60d', [])
        if kl_fb: p = kl_fb[-1].get('close') or 0
    h = h.replace('{CURRENT_PRICE}', f'{p:.2f}' if p else 'NA')
    kl = data.get('kline_60d', [])
    if len(kl) >= 2:
        curr = kl[-1]['close']
        prev = kl[-2]['close']
        chg = curr - prev
        chg_pct = (chg / prev * 100) if prev else 0
        chg_cls = 'up' if chg >= 0 else 'down'
        h = h.replace('{PRICE_CHANGE}', f"{'+' if chg >= 0 else ''}{chg:.2f} ({chg_pct:+.1f}%)")
        h = h.replace('{PRICE_CHANGE_CLASS}', chg_cls)
    elif data.get('realtime_price') and data.get('current_price'):
        # 沒 K 線但有即時價 → 用即時 - DB 最後一筆
        curr = data['realtime_price']
        prev = data['current_price']
        chg = curr - prev
        chg_pct = (chg / prev * 100) if prev else 0
        chg_cls = 'up' if chg >= 0 else 'down'
        h = h.replace('{PRICE_CHANGE}', f"{'+' if chg >= 0 else ''}{chg:.2f} ({chg_pct:+.1f}%)")
        h = h.replace('{PRICE_CHANGE_CLASS}', chg_cls)
    else:
        h = h.replace('{PRICE_CHANGE}', 'NA')
        h = h.replace('{PRICE_CHANGE_CLASS}', 'up')
    # v9.1：成交量 / 20日均量 股 → 張（除以 1000）
    def vol_to_zhang(v):
        try:
            return f"{int(round(v / 1000)):,}"
        except: return 'NA'
    vol = data.get('volume') or 0
    if not vol:
        vol = data.get('realtime_volume') or 0  # v9.2 fallback
    if not vol and data.get('kline_60d'):
        vol = data['kline_60d'][-1].get('volume') or 0  # v9.2 fallback
    ma20v = data.get('ma20_vol')
    h = h.replace('{VOLUME}', vol_to_zhang(vol) if vol else 'NA')
    h = h.replace('{MA20_VOL}', vol_to_zhang(ma20v) if ma20v else 'NA')

    # K線60天
    kl = data.get('kline_60d', [])
    h = h.replace('{PRICE_LABELS}', json.dumps([d['date'][5:] for d in kl]))
    h = h.replace('{PRICE_DATA}', json.dumps([d['close'] for d in kl]))

    # 成交量（只留MA20）
    h = h.replace('{VOL_LABELS}', json.dumps([d['date'][5:] for d in kl]))
    h = h.replace('{VOL_DATA}', json.dumps([d['volume'] for d in kl]))
    ma20 = data.get('ma20_vol') or 0
    h = h.replace('{MA20D}', json.dumps([ma20] * len(kl)))
    h = h.replace('{MA5D}', json.dumps([0] * len(kl)))
    h = h.replace('{MA10D}', json.dumps([0] * len(kl)))
    h = h.replace('{MA60D}', json.dumps([0] * len(kl)))
    h = h.replace('{VOL_COLORS}', json.dumps([
        ('rgba(78,205,196,0.6)' if (lambda c_prev, c_curr: (c_curr is not None and c_prev is not None and c_curr >= c_prev))(
            kl[i-1]['close'] if i > 0 else kl[i]['close'],
            kl[i]['close']
        ) else 'rgba(255,107,107,0.6)')
        if kl[i].get('close') is not None else 'rgba(128,128,128,0.3)'
        for i in range(len(kl))
    ]))

    # 月營收（fix: 用原始值算YoY）
    mr = data.get('monthly_revenue', {})
    cy_s = str(datetime.now().year)
    ly_s = str(datetime.now().year - 1)

    def fmt_rev(v):
        if v is None: return 'NA'
        b = float(v) / 1_000_000_000  # FinMind 回傳「千元」→ 轉十億
        return f'{b:.2f}B'

    def yoy(yr_s, m_s):
        c = mr.get(yr_s, {}).get(m_s)
        p = mr.get(str(int(yr_s) - 1), {}).get(m_s)  # 前一年
        if c and p and p > 0:
            return round((c - p) / p * 100, 1)
        return None

    rev_rows = ''
    yoy_all = data.get('rev_yoy_all', [])  # [(month, yoy or None), ...]
    yoy_by_yr = {}
    if yoy_all:
        yoy_by_yr[cy_s] = {m: y for m, y in yoy_all}
        ly_s = str(int(cy_s) - 1)
        ly_mr = mr.get(ly_s, {})
        ly_prev = mr.get(str(int(ly_s) - 1), {})
        ly_yoy = {}
        for m_s in [str(m) for m in range(1, 13)]:
            cur = ly_mr.get(m_s)
            p = ly_prev.get(m_s)
            if cur and p and p > 0:
                ly_yoy[int(m_s)] = round((cur - p) / p * 100, 1)
            else:
                ly_yoy[int(m_s)] = None
        yoy_by_yr[ly_s] = ly_yoy

    for yr_s in [cy_s, ly_s]:
        months = mr.get(yr_s, {})
        cells = ''.join([f'<td>{fmt_rev(months.get(str(m)))}</td>' for m in range(1, 13)])
        yoy_map = yoy_by_yr.get(yr_s, {})
        yoy_cells = ''
        for m in range(1, 13):
            y = yoy_map.get(m)
            if y is not None:
                cls = 'yoy-pos' if y >= 0 else 'yoy-neg'
                yoy_cells += f'<td><span class="{cls}">{y:+.1f}%</span></td>'
            else:
                yoy_cells += '<td>—</td>'
        # v9.1 對調：左 YoY 年增率 | 右 當月營收（十億）
        rev_rows += f'<tr><td class="year-cell">{yr_s}</td>{yoy_cells}{cells}</tr>'
    h = h.replace('{REVENUE_ROWS}', rev_rows)

    # 投資數據總覽（近三年：目前年份, -1, -2）
    eps = data.get('eps', {})
    hl = data.get('price_hl', {})
    pe_h = data.get('pe_hl', {})
    now = datetime.now()
    cy = now.year

    def eps_note(yr):
        """決定 EPS 標示：年報 / Q1+Q2 推估 / Q1 推估"""
        # 嘗試從財務資料偵測季度數
        fin = data.get('financials_raw', [])
        yr_qs = {}
        for row in fin:
            d = row.get('date', '')
            if not d or len(d) < 7: continue
            ryr = int(d[:4]); qm = int(d[5:7])
            if ryr != yr: continue
            qtr = (qm - 1) // 3 + 1
            t = row.get('type')
            if t == 'EPS':
                yr_qs[qtr] = yr_qs.get(qtr, 0) + float(row.get('value') or 0)
        # 若全年累加等於已公佈的年度 EPS，視為年報
        annual_eps = eps.get(str(yr))
        q1_val = yr_qs.get(1, 0)
        q2_val = yr_qs.get(2, 0)
        q3_val = yr_qs.get(3, 0)
        q4_val = yr_qs.get(4, 0)
        current_month = now.month if yr == cy else 12
        if yr == cy:
            if current_month >= 11 or (q3_val > 0 and q4_val > 0):
                return '年報', annual_eps
            elif q2_val > 0 and current_month >= 8:
                return 'Q1+Q2 推估', round(q1_val + q2_val, 2)
            elif q1_val > 0 and current_month >= 5:
                return 'Q1 推估', round(q1_val, 2)
        return '年報', annual_eps

    inv_rows = ''
    # v9.1：先插入近 52 週 資料行（4 季 EPS + 52 週高/低/PE）
    if data.get('eps_4q') and data.get('price_52w') and data.get('pe_52w'):
        ep4 = data['eps_4q']
        p52 = data['price_52w']
        pe52 = data['pe_52w']
        inv_rows += (
            f'<tr class="row-52w"><td class="year-cell">近52週</td>'
            f'<td>{ep4["sum"]:.2f} <small class="note">近{ep4["quarters"]}季</small></td>'
            f'<td>{p52["high"]}</td><td>{p52["low"]}</td>'
            f'<td>{pe52["high_pe"]}</td><td>{pe52["low_pe"]}</td></tr>'
        )
    for yr in [cy, cy - 1, cy - 2]:
        yr_s = str(yr)
        note_str, eps_val = eps_note(yr)
        d_hl = hl.get(yr_s, {})
        d_pe = pe_h.get(yr_s, {})
        high = d_hl.get('high', '—')
        low = d_hl.get('low', '—')
        high_pe = d_pe.get('high_pe', '—')
        low_pe = d_pe.get('low_pe', '—')
        if eps_val is not None:
            eps_str = f'{eps_val:.2f} <small class="note">{note_str}</small>'
        else:
            eps_str = '—'
        inv_rows += f'<tr><td class="year-cell">{yr_s}</td><td>{eps_str}</td><td>{high}</td><td>{low}</td><td>{high_pe}</td><td>{low_pe}</td></tr>'
    h = h.replace('{INVESTMENT_TABLE}', inv_rows)

    # 六大指標（新順序）
    six = data.get('six', {})
    grade_icon = {'AA': '🟢', 'A': '🟡', 'BB': '🟠', 'B': '🔴', 'C': '⚫', None: '⚪'}
    grade_cls = {'AA': 'aa', 'A': 'a', 'BB': 'bb', 'B': 'b', 'C': 'c', None: 'c'}
    # 新顯示順序：營利率、營收YoY、淨利率、EPS、存貨周轉、FCF
    indicators = [
        ('營業利益率', 'om', '%'),
        ('營收 YoY', 'rg', '%'),
        ('稅後淨利率', 'npm', '%'),
        ('EPS', 'ep', '元'),
        ('存貨周轉率', 'inv', '次'),
        ('自由現金流量', 'fcf', 'NTD'),
    ]
    def fmt_val(val, unit):
        if val is None: return 'NA'
        if unit == '%': return f'{val:.1f}%'
        if unit == '元': return f'{val:.2f}元'
        if unit == '次': return f'{val:.2f}次'
        if unit == 'NTD':  # 兆元格式化
            t = val / 1e12
            if abs(t) >= 1: return f'{t:.2f}兆'
            b = val / 1e9
            return f'{b:.1f}十億'
        return str(val)
    si_cards = ''
    for label, key, unit in indicators:
        v = six.get(key, {})
        val = v.get('v'); grd = v.get('g')
        val_s = fmt_val(val, unit)
        grd_s = grade_icon.get(grd, '⚪') + ' ' + (grd or 'NA')
        grd_cl = grade_cls.get(grd, 'c')
        good = grd in ['AA', 'A', 'BB']
        si_cards += f'''<div class="six-card">
            <div class="label">{label}</div>
            <div class="value">{'✅ ' if good else '❌ '}{val_s}</div>
            <div class="grade {grd_cl}">{grd_s}</div>
        </div>'''

    h = h.replace('{SIX_CARDS}', si_cards)

    total = six.get('total', 0)
    assessment = six.get('assessment', 'NA')
    h = h.replace('{SIX_TOTAL}', f"{total}/30 {assessment}")

    # 基本面
    def pct(v):
        if v is None: return 'NA'
        if isinstance(v, float) and 0 < abs(v) <= 1: return f'{v*100:.1f}%'
        return f'{v:.2f}' if isinstance(v, float) else str(v)
    h = h.replace('{PE_RATIO}', f"{data.get('pe_ratio',0):.1f}" if data.get('pe_ratio') else 'NA')
    h = h.replace('{ROE}', pct(data.get('roe')))
    h = h.replace('{ROA}', pct(data.get('roa')))
    rg = data.get('revenue_growth')
    h = h.replace('{REV_GROWTH}', pct(rg) if rg else 'NA')
    h = h.replace('{REV_GROWTH_CLS}', 'up' if rg and rg > 0 else 'down')
    h = h.replace('{OP_MARGIN}', pct(data.get('operating_margin')))
    h = h.replace('{DYIELD}', pct(data.get('dividend_yield')))
    # v10.2 修正（2026-10-05，Boss 驗證）：殖利率異常時加註說明，避免誤判
    yield_warn = data.get('dividend_yield_warning', '')
    if yield_warn:
        h = h.replace('<div class="value">{DYIELD}</div>', f'<div class="value">{{DYIELD}}</div><small style="color:#eab308;">{yield_warn}</small>')

    # 巴菲特紅旗（新結構）
    bf = data.get('buffett_flags', {})
    if isinstance(bf, dict):
        flags       = bf.get('flags', [])
        verdict     = bf.get('verdict', 'N/A')
        verdict_cl  = bf.get('verdict_cl', 'warn')
        verdict_txt = bf.get('verdict_text', '')
        eight_q     = bf.get('eight_q', [])
        moat        = bf.get('moat', [])
    else:
        # 相容舊格式（list）
        flags       = bf if isinstance(bf, list) else []
        verdict     = data.get('verdict', 'N/A')
        verdict_cl  = 'warn'
        verdict_txt = ''
        eight_q     = []
        moat        = []

    # {BUFFETT_8Q} — 8問表格
    def rf_icon(rf):
        return '<span class="red-flag">🚩</span>' if rf else '<span class="pass">✅</span>'
    eight_q_html = ''
    for i, item in enumerate(eight_q, 1):
        eight_q_html += f"<tr><td>{i}</td><td class='q-col'>{item.get('q','')}</td><td class='answer-col'>{item.get('answer','NA')}</td><td class='flag-col'>{rf_icon(item.get('red_flag',False))}</td></tr>"
    h = h.replace('{BUFFETT_8Q}', eight_q_html)

    # {BUFFETT_MOAT} — 護城河網格
    def moat_strength_cl(s):
        return {'強': 'strong', '中': 'medium', '弱': 'weak', '無': 'none', '是': 'none', '否': 'strong'}.get(s, 'none')
    moat_html = ''
    for m in moat:
        scl = moat_strength_cl(m.get('strength', ''))
        moat_html += f"<div class='moat-card'><div class='moat-type'>{m.get('type','')}</div><div class='moat-strength {scl}'>{m.get('strength','')}</div><div style='font-size:0.72em;color:#888;margin-top:4px'>{m.get('note','')}</div></div>"
    h = h.replace('{BUFFETT_MOAT}', moat_html)

    # {BUFFETT_FLAGS} — 紅旗詳解列表
    flag_items = ''.join([f"<div class='flag-item'>{f}</div>" for f in flags]) if flags else "<div class='flag-item no-flag'>✅ 無重大紅旗</div>"
    h = h.replace('{BUFFETT_FLAGS}', flag_items)

    # {VERDICT_CLASS} / {VERDICT_ICON} / {VERDICT_TEXT}
    h = h.replace('{VERDICT_CLASS}', verdict_cl)
    v_icon = '✅' if verdict_cl == 'pass' else '⚠️' if verdict_cl == 'warn' else '❌' if verdict_cl == 'fail' else '🚨'
    h = h.replace('{VERDICT_ICON}', v_icon)
    h = h.replace('{VERDICT_TEXT}', verdict_txt)
    h = h.replace('{VERDICT}', verdict)  # 向後相容

    # 結論
    h = h.replace('{CONCLUSION}', data.get('conclusion', 'N/A'))

    # === v9 新增：整合結論三行填入 ===
    lines = data.get('integrated_verdict_lines', [])
    int_basic = lines[0] if len(lines) > 0 else 'N/A'
    int_sh    = lines[1] if len(lines) > 1 else 'N/A'
    int_intg  = lines[2] if len(lines) > 2 else 'N/A'
    # HTML escape：保留中文 / emoji，僅轉換 < > & " '
    import html as _html
    h = h.replace('{INT_BASIC}', _html.escape(int_basic))
    h = h.replace('{INT_SHAREHOLDING}', _html.escape(int_sh))
    h = h.replace('{INT_INTEGRATED}', _html.escape(int_intg))
    h = h.replace('{BUY_PRICE}', str(data.get('buy_price', 'N/A')))
    h = h.replace('{TARGET_PRICE}', str(data.get('target_price', 'N/A')))
    h = h.replace('{RISK_LEVEL}', data.get('risk_level', 'N/A'))

    # 風險
    risk_items = ''.join([f'<li>⚠️ {r}</li>' for r in data.get('risks', [])])
    h = h.replace('{RISK_LIST}', risk_items or '<li>✅ 無重大風險</li>')

    # === V6: 技術關鍵價位 ===
    tl = data.get('tech_levels', {}) or {}
    h = h.replace('{TECH_HIGH52}', str(tl.get('high52', 'N/A')))
    h = h.replace('{TECH_LOW52}', str(tl.get('low52', 'N/A')))
    h = h.replace('{TECH_PRICE}', str(tl.get('price', 'N/A')))
    h = h.replace('{TECH_MA20}', str(tl.get('ma20', 'N/A')))
    h = h.replace('{TECH_MA60}', str(tl.get('ma60', 'N/A')))
    h = h.replace('{TECH_M20_HIGH}', str(tl.get('m20_high', 'N/A')))
    h = h.replace('{TECH_M20_LOW}', str(tl.get('m20_low', 'N/A')))
    h = h.replace('{TECH_DROP}', str(tl.get('drop_from_high', 'N/A')))

    # === V6: 放空評估 ===
    ss = data.get('short_sell')
    if ss:
        total_s = ss['total']
        color_cls = 'green' if total_s >= 70 else 'yellow' if total_s >= 50 else 'red'
        bar_w = min(100, total_s)
        bar_color = '#4ecdc4' if total_s >= 70 else '#fbbf24' if total_s >= 50 else '#ff6b6b'
        score_cls = {'green': 'lo', 'yellow': 'mid', 'red': 'hi'}[color_cls]

        def ss_score_tag(score):
            c = 'hi' if score >= 7 else 'mid' if score >= 4 else 'lo'
            return f"<span class='short-item-score {c}'>{score}/10</span>"

        short_html = f"""
    <div class="card">
        <h2>📉 放空評估（8問不通過）</h2>
        <div class="short-sell-score {color_cls}">{total_s}</div>
        <div style="text-align:center;color:#888;font-size:0.82em;">放空適合度評分（滿分100）</div>
        <div class="short-bar"><div class="short-bar-fill" style="width:{bar_w}%;background:{bar_color};"></div></div>
        <div class="short-items">
            <div class="short-item"><span class="short-item-label">護城河缺失</span>{ss_score_tag(ss['moat_score'])}</div>
            <div class="short-item"><span class="short-item-label">ROA 偏低</span>{ss_score_tag(ss['roa_score'])}</div>
            <div class="short-item"><span class="short-item-label">負債率過高</span>{ss_score_tag(ss['de_score'])}</div>
            <div class="short-item"><span class="short-item-label">營業利益率低</span>{ss_score_tag(ss['om_score'])}</div>
            <div class="short-item"><span class="short-item-label">產業趨勢向下</span>{ss_score_tag(ss['ind_score'])}</div>
        </div>
        <div class="short-verdict {color_cls}">{ss['recommendation']} {'| 目標：' + str(ss['short_target']) if ss['short_target'] != 'N/A' else ''}</div>
    </div>
"""
    else:
        short_html = ''
    h = h.replace('{SHORT_SELL_SECTION}', short_html)

    # === V6: 執行計劃 ===
    curr_p = data.get('current_price', 0) or 0
    tech = data.get('tech_levels', {}) or {}
    low52 = tech.get('low52', 0) or curr_p
    ma20 = tech.get('ma20', 0) or curr_p
    verdict_cl = data.get('verdict_cl', '')
    red_count = data.get('red_count', 0)

    # 根據紅旗數動態設定執行計劃
    if verdict_cl == 'fail' or red_count >= 3:
        entry = f"{low52:.0f}~{low52*1.05:.0f}" if low52 else 'N/A'
        stop = f"{low52*0.95:.0f}" if low52 else 'N/A'
        t1 = f"{low52*1.10:.0f}" if low52 else 'N/A'
        t2 = f"{low52*1.15:.0f}" if low52 else 'N/A'
        cap = '總部位 10%'
        notice = '八大通不通過，嚴控停損紀律'
        t1n = '+10%' ; t2n = '+15%'
    elif verdict_cl == 'pass':
        entry = f"{curr_p*0.90:.0f}~{curr_p:.0f}" if curr_p else 'N/A'
        stop = f"{curr_p*0.92:.0f}" if curr_p else 'N/A'
        t1 = str(data.get('target_price', 'N/A'))
        t2 = 'N/A'
        cap = '總部位 20%'
        notice = '八大問通過，等拉回佈局'
        t1n = '目標價' ; t2n = ''
    else:  # warn or neutral
        # v10.2 修正（2026-10-05，Boss 驗證）：
        # - 進場區間反寫 → 改為「現價 ~ MA20」（下緣到上緣才合理）
        # - 停損 0.95×MA20 太緊 → 改為 curr_p × 0.92（或 MA20 × 0.90，兩者取寬鬆）
        # - 倉位 15% → 10%（單一上限）
        # - 執行計劃需與「觀望」結論一致：明確標註「僅觀察、不進場」
        if curr_p and ma20:
            low = min(curr_p, ma20)
            high = max(curr_p, ma20)
            entry = f'{low:.0f}~{high:.0f}'
            stop = f'{curr_p*0.92:.0f}'  # 距現價 -8%，給成長股喘息空間
            # 目標價：相對「現價」的漲幅（非相對 MA20）
            t1_price = curr_p * 1.15  # +15% 對現價（拉回進場的合理目標）
            t2_price = curr_p * 1.23  # +23% 對現價
            t1 = f'{t1_price:.0f}'
            t2 = f'{t2_price:.0f}'
            t1_upside = (t1_price - curr_p) / curr_p * 100
            t2_upside = (t2_price - curr_p) / curr_p * 100
            t1n = f'對現價 +{t1_upside:.1f}%'
            t2n = f'對現價 +{t2_upside:.1f}%'
        else:
            entry = 'N/A'; stop = 'N/A'; t1 = 'N/A'; t2 = 'N/A'
            t1n = ''; t2n = ''
        cap = '總部位 10%（單一上限）'
        notice = '結論：觀望不進場；執行計劃僅供觀察，等集保翻正再說'

    h = h.replace('{EXEC_ENTRY}', entry)
    h = h.replace('{EXEC_STOP}', stop)
    h = h.replace('{EXEC_TARGET1}', t1)
    h = h.replace('{EXEC_TARGET2}', t2)
    h = h.replace('{EXEC_CAP}', cap)
    h = h.replace('{EXEC_NOTICE}', notice)
    h = h.replace('{EXEC_T1NOTE}', t1n)
    h = h.replace('{EXEC_T2NOTE}', t2n)

    # === v8 新增：6 指標體質摘要 ===
    spc = data.get('six_pass_cnt', 0)
    h = h.replace('{SIX_PASS_CNT}', str(spc))

    # JSON_URL/JSON_NAME 由 main() 二次替換，不預設

    # ===== v9 新增：即時價量 + 集保訊號 替換 =====
    # 即時價量
    rt_price = data.get('realtime_price') or 0
    rt_volume = data.get('realtime_volume') or 0
    rt_csv_mtime = data.get('realtime_csv_mtime') or 'N/A'
    rt_chg = data.get('realtime_chg_pct')
    rt_chg_str = f'{rt_chg:+.2f}%' if rt_chg is not None else 'N/A'
    rt_chg_cls = 'up' if (rt_chg or 0) >= 0 else 'down'
    last_db = data.get('last_db_date') or 'N/A'
    h = h.replace('{REALTIME_PRICE}', f'{rt_price:.2f}' if rt_price else 'N/A')
    h = h.replace('{REALTIME_VOLUME}', f'{int(round(rt_volume/1000)):,}張' if rt_volume else 'N/A')
    h = h.replace('{REALTIME_CHG_PCT}', rt_chg_str)
    h = h.replace('{REALTIME_CHG_CLS}', rt_chg_cls)
    h = h.replace('{REALTIME_CSV_MTIME}', rt_csv_mtime)
    h = h.replace('{REALTIME_SCHEDULE}', REALTIME_SCHEDULE)
    h = h.replace('{LAST_DB_DATE}', last_db)

    # 集保訊號
    sh = data.get('shareholding_signal')
    if sh and sh.get('signal') and sh['signal'] != 'N/A':
        # 顏色與樣式
        sig_text = sh['signal']
        if '出貨' in sig_text:
            sh_signal_cls = 'fail'
            sh_border = 'rgba(255,107,107,0.4)'
            sh_risk_color = '#ff6b6b'
        elif '集貨' in sig_text:
            sh_signal_cls = 'pass'
            sh_border = 'rgba(78,205,196,0.4)'
            sh_risk_color = '#4ecdc4'
        else:
            sh_signal_cls = 'warn'
            sh_border = 'rgba(249,168,37,0.3)'
            sh_risk_color = '#f9a825'

        retail_chg = sh['retail_shares_chg'] or 0
        big_chg = sh['big_shares_chg'] or 0
        h = h.replace('{SH_SIGNAL}', sig_text)
        h = h.replace('{SH_SIGNAL_CLS}', sh_signal_cls)
        h = h.replace('{SH_BORDER_COLOR}', sh_border)
        h = h.replace('{SH_REASON}', sh['reason'])
        h = h.replace('{SH_WEEKS}', str(sh['weeks']))
        h = h.replace('{SH_FIRST_DATE}', sh['first_date'])
        h = h.replace('{SH_LAST_DATE}', sh['last_date'])
        h = h.replace('{SH_RETAIL_CHG}', f'{retail_chg:+,}')
        h = h.replace('{SH_RETAIL_HOLDERS_CHG}', f'{sh["retail_holders_chg"]:+}')
        h = h.replace('{SH_BIG_CHG}', f'{big_chg:+,}')
        h = h.replace('{SH_BIG_HOLDERS_CHG}', f'{sh["big_holders_chg"]:+}')
        h = h.replace('{SH_RETAIL_CLS}', 'up' if retail_chg > 0 else 'down')
        h = h.replace('{SH_BIG_CLS}', 'up' if big_chg > 0 else 'down')
        h = h.replace('{SH_RISK}', sh['risk'])
        h = h.replace('{SH_RISK_COLOR}', sh_risk_color)
    else:
        # 集保資料不足，隱藏卡片（用樣式隱藏）
        no_data_html = '''
        <div class="verdict-text" style="color:#888;">集保資料不足（DB 查無近 4 週）</div>
        '''
        h = h.replace('{SH_SIGNAL}', 'N/A')
        h = h.replace('{SH_SIGNAL_CLS}', 'warn')
        h = h.replace('{SH_BORDER_COLOR}', 'rgba(255,255,255,0.1)')
        h = h.replace('{SH_REASON}', '集保資料不足')
        h = h.replace('{SH_WEEKS}', '0')
        h = h.replace('{SH_FIRST_DATE}', 'N/A')
        h = h.replace('{SH_LAST_DATE}', 'N/A')
        h = h.replace('{SH_RETAIL_CHG}', 'N/A')
        h = h.replace('{SH_RETAIL_HOLDERS_CHG}', 'N/A')
        h = h.replace('{SH_BIG_CHG}', 'N/A')
        h = h.replace('{SH_BIG_HOLDERS_CHG}', 'N/A')
        h = h.replace('{SH_RETAIL_CLS}', '')
        h = h.replace('{SH_BIG_CLS}', '')
        h = h.replace('{SH_RISK}', 'N/A')
        h = h.replace('{SH_RISK_COLOR}', '#888')

    return h


# ===== 上傳 =====
def upload(fname, content):
    url = f'https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{fname}'
    headers = {'Authorization': f'token {GITHUB_TOKEN}', 'Accept': 'application/vnd.github.v3+json'}
    r = requests.get(url, headers=headers, timeout=10)
    sha = r.json().get('sha') if r.status_code == 200 else None
    payload = {'message': f'Update: {fname}', 'content': base64.b64encode(content.encode('utf-8')).decode(), 'branch': BRANCH}
    if sha: payload['sha'] = sha
    r = requests.put(url, headers=headers, json=payload, timeout=15)
    return r.status_code in [200, 201]

def telegram(msg):
    requests.post(f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage',
                  json={'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'HTML'}, timeout=10)

def check_pages_deployment():
    """🆕 v9.2: 查 GitHub Pages 部署狀態，避免 build 卡住造成 404 但腳本回報成功"""
    try:
        # 查最新 build 狀態
        url = f'https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/pages/builds/latest'
        headers = {'Authorization': f'token {GITHUB_TOKEN}', 'Accept': 'application/vnd.github.v3+json'}
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return True, f'Pages API 查詢失敗 (HTTP {r.status_code})，跳過驗證'
        info = r.json()
        status = info.get('status', 'unknown')  # building / built / errored
        if status == 'built':
            return True, f"Pages build OK ({info.get('duration', 0)}s)"
        elif status == 'building':
            return False, f"Pages build 卡在 building（{info.get('created_at', '')}）"
        elif status == 'errored':
            return False, f"Pages build errored（{info.get('error', {}).get('message', '')}）"
        else:
            return True, f'Pages 狀態: {status}'
    except Exception as e:
        return True, f'Pages 查詢例外: {e}'

def main(stock='2330.TW'):
    print(f'📊 分析股票 (v10): {stock}')
    data = collect(stock)
    print(f'  資料源: {data.get("data_source", "v8 default")}')
    print(f'  股價: {data.get("current_price")}')
    print(f'  即時價 (CSV): {data.get("realtime_price")} | CSV mtime: {data.get("realtime_csv_mtime")}')
    print(f'  月營收年份: {list(data["monthly_revenue"].keys())}')
    print(f'  EPS: {dict(sorted(data["eps"].items(), key=lambda x:int(x[0]), reverse=True))}')
    print(f'  六大指標: ')
    for k, v in data['six'].items():
        print(f'    {k}: {v}')
    print(f'  巴菲特紅旗: {data["buffett_flags"]}')
    print(f'  集保訊號 (v10): {data.get("shareholding_signal")}')
    print(f'  結論: {data["conclusion"]} | {data["verdict"]}')
    ss = data.get('short_sell')
    if ss: print(f'  放空評分: {ss["total"]} → {ss["recommendation"]}')
    tl = data.get('tech_levels', {})
    if tl: print(f'  技術價位: 52W高{tl.get("high52")} / 低{tl.get("low52")} / MA20{tl.get("ma20")}')

    html = build_html(data)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    safe = stock.replace('.', '_')
    html_file = f'{safe}_v10_{ts}.html'
    json_file = f'{safe}_v10_{ts}.json'

    ok1 = upload(html_file, html)
    ok2 = upload(json_file, json.dumps(data, indent=2, ensure_ascii=False))
    json_url = f'https://{REPO_OWNER}.github.io/{REPO_NAME}/{json_file}'
    html = html.replace('{JSON_URL}', json_url).replace('{JSON_NAME}', json_file)
    ok3 = upload(html_file, html)  # 🆕 捕捉第三次上傳回傳值（v9.2 修正）

    html_url = f'https://{REPO_OWNER}.github.io/{REPO_NAME}/{html_file}'
    
    # 🆕 v9.2 修正：完整檢查三次上傳 + Pages 部署狀態
    all_ok = ok1 and ok2 and ok3
    if all_ok:
        print(f'✅ 報告已發佈: {html_url}')
        # 主動查 Pages 部署狀態（避免 build 卡住產生 404）
        pages_ok, pages_msg = check_pages_deployment()
        if not pages_ok:
            print(f'⚠️ Pages 部署異常: {pages_msg}')
            print(f'   → HTML 已上傳但 Pages 可能 404，請去 GitHub 後台查 build 狀態')
    else:
        print(f'❌ 上傳失敗 (HTML初:{ok1} / JSON:{ok2} / HTML覆寫:{ok3})')

    # 🆕 v10：TG 自動推播（盈餘品質評分 + 結論摘要 + HTML 連結）
    try:
        eq = data.get('earnings_quality', {})
        if eq and not eq.get('error'):
            eq_line = f"{eq.get('grade_icon', '⚪')} {eq.get('grade', 'N/A')}（{eq.get('total_score', '?')}/10）"
            dim_lines = []
            for label, key in [('A營運槓桿', 'A_operating_leverage'),
                                ('B費用率', 'B_opex_trend'),
                                ('C R&D', 'C_rd_intensity'),
                                ('D業外', 'D_nonop_ratio'),
                                ('E毛利率', 'E_margin_stability')]:
                s = eq.get('scores', {}).get(key, {})
                dim_lines.append(f"  · {label}（{s.get('score', 0):+d}）{s.get('note', '')[:50]}")
            eq_block = "\n".join(dim_lines)
        else:
            eq_line = "N/A"
            eq_block = "  · 季度資料不足（需 ≥ 2 季）"

        tg_msg = (
            f"🍎 {data['stock_code']} {data.get('stock_name', '')} v10 分析\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 結論：{data.get('conclusion', 'N/A')}\n"
            f"💎 盈餘品質：{eq_line}\n"
            f"{eq_block}\n"
            f"\n💰 股價：{data.get('current_price', '?')}\n"
            f"📄 HTML：{html_url}\n"
            f"⏰ {data.get('fetch_time', '')}"
        )
        telegram(tg_msg)
        print(f'  ✅ TG 推播完成')
    except Exception as e:
        print(f'  ⚠️ TG 推播失敗: {e}')

    return html_url, data

if __name__ == '__main__':
    import sys
    main(sys.argv[1] if len(sys.argv) > 1 else '2330.TW')



