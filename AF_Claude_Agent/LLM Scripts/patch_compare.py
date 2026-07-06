import sys


path = sys.argv[1] if len(sys.argv) > 1 else '/tmp/compare-traces-official.py'

with open(path, 'r') as f:
    src = f.read()

old = "                locations[id] = code[:code.index(')') + 1]"
new = "                if code and ')' in code and id.isdigit():\n                    locations[id] = code[:code.index(')') + 1]"
src = src.replace(old, new)

with open(path, 'w') as f:
    f.write(src)

print('patched OK')
