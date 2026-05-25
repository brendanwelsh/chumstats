import re, pathlib
p = pathlib.Path(".design/new_style.css")
src = p.read_text(encoding="utf-8")
# Find every border-radius declaration and decide what to do
def transform(m):
    val = m.group(1).strip()
    # If it has multiple values (shorthand), zero them all
    parts = val.split()
    keep_pill_or_circle = any(x in val for x in ("999px", "50%"))
    if keep_pill_or_circle:
        return m.group(0)
    # If any part is >= 6px, flatten to 0
    flatten = False
    for part in parts:
        m2 = re.match(r"^(\d+(?:\.\d+)?)(px|em|rem)?$", part)
        if m2:
            n = float(m2.group(1))
            if n >= 6:
                flatten = True
                break
    if flatten:
        return "border-radius: 0;"
    return m.group(0)
new = re.sub(r"border-radius:\s*([^;]+);", transform, src)
p.write_text(new, encoding="utf-8")
# count remaining radii
print("after pass:")
for line in new.splitlines():
    if "border-radius:" in line:
        print("  " + line.strip())
