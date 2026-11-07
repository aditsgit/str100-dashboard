# STR100 Bot · Paper Portfolio

Live page: https://aditsgit.github.io/str100-dashboard/

A daily-updated dashboard that replays simple swing-trading rules on Chartink's
STR100 breakout signals and shows what would have happened:

- buy the day after a signal, only when Market Health (share of liquid NSE stocks above their 50 day average) is 50% or more
- 10 stocks max, capital ÷ 10 each
- stop loss 10%, after day 10 sell on a close below the 20 day average, sell after 40 days

All trades are **simulated**. Not financial advice.

`build_dashboard.py` runs every weekday at 4:47 pm IST (GitHub Actions), writes `data.json`, and GitHub Pages serves `index.html`.
