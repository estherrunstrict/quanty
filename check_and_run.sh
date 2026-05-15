#!/bin/bash

# --- Configuration ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
current_hour=$(TZ="Asia/Seoul" date +%H)
current_minute=$(TZ="Asia/Seoul" date +%M)
current_hour=$((10#$current_hour))
day_of_week=$(TZ="Asia/Seoul" date +%u) # 1=Monday, 7=Sunday
is_weekend=false
if [ "$day_of_week" -ge 6 ]; then
    is_weekend=true
fi

# Virtual environment — use venv python for all tmux sessions.
# Tmux spawns a new shell that does NOT inherit the parent's venv,
# so we must use the full venv python path in tmux commands.
VENV_PYTHON="$HOME/myenv/bin/python3"
if [ ! -f "$VENV_PYTHON" ]; then
  echo "WARNING: venv python not found at $VENV_PYTHON, falling back to system python3"
  VENV_PYTHON="python3"
fi

# Activate for inline commands (e.g., portfolio_summary.py called directly)
VENV_PATH="$HOME/myenv/bin/activate"
if [ -f "$VENV_PATH" ]; then
  source "$VENV_PATH"
fi

# --- Upbit JD Strategy trading schedule (DISABLED — replaced by VB strategy) ---
echo "--- Upbit JD Strategy (Crypto) --- [DISABLED]"
UPBIT_JD_TMUX_SESSION="upbit_jd_trader"
UPBIT_JD_STATE_FILE="/tmp/upbit_jd_trader.run"
# Clean up any lingering JD sessions from before deactivation
if [ -f "$UPBIT_JD_STATE_FILE" ]; then
    rm -f "$UPBIT_JD_STATE_FILE"
fi
tmux kill-session -t "$UPBIT_JD_TMUX_SESSION" 2>/dev/null || true

# Get USA Eastern Time early (needed by BTC VB, Claude Bot, and stock trading)
ny_hour_early=$(TZ="America/New_York" date +%H)
ny_minute_early=$(TZ="America/New_York" date +%M)
ny_hour_early=$((10#$ny_hour_early))
ny_minute_early=$((10#$ny_minute_early))

# --- Upbit BTC Volatility Breakthrough Strategy ---
# Sell & recalculate at US market close (16:00 ET) — runs 365 days (BTC trades 24/7)
echo "--- Upbit BTC VB Strategy (Crypto) ---"
UPBIT_VB_SCRIPT="upbit_vb_strategy_auto_trade.py"
UPBIT_VB_TMUX_SESSION="upbit_vb_trader"
UPBIT_VB_STATE_FILE="/tmp/upbit_vb_trader.run"

# Reset VB state 5 min before US close and run Second Level Bot
SECOND_LEVEL_SCRIPT="SecondLevelBot.py"
SECOND_LEVEL_STATE="/tmp/second_level.run"
if [ "$ny_hour_early" -eq 15 ] && [ "$ny_minute_early" -eq 55 ]; then
    echo "Resetting Upbit VB trader state (US close approaching)."
    rm -f "$UPBIT_VB_STATE_FILE"
    rm -f "$SECOND_LEVEL_STATE"
    tmux kill-session -t "$UPBIT_VB_TMUX_SESSION" 2>/dev/null || true

    # Run Second Level Bot — computes cycle score and writes allocation_override.json
    # Timeout 90s to avoid blocking the 16:00 ET VB launch
    if [ ! -f "$SECOND_LEVEL_STATE" ]; then
        echo "[$(date)] Running Second Level Bot (cycle assessment)..."
        timeout 90 $VENV_PYTHON "$SCRIPT_DIR/$SECOND_LEVEL_SCRIPT" 2>>"$SCRIPT_DIR/second_level_stderr.log"
        touch "$SECOND_LEVEL_STATE"
        echo "[$(date)] Second Level Bot complete."
    fi
fi

# Run VB Strategy at 16:00 ET every day (aligned with US market close)
# EDT: 16:00 ET = 05:00 KST next day | EST: 16:00 ET = 06:00 KST next day
if [ "$ny_hour_early" -eq 16 ] && [ "$ny_minute_early" -eq 0 ]; then
    if [ ! -f "$UPBIT_VB_STATE_FILE" ]; then
        echo "Starting Upbit VB Strategy at US market close..."
        echo "Time: $(TZ='America/New_York' date '+%Y-%m-%d %H:%M:%S ET') / $(TZ='Asia/Seoul' date '+%Y-%m-%d %H:%M:%S KST')"
        tmux new-session -d -s "$UPBIT_VB_TMUX_SESSION" "cd \"$SCRIPT_DIR\" && $VENV_PYTHON $UPBIT_VB_SCRIPT"
        touch "$UPBIT_VB_STATE_FILE"
        echo "Upbit VB trader started at US close. State file created."
        echo "Check logs: tail -f $SCRIPT_DIR/upbit_vb_strategy.log"

        # Refresh realized-PnL aggregate after the BTC sell+state-save settles.
        # Without this, the dashboard shows stale realized P&L until the US
        # window's aggregator run (~6.5h later) — see incident 2026-04-28.
        (
            sleep 180
            echo "Aggregating realized PnL (post BTC VB)..."
            $VENV_PYTHON "$SCRIPT_DIR/scripts/aggregate_realized_pnl.py" \
                >>"$SCRIPT_DIR/aggregate_realized_pnl.log" \
                2>>"$SCRIPT_DIR/aggregate_realized_pnl_stderr.log" \
                || echo "WARN: realized PnL aggregator exited non-zero (see aggregate_realized_pnl_stderr.log)"
        ) &
    else
        echo "Upbit VB trader has already been run today."
    fi
fi

# --- Claude Trading Bot (AI market analysis + screener) ---
echo "--- Claude Trading Bot ---"
CLAUDE_BOT_DIR="$SCRIPT_DIR/claude-trading-bot"
CLAUDE_BOT_STATE_ANALYSIS="/tmp/claude_bot_analysis.run"
CLAUDE_BOT_STATE_SCREENER="/tmp/claude_bot_screener.run"
CLAUDE_BOT_TMUX_ANALYSIS="claude_bot_analysis"
CLAUDE_BOT_TMUX_SCREENER="claude_bot_screener"

# Pre-market analysis at 08:00 ET (regime check before all stock bots)
if [ "$ny_hour_early" -eq 8 ] && [ "$ny_minute_early" -eq 0 ] && ! $is_weekend; then
    if [ ! -f "$CLAUDE_BOT_STATE_ANALYSIS" ]; then
        echo "[$(date)] Starting Claude Trading Bot — pre-market analysis"
        tmux kill-session -t "$CLAUDE_BOT_TMUX_ANALYSIS" 2>/dev/null || true
        tmux new-session -d -s "$CLAUDE_BOT_TMUX_ANALYSIS" \
            "cd \"$CLAUDE_BOT_DIR\" && bash run_bot.sh analysis 2>>\"$CLAUDE_BOT_DIR/logs/stderr.log\""
        touch "$CLAUDE_BOT_STATE_ANALYSIS"
        echo "Claude Bot analysis started."
    fi
fi

# Post-market screeners at 16:30 ET
if [ "$ny_hour_early" -eq 16 ] && [ "$ny_minute_early" -eq 30 ] && ! $is_weekend; then
    if [ ! -f "$CLAUDE_BOT_STATE_SCREENER" ]; then
        echo "[$(date)] Starting Claude Trading Bot — post-market screeners"
        tmux kill-session -t "$CLAUDE_BOT_TMUX_SCREENER" 2>/dev/null || true
        tmux new-session -d -s "$CLAUDE_BOT_TMUX_SCREENER" \
            "cd \"$CLAUDE_BOT_DIR\" && bash run_bot.sh screeners 2>>\"$CLAUDE_BOT_DIR/logs/stderr.log\""
        touch "$CLAUDE_BOT_STATE_SCREENER"
        echo "Claude Bot screener started."
    fi
fi

# --- Stock trading schedule ---
echo "--- Stock trading ---"
# This script runs the trading scripts (Korea or USA) once per trading window.
# It uses state files to prevent restarting a script that has already run.

KOREA_SCRIPT="KoreaETFMomentumAutoTrade.py"  # Replaced KoreaStockAutoTrade.py with ETF Momentum Rotation
USA_SCRIPT="UsaStockAutoTrade.py"
JD_USA_SCRIPT="JDStrategyAutoTrade.py"
MDM_SCRIPT="ModifiedDualMomentumAutoTrade.py"
HYBRID_VB_SCRIPT="HybridVBAutoTrade.py"
CLAUDE_AI_SCRIPT="ClaudeTradingBotAutoTrade.py"
KOREA_TMUX_SESSION="korea_trader_session"
USA_TMUX_SESSION="usa_trader_session"
JD_USA_TMUX_SESSION="jd_usa_trader_session"
MDM_TMUX="mdm_trader_session"
HYBRID_VB_KR_TMUX="hybrid_vb_kr_session"
HYBRID_VB_US_TMUX="hybrid_vb_us_session"
CLAUDE_AI_TMUX="claude_ai_trader_session"

# State files to track if a script has been run in the current window
STATE_DIR="/tmp"
KOREA_STATE_FILE="$STATE_DIR/korea_trader.run"
USA_STATE_FILE="$STATE_DIR/usa_trader.run"
JD_USA_STATE_FILE="$STATE_DIR/jd_usa_trader.run"
MDM_STATE="$STATE_DIR/mdm_trader.run"
HYBRID_VB_KR_STATE="$STATE_DIR/hybrid_vb_kr.run"
HYBRID_VB_US_STATE="$STATE_DIR/hybrid_vb_us.run"
CLAUDE_AI_STATE="$STATE_DIR/claude_ai_trader.run"

# Trading hours
# Korea Stock Market: 9:00 ~ 15:30 KST
# USA Stock Market: 9:30 ~ 16:00 ET (Eastern Time)
KOREA_START_HOUR=9
KOREA_END_HOUR=15
KOREA_END_MINUTE=30

# Get USA Eastern Time (auto-handles DST/Standard time)
ny_hour=$(TZ="America/New_York" date +%H)
ny_minute=$(TZ="America/New_York" date +%M)
ny_hour=$((10#$ny_hour))
ny_minute=$((10#$ny_minute))

# USA Market Opening Time: 9:30 ET
USA_MARKET_START_HOUR=9
USA_MARKET_START_MINUTE=30

# --- Main Logic ---

# Determine current trading window (is_weekend already set above)
is_korea_time=false
# Korea market: 9:00 ~ 15:30 KST
if ! $is_weekend; then
    if [ "$current_hour" -gt "$KOREA_START_HOUR" ] && [ "$current_hour" -lt "$KOREA_END_HOUR" ]; then
        is_korea_time=true
    elif [ "$current_hour" -eq "$KOREA_START_HOUR" ]; then
        is_korea_time=true
    elif [ "$current_hour" -eq "$KOREA_END_HOUR" ] && [ "$current_minute" -lt "$KOREA_END_MINUTE" ]; then
        is_korea_time=true
    fi
fi

is_usa_time=false
# USA time is defined as weekday non-Korea time.
if ! $is_weekend && ! $is_korea_time; then
    is_usa_time=true
fi

is_usa_market_opening=false
# Check if it's USA market opening time (9:30 ET)
if [ "$ny_hour" -eq "$USA_MARKET_START_HOUR" ] && [ "$ny_minute" -eq "$USA_MARKET_START_MINUTE" ]; then
    is_usa_market_opening=true
fi

# --- Execution ---

if $is_korea_time; then
    # It's Korea time. Reset the USA state from the previous night.
    rm -f "$CLAUDE_BOT_STATE_ANALYSIS" "$CLAUDE_BOT_STATE_SCREENER"
    tmux kill-session -t "$CLAUDE_BOT_TMUX_ANALYSIS" 2>/dev/null || true
    tmux kill-session -t "$CLAUDE_BOT_TMUX_SCREENER" 2>/dev/null || true
    if [ -f "$USA_STATE_FILE" ]; then
        echo "Resetting USA trader state."
        rm -f "$USA_STATE_FILE"
        tmux kill-session -t "$USA_TMUX_SESSION" 2>/dev/null
    fi
    if [ -f "$JD_USA_STATE_FILE" ]; then
        echo "Resetting JD USA trader state."
        rm -f "$JD_USA_STATE_FILE"
        tmux kill-session -t "$JD_USA_TMUX_SESSION" 2>/dev/null
    fi
    if [ -f "$MDM_STATE" ]; then
        echo "Resetting Modified Dual Momentum trader state."
        rm -f "$MDM_STATE"
        tmux kill-session -t "$MDM_TMUX" 2>/dev/null
    fi
    if [ -f "$HYBRID_VB_US_STATE" ]; then
        echo "Resetting Hybrid VB US state."
        rm -f "$HYBRID_VB_US_STATE"
        tmux kill-session -t "$HYBRID_VB_US_TMUX" 2>/dev/null
    fi
    if [ -f "$CLAUDE_AI_STATE" ]; then
        echo "Resetting Claude AI Trading Bot state."
        rm -f "$CLAUDE_AI_STATE"
        tmux kill-session -t "$CLAUDE_AI_TMUX" 2>/dev/null
    fi

    # Run the Korea script ONLY at market opening time (9:00 KST)
    if [ "$current_hour" -eq "$KOREA_START_HOUR" ] && [ "$current_minute" -eq 0 ]; then
        if [ ! -f "$KOREA_STATE_FILE" ]; then
            echo "Korea market opening time (9:00 KST). Starting Korea trader..."
            # Kill any stale session to avoid "duplicate session" error
            tmux kill-session -t "$KOREA_TMUX_SESSION" 2>/dev/null || true
            tmux new-session -d -s "$KOREA_TMUX_SESSION" \
                "cd \"$SCRIPT_DIR\" && $VENV_PYTHON $KOREA_SCRIPT 2>>$SCRIPT_DIR/korea_etf_momentum_stderr.log"
            touch "$KOREA_STATE_FILE"
            echo "Korea trader started. State file created."
        else
            echo "Korea trader has already been run in this window."
        fi
    fi

    # Run Hybrid VB Korea at 09:01 KST (1 min after momentum bot to avoid KIS token collision)
    if [ "$current_hour" -eq 9 ] && [ "$current_minute" -eq 1 ]; then
        if [ ! -f "$HYBRID_VB_KR_STATE" ]; then
            echo "[$(date)] Starting Hybrid VB — Korea session"
            tmux kill-session -t "$HYBRID_VB_KR_TMUX" 2>/dev/null || true
            tmux new-session -d -s "$HYBRID_VB_KR_TMUX" \
                "cd \"$SCRIPT_DIR\" && $VENV_PYTHON $HYBRID_VB_SCRIPT --market kr 2>>$SCRIPT_DIR/hybrid_vb_kr_stderr.log"
            touch "$HYBRID_VB_KR_STATE"
            echo "Hybrid VB KR started."
        fi
    fi

elif $is_usa_time; then
    # It's USA time. Reset the Korea state from the day.
    if [ -f "$KOREA_STATE_FILE" ]; then
        echo "Resetting Korea trader state."
        rm -f "$KOREA_STATE_FILE"
        tmux kill-session -t "$KOREA_TMUX_SESSION" 2>/dev/null
    fi
    if [ -f "$HYBRID_VB_KR_STATE" ]; then
        echo "Resetting Hybrid VB KR state."
        rm -f "$HYBRID_VB_KR_STATE"
        tmux kill-session -t "$HYBRID_VB_KR_TMUX" 2>/dev/null
    fi

    # Run USA traders ONLY at market opening time (9:30 ET)
    if $is_usa_market_opening; then
        # Run both USA strategies simultaneously (dual strategy mode)
        # They will coordinate via config.yaml capital allocation settings

        # Run the USA script (Quant40) if it hasn't been run for this window.
        if [ ! -f "$USA_STATE_FILE" ]; then
            echo "USA market opening time (9:30 ET). Starting USA trader (Quant40 Strategy)..."
            tmux kill-session -t "$USA_TMUX_SESSION" 2>/dev/null || true
            tmux new-session -d -s "$USA_TMUX_SESSION" \
                "cd \"$SCRIPT_DIR\" && $VENV_PYTHON $USA_SCRIPT 2>>$SCRIPT_DIR/usa_trader_stderr.log"
            touch "$USA_STATE_FILE"
            echo "USA trader (Quant40) started. State file created."
        else
            echo "USA trader (Quant40) has already been run in this window."
        fi

        # Delay JD start by 30s to avoid KIS API token race, then run portfolio summary after both finish
        # Run in background subshell so check_and_run.sh returns immediately (doesn't block cron)
        (
            sleep 30

            # Run the JD USA script if it hasn't been run for this window.
            if [ ! -f "$JD_USA_STATE_FILE" ]; then
                echo "USA market opening time (9:30 ET). Starting JD USA trader (JD Strategy)..."
                tmux kill-session -t "$JD_USA_TMUX_SESSION" 2>/dev/null || true
                tmux new-session -d -s "$JD_USA_TMUX_SESSION" \
                    "cd \"$SCRIPT_DIR\" && $VENV_PYTHON $JD_USA_SCRIPT 2>>$SCRIPT_DIR/jd_usa_trader_stderr.log"
                touch "$JD_USA_STATE_FILE"
                echo "JD USA trader (JD Strategy) started. State file created."
            else
                echo "JD USA trader (JD Strategy) has already been run in this window."
            fi

            # Delay Modified Dual Momentum start by another 30s (60s total) to avoid KIS API token race
            sleep 30

            # Run the Modified Dual Momentum script if it hasn't been run for this window.
            if [ ! -f "$MDM_STATE" ]; then
                echo "USA market opening time (9:30 ET). Starting Modified Dual Momentum trader..."
                tmux kill-session -t "$MDM_TMUX" 2>/dev/null || true
                tmux new-session -d -s "$MDM_TMUX" \
                    "cd \"$SCRIPT_DIR\" && $VENV_PYTHON $MDM_SCRIPT 2>>$SCRIPT_DIR/mdm_stderr.log"
                touch "$MDM_STATE"
                echo "Modified Dual Momentum trader started. State file created."
            else
                echo "Modified Dual Momentum trader has already been run in this window."
            fi

            # Delay Hybrid VB US start by another 30s (90s total)
            sleep 30

            if [ ! -f "$HYBRID_VB_US_STATE" ]; then
                echo "[$(date)] Starting Hybrid VB — US session"
                tmux kill-session -t "$HYBRID_VB_US_TMUX" 2>/dev/null || true
                tmux new-session -d -s "$HYBRID_VB_US_TMUX" \
                    "cd \"$SCRIPT_DIR\" && $VENV_PYTHON $HYBRID_VB_SCRIPT --market us 2>>$SCRIPT_DIR/hybrid_vb_us_stderr.log"
                touch "$HYBRID_VB_US_STATE"
                echo "Hybrid VB US started."
            fi

            # Delay Claude AI Trading Bot start by another 30s (120s total)
            sleep 30

            if [ ! -f "$CLAUDE_AI_STATE" ]; then
                echo "[$(date)] Starting Claude AI Trading Bot"
                tmux kill-session -t "$CLAUDE_AI_TMUX" 2>/dev/null || true
                tmux new-session -d -s "$CLAUDE_AI_TMUX" \
                    "cd \"$SCRIPT_DIR\" && $VENV_PYTHON $CLAUDE_AI_SCRIPT 2>>$SCRIPT_DIR/claude_ai_trader_stderr.log"
                touch "$CLAUDE_AI_STATE"
                echo "Claude AI Trading Bot started."
            fi

            # Wait for all strategies to complete, then generate portfolio summary
            sleep 300
            echo "Generating integrated portfolio summary..."
            $VENV_PYTHON "$SCRIPT_DIR/portfolio_summary.py" 2>>"$SCRIPT_DIR/portfolio_summary_stderr.log"

            # Refresh realized-PnL YTD aggregate (per-bot → strategy_results/realized_pl_YYYY.json).
            # Reads each bot's trade-history state file; atomic write, safe during live trading.
            echo "Aggregating realized PnL..."
            $VENV_PYTHON "$SCRIPT_DIR/scripts/aggregate_realized_pnl.py" \
                >>"$SCRIPT_DIR/aggregate_realized_pnl.log" \
                2>>"$SCRIPT_DIR/aggregate_realized_pnl_stderr.log" \
                || echo "WARN: realized PnL aggregator exited non-zero (see aggregate_realized_pnl_stderr.log)"
        ) &
    fi
else # This block handles the weekend.
    echo "Markets are closed (weekend). Resetting stock state."
    # Clean up STOCK state files only — BTC trades 24/7, VB monitor may still be running
    rm -f "$KOREA_STATE_FILE" "$USA_STATE_FILE" "$JD_USA_STATE_FILE" "$UPBIT_JD_STATE_FILE" "$MDM_STATE" "$HYBRID_VB_KR_STATE" "$HYBRID_VB_US_STATE" "$CLAUDE_AI_STATE" "$CLAUDE_BOT_STATE_ANALYSIS" "$CLAUDE_BOT_STATE_SCREENER"
    # Clean up stock tmux sessions
    tmux kill-session -t "$KOREA_TMUX_SESSION" 2>/dev/null
    tmux kill-session -t "$USA_TMUX_SESSION" 2>/dev/null
    tmux kill-session -t "$JD_USA_TMUX_SESSION" 2>/dev/null
    tmux kill-session -t "$UPBIT_JD_TMUX_SESSION" 2>/dev/null
    tmux kill-session -t "$MDM_TMUX" 2>/dev/null
    tmux kill-session -t "$HYBRID_VB_KR_TMUX" 2>/dev/null
    tmux kill-session -t "$HYBRID_VB_US_TMUX" 2>/dev/null
    tmux kill-session -t "$CLAUDE_AI_TMUX" 2>/dev/null
    # NOTE: VB strategy (crypto) is NOT killed on weekends — BTC is 24/7.
    # The VB tmux session and state file are managed by the 08:55 KST daily reset,
    # which runs every day including weekends.
fi

