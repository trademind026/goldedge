import os
import re

path = 'dashboard/app/api/flask/[...path]/route.ts'
with open(path, 'r') as f:
    content = f.read()

content = re.sub(r'  if \(!hasTwoFactorEnabled\(session\.user\)\) \{.*?\n  \}\n', '', content, flags=re.DOTALL)

with open(path, 'w') as f:
    f.write(content)
