"""Paper Factory — research/paper-layer orchestration. NEVER touches live code."""
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]          # automation_oracle/
STATE_DIR = REPO / "paper_state"
PAPER_CRON = REPO / "check_and_run_paper.sh"
WIKI = REPO.parent / "llm-wiki" / "Quanty" / "wiki"
