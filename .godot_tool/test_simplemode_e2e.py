# -*- coding: utf-8 -*-
# 简易模式端到端测试（GUI 层）：
# 定位血量 → 写入 99999 → 锁定（掉血被拉回）→ 定位金币 → 写入 1 → 验证
import importlib.util, subprocess, sys, time, struct

sys.path.insert(0, r"E:\baidu-game\.godot_tool")
spec = importlib.util.spec_from_file_location("mt", r"E:\baidu-game\纵横三界修改器\memory_trainer.py")
mt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mt)

sim = subprocess.Popen([sys.executable, r"E:\baidu-game\.godot_tool\sim_target.py"],
                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                       stderr=subprocess.DEVNULL, text=True)
try:
    assert 'SIM_READY' in sim.stdout.readline()
    time.sleep(0.3)
    h = mt.open_process(sim.pid)
    assert h, 'attach failed'

    import tkinter as tk
    import tkinter.messagebox as mb
    root = tk.Tk()
    root.withdraw()
    app = mt.TrainerApp(root)
    app.h = h
    app.pid = sim.pid

    # 弹窗 mock：只计数“确认”弹窗；第2次（血量校准）前掉血；第4次（金币校准）前花金币
    n = {'c': 0}
    def fake_ask(*a, **k):
        n['c'] += 1
        if n['c'] == 2:
            sim.stdin.write('down\n'); sim.stdin.flush(); sim.stdout.readline()
        elif n['c'] == 4:
            sim.stdin.write('gold\n'); sim.stdin.flush(); sim.stdout.readline()
        return True
    mb.askyesno = fake_ask
    for name in ('showinfo', 'showwarning', 'showerror'):
        setattr(mb, name, lambda *a, **k: True)

    # 打开简易窗口
    app.open_simple()
    for _ in range(10):
        root.update(); time.sleep(0.03)
    assert app.simple_tree is not None

    # ---- 1. 定位血量 ----
    app.simple_locate('hp')
    deadline = time.time() + 90
    while time.time() < deadline:
        root.update()
        if 'hp' in app.simple_addrs and app.simple_addrs['hp']['addrs']:
            break
        time.sleep(0.03)
    assert 'hp' in app.simple_addrs, 'hp locate failed: %s' % app.simple_status.get()
    print('HP_LOCATE_OK', app.simple_status.get())

    # ---- 2. 写入 99999 ----
    app._simple_rows['hp']['newval'].delete(0, 'end')
    app._simple_rows['hp']['newval'].insert(0, '99999')
    app._simple_write('hp')
    dbl_addr = next(a for tn, a in app.simple_addrs['hp']['addrs'] if tn == 'double')
    raw = mt.read_mem(h, dbl_addr, 8)
    v = struct.unpack('<d', raw)[0] if raw else None
    print('HP_WRITE=%s' % v)
    assert v == 99999.0, v
    print('HP_WRITE_OK')

    # ---- 3. 锁定血量：再掉血应被拉回 99999 ----
    app._simple_rows['hp']['locked'].set(True)
    app._simple_toggle_lock('hp', app._simple_rows['hp']['locked'])
    assert app.simple_addrs['hp']['locked'] is True
    sim.stdin.write('down\n'); sim.stdin.flush(); sim.stdout.readline()
    time.sleep(0.6)
    raw = mt.read_mem(h, dbl_addr, 8)
    v = struct.unpack('<d', raw)[0] if raw else None
    print('HP_LOCKED_AFTER_DMG=%s' % v)
    assert v == 99999.0, v
    print('HP_LOCK_OK')

    # ---- 4. 定位金币 → 写入 1 ----
    app.simple_locate('gold')
    deadline = time.time() + 90
    while time.time() < deadline:
        root.update()
        if 'gold' in app.simple_addrs and app.simple_addrs['gold']['addrs']:
            break
        time.sleep(0.03)
    assert 'gold' in app.simple_addrs, 'gold locate failed: %s' % app.simple_status.get()
    print('GOLD_LOCATE_OK', app.simple_status.get())
    app._simple_rows['gold']['newval'].delete(0, 'end')
    app._simple_rows['gold']['newval'].insert(0, '1')
    app._simple_write('gold')
    gdbl = next(a for tn, a in app.simple_addrs['gold']['addrs'] if tn == 'double')
    raw = mt.read_mem(h, gdbl, 8)
    v = struct.unpack('<d', raw)[0] if raw else None
    print('GOLD_WRITE=%s' % v)
    assert v == 1.0, v
    print('GOLD_WRITE_OK')

    app.simple_stop_lock.set()
    root.destroy()
    print('SIMPLE_MODE_E2E_PASS')
finally:
    try:
        sim.kill()
        sim.wait(timeout=3)
    except Exception:
        pass
