import re
with open('dashboard/components/RetroCalendar.tsx', 'r') as f:
    c = f.read()
c = re.sub(r'\(y,\s*m\)', '(y: any, m: any)', c)
with open('dashboard/components/RetroCalendar.tsx', 'w') as f:
    f.write(c)
