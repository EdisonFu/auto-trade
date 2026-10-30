from ApplicationServices import (AXUIElementCreateApplication, AXUIElementCopyAttributeValue,
    AXUIElementIsAttributeSettable, AXUIElementCopyActionNames)
from Cocoa import NSWorkspace

BID = 'cn.com.10jqka.macstockPro'
pid = None
for a in NSWorkspace.sharedWorkspace().runningApplications():
    if a.bundleIdentifier() == BID:
        pid = a.processIdentifier()
app = AXUIElementCreateApplication(pid)

def attr(e, n):
    err, v = AXUIElementCopyAttributeValue(e, n, None)
    return v if err == 0 else None

def acts(e):
    err, a = AXUIElementCopyActionNames(e, None)
    return list(a) if err == 0 else []

def settable(e):
    err, s = AXUIElementIsAttributeSettable(e, 'AXValue', None)
    return bool(s) if err == 0 else False

wins = attr(app, 'AXWindows') or []
print(f"窗口数: {len(wins)}\n")

KEYS = ('买入','卖出','委托','下单','代码','价格','数量','可用','股数','确认','撤单','持仓','资金')

for wi, w in enumerate(wins):
    print(f"===== 窗口{wi}: title={attr(w,'AXTitle')!r} size={attr(w,'AXSize')} =====")
    hits = []
    def walk(e, depth=0, path=''):
        if depth > 16: return
        role = str(attr(e, 'AXRole') or '')
        title = attr(e, 'AXTitle'); desc = attr(e, 'AXDescription'); val = attr(e, 'AXValue')
        ph = attr(e, 'AXPlaceholderValue')
        blob = ' '.join(str(x) for x in (title, desc, val, ph) if x)
        # 收集所有可写输入框 和 命中关键字的控件
        if role in ('AXTextField','AXTextArea','AXComboBox') and settable(e):
            hits.append(('INPUT', path, role, title, desc, val, ph, acts(e)))
        elif role == 'AXButton' and any(k in blob for k in KEYS):
            hits.append(('BUTTON', path, role, title, desc, val, ph, acts(e)))
        elif any(k in blob for k in KEYS) and role in ('AXStaticText','AXRadioButton','AXCheckBox'):
            hits.append((role, path, role, title, desc, val, ph, acts(e)))
        for i, c in enumerate(attr(e, 'AXChildren') or []):
            walk(c, depth+1, f'{path}/{i}')
    walk(w)
    if not hits:
        print("  (无命中)\n")
        continue
    for kind, path, role, t, d, v, ph, ac in hits:
        bits = [f"[{kind}]", path, role]
        for label, x in (('Title',t),('Desc',d),('Value',v),('Placeholder',ph)):
            if x not in (None,''):
                s = str(x).strip().replace('\n',' ')
                if len(s) < 50: bits.append(f"{label}={s!r}")
        if ac: bits.append(f"acts={ac}")
        print("  " + "  ".join(bits))
    print()
