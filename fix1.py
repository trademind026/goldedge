import os
import re

# Fix 1: Add missing dummy types to api.ts
api_path = 'dashboard/lib/api.ts'
with open(api_path, 'a') as f:
    f.write("\nexport type OptionChainRow = any;\nexport type OptionTickData = any;\n")

# Fix 2: Fix charts/page.tsx (any types and undefined ticks)
charts_path = 'dashboard/app/charts/page.tsx'
with open(charts_path, 'r') as f:
    c_content = f.read()
c_content = c_content.replace('(_)', '(_: any)')
c_content = c_content.replace('(_, i)', '(_: any, i: any)')
c_content = c_content.replace('info.ticks > 0', '(info.ticks ?? 0) > 0')
with open(charts_path, 'w') as f:
    f.write(c_content)

# Fix 3: Fix settings/page.tsx (Zap icon)
settings_path = 'dashboard/app/settings/page.tsx'
with open(settings_path, 'r') as f:
    s_content = f.read()
if "import { Zap" not in s_content:
    s_content = s_content.replace('import { RefreshCw', 'import { Zap, RefreshCw')
with open(settings_path, 'w') as f:
    f.write(s_content)

# Fix 4: Fix trades/page.tsx (Options terminology)
trades_path = 'dashboard/app/trades/page.tsx'
with open(trades_path, 'r') as f:
    t_content = f.read()
t_content = t_content.replace('t.direction === "CALL"', 't.direction === "BUY"')
t_content = t_content.replace('filter === "CALL"', 'filter === "BUY"')
t_content = t_content.replace('filter === "PUT"', 'filter === "SELL"')
t_content = t_content.replace('t.direction === "PUT" || t.direction === "CALL"', 't.direction === "BUY" || t.direction === "SELL"')
t_content = re.sub(r'\{(t\.entry_price \?\? t\.entry_premium)\}', '{t.entry_price}', t_content)
t_content = re.sub(r'\{(t\.exit_price \?\? t\.exit_premium)\}', '{t.exit_price}', t_content)
with open(trades_path, 'w') as f:
    f.write(t_content)

# Fix 5: Fix lib/time.ts
time_path = 'dashboard/lib/time.ts'
with open(time_path, 'r') as f:
    time_content = f.read()
time_content = time_content.replace('toISTTimeFull', 'toUTCTimeFull')
time_content = time_content.replace('toISTTime', 'toUTCTime')
time_content = time_content.replace('"Asia/Kolkata"', '"UTC"')
with open(time_path, 'w') as f:
    f.write(time_content)

print("Pass 1 applied")
