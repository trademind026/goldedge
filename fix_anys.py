import re
import glob

files = glob.glob('dashboard/**/*.tsx', recursive=True) + glob.glob('dashboard/**/*.ts', recursive=True)

patterns = [
    (r'\b\(e\) =>', '(e: any) =>'),
    (r'\b\(event\) =>', '(event: any) =>'),
    (r'\b\(t\) =>', '(t: any) =>'),
    (r'\b\(p\) =>', '(p: any) =>'),
    (r'\b\(v\) =>', '(v: any) =>'),
    (r'\b\(d\) =>', '(d: any) =>'),
    (r'\b\(s\) =>', '(s: any) =>'),
    (r'\b\(row\) =>', '(row: any) =>'),
    (r'\b\(cell, i\) =>', '(cell: any, i: number) =>'),
    (r'\b\(prev\) =>', '(prev: any) =>'),
    (r'\b\(alert, idx\) =>', '(alert: any, idx: number) =>'),
    (r'\b\(exp\) =>', '(exp: any) =>'),
    (r'\b\(sum, t\) =>', '(sum: any, t: any) =>'),
    (r'\b\(backupCode\) =>', '(backupCode: any) =>'),
    (r'\b\(y, m\) =>', '(y: any, m: any) =>'),
    (r'\b\(o\) =>', '(o: any) =>'),
]

for f in files:
    if 'node_modules' in f or '.next' in f: continue
    with open(f, 'r', encoding='utf-8') as file:
        content = file.read()
    
    new_content = content
    for pat, rep in patterns:
        new_content = re.sub(pat, rep, new_content)
        
    # Also catch some unparenthesized ones:
    new_content = re.sub(r'(?<!\w)e =>', '(e: any) =>', new_content)
    new_content = re.sub(r'(?<!\w)event =>', '(event: any) =>', new_content)
    new_content = re.sub(r'(?<!\w)t =>', '(t: any) =>', new_content)
    new_content = re.sub(r'(?<!\w)p =>', '(p: any) =>', new_content)
    new_content = re.sub(r'(?<!\w)v =>', '(v: any) =>', new_content)
    new_content = re.sub(r'(?<!\w)d =>', '(d: any) =>', new_content)
    new_content = re.sub(r'(?<!\w)s =>', '(s: any) =>', new_content)
    new_content = re.sub(r'(?<!\w)prev =>', '(prev: any) =>', new_content)
    new_content = re.sub(r'(?<!\w)row =>', '(row: any) =>', new_content)

    if new_content != content:
        with open(f, 'w', encoding='utf-8') as file:
            file.write(new_content)
        print(f"Fixed {f}")
print("Done")
