# Automation Oracle — Trading Bots

## Deployment

### Remote Server
- **Host:** `ubuntu@193.123.246.52`
- **SSH Key:** `~/.ssh/oci_rsa`
- **Remote Path:** `/home/ubuntu/koreainvestment-autotrade/`
- **Local Path:** `automation_oracle/` (this directory)

### How to Deploy

Copy changed files from local to remote:
```bash
scp -i ~/.ssh/oci_rsa <local-file> ubuntu@193.123.246.52:/home/ubuntu/koreainvestment-autotrade/<file>
```

Example (single file):
```bash
scp -i ~/.ssh/oci_rsa automation_oracle/upbit_vb_strategy_auto_trade.py ubuntu@193.123.246.52:/home/ubuntu/koreainvestment-autotrade/upbit_vb_strategy_auto_trade.py
```

Example (multiple files):
```bash
scp -i ~/.ssh/oci_rsa automation_oracle/{file1.py,file2.py} ubuntu@193.123.246.52:/home/ubuntu/koreainvestment-autotrade/
```

### No restart needed
- Cron runs `check_and_run.sh` **every minute** on the server
- It launches Python scripts via tmux at their scheduled times
- Updating the .py file in place is enough — next scheduled run picks up changes automatically

### Cron Schedule (on server)
```
* * * * *  /bin/bash /home/ubuntu/koreainvestment-autotrade/check_and_run.sh
```

### Key Schedules (KST)
| Bot | Trigger Time | Script |
|-----|-------------|--------|
| BTC VB Strategy | 09:00 KST daily (incl. weekends) | `upbit_vb_strategy_auto_trade.py` |
| Korea ETF Momentum | 09:00 KST weekdays | `KoreaETFMomentumAutoTrade.py` |
| USA Stock (Quant40) | 09:30 ET weekdays | `UsaStockAutoTrade.py` |
| USA JD Strategy | 09:30 ET weekdays (+30s delay) | `JDStrategyAutoTrade.py` |
| Uranium VB Strategy | 09:30 ET weekdays (+60s delay) | `UraniumVBAutoTrade.py` |

### SSH Quick Commands
```bash
# Connect to server
ssh -i ~/.ssh/oci_rsa ubuntu@193.123.246.52

# Check running tmux sessions
ssh -i ~/.ssh/oci_rsa ubuntu@193.123.246.52 "tmux ls"

# View VB strategy logs
ssh -i ~/.ssh/oci_rsa ubuntu@193.123.246.52 "tail -50 /home/ubuntu/koreainvestment-autotrade/upbit_vb_strategy.log"

# Check cron log
ssh -i ~/.ssh/oci_rsa ubuntu@193.123.246.52 "tail -20 /home/ubuntu/koreainvestment-autotrade/cron.log"
```
