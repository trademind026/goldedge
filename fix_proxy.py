import re

path = "dashboard/app/api/flask/[...path]/route.ts"
with open(path, "r") as f:
    c = f.read()

c = c.replace('const upstreamPath = `/${params.path.join("/")}`;', 'const upstreamPath = `/api/${params.path.join("/")}`;')

with open(path, "w") as f:
    f.write(c)
