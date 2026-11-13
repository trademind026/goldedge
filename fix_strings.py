import re
def repl(f, o, n):
    with open(f, 'r', encoding='utf-8') as file: c = file.read()
    new = c.replace(o, n)
    if new != c:
        with open(f, 'w', encoding='utf-8') as file: file.write(new)

repl('dashboard/app/layout.tsx', 'XAUUSD Options AI Trading System', 'XAUUSD AI Trading System')
repl('dashboard/app/trades/page.tsx', 'pt.option_price ?? pt.price ?? 0', 'pt.price ?? 0')
repl('dashboard/app/trades/page.tsx', 'name="Option $"', 'name="Price"')
repl('dashboard/app/charts/page.tsx', '/api/options/ticks', '/api/depth/ticks')
repl('dashboard/app/charts/page.tsx', 'Option chain', 'Market depth')
repl('dashboard/app/charts/page.tsx', 'Option Chain', 'Market Depth')
repl('dashboard/app/charts/page.tsx', 'Option ticks', 'Tick depth')
repl('dashboard/app/charts/page.tsx', 'live option-chain prices', 'live depth prices')
