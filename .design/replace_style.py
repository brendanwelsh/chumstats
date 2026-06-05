import re, pathlib, sys
server = pathlib.Path("src/ballshark/server.py")
src = server.read_text(encoding="utf-8")
new_css = pathlib.Path(".design/new_style.css").read_text(encoding="utf-8")
pattern = re.compile(r'_STYLE_TAG\s*=\s*""".*?"""', re.DOTALL)
m = pattern.search(src)
if not m:
    print("ERROR: _STYLE_TAG block not found"); sys.exit(1)
new_block = "_STYLE_TAG = \"\"\"\n<style>\n" + new_css + "\n</style>\n\"\"\""
src2 = src[:m.start()] + new_block + src[m.end():]
server.write_text(src2, encoding="utf-8")
print(f"replaced _STYLE_TAG block. old={m.end()-m.start()}, new={len(new_block)}. file now {len(src2)} bytes.")
