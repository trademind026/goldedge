import os
import glob
import re

files_to_check = glob.glob('dashboard/**/*.tsx', recursive=True) + glob.glob('dashboard/**/*.ts', recursive=True)

replacements = [
    (r'₹', '$'),
    (r'\bNIFTY\b', 'XAUUSD'),
    (r'\bNifty\b', 'Xauusd'),
    (r'\bZerodha\b', 'Broker'),
    (r'\bKite\b', 'Broker'),
    (r'\bkite\b', 'broker'),
    (r'\bCALL\b', 'BUY'),
    (r'\bPUT\b', 'SELL'),
    (r'\bpremium\b', 'price'),
    (r'\bPremium\b', 'Price'),
    (r'\bOptionChainRow\b', 'MarketDepthRow'),
    (r'\bOptionTickData\b', 'DepthTickData'),
    (r'NiftyCandleChart', 'XauusdCandleChart'),
    (r'Asia/Kolkata', 'UTC'),
    (r'IST', 'UTC'),
    (r'max_premium', 'max_price'),
    (r'entry_premium', 'entry_price'),
    (r'exit_premium', 'exit_price'),
    (r'current_premium', 'current_price'),
    (r'/api/options/chain', '/api/depth'),
    (r'/api/options/expiries', '/api/symbols'),
]

for filepath in files_to_check:
    if 'node_modules' in filepath or '.next' in filepath: continue
    
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
        
    new_content = content
    for pattern, repl in replacements:
        new_content = re.sub(pattern, repl, new_content)
        
    if new_content != content:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(new_content)
        print(f"Updated {filepath}")

print("Scrub complete.")
