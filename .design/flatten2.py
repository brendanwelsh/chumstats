import re, pathlib
p = pathlib.Path(".design/new_style.css")
src = p.read_text(encoding="utf-8")
# Now flatten EVERYTHING except circles (50%) and small <=4px decorative radii.
def transform(m):
    val = m.group(1).strip()
    if "50%" in val:
        return m.group(0)
    # tiny decorative radii on dots/stripes can stay
    parts = val.split()
    if len(parts) == 1:
        m2 = re.match(r"^(\d+(?:\.\d+)?)(px|em|rem)?$", parts[0])
        if m2 and float(m2.group(1)) <= 2:
            return m.group(0)
    return "border-radius: 0;"
new = re.sub(r"border-radius:\s*([^;]+);", transform, src)
p.write_text(new, encoding="utf-8")
print("done")
