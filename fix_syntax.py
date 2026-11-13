import re
import glob

files = glob.glob('dashboard/**/*.tsx', recursive=True) + glob.glob('dashboard/**/*.ts', recursive=True)

patterns = [
    (r'\(s: any\)(?!\s*=>)', '(s)'),
    (r'\(e: any\)(?!\s*=>)', '(e)'),
    (r'\(v: any\)(?!\s*=>)', '(v)'),
    (r'\(t: any\)(?!\s*=>)', '(t)'),
    (r'\(p: any\)(?!\s*=>)', '(p)'),
    (r'\(d: any\)(?!\s*=>)', '(d)'),
    (r'\(exp: any\)(?!\s*=>)', '(exp)'),
    (r'\(prev: any\)(?!\s*=>)', '(prev)'),
    (r'\(row: any\)(?!\s*=>)', '(row)'),
    (r'\(user: any\)(?!\s*=>)', '(user)'),
    (r'\(event: any\)(?!\s*=>)', '(event)'),
    (r'\(y: any, m: any\)(?!\s*=>)', '(y, m)'),
    (r'\(alert: any, idx: number\)(?!\s*=>)', '(alert, idx)'),
    (r'\(cell: any, i: number\)(?!\s*=>)', '(cell, i)'),
    (r'\(s: any, t: any\)(?!\s*=>)', '(s, t)'),
    (r'\(sum: any, t: any\)(?!\s*=>)', '(sum, t)'),
]

for f in files:
    if 'node_modules' in f or '.next' in f: continue
    with open(f, 'r', encoding='utf-8') as file:
        content = file.read()
    
    new_content = content
    for pat, rep in patterns:
        new_content = re.sub(pat, rep, new_content)
        
    if new_content != content:
        with open(f, 'w', encoding='utf-8') as file:
            file.write(new_content)
        print(f"Fixed syntax {f}")
print("Done")
