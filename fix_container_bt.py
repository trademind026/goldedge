import re

path = "src/goldedge/config/container.py"
with open(path, "r") as f:
    c = f.read()

old_block = """    run_backtest_uc = providers.Factory(
        RunBacktestUseCase,
        candle_repo=candle_repo,
        tick_repo=tick_repo,
        trade_repo=trade_repo,
        event_bus=event_bus,
    )"""

new_block = """    run_backtest_uc = providers.Factory(
        RunBacktestUseCase,
        tick_repo=tick_repo,
    )"""

c = c.replace(old_block, new_block)

with open(path, "w") as f:
    f.write(c)
