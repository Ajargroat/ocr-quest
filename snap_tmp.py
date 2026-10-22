"""One-shot Change 2 sweep for rework cycle 2 (deleted after run)."""
import re
import sys

P = "ui/styles.css"
lines = open(P, encoding="utf-8").read().split("\n")
assert lines[23].strip() == "}", lines[23]  # :root/light blocks end at line 24
css = "\n".join(lines)

named = [
# header / sidebar / buttons
("header{display:flex;justify-content:space-between;align-items:center;gap:16px;padding:32px 40px 16px;flex-wrap:wrap}",
 "header{display:flex;justify-content:space-between;align-items:center;gap:var(--sp-5);padding:var(--sp-5) var(--sp-6) var(--sp-4);flex-wrap:wrap}"),
(".side-btn{display:flex;align-items:center;gap:11px;width:100%;border:0;cursor:pointer;font-family:inherit;\n  font-size:14px;font-weight:600;color:var(--muted);background:transparent;padding:13px 15px;",
 ".side-btn{display:flex;align-items:center;gap:var(--sp-3);width:100%;border:0;cursor:pointer;font-family:inherit;\n  font-size:var(--fs-3);font-weight:600;color:var(--muted);background:transparent;padding:var(--sp-3) var(--sp-4);"),
(".btn{font-family:inherit;font-weight:700;color:var(--text);border:1px solid var(--border);cursor:pointer;padding:11px 20px;border-radius:12px;background:var(--panel);backdrop-filter:blur(10px);transition:transform .15s,border-color .2s,box-shadow .2s,color .2s;font-size:13.5px}",
 ".btn{font-family:inherit;font-weight:700;color:var(--text);border:1px solid var(--border);cursor:pointer;padding:var(--sp-3) var(--sp-5);border-radius:var(--r-3);background:var(--panel);backdrop-filter:blur(10px);transition:transform .15s,border-color .2s,box-shadow .2s,color .2s;font-size:var(--fs-3)}"),
# panel / log / ghost
(".panel{background:var(--panel);border:1px solid var(--border);border-radius:20px;backdrop-filter",
 ".panel{background:var(--panel);border:1px solid var(--border);border-radius:var(--r-4);backdrop-filter"),
(".panel-head{display:flex;justify-content:space-between;align-items:center;padding:20px 26px;border-bottom:1px solid var(--border)}",
 ".panel-head{display:flex;justify-content:space-between;align-items:center;padding:var(--sp-4) var(--sp-5);border-bottom:1px solid var(--border)}"),
(".panel-head h2{font-size:13px;",
 ".panel-head h2{font-size:var(--fs-2);"),
(".log{flex:1;overflow-y:auto;padding:14px 16px;font-family:'JetBrains Mono',monospace;font-size:12.5px;line-height:1.9}",
 ".log{flex:1;overflow-y:auto;padding:var(--sp-4);font-family:'JetBrains Mono',monospace;font-size:var(--fs-3);line-height:var(--lh-body)}"),
(".ghost-btn{display:inline-flex;align-items:center;gap:6px;padding:7px 14px;border:1px solid var(--border);border-radius:999px;background:transparent;color:var(--muted);font:600 11px/1 'Inter';",
 ".ghost-btn{display:inline-flex;align-items:center;gap:var(--sp-2);padding:var(--sp-2) var(--sp-4);border:1px solid var(--border);border-radius:999px;background:transparent;color:var(--muted);font:600 var(--fs-2)/1 'Inter';"),
# chips / cards / question text
(".kind{font-family:'Vazirmatn';font-size:11px;color:var(--muted);background:rgba(255,255,255,.06);padding:2px 8px;border-radius:999px;flex:0 0 auto}",
 ".kind{font-family:'Vazirmatn';font-size:var(--fs-2);color:var(--muted);background:rgba(255,255,255,.06);padding:var(--sp-1) var(--sp-2);border-radius:var(--r-pill);flex:0 0 auto}"),
(".dd-btn{display:flex;align-items:center;gap:6px;width:auto;max-width:190px;min-width:0;background:transparent;border:1px solid var(--border);color:var(--muted);font-family:'Vazirmatn';font-size:11px;font-weight:600;padding:4px 11px;border-radius:999px;",
 ".dd-btn{display:flex;align-items:center;gap:var(--sp-2);width:auto;max-width:190px;min-width:0;background:transparent;border:1px solid var(--border);color:var(--muted);font-family:'Vazirmatn';font-size:var(--fs-2);font-weight:600;padding:var(--sp-1) var(--sp-3);border-radius:var(--r-pill);"),
(".btn-ghost{font-family:'Vazirmatn';font-weight:600;font-size:13px;color:var(--text);background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:10px 18px;",
 ".btn-ghost{font-family:'Vazirmatn';font-weight:600;font-size:var(--fs-3);color:var(--text);background:var(--panel);border:1px solid var(--border);border-radius:var(--r-3);padding:var(--sp-3) var(--sp-5);"),
(".status-chip{font-size:11px;font-weight:700;padding:4px 12px;border-radius:999px;font-family:'Vazirmatn';flex:0 0 auto}",
 ".status-chip{font-size:var(--fs-2);font-weight:700;padding:var(--sp-1) var(--sp-3);border-radius:var(--r-pill);font-family:'Vazirmatn';flex:0 0 auto}"),
(".mchip{font-size:11px;color:var(--muted);background:rgba(255,255,255,.06);border:1px solid var(--border);padding:3px 10px;border-radius:999px;font-family:'Vazirmatn'}",
 ".mchip{font-size:var(--fs-2);color:var(--muted);background:rgba(255,255,255,.06);border:1px solid var(--border);padding:var(--sp-1) var(--sp-3);border-radius:var(--r-pill);font-family:'Vazirmatn'}"),
(".qtext{padding:12px 16px;font-size:14.5px;line-height:2.2;",
 ".qtext{padding:var(--sp-4);font-size:var(--fs-4);line-height:var(--lh-body);"),
(".opts li{display:flex;gap:9px;align-items:flex-start;background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:10px;padding:8px 10px;font-size:13.5px;line-height:2;",
 ".opts li{display:flex;gap:var(--sp-2);align-items:flex-start;background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:var(--r-2);padding:var(--sp-3) var(--sp-4);font-size:var(--fs-3);line-height:var(--lh-body);"),
# rail / usage / period stats
(".rail-btn{display:flex;flex-direction:column;align-items:center;gap:5px;width:100%;",
 ".rail-btn{display:flex;flex-direction:column;align-items:center;gap:var(--sp-2);width:100%;"),
(".usage-table{width:100%;border-collapse:collapse;font-size:12.5px}",
 ".usage-table{width:100%;border-collapse:collapse;font-size:var(--fs-2)}"),
(".usage-table td{padding:7px 9px;",
 ".usage-table td{padding:var(--sp-2) var(--sp-3);"),
(".ps-chip{font-family:inherit;font-size:11.5px;font-weight:700;padding:6px 13px;border-radius:999px;",
 ".ps-chip{font-family:inherit;font-size:var(--fs-2);font-weight:700;padding:var(--sp-2) var(--sp-4);border-radius:999px;"),
(".ps-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(112px,1fr));gap:10px}",
 ".ps-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(112px,1fr));gap:var(--sp-4)}"),
(".ps-cell{display:flex;flex-direction:column;gap:3px;padding:10px 12px;border:1px solid var(--border);\n  border-radius:12px;background:rgba(255,255,255,.03)}",
 ".ps-cell{display:flex;flex-direction:column;gap:var(--sp-1);padding:var(--sp-4) var(--sp-5);\n  background:var(--surface-2);border-radius:var(--r-3)}"),
(".ps-cell label{font-size:10.5px;",
 ".ps-cell label{font-size:var(--fs-2);"),
(".ps-cell .k{font-family:'JetBrains Mono',monospace;font-size:19px;",
 ".ps-cell .k{font-family:'JetBrains Mono',monospace;font-size:var(--fs-6);"),
]

dead = [
".sub{color:var(--muted);font-size:12.5px;margin-top:2px}\n",
".stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;padding:20px 34px 6px}\n",
".stat{background:var(--panel);border:1px solid var(--border);border-radius:18px;padding:18px 20px;backdrop-filter:blur(14px);transition:transform .2s,border-color .2s}\n",
".stat:hover{transform:translateY(-3px);border-color:rgba(255,255,255,.18)}\n",
".stat .k{font-size:30px;font-weight:800;font-variant-numeric:tabular-nums;display:block}\n",
".stat label{color:var(--muted);font-size:12px;letter-spacing:.4px;text-transform:uppercase}\n",
".stage-now .sep{color:var(--stroke)}\n",
'.stage-now[data-state="idle"] .sep{display:none}\n',
]

for old, new in named:
    n = css.count(old)
    assert n == 1, "NAMED %d: %r" % (n, old[:70])
    css = css.replace(old, new)
print("named deltas applied: %d" % len(named))

for d in dead:
    n = css.count(d)
    assert n == 1, "DEAD %d: %r" % (n, d[:70])
    css = css.replace(d, "")
print("dead rules deleted: %d" % len(dead))


def fs_map(v):
    v = float(v)
    if v in (4.4, 7.4):
        return None
    if v <= 10.5:
        return "var(--fs-1)"
    if v <= 12.5:
        return "var(--fs-2)"
    if v <= 14.5:
        return "var(--fs-3)"
    if v <= 19:
        return "var(--fs-4)"
    if v <= 24:
        return "var(--fs-5)"
    return "var(--fs-6)"


def sp_map(v):
    v = float(v)
    if v > 40:
        return None  # above the snap table (60/90px spacers) - leave literal
    if v <= 4:
        return "var(--sp-1)"
    if v <= 8:
        return "var(--sp-2)"
    if v <= 12:
        return "var(--sp-3)"
    if v <= 16:
        return "var(--sp-4)"
    if v <= 24:
        return "var(--sp-5)"
    return "var(--sp-6)"


def r_map(v):
    v = float(v)
    if v <= 7:
        return "var(--r-1)"
    if v <= 11:
        return "var(--r-2)"
    if v <= 15:
        return "var(--r-3)"
    if v <= 20:
        return "var(--r-4)"
    return "var(--r-pill)"


raw = css.split("\n")
assert raw[23].strip() == "}", raw[23]
head, tail = "\n".join(raw[:24]), raw[24:]
n_fs = n_sp = 0
out = []
for ln in tail:
    if ".dn-t" in ln or ".dn-s" in ln or ln.lstrip().startswith("@media"):
        out.append(ln)
        continue
    ln, c = re.subn(r"font-size\s*:\s*([\d.]+)px",
                    lambda m: fs_map(m.group(1)) or m.group(0), ln)
    n_fs += c
    def snap_val(m, fn):
        return re.sub(r"(\d+(?:\.\d+)?)px", lambda m2: fn(m2.group(1)), m.group(0))
    ln, c = re.subn(r"(?<![-\w])padding\s*:[^;{}]+", lambda m: snap_val(m, sp_map), ln)
    n_sp += c
    ln, c = re.subn(r"(?<![-\w])gap\s*:[^;{}]+", lambda m: snap_val(m, sp_map), ln)
    n_sp += c
    ln, c = re.subn(r"(?<![-\w])border-radius\s*:[^;{}]+", lambda m: snap_val(m, r_map), ln)
    n_sp += c
    out.append(ln)
css = head + "\n" + "\n".join(out)
print("mechanical: font-size=%d padding/gap/radius=%d" % (n_fs, n_sp))

open(P, "w", encoding="utf-8").write(css)

rest = sorted(set(re.findall(r"font-size:([\d.]+)px", css)))
print("remaining font-size literals:", rest)
assert rest == ["4.4", "7.4"], rest
assert ".stat{" not in css and ".stats{" not in css, "dead rules remain"
print("OK")
