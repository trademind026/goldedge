import os
import re

path = 'dashboard/lib/auth.ts'
with open(path, 'r') as f:
    content = f.read()

content = content.replace('twoFactor: {\n    enabled: true,', 'twoFactor: {\n    enabled: false,')

with open(path, 'w') as f:
    f.write(content)
