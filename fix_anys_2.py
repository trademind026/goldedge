import re
import glob

files = glob.glob('dashboard/**/*.tsx', recursive=True) + glob.glob('dashboard/**/*.ts', recursive=True)

patterns = [
    (r'import\s*\{\s*Badge\s*\}\s*from\s*"./Badge"', 'import Badge from "./Badge"'),
    (r'\(user\)', '(user: any)'),
    (r'\(prev\)', '(prev: any)'),
    (r'\(row\)', '(row: any)'),
    (r'\(exp\)', '(exp: any)'),
    (r'\(alert, idx\)', '(alert: any, idx: number)'),
    (r'\(s\)', '(s: any)'),
    (r'\(s,\s*t\)', '(s: any, t: any)'),
    (r'\(sum,\s*t\)', '(sum: any, t: any)'),
    (r'\(e\)', '(e: any)'),
    (r'\(backupCode\)', '(backupCode: any)'),
    (r'\(v\)', '(v: any)'),
    (r'\(y,\s*m\)', '(y: any, m: any)'),
    (r'\(o\)', '(o: any)'),
    (r'\(cell,\s*i\)', '(cell: any, i: number)'),
    (r'\(event\)', '(event: any)'),
    (r'(?<!\w)event =>', '(event: any) =>'),
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
        print(f"Fixed {f}")
print("Done")
