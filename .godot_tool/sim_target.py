# -*- coding: utf-8 -*-
# 模拟游戏进程（简易模式/无敌测试用）—— 启动时从真实存档初始化数值
#   double HP    @0    = 存档 Hp
#   float  HP    @100  = 存档 Hp
#   double gold  @200  = 存档 gold
#   int64  soul  @300  = 存档 soul_count
# 命令：down→HP减5；gold→金币减1000；soul→灵魂减9999239；up→恢复初始
import ctypes, struct, sys, time, json, os

# 读存档初始化（与存档编辑器同路径规则）
hp = mhp = 80.0
gold = 5000.0
soul = 99992399
try:
    base = os.environ.get('APPDATA', '')
    p = os.path.join(base, 'Godot', 'app_userdata',
                     '造梦西游 · 圆梦计划：纵横三界篇 (4.5)', 'saves', 'save_1.json')
    if os.path.exists(p):
        data = json.load(open(p, encoding='utf-8'))
        cd = data.get('character_data', {})
        stats = cd.get('player_stats', {})
        if stats.get('Hp') is not None:
            hp = float(stats['Hp'])
            mhp = float(stats.get('max_Hp', hp))
        gold = float(cd.get('gold', gold))
        soul = int(cd.get('soul_count', soul))
except Exception:
    pass

buf_size = 128 * 1024 * 1024
buf = ctypes.create_string_buffer(buf_size)


def write_init():
    struct.pack_into('<d', buf, 0, hp)
    struct.pack_into('<f', buf, 100, hp)
    struct.pack_into('<d', buf, 200, gold)
    struct.pack_into('<q', buf, 300, soul)


write_init()
print('SIM_READY hp=%s gold=%s soul=%d' % (hp, gold, soul), flush=True)
while True:
    line = sys.stdin.readline()
    if not line:
        time.sleep(0.5)
        continue
    cmd = line.strip()
    if cmd == 'down':
        struct.pack_into('<d', buf, 0, hp - 5.0)
        struct.pack_into('<f', buf, 100, hp - 5.0)
        print('DOWN', flush=True)
    elif cmd == 'gold':
        struct.pack_into('<d', buf, 200, gold - 1000.0)
        print('GOLD', flush=True)
    elif cmd == 'soul':
        struct.pack_into('<q', buf, 300, soul - 9999239)
        print('SOUL', flush=True)
    elif cmd == 'up':
        write_init()
        print('UP', flush=True)
