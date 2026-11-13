import os
import importlib
import sys

src_path = os.path.abspath('src')
sys.path.insert(0, src_path)

errors = []
for root, _, files in os.walk('src/goldedge'):
    for f in files:
        if f.endswith('.py') and f != '__init__.py':
            rel = os.path.relpath(os.path.join(root, f), 'src')
            mod = rel.replace(os.sep, '.')[:-3]
            try:
                importlib.import_module(mod)
            except Exception as e:
                errors.append(f"{mod}: {type(e).__name__} - {e}")

if errors:
    print("Found import errors:")
    for e in errors:
        print(e)
else:
    print("ALL IMPORTS OK!")
