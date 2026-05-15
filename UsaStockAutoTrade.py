# -*- coding: utf-8 -*-
"""
Quant Replacing the 40 - Fixed Trading Bot
Look-ahead bias 제거 및 백테스트 일치 버전
"""
import pandas as pd
from datetime import datetime, timezone, timedelta
import sys
import yaml
import logging
import requests
import os
import numpy as np
import yfinance as yf
import matplotlib.pyplot as plt
import json

# Discord notifications
from discord_notifier import get_notifier

# --- 경로 설정 ---
current_dir = os.path.dirname(__file__)
project_root = os.path.abspath(os.path.join(current_dir))
sys.path.append(os.path.join(project_root, 'open-trading-api', 'examples_llm'))
# Add paths for problematic imports
sys.path.append(os.path.join(project_root, 'open-trading-api', 'examples_llm', 'overseas_stock', 'inquire_balance'))
sys.path.append(os.path.join(project_root, 'open-trading-api', 'examples_llm', 'overseas_stock', 'inquire_psamount'))
sys.path.append(os.path.join(project_root, 'open-trading-api', 'examples_llm', 'overseas_stock', 'dailyprice'))
sys.path.append(os.path.join(project_root, 'open-trading-api', 'examples_llm', 'overseas_stock', 'order'))
sys.path.append(os.path.join(project_root, 'open-trading-api', 'examples_llm', 'overseas_stock', 'inquire_daily_chartprice'))
sys.path.append(os.path.join(project_root, 'open-trading-api', 'examples_llm', 'domestic_stock', 'inquire_account_balance'))
# -*- coding: utf-8 -*-
"""
Quant 60/40 Inverse ETF Strategy - Trading Bot
Implements the '60_40_Inverse_ETF_with_Cost' strategy from the backtest.
"""

# --- KIS API 모듈 임포트 ---
import kis_auth as ka
import order
import inquire_balance
import inquire_daily_chartprice as idc
import inquire_psamount
import inquire_account_balance as iab

# --- Position Tracking ---
from strategy_positions import get_tracker

# --- Portfolio Summary (for dual-strategy mode) ---
from portfolio_summary import send_portfolio_summary

# --- 로거 설정 ---
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

if not logger.handlers:
    file_handler = logging.FileHandler('daily_best_strategy_trader.log')
    stream_handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

class UsaStockTrader:
    def __init__(self):
        """초기화 함수"""
        self.config = self._load_config()
        self.strategy_params = self.config['quant_replacing_the_40']

        # Discord notifications (NEW: Using professional notifier)
        webhook_url = self.config.get("DISCORD_WEBHOOK_URL")
        self.notifier = get_notifier(webhook_url, bot_type='usa_stock')

        self.my_acct = None
        self.my_prod = None
        self.usd_krw_exchange_rate = None
        self.latest_prices = None

        # Dual Strategy Configuration
        dual_config = self.config.get('dual_strategy', {})
        self.dual_strategy_enabled = dual_config.get('ENABLED', False)

        # Capital allocation: read from centralized capital_management
        cap_mgmt = self.config.get('capital_management', {}).get('quant40', {})
        budget_usd = cap_mgmt.get('budget_usd', 0.0)
        # Second Level Bot allocation override
        sl_mult = self._load_allocation_override()
        budget_usd *= sl_mult
        if budget_usd > 0:
            self.allocation_mode = 'fixed'
            self.allocated_capital_fixed = budget_usd
            self.capital_allocation = None
            logger.info(f"QUANT40 Strategy using FIXED capital budget: ${self.allocated_capital_fixed:,.2f} (SL: {sl_mult:.0%})")
        else:
            # Fallback: no cap, use all available
            self.allocation_mode = 'percentage'
            self.capital_allocation = 1.0
            self.allocated_capital_fixed = None
            logger.info(f"QUANT40 Strategy using ALL available capital (no budget cap)")

        self.asset_list = [
            self.strategy_params['EQUITY_ASSET'],
            self.strategy_params['SAFE_ASSET'],
            self.strategy_params['INVERSE_EQUITY_ASSET']
        ]
        self.asset_exchange_map = {
            "SPY": "AMEX",
            "IEF": "NASD",
            "SH": "AMEX"
        }

        # Position tracker for dual strategy mode
        self.position_tracker = get_tracker()
        self.strategy_name = "QUANT40"

        # Results directory for portfolio summary
        self.results_dir = os.path.join(project_root, 'strategy_results')
        os.makedirs(self.results_dir, exist_ok=True)
        self.result_file = os.path.join(self.results_dir, 'quant40_result.json')

        self._authenticate()

    def _load_allocation_override(self):
        """Read allocation multiplier from SecondLevelBot. Returns 1.0 if stale or unavailable."""
        override_path = os.path.join(project_root, 'allocation_override.json')
        try:
            if os.path.exists(override_path):
                with open(override_path) as f:
                    data = json.load(f)
                ts = data.get('timestamp', '')
                if ts:
                    from datetime import datetime as dt, timezone
                    override_time = dt.fromisoformat(ts)
                    now = dt.now(override_time.tzinfo or timezone.utc)
                    age_hours = (now - override_time).total_seconds() / 3600
                    if age_hours > 2:
                        logger.warning(f"Second Level override is {age_hours:.1f}h old — using default 1.0")
                        return 1.0
                mult = data.get('allocation_multiplier', 1.0)
                regime = data.get('regime', 'NEUTRAL')
                logger.info(f"Second Level override: {regime} → allocation {mult:.0%}")
                return mult
        except Exception as e:
            logger.warning(f"Failed to read allocation override: {e}")
        return 1.0

    def _load_config(self):
        """config.yaml 파일에서 설정을 불러옵니다."""
        try:
            with open(os.path.join(project_root, 'config.yaml'), 'r') as file:
                return yaml.safe_load(file)
        except FileNotFoundError:
            logger.error("config.yaml 파일을 찾을 수 없습니다.")
            sys.exit(1)
        except yaml.YAMLError as e:
            logger.error(f"config.yaml 파일 파싱 오류: {e}")
            sys.exit(1)

    def _authenticate(self):
        """API 인증을 수행합니다."""
        try:
            ka.auth(svr="prod")
            logger.info("✅ API 인증 성공")
            acct_info = ka.getTREnv()
            self.my_acct, self.my_prod = acct_info.my_acct, acct_info.my_prod
            logger.info(f"계좌번호: {self.my_acct}, 상품코드: {self.my_prod}")
        except Exception as e:
            logger.error(f"❌ API 인증 실패: {e}")
            self.notifier.send_error("KIS API Authentication Failed", str(e))
            sys.exit(1)

    # OLD Discord method removed - now using self.notifier from discord_notifier.py

    def _send_discord_file(self, file_path, title):
        """디스코드 웹훅으로 파일을 전송합니다."""
        if not self.notifier.webhook_url:
            return
        try:
            with open(file_path, 'rb') as f:
                payload = {
                    "embeds": [{
                        "title": title,
                        "color": 12745742,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "image": {
                            "url": f"attachment://{os.path.basename(file_path)}"
                        }
                    }]
                }
                files = {'file': (os.path.basename(file_path), f, 'image/png')}
                response = requests.post(self.notifier.webhook_url, files=files, data={'payload_json': json.dumps(payload)})
                response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logger.error(f"Discord 파일 전송 실패: {e}")
        except FileNotFoundError:
            logger.error(f"차트 파일({file_path})을 찾을 수 없습니다.")

    def _create_spy_trend_chart(self, prices):
        """SPY 트렌드 차트를 생성하고 저장합니다."""
        try:
            plt.figure(figsize=(15, 7))
            
            prices.plot(label='SPY Price', color='black', linewidth=2)

            ma_windows = [(8, 32), (16, 64), (32, 128), (64, 256)]
            colors = ['#FF0000', '#00FF00', '#0000FF', '#FFFF00']
            linestyles = ['--', ':']

            for i, (short, long) in enumerate(ma_windows):
                short_ma = prices.rolling(window=short).mean()
                long_ma = prices.rolling(window=long).mean()
                short_ma.plot(label=f'MA({short})', color=colors[i], linestyle=linestyles[0], linewidth=1)
                long_ma.plot(label=f'MA({long})', color=colors[i], linestyle=linestyles[1], linewidth=1)

            plt.title('SPY Trend Signal Analysis')
            plt.ylabel('Price (USD)')
            plt.legend()
            plt.grid(True)
            plt.tight_layout()

            chart_path = os.path.join(project_root, 'spy_trend_chart.png')
            plt.savefig(chart_path)
            plt.close()
            logger.info(f"SPY 트렌드 차트 생성 완료: {chart_path}")
            return chart_path
        except Exception as e:
            logger.error(f"SPY 트렌드 차트 생성 실패: {e}")
            return None

    def _get_exchange_rate(self):
        """환율 정보를 가져옵니다."""
        try:
            exchange_rate_data = yf.download('USDKRW=X', period='1d')
            if not exchange_rate_data.empty:
                self.usd_krw_exchange_rate = exchange_rate_data['Close'].iloc[-1].item()
                logger.info(f"✅ yfinance USD/KRW 환율 정보 조회 성공: {self.usd_krw_exchange_rate:,.2f}")
            else:
                raise ValueError("yfinance 환율 정보 조회 실패")
        except Exception as e_yf:
            logger.error(f"❌ yfinance 환율 정보 조회 실패: {e_yf}, KIS API로 대체합니다.")
            today = datetime.now().strftime('%Y%m%d')
            try:
                df_exchange_rate, _ = idc.inquire_daily_chartprice(
                    fid_cond_mrkt_div_code='X',
                    fid_input_iscd='USD',
                    fid_input_date_1=today,
                    fid_input_date_2=today,
                    fid_period_div_code='D',
                    env_dv="real"
                )
                if not df_exchange_rate.empty:
                    self.usd_krw_exchange_rate = float(df_exchange_rate['ovrs_nmix_prpr'].iloc[0])
                    logger.info(f"✅ KIS API USD/KRW 환율 정보 조회 성공: {self.usd_krw_exchange_rate:,.2f}")
                else:
                    raise ValueError("KIS API 환율 정보 조회 실패")
            except Exception as e:
                logger.error(f"❌ KIS API 환율 정보 조회 실패: {e}")
                self.usd_krw_exchange_rate = 1450  # Fallback (updated 2026)
                logger.warning(f"기본 환율 {self.usd_krw_exchange_rate}을 사용합니다.")

    def _get_latest_prices(self):
        """최신 자산 가격을 가져옵니다."""
        logger.info("최신 자산 가격 조회 중...")
        self.latest_prices = yf.download(self.asset_list, period='1d', auto_adjust=False)['Adj Close']
        if self.latest_prices.empty:
            logger.error("❌ 가격 정보 조회 실패")
            self._send_discord_message("시스템 오류", fields=[{"name": "오류", "value": "자산 가격 정보 조회 실패"}], color=15158332)
            sys.exit(1)

    def get_account_summary(self):
        """계좌의 전체 잔고 정보를 가져옵니다. (USD + KRW 동시 지원)"""
        summary = {
            'stocks': [],
            'cash_balance_usd': 0.0,
            'cash_balance_krw': 0.0,
            'cash_balance_total_usd': 0.0,
            'total_value': 0.0,
            'total_profit': 0.0,
            'total_profit_krw': 0.0
        }
        try:
            # USD 잔고 조회
            output1_usd, _ = inquire_balance.inquire_balance(
                cano=self.my_acct,
                acnt_prdt_cd=self.my_prod,
                ovrs_excg_cd="NASD",
                tr_crcy_cd="USD",
                env_dv="real"
            )

            # KRW 잔고 조회
            output1_krw, _ = inquire_balance.inquire_balance(
                cano=self.my_acct,
                acnt_prdt_cd=self.my_prod,
                ovrs_excg_cd="NASD",
                tr_crcy_cd="KRW",
                env_dv="real"
            )

            total_stock_value = 0.0
            processed_tickers = set()
            all_holdings = []

            # USD 보유 종목 처리 - 먼저 ALL holdings 수집
            if output1_usd is not None and not output1_usd.empty:
                for _, row in output1_usd.iterrows():
                    ticker = row['ovrs_pdno']
                    if ticker in processed_tickers:
                        continue
                    processed_tickers.add(ticker)

                    quantity = int(row['ovrs_cblc_qty'])
                    purchase_amount = float(row.get('frcr_pchs_amt1', 0))
                    avg_price = purchase_amount / quantity if quantity > 0 else 0

                    # Use KIS API's real-time valuation (not yfinance T-1 close)
                    market_value = float(row.get('ovrs_stck_evlu_amt', 0))
                    profit = float(row.get('frcr_evlu_pfls_amt', 0))

                    if market_value > 0 and quantity > 0:
                        current_price = market_value / quantity
                        stock_value = market_value
                    else:
                        # Fallback to yfinance if KIS valuation missing
                        if ticker in self.latest_prices:
                            current_price = float(self.latest_prices[ticker].iloc[-1])
                        else:
                            current_price = avg_price
                        stock_value = current_price * quantity
                        profit = (current_price - avg_price) * quantity if quantity > 0 else 0

                    profit_krw = profit * self.usd_krw_exchange_rate

                    all_holdings.append({
                        'ticker': ticker,
                        'quantity': quantity,
                        'value': stock_value,
                        'avg_price': avg_price,
                        'current_price': current_price,
                        'profit': profit,
                        'profit_krw': profit_krw,
                        'profit_rate': (profit / purchase_amount * 100) if purchase_amount > 0 else 0,
                        'currency': 'USD'
                    })

            # KRW 보유 종목 처리
            if output1_krw is not None and not output1_krw.empty:
                for _, row in output1_krw.iterrows():
                    ticker = row['ovrs_pdno']
                    if ticker in processed_tickers:
                        continue
                    processed_tickers.add(ticker)

                    quantity = int(row['ovrs_cblc_qty'])
                    purchase_amount_krw = float(row.get('frcr_pchs_amt1', 0))
                    avg_price_usd = (purchase_amount_krw / self.usd_krw_exchange_rate) / quantity if quantity > 0 else 0

                    # Use KIS API's real-time valuation (not yfinance T-1 close)
                    market_value = float(row.get('ovrs_stck_evlu_amt', 0))
                    profit_raw = float(row.get('frcr_evlu_pfls_amt', 0))

                    if market_value > 0 and quantity > 0:
                        current_price = market_value / quantity
                        stock_value = market_value
                        profit = profit_raw
                    else:
                        # Fallback to yfinance if KIS valuation missing
                        if ticker in self.latest_prices:
                            current_price = float(self.latest_prices[ticker].iloc[-1])
                        else:
                            current_price = avg_price_usd
                        stock_value = current_price * quantity
                        profit = (current_price - avg_price_usd) * quantity if quantity > 0 else 0

                    profit_krw = profit * self.usd_krw_exchange_rate

                    all_holdings.append({
                        'ticker': ticker,
                        'quantity': quantity,
                        'value': stock_value,
                        'avg_price': avg_price_usd,
                        'current_price': current_price,
                        'profit': profit,
                        'profit_krw': profit_krw,
                        'profit_rate': (profit / (avg_price_usd * quantity) * 100) if (avg_price_usd * quantity) > 0 else 0,
                        'currency': 'KRW'
                    })

            # Calculate TOTAL stock value from ALL holdings (before filtering)
            total_all_stock_value = sum(stock['value'] for stock in all_holdings)

            # Filter holdings - only include positions owned by THIS strategy
            if self.dual_strategy_enabled:
                owned_holdings, other_positions_value = self.position_tracker.filter_account_holdings(
                    self.strategy_name, all_holdings
                )
                summary['stocks'] = owned_holdings
                summary['other_strategy_value'] = other_positions_value

                # Calculate values only from OUR positions (for display)
                for stock in owned_holdings:
                    total_stock_value += stock['value']
                    summary['total_profit'] += stock['profit']
                    summary['total_profit_krw'] += stock['profit_krw']

                logger.info(f"Position Filter: {len(owned_holdings)}/{len(all_holdings)} positions belong to {self.strategy_name}")
                if other_positions_value > 0:
                    logger.info(f"Other strategy positions value: ${other_positions_value:,.2f}")
            else:
                # No dual strategy - include all positions
                summary['stocks'] = all_holdings
                for stock in all_holdings:
                    total_stock_value += stock['value']
                    summary['total_profit'] += stock['profit']
                    summary['total_profit_krw'] += stock['profit_krw']
                total_all_stock_value = total_stock_value

            # USD 현금 잔고 조회
            df_cash_usd = inquire_psamount.inquire_psamount(
                cano=self.my_acct,
                acnt_prdt_cd=self.my_prod,
                ovrs_excg_cd="NAS",
                item_cd="IEF",
                ovrs_ord_unpr="0",
                env_dv="real"
            )
            if df_cash_usd is not None and not df_cash_usd.empty:
                summary['cash_balance_usd'] = float(df_cash_usd.iloc[0]['ord_psbl_frcr_amt'])

            # KRW 현금 잔고 조회 (국내 계좌 API 사용)
            try:
                _, df_cash_krw = iab.inquire_account_balance(
                    cano=self.my_acct,
                    acnt_prdt_cd=self.my_prod,
                )
                if df_cash_krw is not None and not df_cash_krw.empty:
                    cash_krw = float(df_cash_krw.iloc[0].get('dncl_amt', 0))
                    summary['cash_balance_krw'] = cash_krw
                    logger.info(f"KRW 현금 잔고 (국내 API): ₩{cash_krw:,.0f}")
                else:
                    logger.warning("KRW 잔고 정보가 없습니다 (국내 API).")
                    summary['cash_balance_krw'] = 0.0
            except Exception as e:
                logger.warning(f"KRW 현금 잔고 조회 실패 (국내 API): {e}")
                summary['cash_balance_krw'] = 0.0

            # 총 현금 잔고 (USD 기준)
            summary['cash_balance_total_usd'] = summary['cash_balance_usd'] + (summary['cash_balance_krw'] / self.usd_krw_exchange_rate)

            # Calculate total values
            summary['total_value'] = total_stock_value + summary['cash_balance_total_usd']

            # Apply capital allocation if dual strategy is enabled
            if self.dual_strategy_enabled:
                total_account_value = total_all_stock_value + summary['cash_balance_total_usd']

                if self.allocation_mode == 'fixed':
                    # Fixed dollar allocation (money input base)
                    summary['allocated_capital'] = self.allocated_capital_fixed
                    summary['available_capital'] = self.allocated_capital_fixed
                    allocation_pct = (self.allocated_capital_fixed / total_account_value * 100) if total_account_value > 0 else 0
                    logger.info(f"Dual Strategy Mode (FIXED): ${self.allocated_capital_fixed:,.2f} allocated to Quant40 Strategy ({allocation_pct:.1f}%)")
                else:
                    # Percentage-based allocation
                    summary['allocated_capital'] = total_account_value * self.capital_allocation
                    summary['available_capital'] = summary['allocated_capital']
                    logger.info(f"Dual Strategy Mode (PERCENTAGE): {self.capital_allocation*100:.0f}% allocation to Quant40 Strategy")

                logger.info(f"Total Account Value: ${total_account_value:,.2f}")
                logger.info(f"Allocated Capital (Quant40): ${summary['allocated_capital']:,.2f}")
            else:
                summary['allocated_capital'] = summary['total_value']
                summary['available_capital'] = summary['total_value']

            logger.info(f"계좌 요약 (USD+KRW): 총자산=${summary['total_value']:,.2f}, USD현금=${summary['cash_balance_usd']:,.2f}, KRW현금=₩{summary['cash_balance_krw']:,.0f}, 총손익=${summary['total_profit']:,.2f} (₩{summary['total_profit_krw']:,.0f})")
            return summary

        except Exception as e:
            logger.error(f"계좌 요약 정보 조회 오류: {e}")
            return None

    def get_latest_trend_signal(self):
        """트렌드 시그널을 계산합니다. (T-1: 전일 종가 기준)"""
        logger.info("=== 트렌드 시그널 계산 시작 ===")
        try:
            spy_data = yf.download(self.strategy_params['SIGNAL_ASSET'], period="400d", auto_adjust=False)
            if spy_data.empty:
                logger.error(f"{self.strategy_params['SIGNAL_ASSET']} 데이터 다운로드 실패")
                return None, None

            prices = spy_data['Adj Close'].squeeze() # Squeeze to ensure it's a Series
            cutoff_date = datetime.now() - timedelta(days=1)
            prices = prices[prices.index.date < cutoff_date.date()]
            logger.info(f"T-1 시그널 사용: {prices.index[-1].date()} 종가 데이터로 계산")

            ma_windows = [(8, 32), (16, 64), (32, 128), (64, 256)]
            ma_scores = [1 if prices.rolling(window=short).mean().iloc[-1] >= prices.rolling(window=long).mean().iloc[-1] else 0 for short, long in ma_windows]
            sums = sum(ma_scores)

            if sums == 4: trend_signal = 1.0
            elif sums == 3: trend_signal = 0.5
            elif sums == 2: trend_signal = 0.0
            elif sums == 1: trend_signal = -0.5
            else: trend_signal = -1.0

            signal_date = prices.index[-1]

            logger.info(f"시그널 계산 완료: 기준일={signal_date.date()}, 점수={sums}/4, 시그널={trend_signal}")
            return trend_signal, signal_date, prices

        except Exception as e:
            logger.error(f"시그널 계산 중 오류: {e}", exc_info=True)
            return None, None, None

    def calculate_target_positions(self, total_value, trend_signal, allocated_capital=None):
        """목표 포지션을 계산합니다. (Capital allocation 고려)"""
        logger.info("=== 목표 포지션 계산 ===")

        # Use allocated capital if dual strategy is enabled
        capital_to_use = allocated_capital if (allocated_capital is not None and allocated_capital > 0) else total_value

        spy_static_pct = self.strategy_params['SPY_ALLOCATION']
        trend_pct = self.strategy_params['TREND_ALLOCATION']

        spy_static_value = spy_static_pct * capital_to_use
        trend_portion_value = trend_pct * capital_to_use
        
        if trend_signal >= 0:
            spy_trend_value = trend_portion_value * trend_signal
            sh_value = 0.0
        else:
            spy_trend_value = 0.0
            sh_value = trend_portion_value * abs(trend_signal)
        
        target_spy = spy_static_value + spy_trend_value
        target_sh = sh_value

        logger.info(f"포트폴리오 총액: ${total_value:,.2f}")
        if self.dual_strategy_enabled and allocated_capital:
            # Calculate effective percentage for logging
            if self.allocation_mode == 'fixed':
                effective_pct = (capital_to_use / total_value * 100) if total_value > 0 else 0
                logger.info(f"Quant40 할당 자본: ${capital_to_use:,.2f} (Fixed: {effective_pct:.1f}%)")
            else:
                logger.info(f"Quant40 할당 자본: ${capital_to_use:,.2f} ({self.capital_allocation*100:.0f}%)")
        logger.info(f"SPY 목표: ${target_spy:,.2f} ({target_spy/capital_to_use*100:.1f}%)")
        logger.info(f"SH 목표: ${target_sh:,.2f} ({target_sh/capital_to_use*100:.1f}%)")

        return {'SPY': target_spy, 'SH': target_sh, 'IEF': 0.0}

    def place_order(self, side, ticker, quantity, price):
        """주문을 실행합니다. (Position tracking 포함)"""
        logger.info(f"{ticker} {quantity}주 {side.upper()} 주문 실행")

        # Check if we can trade this ticker (position ownership)
        if self.dual_strategy_enabled:
            can_trade, reason = self.position_tracker.can_trade(self.strategy_name, ticker)
            if not can_trade:
                logger.warning(f"⚠️ Cannot trade {ticker}: {reason}")
                self._send_discord_message(
                    "⚠️ **Trade Blocked - Position Ownership**",
                    fields=[
                        {"name": "Ticker", "value": ticker, "inline": True},
                        {"name": "Side", "value": side.upper(), "inline": True},
                        {"name": "Reason", "value": reason, "inline": False}
                    ],
                    color=16776960
                )
                return None

        try:
            order_price = str(round(price * (1.03 if side == 'buy' else 0.95), 2))
            exchange = self.asset_exchange_map.get(ticker, self.strategy_params['EXCHANGE_CODE'])

            df_order = order.order(
                cano=self.my_acct,
                acnt_prdt_cd=self.my_prod,
                ovrs_excg_cd=exchange,
                pdno=ticker,
                ord_qty=str(int(quantity)),
                ovrs_ord_unpr=order_price,
                ord_dv=side,
                ctac_tlno="",
                mgco_aptm_odno="",
                ord_svr_dvsn_cd="0",
                ord_dvsn="00", # 지정가
                env_dv="real"
            )
            logger.info(f"{side.upper()} 주문 결과:\n{df_order}")

            # Validate order was accepted before tracking
            order_ok = df_order is not None
            if isinstance(df_order, pd.DataFrame) and not df_order.empty:
                if 'rt_cd' in df_order.columns:
                    order_ok = str(df_order['rt_cd'].iloc[0]) == '0'

            # Register/update position ownership only on confirmed orders
            if self.dual_strategy_enabled and order_ok:
                if side == 'buy':
                    # Register or update position
                    self.position_tracker.register_position(
                        self.strategy_name, ticker, quantity, price
                    )
                elif side == 'sell':
                    # For sell orders, we need to track remaining quantity
                    # This will be handled in the main run() method after getting updated holdings
                    pass

            return df_order
        except Exception as e:
            logger.error(f"{side.upper()} 주문 실패: {e}")
            return None

    def _save_strategy_result(self, account_summary, trend_details, orders_executed):
        """Save strategy results to JSON file for portfolio summary"""
        try:
            # Calculate allocation percentage for display
            allocated_capital = account_summary.get('allocated_capital', 0)
            total_account_value = account_summary.get('total_value', 0)  # This might include other strategies' value
            allocation_pct = (allocated_capital / total_account_value) if total_account_value > 0 else 0

            result = {
                'strategy_name': self.strategy_name,
                'timestamp': datetime.now().isoformat(),
                'allocation_mode': self.allocation_mode,
                'capital_allocation': allocation_pct if self.allocation_mode == 'fixed' else self.capital_allocation,
                'allocated_capital': allocated_capital,
                'total_value': account_summary.get('total_value', 0),
                'cash_balance': account_summary.get('cash_balance_total_usd', 0),
                'total_profit': account_summary.get('total_profit', 0),
                'total_profit_krw': account_summary.get('total_profit_krw', 0),
                'holdings': [
                    {
                        'ticker': stock['ticker'],
                        'quantity': stock['quantity'],
                        'value': stock['value'],
                        'avg_price': stock['avg_price'],
                        'current_price': stock['current_price'],
                        'profit': stock['profit'],
                        'profit_krw': stock['profit_krw'],
                        'profit_rate': stock['profit_rate'],
                        'currency': stock['currency']
                    }
                    for stock in account_summary.get('stocks', [])
                ],
                'session_summary': {
                    'orders_executed': len(orders_executed) if orders_executed else 0,
                    'trend_signal': trend_details.get('signal_value'),
                    'trend_signal_date': trend_details.get('signal_date'),
                    'trend_description': trend_details.get('description'),
                    'target_allocations': trend_details.get('target_allocations'),
                    'current_allocations': trend_details.get('current_allocations'),
                    'usd_krw_rate': self.usd_krw_exchange_rate,
                    'dual_strategy_enabled': self.dual_strategy_enabled
                }
            }

            tmp = self.result_file + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(result, f, indent=2)
            os.replace(tmp, self.result_file)

            logger.info(f"Strategy result saved to {self.result_file}")

        except Exception as e:
            logger.error(f"Failed to save strategy result: {e}")

    def run(self):
        """자동매매 전략을 실행합니다."""
        self._get_exchange_rate()
        self._get_latest_prices()

        account_summary = self.get_account_summary()
        if not account_summary:
            self.notifier.send_error("Failed to retrieve account information", "Account summary returned None")
            return

        # Startup notification - skip in dual strategy mode (portfolio summary handles it)
        if not self.dual_strategy_enabled:
            self.notifier.send_startup(
                strategy_name="Quant Replacing the 40",
                config={
                    'mode': 'Real Trading',
                    'portfolio_value': account_summary['total_value'],
                    'allocation': "100% (Full Portfolio)"
                }
            )

        # Initial Portfolio Status message REMOVED - info now in startup notification

        trend_signal, signal_date, prices = self.get_latest_trend_signal()
        if trend_signal is None:
            self.notifier.send_error("Failed to calculate trend signal", "Trend signal calculation returned None")
            return

        # Use allocated capital if dual strategy is enabled
        allocated_capital = account_summary.get('allocated_capital', account_summary['total_value'])
        target_allocations = self.calculate_target_positions(
            account_summary['total_value'],
            trend_signal,
            allocated_capital if self.dual_strategy_enabled else None
        )

        current_allocations = {asset: 0.0 for asset in self.asset_list}
        for stock in account_summary['stocks']:
            if stock['ticker'] in current_allocations:
                current_allocations[stock['ticker']] = stock['value']

        # Determine trend description
        if trend_signal == 1.0:
            trend_desc = "🟢 Strong Bullish (4/4)"
        elif trend_signal == 0.5:
            trend_desc = "🟡 Bullish (3/4)"
        elif trend_signal == 0.0:
            trend_desc = "⚪ Neutral (2/4)"
        elif trend_signal == -0.5:
            trend_desc = "🟠 Bearish (1/4)"
        else:
            trend_desc = "🔴 Strong Bearish (0/4)"

        spy_static_pct = self.strategy_params['SPY_ALLOCATION']
        trend_pct = self.strategy_params['TREND_ALLOCATION']

        analysis_fields = [
            {"name": "📅 Signal Date (T-1)", "value": f"{str(signal_date.date())} (Yesterday's Close)", "inline": True},
            {"name": "📈 Trend Signal", "value": trend_desc, "inline": True},
            {"name": "⚖️ Strategy Allocation", "value": f"Static: {spy_static_pct*100:.0f}% SPY | Dynamic: {trend_pct*100:.0f}% Trend", "inline": False}
        ]

        # Show target vs current allocations
        allocation_details = []
        orders_to_execute = []
        min_trade_value = 100

        for asset in self.asset_list:
            target_value = target_allocations.get(asset, 0)
            current_value = current_allocations.get(asset, 0)
            price = float(self.latest_prices[asset].iloc[-1])
            diff_value = target_value - current_value

            target_pct = (target_value / account_summary['total_value'] * 100) if account_summary['total_value'] > 0 else 0
            current_pct = (current_value / account_summary['total_value'] * 100) if account_summary['total_value'] > 0 else 0

            allocation_details.append(
                f"**{asset}**: Current ${current_value:,.2f} ({current_pct:.1f}%) → Target ${target_value:,.2f} ({target_pct:.1f}%)"
            )

            if abs(diff_value) > max(price, min_trade_value):
                quantity = int(abs(diff_value) / price)
                side = 'buy' if diff_value > 0 else 'sell'
                orders_to_execute.append({'asset': asset, 'side': side, 'quantity': quantity, 'price': price, 'value': abs(diff_value)})

        # Market Analysis message REMOVED - will be included in daily summary

        if not orders_to_execute:
            logger.info("✅ 리밸런싱이 필요하지 않습니다.")
            # No trades = no trade notification (will show in daily summary)
        else:
            logger.info("🔄 리밸런싱 주문 실행 중...")

            sell_orders = [o for o in orders_to_execute if o['side'] == 'sell']
            buy_orders = [o for o in orders_to_execute if o['side'] == 'buy']

            # NEW: Collect trades for batched notification
            executed_trades = []
            failed_orders = []

            for order_info in sell_orders + buy_orders:
                asset, side, quantity, price = order_info['asset'], order_info['side'], order_info['quantity'], order_info['price']

                if side == 'sell':
                    current_qty = next((s['quantity'] for s in account_summary['stocks'] if s['ticker'] == asset), 0)
                    if quantity > current_qty:
                        logger.warning(f"⚠️ {asset} 매도 수량({quantity}) > 보유({current_qty}), 조정")
                        quantity = current_qty
                        if quantity == 0:
                            failed_orders.append(f"❌ {asset}: Insufficient shares to sell")
                            continue

                if quantity > 0:
                    result = self.place_order(side, asset, quantity, price)
                    order_price = round(price * (1.03 if side == 'buy' else 0.95), 2)
                    total_value = quantity * order_price

                    if result is not None:
                        # Add to executed trades list
                        executed_trades.append({
                            'action': side.upper(),
                            'ticker': asset,
                            'quantity': quantity,
                            'price': order_price,
                            'value': total_value,
                            'reason': f'Rebalancing (Trend: {trend_signal})'
                        })
                        logger.info(f"✅ {side.upper()} 주문 성공: {asset} {quantity}주")
                    else:
                        failed_orders.append(f"❌ {asset}: {side.upper()} {quantity} shares @ ${order_price:.2f}")
                        logger.error(f"❌ {side.upper()} 주문 실패: {asset} {quantity}주")

            # NEW: Send batched trade execution summary
            if executed_trades:
                self.notifier.send_trade_execution(executed_trades)

            # Send failure alerts if any
            if failed_orders:
                self.notifier.send_alert(
                    title="Trade Execution Failures",
                    message="\n".join(failed_orders),
                    severity='error'
                )

        final_summary = self.get_account_summary()

        # Calculate portfolio changes
        portfolio_change = final_summary['total_value'] - account_summary['total_value']
        portfolio_change_pct = (portfolio_change / account_summary['total_value'] * 100) if account_summary['total_value'] > 0 else 0

        # Calculate total P/L percentage
        total_profit_pct = (final_summary['total_profit'] / (final_summary['total_value'] - final_summary['total_profit']) * 100) if (final_summary['total_value'] - final_summary['total_profit']) > 0 else 0

        # Send individual daily summary ONLY if dual strategy is disabled
        # (Portfolio summary will handle reporting in dual-strategy mode)
        if not self.dual_strategy_enabled:
            self.notifier.send_daily_summary({
                'portfolio_value': final_summary['total_value'],
                'portfolio_change': portfolio_change,
                'portfolio_change_pct': portfolio_change_pct,
                'cash_usd': final_summary['cash_balance_usd'],
                'cash_krw': final_summary['cash_balance_krw'],
                'total_profit': final_summary['total_profit'],
                'total_profit_pct': total_profit_pct,
                'holdings': final_summary['stocks'],
                'trades_executed': len(executed_trades) if 'executed_trades' in locals() else 0,
                'next_action': f"Trend Signal: {trend_signal} | Next rebalance: Tomorrow"
            })

        # SPY trend chart - skip in dual strategy mode (not needed with portfolio summary)
        # if prices is not None and not self.dual_strategy_enabled:
        #     chart_path = self._create_spy_trend_chart(prices)
        #     if chart_path:
        #         self._send_discord_file(chart_path, "📊 SPY Trend Signal Chart")

        # Save strategy result for portfolio summary with detailed trend signal info
        trend_details = {
            'signal_value': trend_signal,
            'signal_date': str(signal_date.date()) if signal_date else None,
            'description': trend_desc,
            'target_allocations': {
                'SPY': target_allocations.get('SPY', 0),
                'SH': target_allocations.get('SH', 0),
                'IEF': target_allocations.get('IEF', 0)
            },
            'current_allocations': current_allocations
        }
        self._save_strategy_result(final_summary, trend_details, executed_trades if 'executed_trades' in locals() else [])

        # Send shutdown notification (skip in dual strategy mode - portfolio summary handles it)
        if not self.dual_strategy_enabled:
            self.notifier.send_shutdown({
                'trades_executed': len(executed_trades) if 'executed_trades' in locals() else 0,
                'portfolio_value': final_summary['total_value']
            })

        # Portfolio summary is sent by check_and_run.sh after both bots complete
        logger.info("="*70)
        logger.info("=== 자동매매 봇 종료 ===")
        logger.info("="*70)

if __name__ == "__main__":
    trader = UsaStockTrader()
    trader.run()




