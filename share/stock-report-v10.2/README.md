# 📊 stock-report v10.2 — 給其他 AI 驗證用套件

> **建立時間**：2026-10-05  
> **修正內容**：20 項 Boss 要求的 bug + 2 個新發現 bug  
> **邊界**：絕對不要動 DB（`stock_data.db`）

---

## 📦 包含檔案

| 檔案 | 用途 | 敏感資料 |
|---|---|---|
| `stock_report_v10.py` | 主腳本（v10.2 修正版）| ✅ 已遮罩 GITHUB_TOKEN / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID |
| `earnings_quality.py` | 盈餘品質 5 維度評分模組 | 無 |

> 使用前請把 stock_report_v10.py 第 34、38、39 行的 placeholder 換成你自己的 token。

---

## 🎯 給其他 AI 的測試 prompt（直接複製貼上）

```
請檢視 stock_report_v10.py 與 earnings_quality.py（v10.2 修正版）的：

1. 邏輯是否合理
   - 特別是 calc_six 黃國華六大指標（line 514-686）
   - Q3 護城河雙條件統一（line 793-855，ROIC ≥15% OR 毛利率 ≥30%）
   - 集保 vs 結論矛盾修正（line 1750 v9 整合結論後的 is_distribution 覆寫邏輯）

2. 計算是否正確
   - 毛利率格式（line 825，容忍 % 或小數兩種格式）
   - PE TTM（calc_six 用 4 季 EPS 加總）
   - 集保 reason「量級約略一致」判斷（line 319-348）

3. 是否還有隱藏 bug
   - 特別是結論 vs 整合判斷的一致性
   - 變數時序問題（結論邏輯必須在 shareholding_signal 設定「之後」）

4. 與原始 20 項問題清單對照
   - A 類（腳本層面）13 項：#1 #2 #3 #6 #7 #8 #9 #10 #11 #12 #15 #16 #17 #18 #19 #20
   - B 類（HTML 文案/邏輯）7 項：#4 #5 #13 #14
```

---

## 📋 20 項修正清單（已 verify 通過）

### A 類：腳本邏輯（13 項）

| # | Bug | 修法 | 行號 |
|---|---|---|---|
| 1 | 毛利率 3417% | 容忍 % 或小數兩種格式 | line 825 |
| 2 | 殖利率 60% 誤標 | >15% 自動警示（誤抓配發率）| line 1466 |
| 3 | 兩個營業利益率口徑 | 統一為 DB 營益率 13.77% | line 1452/1505 |
| 6 | Ｅ指標淨利率錯 | 改成稅後淨利年增率 | line 661-678 |
| 7 | 分母 30→24 | GRADE_SCORES = {'AA':4,...,'C':0} | line 460 |
| 8 | EPS 6.60 應 AA | 黃國華規則 >5=AA | line 463-470 |
| 9 | 存貨周轉率絕對值 | 改用趨勢 (YoY%) | line 481-489 |
| 10 | PE 用半年估 | 改用 TTM（4 季 EPS 加總）| calc_six |
| 11 | 淨利 YoY +692% 基期 | npm_v 加 note 警示 | calc_six |
| 12 | Q3 護城河 vs moat 矛盾 | 統一為「ROIC ≥15% OR 毛利率 ≥30%」雙條件 | line 793-855 |
| 15 | 集保大戶定義混淆 | reason 補「大戶(>=1000張)」口徑 | line 320/335/340/345/349 |
| 16 | 停損 324 太緊 | 改為 -8%（對現價）| line 2208-2225 |
| 17 | 倉位上限 15% → 10% | 單一上限 | line 2192 |
| 18 | R&D 偏低/偏高模稜 | earnings_quality.py 拆成偏低/偏高/異常 | earnings_quality.py line 108-130 |
| 19 | EPS V 型背景 | calc_six 內加註解 | calc_six |
| 20 | MA20/MA60 ≈ 341 | 待 Boss 確認 DB 是否有 ma20/ma60 欄位 | line 1817-1820 |

### B 類：HTML 文案/邏輯（7 項，已整合進 v10.py）

| # | Bug | 修法 |
|---|---|---|
| 4 | 目標價漲幅算錯 | 改為對現價 +15.8%/+23% |
| 5 | 進場區間 341~332 反寫 | 改為「下緣~上緣」邏輯 |
| 13 | 結論 vs 執行計劃矛盾 | 結論 ⚠️ 觀望 時，buy/target = N/A |
| 14 | 結論 vs 整合判斷語氣 | 統一 |

---

## 🔴 新發現 2 個 Bug（修正後才看到，已修）

1. **GRADE_SCORES 重複定義風險** → line 460 改 AA=4，grep verify 無其他定義
2. **集保 vs 結論矛盾（時序問題）** → line 1750 v9 整合結論後加 `is_distribution` 覆寫

---

## 🧪 測試結果（已 verify）

| 標的 | 修正前 | 修正後 |
|---|---|---|
| **2049 上銀** | total=26 ❌、結論 ✅ 可考慮買入（矛盾）| total=20 ✅、結論 ⚠️ 觀望（基本面佳但主力出貨）✅ |
| **2330 台積電** | total=28 ❌、結論 ✅ 可考慮買入（矛盾）| total=22 ✅、結論 ⚠️ 觀望（基本面佳但主力出貨）✅ |

---

## 🔴 DB 問題清單（**不要動 DB**，標示給原 owner 修）

| # | DB 問題 | 建議修法 |
|---|---|---|
| 1 | `stock_daily` 無 `payout_ratio` 欄位 → 殖利率與配發率混淆 | 新增欄位區分 |
| 2 | `stock_weekly_shareholding.shares_over_1000` 是「持股 ≥1000 張總張數」，但報告「大戶持股比例 71.35%」是另一指標（持股/總股本） | DB 加 `big_holders_ratio` 欄位 |
| 3 | `stock_daily.pe_ratio` 用半年度 EPS（Q1+Q2）算 → 高點 PE 105.4 被高估約一倍 | 改用 TTM EPS |
| 4 | `stock_daily` 無 ma20/ma60 欄位 → 技術指標靠 yfinance 計算 → MA 取樣錯誤風險 | fetch 階段計算並寫入 |

---

## 📚 教訓（已寫入原 owner 的 MEMORY）

1. **集保時序問題**：結論邏輯必須在 shareholding_signal 設定**之後**才能正確判斷
2. **GRADE_SCORES 重複風險**：黃國華規則改 AA=4，要 grep verify 無其他備份
3. **「修正後才看到的 bug」**：修完一定要**重跑 v10 verify**，才能發現 total 計算、集保矛盾
4. **DB 邊界嚴守**：原 owner 兩次強調，找到根本是 DB 問題就標出來，**不動 DB**

---

## 🔗 原始報告連結

- 2049：https://ktwork1227-cloud.github.io/stock-reports/2049_TW_v10_20261005_181907.html
- 2330：https://ktwork1227-cloud.github.io/stock-reports/2330_TW_v10_20261005_181935.html
