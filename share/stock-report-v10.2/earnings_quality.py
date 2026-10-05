#!/usr/bin/env python3
"""
盈餘品質評分模組（Earnings Quality Score）
用途：拆解個股季報的「常規 vs 一次性」獲利結構，補 v9 報告未涵蓋的盈餘品質面
作者：小蘋果 🍎
版本：v1.1（2026-08-11）— 補 None 保護，處理「半季資料缺失」情境
DB：唯讀查詢 stock_data.db

評分維度（每項 -2~+2 分，raw 範圍 -10~+10，正規化 0~10）：
  A. 營運槓桿（淨利 YoY vs 營收 YoY）
  B. 營業費用率趨勢（YoY）
  C. R&D 投資強度（健康範圍）
  D. 業外收入佔比（一次性 vs 常規）
  E. 毛利率穩定性（近 4 季標準差）

評級：
  9~10 = A+ 🟢🟢 優異
  7~8  = A  🟢   穩健
  5~6  = B  🟡   中性
  3~4  = C  🟠   警訊
  0~2  = D  🔴   危險

【v1.1 修補】
- 修 Bug：score_opex_trend / analyze 主函數 result dict 沒保護 NULL 欄位
  → 2026 Q2 整批欄位缺資料時，None / float 觸發 TypeError
- 新增 _safe_pct() helper：None / 0 安全處理
- 新增 _has_fields() helper：dict 必填欄位檢查
- 缺資料維度回傳 0 分（中性）+ 標註「OO 缺失」
- ANSI 約定：缺資料欄位 result dict 填 None（不再 round() 避免二次爆）
"""

import sqlite3
import sys
from datetime import datetime

DB_PATH = '/Users/kt/Python/stock_project/data/stock_data.db'


def _has_fields(d, *keys):
    """檢查 dict 是否存在且所有指定欄位都不是 None"""
    if not d:
        return False
    return all(d.get(k) is not None for k in keys)


def _safe_pct(num, den):
    """安全百分比：num / den * 100，缺資料回傳 None"""
    if num is None or den is None or den == 0:
        return None
    return num / den * 100


def get_quarterly_data(code: str):
    """取近 8 季損益表 raw data"""
    conn = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT year, season, revenue, gross_profit, operating_expenses,
               rd_expenses, operating_income, non_operating_income,
               net_income, eps, gross_margin, operating_margin
        FROM stock_income_statement
        WHERE code = ?
        ORDER BY year DESC, season DESC
        LIMIT 8
    """, (code,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def score_operating_leverage(curr, prev):
    """A. 營運槓桿：淨利增速 vs 營收增速（差距越大越正向）"""
    if not _has_fields(curr, 'revenue', 'net_income') or not _has_fields(prev, 'revenue', 'net_income'):
        return 0, "資料缺失（營收/淨利）"
    rev_growth = (curr['revenue'] - prev['revenue']) / prev['revenue'] * 100
    ni_growth = (curr['net_income'] - prev['net_income']) / prev['net_income'] * 100
    diff = ni_growth - rev_growth
    if diff > 10:
        return 2, f"正向槓桿強（淨利 +{ni_growth:.1f}% vs 營收 +{rev_growth:.1f}%, 差 {diff:+.1f}pp）"
    elif diff > 0:
        return 1, f"正向槓桿（淨利 +{ni_growth:.1f}% vs 營收 +{rev_growth:.1f}%, 差 {diff:+.1f}pp）"
    elif diff > -5:
        return 0, f"中性（淨利 +{ni_growth:.1f}% vs 營收 +{rev_growth:.1f}%, 差 {diff:+.1f}pp）"
    else:
        return -1, f"負向槓桿（淨利 +{ni_growth:.1f}% vs 營收 +{rev_growth:.1f}%, 差 {diff:+.1f}pp）⚠️"


def score_opex_trend(curr, prev):
    """B. 營業費用率 YoY"""
    if not _has_fields(curr, 'operating_expenses', 'revenue') or not _has_fields(prev, 'operating_expenses', 'revenue'):
        return 0, "資料缺失（營業費用）"
    curr_rate = curr['operating_expenses'] / curr['revenue'] * 100
    prev_rate = prev['operating_expenses'] / prev['revenue'] * 100
    diff = curr_rate - prev_rate
    if diff < -2:
        return 2, f"費用率大幅下降（{prev_rate:.2f}% → {curr_rate:.2f}%, {diff:+.2f}pp）🟢"
    elif diff < -0.5:
        return 1, f"費用率微降（{prev_rate:.2f}% → {curr_rate:.2f}%, {diff:+.2f}pp）"
    elif diff < 0.5:
        return 0, f"費用率持平（{prev_rate:.2f}% → {curr_rate:.2f}%, {diff:+.2f}pp）"
    elif diff < 2:
        return -1, f"費用率微升（{prev_rate:.2f}% → {curr_rate:.2f}%, {diff:+.2f}pp）"
    else:
        return -2, f"費用率飆升（{prev_rate:.2f}% → {curr_rate:.2f}%, {diff:+.2f}pp）🔴"


def score_rd_intensity(curr):
    """C. R&D 投資強度（科技股 5~10% 為健康；v10.2 拆開偏低/偏高，2026-10-05 Boss 驗證）"""
    if not _has_fields(curr, 'rd_expenses', 'revenue'):
        return 0, "資料缺失（R&D 費用）"
    if curr['revenue'] == 0:
        return 0, "營收為 0"
    rd_rate = curr['rd_expenses'] / curr['revenue'] * 100
    if 5 <= rd_rate <= 12:
        return 2, f"R&D 健康（{rd_rate:.2f}%，科技股理想區間）"
    elif rd_rate < 3:
        return 0, f"R&D 偏低（{rd_rate:.2f}%，創新動能不足，技術升級風險高）"
    elif 3 <= rd_rate < 5:
        return 1, f"R&D 偏低（{rd_rate:.2f}%，低於科技股建議 5%，但機械業可接受）"
    elif 12 < rd_rate <= 15:
        return 1, f"R&D 偏高（{rd_rate:.2f}%，高於建議 12%，轉型訊號但獲利承壓）"
    else:
        return 0, f"R&D 異常（{rd_rate:.2f}%，脫離合理區間，需查營收/費用分類）"


def score_nonop_ratio(curr):
    """D. 一次性業外佔比（佔淨利越低越穩健）"""
    if not _has_fields(curr, 'non_operating_income', 'net_income'):
        return 0, "資料缺失（業外/淨利）"
    if curr['net_income'] == 0:
        return 0, "淨利為 0"
    ratio = curr['non_operating_income'] / curr['net_income'] * 100
    if ratio < 15:
        return 2, f"常規獲利為主（業外佔 {ratio:.1f}%）🟢"
    elif ratio < 30:
        return 1, f"業外適度（佔 {ratio:.1f}%）"
    elif ratio < 50:
        return 0, f"業外偏高（佔 {ratio:.1f}%）⚠️"
    else:
        return -2, f"業外暴增（佔 {ratio:.1f}%）🔴"


def score_margin_stability(quarters):
    """E. 毛利率穩定性（近 4 季標準差）"""
    margins = [q['gross_margin'] for q in quarters[:4] if q['gross_margin'] is not None]
    if len(margins) < 3:
        return 0, "資料不足（毛利率）"
    mean = sum(margins) / len(margins)
    variance = sum((m - mean) ** 2 for m in margins) / len(margins)
    std = variance ** 0.5
    if std < 0.5:
        return 2, f"毛利率穩定（近 4 季 std={std:.2f}pp）"
    elif std < 1.5:
        return 1, f"毛利率略波動（std={std:.2f}pp）"
    else:
        return 0, f"毛利率劇烈波動（std={std:.2f}pp）⚠️"


def grade(total_score):
    """總分轉評級"""
    if total_score >= 9:
        return "A+", "🟢🟢"
    elif total_score >= 7:
        return "A", "🟢"
    elif total_score >= 5:
        return "B", "🟡"
    elif total_score >= 3:
        return "C", "🟠"
    else:
        return "D", "🔴"


def analyze(code: str, verbose: bool = False):
    """主分析：給定股票代碼，回傳 dict 含評分 + 細節"""
    quarters = get_quarterly_data(code)
    if len(quarters) < 2:
        return {'error': f'code={code} 季度資料不足（需 ≥ 2 季）'}

    curr = quarters[0]  # 最新一季
    prev_yoy = quarters[4] if len(quarters) >= 5 else quarters[-1]  # 去年同期

    scores = {}
    s_a, note_a = score_operating_leverage(curr, prev_yoy)
    s_b, note_b = score_opex_trend(curr, prev_yoy)
    s_c, note_c = score_rd_intensity(curr)
    s_d, note_d = score_nonop_ratio(curr)
    s_e, note_e = score_margin_stability(quarters)

    scores['A_operating_leverage'] = {'score': s_a, 'note': note_a}
    scores['B_opex_trend'] = {'score': s_b, 'note': note_b}
    scores['C_rd_intensity'] = {'score': s_c, 'note': note_c}
    scores['D_nonop_ratio'] = {'score': s_d, 'note': note_d}
    scores['E_margin_stability'] = {'score': s_e, 'note': note_e}

    # 0~10 換算：5 項每項 -2~+2 = raw 範圍 -10~+10 → 正規化 0~10
    raw_total = s_a + s_b + s_c + s_d + s_e
    total_score = (raw_total + 10) / 2  # range: 0~10
    grade_letter, grade_icon = grade(round(total_score, 1))

    # 計算輔助指標（缺資料時填 None，不 round() 避免二次爆）
    rev_yoy = _safe_pct(curr['revenue'] - prev_yoy['revenue'], prev_yoy['revenue'])
    ni_yoy = _safe_pct(curr['net_income'] - prev_yoy['net_income'], prev_yoy['net_income'])
    opex_rate_curr = _safe_pct(curr['operating_expenses'], curr['revenue'])
    opex_rate_prev = _safe_pct(prev_yoy['operating_expenses'], prev_yoy['revenue'])
    rd_rate_curr = _safe_pct(curr['rd_expenses'], curr['revenue'])
    nonop_ratio_curr = _safe_pct(curr['non_operating_income'], curr['net_income'])

    result = {
        'code': code,
        'quarter': f"{curr['year']}Q{curr['season']}",
        'prev_year_quarter': f"{prev_yoy['year']}Q{prev_yoy['season']}",
        'scores': scores,
        'total_score': total_score,
        'grade': grade_letter,
        'grade_icon': grade_icon,
        'revenue_yoy_pct': round(rev_yoy, 2) if rev_yoy is not None else None,
        'ni_yoy_pct': round(ni_yoy, 2) if ni_yoy is not None else None,
        'gross_margin_curr': curr['gross_margin'],
        'gross_margin_prev': prev_yoy['gross_margin'],
        'opex_rate_curr': round(opex_rate_curr, 2) if opex_rate_curr is not None else None,
        'opex_rate_prev': round(opex_rate_prev, 2) if opex_rate_prev is not None else None,
        'rd_rate_curr': round(rd_rate_curr, 2) if rd_rate_curr is not None else None,
        'nonop_ratio_curr': round(nonop_ratio_curr, 2) if nonop_ratio_curr is not None else None,
        'eps_curr': curr['eps'],
        'eps_prev': prev_yoy['eps'],
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }

    if verbose:
        print_pretty(result)

    return result


def print_pretty(r: dict):
    """CLI 友善輸出"""
    if 'error' in r:
        print(f"❌ {r['error']}")
        return

    def fmt_pct(v):
        return f"{v:+.2f}%" if v is not None else "N/A"

    print("=" * 60)
    print(f"📊 盈餘品質評分 — {r['code']} ({r['quarter']} vs {r['prev_year_quarter']})")
    print("=" * 60)
    print(f"評級：{r['grade_icon']} {r['grade']} （總分 {r['total_score']}/10）")
    print(f"營收 YoY：{fmt_pct(r['revenue_yoy_pct'])}")
    print(f"淨利 YoY：{fmt_pct(r['ni_yoy_pct'])}")
    if r['revenue_yoy_pct'] is not None and r['ni_yoy_pct'] is not None:
        print(f"營運槓桿：{r['ni_yoy_pct'] - r['revenue_yoy_pct']:+.2f}pp")
    print("-" * 60)

    labels = [
        ('A', '營運槓桿', 'A_operating_leverage'),
        ('B', '費用率趨勢', 'B_opex_trend'),
        ('C', 'R&D 強度', 'C_rd_intensity'),
        ('D', '業外佔比', 'D_nonop_ratio'),
        ('E', '毛利率穩定', 'E_margin_stability'),
    ]
    for code_letter, label, key in labels:
        s = r['scores'][key]
        icon = "🟢" if s['score'] > 0 else ("⚪" if s['score'] == 0 else "🔴")
        print(f"  {icon} {code_letter}. {label}（{s['score']:+d} 分）：{s['note']}")

    print("-" * 60)
    print(f"毛利率：{r['gross_margin_prev']:.2f}% → {r['gross_margin_curr']:.2f}%"
          if r['gross_margin_prev'] is not None and r['gross_margin_curr'] is not None
          else f"毛利率：{r['gross_margin_prev']} → {r['gross_margin_curr']}（資料缺失）")
    print(f"營業費用率：{fmt_pct(r['opex_rate_prev'])} → {fmt_pct(r['opex_rate_curr'])}")
    print(f"R&D 費用率：{fmt_pct(r['rd_rate_curr'])}")
    print(f"業外佔淨利：{fmt_pct(r['nonop_ratio_curr'])}")
    if r['eps_prev'] and r['eps_prev'] != 0:
        eps_chg = (r['eps_curr'] - r['eps_prev']) / r['eps_prev'] * 100
        print(f"EPS：{r['eps_prev']:.2f} → {r['eps_curr']:.2f}（{eps_chg:+.1f}%）")
    else:
        print(f"EPS：{r['eps_prev']:.2f} → {r['eps_curr']:.2f}")
    print("=" * 60)
    print(f"報告時間：{r['timestamp']}")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("用法：python3 earnings_quality.py <股票代碼>")
        print("範例：python3 earnings_quality.py 2395.TW")
        sys.exit(1)
    code = sys.argv[1].replace('.TW', '').replace('.TWO', '')
    analyze(code, verbose=True)
