#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
纵横三界 存档编辑器（个人测试用，勿外传）
支持：读档/改档/重算 guard / 存档码导入导出（SaveCodec v2）

存档位置：%APPDATA%\\Godot\\app_userdata\\造梦西游 · 圆梦计划：纵横三界篇 (4.5)\\saves\\save_N.json

核心逻辑来自对游戏脚本的反编译（SaveCodec.gd / Global.gd）：
- 存档文件 = 明文 JSON：{ character_data, category_states, category_states_2p,
  timestamp_str, timestamp, save_uid, guard }
- guard = { level, soul, soul2p, stats }，每项 = md5(_canon_json(值) + "|zhxys-0.67-save-guard")
- 存档码 = "ZM2.<原始长度>.<base64>"，内容 = SHA256(前4字节) + flag + ZSTD 压缩 + XOR 流
  XOR 密钥流 = SHA256("ZMXY-SaveCodec-v2-20260926#<计数器>")
"""
import ctypes
import hashlib
import json
import os
import re
import shutil
import struct
import sys
import time

try:
    import zstandard as _zstd
    HAS_ZSTD = True
except Exception:
    HAS_ZSTD = False

# Godot JSON 字符串化（与 JSON.stringify 兼容：无空格、不转义非 ASCII）
def godot_json(v):
    return json.dumps(v, ensure_ascii=False, separators=(',', ':'))


def _canon_sort(o):
    """递归：dict 键按 ASCII 排序（Godot parse_string→Dictionary 的迭代序）"""
    if isinstance(o, dict):
        return {k: _canon_sort(o[k]) for k in sorted(o.keys())}
    if isinstance(o, list):
        return [_canon_sort(x) for x in o]
    return o


def canon_json(value):
    # 复刻 Godot: JSON.stringify → parse_string → JSON.stringify
    # parse_string 会把所有数字解析为 float（3 -> 3.0），dict 键重排为排序序
    txt = godot_json(value)
    parsed = json.loads(txt, parse_int=float, parse_float=float)
    return godot_json(_canon_sort(parsed))


GUARD_SALT = "zhxys-0.67-save-guard"
XOR_SALT = "ZMXY-SaveCodec-v2-20260926"
CODE_PREFIX = "ZM2."


def guard_of(value):
    return hashlib.md5((canon_json(value) + "|" + GUARD_SALT).encode('utf-8')).hexdigest()


def make_save_guard(save_data):
    cd = save_data.get("character_data", {})
    if not isinstance(cd, dict):
        return {}
    stats = cd.get("player_stats", {})
    return {
        "level": guard_of(cd.get("level", 1)),
        "soul": guard_of(cd.get("soul_count", 0)),
        "soul2p": guard_of(cd.get("soul_count_2p", 0)),
        "stats": guard_of(stats),
    }


def xor_stream(data: bytes) -> bytes:
    out = bytearray(data)
    off = 0
    counter = 0
    while off < len(out):
        block = (XOR_SALT + "#" + str(counter)).encode('utf-8')
        h = hashlib.sha256(block).digest()
        for b in h:
            if off >= len(out):
                break
            out[off] ^= b
            off += 1
        counter += 1
    return bytes(out)


def encode_code(raw: bytes) -> str:
    """raw = 存档 JSON 文件字节 → 存档码字符串"""
    if not raw:
        return ""
    body = raw
    flag = 0  # PACK_NONE: 不解构/重构，保持原样
    if not HAS_ZSTD:
        return ""
    compressed = _zstd.ZstdCompressor().compress(body)
    blob = hashlib.sha256(compressed).digest()[:4] + bytes([flag]) + compressed
    enc = xor_stream(blob)
    b64 = __import__('base64').b64encode(enc).decode('ascii').rstrip('=')
    return "%s%d.%s" % (CODE_PREFIX, len(body), b64)


def decode_code(code: str):
    """存档码 → (ok, raw_bytes)"""
    if not HAS_ZSTD:
        return False, "缺少 zstandard 库，无法解码存档码"
    s = code.strip().replace(' ', '').replace('\n', '').replace('\t', '')
    if not s.startswith(CODE_PREFIX) and not s.startswith("ZM1."):
        return False, "存档码必须以 ZM2. 或 ZM1. 开头"
    v2 = s.startswith(CODE_PREFIX)
    prefix = CODE_PREFIX if v2 else "ZM1."
    rest = s[len(prefix):]
    if '.' not in rest:
        return False, "存档码格式错误"
    size_str, b64 = rest.split('.', 1)
    try:
        size = int(size_str)
    except ValueError:
        return False, "存档码长度字段无效"
    if size <= 0 or size > 32 * 1024 * 1024:
        return False, "存档码长度异常"
    while len(b64) % 4 != 0:
        b64 += "="
    try:
        enc = __import__('base64').b64decode(b64)
    except Exception:
        return False, "base64 解码失败"
    if not v2:
        # V1: 无 checksum/flag/xor，直接 zstd 解压
        try:
            raw = _zstd.ZstdDecompressor().decompress(enc, max_output_size=size)
        except Exception:
            return False, "解压失败（数据不完整）"
        return (True, raw) if len(raw) == size else (False, "解压长度不符")
    if len(enc) < 5:
        return False, "存档码内容无效"
    blob = xor_stream(enc)
    chk = blob[0:4]
    flag = blob[4]
    compressed = blob[5:]
    if hashlib.sha256(compressed).digest()[:4] != chk:
        return False, "存档码校验失败（内容被修改或复制不完整）"
    try:
        body = _zstd.ZstdDecompressor().decompress(compressed, max_output_size=size)
    except Exception:
        return False, "解压失败（数据不完整）"
    if len(body) != size:
        return False, "解压长度不符"
    return True, body


# ---------- 存档文件定位 ----------
def default_save_dir():
    appdata = os.environ.get('APPDATA', '')
    base = os.path.join(appdata, 'Godot', 'app_userdata')
    if not os.path.isdir(base):
        return None
    cands = []
    for name in os.listdir(base):
        p = os.path.join(base, name, 'saves')
        if os.path.isdir(p):
            cands.append((name, p))
    # 优先“纵横三界”，其次任何含“三界”的
    def score(item):
        n = item[0]
        if '纵横三界' in n:
            return 0
        if '三界' in n:
            return 1
        return 2
    cands.sort(key=score)
    return cands[0][1] if cands else None


def list_slots(save_dir):
    """返回 [(slot_index, path), ...] 按槽位号排序"""
    out = []
    if not save_dir or not os.path.isdir(save_dir):
        return out
    for fn in os.listdir(save_dir):
        if fn.startswith('save_') and fn.endswith('.json') and not fn.endswith('.tmp'):
            try:
                slot = int(fn[len('save_'):-len('.json')])
            except ValueError:
                continue
            if 1 <= slot <= 24:
                out.append((slot, os.path.join(save_dir, fn)))
    out.sort()
    return out


def load_save(path):
    with open(path, 'r', encoding='utf-8-sig') as f:
        return json.load(f)


def slot_preview(data):
    cd = data.get('character_data', {})
    stats = cd.get('player_stats', {}) if isinstance(cd, dict) else {}
    role = ''
    re = cd.get('role_equipment', {}) if isinstance(cd, dict) else {}
    if isinstance(re, dict):
        role = str(re.get('current_role', ''))
    return {
        'level': cd.get('level', 1) if isinstance(cd, dict) else 1,
        'soul': cd.get('soul_count', 0) if isinstance(cd, dict) else 0,
        'gold': cd.get('gold', 0) if isinstance(cd, dict) else 0,
        'role': role,
        'hp': stats.get('Hp'),
        'attack': stats.get('attack'),
    }


def write_save(path, data):
    """原子写入 + guard 重算 + 时间戳刷新"""
    cd = data.get('character_data', {})
    if isinstance(cd, dict):
        data['guard'] = make_save_guard(data)
    now = time.localtime()
    data['timestamp_str'] = time.strftime('%Y-%m-%d %H:%M:%S', now)
    data['timestamp'] = int(time.time())
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(godot_json(data))
    os.replace(tmp, path)


# 主编辑字段
MAIN_FIELDS = [
    ('level', '等级', 1, 80),
    ('experience', '经验', 0, 999999999),
    ('gold', '金币', 0, 10 ** 13),
    ('soul_count', '灵魂', 0, 10 ** 13),
    ('soul_count_2p', '2P灵魂', 0, 10 ** 13),
    ('max_soul_count', '灵魂上限', 1, 10 ** 13),
    ('unlocked_level', '已解锁关卡', 1, 999),
    ('last_world_map', '当前地图', 1, 999),
    ('wushuang_energy', '怒气值', 0, 10 ** 13),
    ('unlocked_level_demon', '已解锁恶魔关卡', 1, 999),
]

STAT_FIELDS = [
    ('level', '等级', 1, 80), ('Hp', '生命', -1e15, 2e7), ('max_Hp', '生命上限', -1e15, 2e7),
    ('Mp', '法力', -1e15, 2e7), ('max_Mp', '法力上限', -1e15, 2e7),
    ('Exp', '经验', 0, 1e9), ('max_Exp', '升级经验', 1, 1e9),
    ('attack', '攻击', 0, 500000), ('defense', '防御', 0, 200000),
    ('magic_defense', '法防', 0, 200000), ('critical_rate', '暴击率', 0, 300),
    ('critical_damage', '暴击伤害', 0, 1000), ('lucky', '幸运', 0, 1000),
    ('dodge', '闪避', 0, 150), ('hp_regen', '回血', 0, 1e6), ('mp_regen', '回蓝', 0, 1e6),
]

# 与游戏 SAVE_LIMITS 一致（游戏反作弊关闭时不会处罚，仅作安全参考）
SAVE_LIMITS = {
    'level': 80, 'soul': 10 ** 13, 'soul_start_area': 300000,
    'attack': 500000, 'hp_ceiling': 20000000, 'mp_ceiling': 20000000,
    'defense': 200000, 'crit': 300, 'dodge': 150,
    'dignored': 200, 'mdignored': 200, 'stack': 99999,
}


def check_save_compliance(data):
    """按游戏 SAVE_LIMITS 检查存档数值，返回问题列表"""
    out = []
    cd = data.get('character_data', {})
    if not isinstance(cd, dict):
        return ['character_data 缺失']
    lv = int(cd.get('level', 1))
    if lv < 1 or lv > SAVE_LIMITS['level']:
        out.append('等级=%d（允许 1-%d）' % (lv, SAVE_LIMITS['level']))
    soul = int(cd.get('soul_count', 0))
    if soul < 0 or soul > SAVE_LIMITS['soul']:
        out.append('灵魂=%d（允许 0-%d）' % (soul, SAVE_LIMITS['soul']))
    clears = cd.get('level_clears', {})
    no_clear = not (isinstance(clears, dict) and clears)
    stats = cd.get('player_stats', {}) if isinstance(cd.get('player_stats'), dict) else {}
    newbie_level = int(stats.get('level', 1))
    if (no_clear and newbie_level < 10 and int(cd.get('last_world_map', 1)) == 1
            and soul > SAVE_LIMITS['soul_start_area']):
        out.append('新手区灵魂=%d（超过 %d）' % (soul, SAVE_LIMITS['soul_start_area']))
    for tag, pkey in (('', 'player_stats'), ('2P', 'player_stats_2p')):
        st = cd.get(pkey, {})
        if not isinstance(st, dict):
            continue
        checks = [('attack', SAVE_LIMITS['attack']), ('max_Hp', SAVE_LIMITS['hp_ceiling']),
                  ('Hp', SAVE_LIMITS['hp_ceiling']), ('max_Mp', SAVE_LIMITS['mp_ceiling']),
                  ('Mp', SAVE_LIMITS['mp_ceiling']), ('defense', SAVE_LIMITS['defense']),
                  ('magic_defense', SAVE_LIMITS['defense']), ('critical_rate', SAVE_LIMITS['crit']),
                  ('dodge', SAVE_LIMITS['dodge']), ('dignored', SAVE_LIMITS['dignored']),
                  ('mdignored', SAVE_LIMITS['mdignored'])]
        for key, cap in checks:
            if key in st:
                v = st[key]
                if isinstance(v, (int, float)) and (v < 0 or v > cap):
                    out.append('%s%s=%s（允许 0-%d）' % (tag, key, v, cap))
    # 道具堆叠（遍历背包）
    for pkey in ('backpack_items', 'backpack_items_2p'):
        items = cd.get(pkey, [])
        if not isinstance(items, list):
            continue
        for it in items:
            if isinstance(it, dict) and '数量' in it:
                n = it['数量']
                if isinstance(n, (int, float)) and (n < 0 or n > SAVE_LIMITS['stack']):
                    out.append('道具 %s 数量=%s（允许 0-%d）' % (it.get('id', '?'), n, SAVE_LIMITS['stack']))
    return out


def read_save_history(save_dir):
    """读取 save_history.json（游戏存档码历史）"""
    p = os.path.join(save_dir, 'save_history.json')
    if not os.path.exists(p):
        return []
    try:
        with open(p, 'r', encoding='utf-8-sig') as f:
            data = json.load(f)
        if isinstance(data, list):
            return [e for e in data if isinstance(e, dict)]
    except Exception:
        pass
    return []


# ================= 装备系统（复刻游戏 AllEquipment.gd / Global.gd / UniformSystem.gd） =================

def _resource_path(name):
    """打包成 exe 后从解包临时目录找资源；开发时从脚本同目录找"""
    base = getattr(sys, '_MEIPASS', None)
    if base:
        return os.path.join(base, name)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


EQUIP_TABLE_JSON = _resource_path('equipment_table.json')
ALL_EQUIP_GDC = r'E:\baidu-game\.godot_tool\decompiled\AllEquipment.gdc.gd'

# 槽位（SLOT_TYPES）：0 武器 / 1 法宝 / 2 防具 / 3 饰品 / 4 时装 / 5 头衔 / 6 双刀
SLOT_NAMES = {0: '武器', 1: '法宝', 2: '防具', 3: '饰品', 4: '时装', 5: '头衔', 6: '双刀'}
ROLE_NAME_MAP = {  # Global.gd ROLE_NAME_MAP（反向：中文名 → role_id）
    '孙悟空': 'wu_kong', '唐三藏': 'tang_seng', '猪八戒': 'ba_jie', '沙僧': 'sha_seng',
    '白龙': 'bai_long', '玉兔': 'yu_tu', '玉兔·二形态': 'yu_tu2', '哪吒': 'ne_zha',
}
ROLE_ID_MAP = {v: k for k, v in ROLE_NAME_MAP.items()}
BACKPACK_PAGE_COUNT = 20
BACKPACK_SLOT_COUNT = 25
NON_STACKABLE_TYPES = ['武器', '防具', '饰品', '法宝', '头衔', '翅膀']


def _parse_equipment_from_gdc(path):
    """从反编译的 AllEquipment.gdc.gd 重建装备表（与 parse_v6.py 相同逻辑）"""
    import ast as _ast
    import math as _math
    import operator as _op

    def safe_eval(expr):
        tree = _ast.parse(expr, mode='eval')
        allowed = (_ast.Expression, _ast.BinOp, _ast.Add, _ast.Sub, _ast.Mult, _ast.Div, _ast.FloorDiv,
                   _ast.UnaryOp, _ast.UAdd, _ast.USub, _ast.Constant, _ast.Call, _ast.Name, _ast.Load)
        for node in _ast.walk(tree):
            if not isinstance(node, allowed):
                raise ValueError('disallowed: %s' % type(node).__name__)
            if isinstance(node, _ast.Call):
                if not (isinstance(node.func, _ast.Name) and node.func.id == 'int' and len(node.args) == 1):
                    raise ValueError('disallowed call')
            if isinstance(node, _ast.Constant):
                if not isinstance(node.value, (int, float)):
                    raise ValueError('bad const')

        def ev(n):
            if isinstance(n, _ast.Constant):
                return n.value
            if isinstance(n, _ast.BinOp):
                l, r = ev(n.left), ev(n.right)
                return {'Add': _op.add, 'Sub': _op.sub, 'Mult': _op.mul,
                        'Div': _op.truediv, 'FloorDiv': _op.floordiv}[type(n.op).__name__](l, r)
            if isinstance(n, _ast.UnaryOp):
                v = ev(n.operand)
                return +v if isinstance(n.op, _ast.UAdd) else -v
            if isinstance(n, _ast.Call):
                return int(ev(n.args[0]))
            raise ValueError('node')
        return ev(tree.body)

    def sanitize(v):
        t = re.sub(r'randf_range\(([\d.]+),\s*[\d.]+\)', r'\1', v)
        t = re.sub(r'randi_range\((\d+),\s*\d+\)', r'\1', t)

        def snap(m):
            x, s = float(m.group(1)), float(m.group(2))
            return repr(round(_math.floor(x / s + 1e-9) * s, 6))
        t = re.sub(r'snapped\(([\d.]+),([\d.]+)\)', snap, t)
        t = re.sub(r',(\s*[}\]])', r'\1', t)
        t = re.sub(r'\btrue\b', 'True', t)
        t = re.sub(r'\bfalse\b', 'False', t)
        t = re.sub(r'\bnull\b', 'None', t)
        return t

    def eval_value(v):
        s = sanitize(v)
        try:
            return _ast.literal_eval(s)
        except Exception:
            return safe_eval(s)

    def find_pairs(text, s):
        depth = 0
        in_str = False
        k = s
        n = len(text)
        while k < n:
            c = text[k]
            if in_str:
                if c == '\\':
                    k += 2
                    continue
                if c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        return k
            k += 1
        return -1

    def parse_entry(text):
        t = text[1:-1].strip()
        parts = []
        depth = 0
        in_str = False
        cur = ''
        k = 0
        n = len(t)
        while k < n:
            c = t[k]
            if in_str:
                cur += c
                if c == '\\':
                    cur += t[k + 1]
                    k += 2
                    continue
                if c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                    cur += c
                elif c in '([{':
                    depth += 1
                    cur += c
                elif c in ')]}':
                    depth -= 1
                    cur += c
                elif c == ',' and depth == 0:
                    parts.append(cur)
                    cur = ''
                else:
                    cur += c
            k += 1
        if cur.strip():
            parts.append(cur)
        out = {}
        for p in parts:
            if ':' not in p:
                raise ValueError('no colon: ' + p[:60])
            key, _, v = p.partition(':')
            out[_ast.literal_eval(key.strip())] = eval_value(v.strip())
        return out

    src = open(path, encoding='utf-8').read()
    i = src.find('funcAll_equ():')
    body = src[i:]
    start = body.find('{')
    pat = re.compile(r'"([a-z0-9_]+)":\s*\{')
    items = {}
    pos = start
    while True:
        m = pat.search(body, pos)
        if not m:
            break
        oid = m.group(1)
        ostart = m.end() - 1
        oend = find_pairs(body, ostart)
        if oend == -1:
            pos = m.end()
            continue
        entry_text = body[ostart:oend + 1]
        try:
            cleaned = sanitize(entry_text)
            try:
                items[oid] = _ast.literal_eval(cleaned)
            except Exception:
                items[oid] = parse_entry(entry_text)
        except Exception:
            pass
        pos = oend + 1
    return items


def load_equipment_table():
    """加载装备全量表（优先同目录 JSON，缺失时从反编译文件重建）"""
    if os.path.exists(EQUIP_TABLE_JSON):
        try:
            with open(EQUIP_TABLE_JSON, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict) and data:
                return data
        except Exception:
            pass
    if os.path.exists(ALL_EQUIP_GDC):
        try:
            return _parse_equipment_from_gdc(ALL_EQUIP_GDC)
        except Exception:
            pass
    return {}


def get_item_category(item_type):
    """复刻 Global.get_item_category：类型 → 背包分类"""
    cat = {
        'equipment': ['武器', '防具', '饰品', '头衔', '翅膀', '法宝'],
        'item': ['道具', '消耗品', '宝箱', '材料'],
        'fashion': ['时装'],
        'book': ['经书'],
    }
    for c, types in cat.items():
        if item_type in types:
            return c
    return 'item'


def is_item_stackable(item_type):
    """复刻 Global.is_item_stackable"""
    return item_type not in NON_STACKABLE_TYPES


def find_empty_slot(backpack_items, category):
    """复刻 Global.find_empty_slot_for_item：返回 (page, slot_index) 或 None"""
    used = set()
    if isinstance(backpack_items, list):
        for it in backpack_items:
            if isinstance(it, dict) and it.get('category') == category:
                used.add((int(it.get('page', 0)), int(it.get('slot_index', 0))))
    for page in range(BACKPACK_PAGE_COUNT):
        for slot in range(BACKPACK_SLOT_COUNT):
            if (page, slot) not in used:
                return (page, slot)
    return None


def build_item_data(table_entry, item_id, amount=1):
    """复刻 Global.create_stable_item_data + 数量"""
    item = {}
    for k, v in table_entry.items():
        item[k] = v
    item['id'] = item_id
    item['数量'] = amount
    return item


def _ensure_category_states(data, cat, player=1):
    """确保 category_states[cat] 至少 20 页（dict 数组），返回该分类列表"""
    key = 'category_states' if player == 1 else 'category_states_2p'
    cs = data.setdefault(key, {})
    if not isinstance(cs, dict):
        cs = {}
        data[key] = cs
    pages = cs.get(cat)
    if not isinstance(pages, list):
        pages = []
        cs[cat] = pages
    while len(pages) < BACKPACK_PAGE_COUNT:
        pages.append({})
    return pages


def add_equipment_to_backpack(data, item_id, amount=1, player=1):
    """复刻 Global.add_item_to_backpack：往背包加装备/道具
    返回 (ok, msg)"""
    table = load_equipment_table()
    entry = table.get(item_id)
    if not entry:
        return False, '装备表中不存在该 id：%s' % item_id
    if not isinstance(entry, dict):
        return False, '装备表条目格式异常：%s' % item_id
    item_type = str(entry.get('类型', ''))
    if not item_type:
        return False, '该条目缺少「类型」字段：%s' % item_id
    if amount < 1:
        return False, '数量必须 ≥ 1'
    cd = data.setdefault('character_data', {})
    key = 'backpack_items' if player == 1 else 'backpack_items_2p'
    items = cd.get(key)
    if not isinstance(items, list):
        items = []
        cd[key] = items
    category = get_item_category(item_type)
    stackable = is_item_stackable(item_type)
    # 堆叠类：先尝试合并同 id
    if stackable:
        for it in items:
            if isinstance(it, dict):
                idata = it.get('item_data')
                if isinstance(idata, dict) and str(idata.get('id', '')) == item_id:
                    idata['数量'] = int(idata.get('数量', 1)) + amount
                    _refresh_category_page(data, category, int(it.get('page', 0)), int(it.get('slot_index', 0)), idata, player)
                    return True, '已合并 %s ×%d（现共 %d）' % (entry.get('名字', item_id), amount, idata['数量'])
    slot = find_empty_slot(items, category)
    if not slot:
        return False, '背包已满（%s 分类 20 页 × 25 格）' % category
    page, slot_index = slot
    item_data = build_item_data(entry, item_id, amount)
    items.append({'item_data': item_data, 'category': category, 'page': page, 'slot_index': slot_index})
    _refresh_category_page(data, category, page, slot_index, item_data, player)
    return True, '已加入背包：%s ×%d（%s 第%d页第%d格）' % (entry.get('名字', item_id), amount, category, page + 1, slot_index + 1)


def _refresh_category_page(data, category, page, slot_index, item_data, player=1):
    """同步更新 category_states（背包 UI 显示缓存）"""
    pages = _ensure_category_states(data, category, player)
    if page < len(pages):
        pages[page][str(slot_index)] = item_data


def equip_item_to_role(data, role_id, item_id, player=1):
    """复刻 UniformSystem.auto_equip_item + equip_item：把装备直接穿到角色槽位
    返回 (ok, msg)"""
    table = load_equipment_table()
    entry = table.get(item_id)
    if not entry or not isinstance(entry, dict):
        return False, '装备表中不存在该 id：%s' % item_id
    item_type = str(entry.get('类型', ''))
    if not item_type:
        return False, '该条目缺少「类型」字段'
    if role_id not in ROLE_ID_MAP:
        return False, '无效角色 id：%s' % role_id
    # 目标槽位（auto_equip_item 规则）
    wtype = str(entry.get('武器类型', ''))
    if item_type == '武器':
        if role_id == 'bai_long' and wtype == '枪':
            target = 6
        elif role_id == 'yu_tu' and wtype == '刀':
            target = 6
        elif role_id == 'ne_zha' and wtype == '枪':
            target = 6
        elif role_id == 'sha_seng' and wtype in ('枪', '弓'):
            target = 6
        else:
            target = 0
    elif item_type == '法宝':
        target = 1
    elif item_type == '防具':
        target = 2
    elif item_type == '饰品':
        target = 3
    elif item_type == '时装':
        target = 4
    elif item_type == '头衔':
        target = 5
    else:
        return False, '%s 不能直接穿到角色身上（类型：%s）' % (entry.get('名字', item_id), item_type)
    cd = data.setdefault('character_data', {})
    re_data = cd.setdefault('role_equipment', {'current_role': role_id, 'roles': {}})
    if not isinstance(re_data, dict):
        re_data = {'current_role': role_id, 'roles': {}}
        cd['role_equipment'] = re_data
    roles = re_data.setdefault('roles', {})
    if not isinstance(roles, dict):
        roles = {}
        re_data['roles'] = roles
    role_data = roles.setdefault(role_id, {'slots': [], 'appearance': {'weapon': '', 'body': '', 'accessory': ''}, 'skills': {}})
    if not isinstance(role_data, dict):
        role_data = {'slots': [], 'appearance': {'weapon': '', 'body': '', 'accessory': ''}, 'skills': {}}
        roles[role_id] = role_data
    slots = role_data.setdefault('slots', [])
    while len(slots) < 7:
        slots.append({})
    old_item = None
    if slots[target]:
        # 已有装备，换下旧的
        old_item = slots[target]
        slots[target] = {}
    item_data = build_item_data(entry, item_id, 1)
    slots[target] = item_data
    # 同步 appearance（武器/防具路径，复刻 UniformSystem.update_appearance）
    app = role_data.setdefault('appearance', {})
    if not isinstance(app, dict):
        app = {}
        role_data['appearance'] = app
    if target == 0 or target == 6:
        app['weapon'] = 'res://assets/Art/HeroPicture/Role2AllEquipment/Role_2_Eq_%s.png' % item_id
    elif target == 2:
        app['body'] = 'res://assets/Art/HeroPicture/Role2AllEquipment/Role_2_Body_%s.png' % item_id
    # 同步 category_states 装备页（保持背包/装备一致性）
    pages = _ensure_category_states(data, 'equipment', player)
    # 若有换下的旧装备，放回背包
    if old_item:
        try:
            items = cd.get('backpack_items')
            if not isinstance(items, list):
                items = []
                cd['backpack_items'] = items
            slot_info = find_empty_slot(items, 'equipment')
            if slot_info:
                p, s = slot_info
                items.append({'item_data': old_item, 'category': 'equipment', 'page': p, 'slot_index': s})
                if p < len(pages):
                    pages[p][str(s)] = old_item
        except Exception:
            pass
    return True, '已穿上：%s → %s（槽位：%s）' % (entry.get('名字', item_id), ROLE_ID_MAP[role_id], SLOT_NAMES.get(target, str(target)))


# ================= 角色解锁系统（复刻 UnlockSystem.gd / UnlockConfig.gd） =================
# 解锁数据存 user://unlock_data.json（游戏用户数据目录下，与 saves 同级）：
#   {"unlocks": {unlock_key: bool}, "announced_roles": [role_id, ...]}
# 默认解锁：孙悟空(wu_kong)、唐僧(tang_seng)；其余角色需解锁。
# 玉兔·二形态(yu_tu2) 跟随玉兔，无独立 key。
ROLE_UNLOCK_TABLE = [
    # (role_id, 显示名, unlock_key)
    ('ba_jie', '猪八戒', 'role_ba_jie'),
    ('sha_seng', '沙僧', 'role_sha_seng'),
    ('yu_tu', '玉兔', 'role_yu_tu'),
    ('ne_zha', '哪吒', 'role_ne_zha'),
    ('bai_long', '白龙', 'role_bai_long'),
]
DEFAULT_UNLOCKED_ROLES = ['wu_kong', 'tang_seng']


def default_userdata_dir():
    """游戏用户数据目录（unlock_data.json 所在，saves 的上一级）"""
    save_dir = default_save_dir()
    if save_dir:
        return os.path.dirname(save_dir)
    return None


def unlock_data_path(userdata_dir):
    return os.path.join(userdata_dir, 'unlock_data.json')


def read_unlock_data(userdata_dir):
    """读取 unlock_data.json，异常/缺失时返回空结构"""
    p = unlock_data_path(userdata_dir)
    if not os.path.exists(p):
        return {'unlocks': {}, 'announced_roles': []}
    try:
        with open(p, 'r', encoding='utf-8-sig') as f:
            data = json.load(f)
    except Exception:
        return {'unlocks': {}, 'announced_roles': []}
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get('unlocks'), dict):
        data['unlocks'] = {}
    if not isinstance(data.get('announced_roles'), list):
        data['announced_roles'] = []
    return data


def write_unlock_data(userdata_dir, data):
    """原子写入 unlock_data.json（Godot JSON.stringify 兼容：紧凑、UTF-8、不转义中文）"""
    p = unlock_data_path(userdata_dir)
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(godot_json(data))
    os.replace(tmp, p)


def get_locked_roles(data):
    """返回当前仍未解锁的角色 [(role_id, 显示名, key), ...]"""
    out = []
    for rid, disp, key in ROLE_UNLOCK_TABLE:
        if not data.get('unlocks', {}).get(key, False):
            out.append((rid, disp, key))
    return out


def get_unlocked_display_names(data):
    out = []
    for rid, disp, key in ROLE_UNLOCK_TABLE:
        if data.get('unlocks', {}).get(key, False):
            out.append(disp)
    return out


def unlock_all_roles(userdata_dir):
    """一键解锁所有角色（等价游戏内 UnlockSystem.unlock_all_roles()）
    返回本次新解锁的角色显示名列表"""
    data = read_unlock_data(userdata_dir)
    newly = []
    for rid, disp, key in ROLE_UNLOCK_TABLE:
        if not data['unlocks'].get(key, False):
            data['unlocks'][key] = True
            newly.append(disp)
    if newly:
        # 写前备份
        p = unlock_data_path(userdata_dir)
        if os.path.exists(p):
            try:
                shutil.copy2(p, p + '.backup_解锁前')
            except Exception:
                pass
        write_unlock_data(userdata_dir, data)
    return newly


# ================= GUI =================
def main():
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox, filedialog
    except Exception:
        print('需要 tkinter（标准 Python 自带）')
        return

    root = tk.Tk()
    root.title('纵横三界 存档编辑器')
    root.geometry('820x640')
    root.minsize(760, 580)

    save_dir = default_save_dir()
    current_path = [None]
    current_data = [None]

    frm = ttk.Frame(root, padding=6)
    frm.pack(fill='both', expand=True)

    # ---- 顶部：目录 + 槽位 ----
    top = ttk.LabelFrame(frm, text='存档位置', padding=4)
    top.pack(fill='x')
    dir_var = tk.StringVar(value=save_dir or '')
    ttk.Entry(top, textvariable=dir_var, width=80).pack(side='left', fill='x', expand=True)
    ttk.Button(top, text='浏览…', command=lambda: _browse()).pack(side='left', padx=4)

    slots_frame = ttk.LabelFrame(frm, text='存档槽位', padding=4)
    slots_frame.pack(fill='x', pady=4)
    slot_list = ttk.Treeview(slots_frame, columns=('slot', 'info'), show='headings', height=3)
    slot_list.heading('slot', text='槽位')
    slot_list.heading('info', text='内容预览')
    slot_list.column('slot', width=60, anchor='center')
    slot_list.column('info', width=680)
    slot_list.pack(fill='x')
    # slot_list.bind('<<TreeviewSelect>>', ...) 在函数定义后统一绑定

    # ---- 主编辑区 ----
    edit = ttk.LabelFrame(frm, text='角色数据（character_data）', padding=4)
    edit.pack(fill='both', expand=True, pady=4)
    canvas = tk.Canvas(edit, highlightthickness=0)
    vsb = ttk.Scrollbar(edit, orient='vertical', command=canvas.yview)
    inner = ttk.Frame(canvas)
    inner.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
    canvas.create_window((0, 0), window=inner, anchor='nw')
    canvas.configure(yscrollcommand=vsb.set)
    canvas.pack(side='left', fill='both', expand=True)
    vsb.pack(side='right', fill='y')

    main_vars = {}
    grid = ttk.Frame(inner)
    grid.pack(fill='x', pady=2)
    ttk.Label(grid, text='主属性', font=('', 10, 'bold')).grid(row=0, column=0, sticky='w', padx=4, pady=2)
    for i, (key, label, lo, hi) in enumerate(MAIN_FIELDS):
        r = (i // 4) + 1
        c = (i % 4) * 2
        ttk.Label(grid, text=label + ':').grid(row=r, column=c, sticky='e', padx=2, pady=1)
        v = tk.StringVar()
        ttk.Entry(grid, textvariable=v, width=12).grid(row=r, column=c + 1, sticky='w', padx=2)
        main_vars[key] = v

    stat_vars = {}
    stat_note = ttk.Label(inner, text='角色属性（player_stats，1P）', font=('', 10, 'bold'))
    stat_note.pack(fill='x', padx=6, pady=(6, 2))
    sgrid = ttk.Frame(inner)
    sgrid.pack(fill='x', padx=6)
    for i, (key, label, lo, hi) in enumerate(STAT_FIELDS):
        r = (i // 5) * 2
        c = (i % 5) * 2
        ttk.Label(sgrid, text=label + ':').grid(row=r, column=c, sticky='e', padx=2, pady=1)
        v = tk.StringVar()
        ttk.Entry(sgrid, textvariable=v, width=10).grid(row=r, column=c + 1, sticky='w', padx=2)
        stat_vars[key] = v

    stat2_vars = {}
    stat2_note = ttk.Label(inner, text='2P 属性（player_stats_2p）', font=('', 10, 'bold'))
    stat2_note.pack(fill='x', padx=6, pady=(6, 2))
    s2grid = ttk.Frame(inner)
    s2grid.pack(fill='x', padx=6)
    for i, (key, label, lo, hi) in enumerate(STAT_FIELDS):
        r = (i // 5) * 2
        c = (i % 5) * 2
        ttk.Label(s2grid, text=label + ':').grid(row=r, column=c, sticky='e', padx=2, pady=1)
        v = tk.StringVar()
        ttk.Entry(s2grid, textvariable=v, width=10).grid(row=r, column=c + 1, sticky='w', padx=2)
        stat2_vars[key] = v

    # 背包数量
    bag_frame = ttk.LabelFrame(inner, text='背包数量（backpack_items，id → 数量）', padding=4)
    bag_frame.pack(fill='x', padx=6, pady=6)
    bag_tree = ttk.Treeview(bag_frame, columns=('id', 'name', 'amount'), show='headings', height=4)
    bag_tree.heading('id', text='道具 id')
    bag_tree.heading('name', text='名字')
    bag_tree.heading('amount', text='数量')
    bag_tree.column('id', width=120)
    bag_tree.column('name', width=160)
    bag_tree.column('amount', width=120)
    bag_tree.pack(fill='x')
    bag_edit = ttk.Entry(bag_frame, width=12)
    bag_edit.pack(side='left', pady=2)
    ttk.Button(bag_frame, text='应用到选中道具', command=lambda: _apply_bag_amount(bag_edit.get())).pack(side='left', padx=4)
    ttk.Label(bag_frame, text='（背包数量上限 99999）').pack(side='left')

    # ---- 按钮（在函数定义之后创建，避免 UnboundLocalError）----
    # 见文件末尾 _build_actions 区域

    status = tk.StringVar(value='就绪')
    ttk.Label(frm, textvariable=status, relief='sunken', anchor='w').pack(fill='x', pady=(4, 0))

    # ---- 逻辑 ----
    def _browse():
        d = filedialog.askdirectory(initialdir=dir_var.get() or None, title='选择 saves 目录')
        if d:
            dir_var.set(d)
            _refresh_slots()

    def _refresh_slots():
        slot_list.delete(*slot_list.get_children())
        d = dir_var.get()
        for slot, path in list_slots(d):
            try:
                data = load_save(path)
                p = slot_preview(data)
                info = 'Lv.%s 灵魂=%s 金币=%s 角色=%s HP=%s 攻=%s' % (
                    p['level'], p['soul'], p['gold'], p['role'] or '?', p['hp'], p['attack'])
            except Exception as e:
                info = '读取失败: %s' % e
            slot_list.insert('', 'end', iid=str(slot), values=(slot, info))
        status.set('存档目录: %s' % (d or '未设置'))

    def _on_slot_select():
        sel = slot_list.selection()
        if not sel:
            return
        slot = int(sel[0])
        d = dir_var.get()
        if not d:
            return
        path = os.path.join(d, 'save_%d.json' % slot)
        if not os.path.exists(path):
            status.set('槽位 %d 无存档文件' % slot)
            return
        try:
            data = load_save(path)
        except Exception as e:
            messagebox.showerror('错误', '读取存档失败: %s' % e)
            return
        current_path[0] = path
        current_data[0] = data
        cd = data.get('character_data', {})
        if not isinstance(cd, dict):
            cd = {}
        for k, v in main_vars.items():
            main_vars[k].set(str(cd.get(k, '')))
        stats = cd.get('player_stats', {})
        if not isinstance(stats, dict):
            stats = {}
        for k, v in stat_vars.items():
            stat_vars[k].set(str(stats.get(k, '')))
        stats2 = cd.get('player_stats_2p', {})
        if not isinstance(stats2, dict):
            stats2 = {}
        for k, v in stat2_vars.items():
            stat2_vars[k].set(str(stats2.get(k, '')))
        # 背包
        bag_tree.delete(*bag_tree.get_children())
        for item in (cd.get('backpack_items', []) if isinstance(cd.get('backpack_items'), list) else []):
            if not isinstance(item, dict):
                continue
            iid = str(item.get('id', '?'))
            name = item.get('名字', '') or ''
            amt = item.get('数量', 1)
            bag_tree.insert('', 'end', iid=iid + '|' + str(len(bag_tree.get_children())), values=(iid, name, amt))
        status.set('已载入槽位 %d: %s' % (slot, os.path.basename(path)))

    def _apply_bag_amount(text):
        if not current_data[0]:
            messagebox.showwarning('提示', '请先载入存档')
            return
        try:
            n = int(text)
        except ValueError:
            messagebox.showwarning('提示', '数量必须是整数')
            return
        if n < 0 or n > 99999999:
            messagebox.showwarning('提示', '数量超出范围')
            return
        sel = bag_tree.selection()
        if not sel:
            messagebox.showwarning('提示', '请先选择背包中的道具行')
            return
        iid = sel[0]
        id_part = iid.split('|')[0]
        cd = current_data[0].get('character_data', {})
        changed = 0
        for item in cd.get('backpack_items', []):
            if isinstance(item, dict) and str(item.get('id', '')) == id_part:
                item['数量'] = n
                changed += 1
        status.set('已更新 %d 个同名道具的数量' % changed)
        _refresh_bag_tree(current_data[0])

    def _refresh_bag_tree(data):
        bag_tree.delete(*bag_tree.get_children())
        cd = data.get('character_data', {})
        items = cd.get('backpack_items', []) if isinstance(cd.get('backpack_items'), list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            iid = str(item.get('id', '?'))
            bag_tree.insert('', 'end', iid=iid + '|' + str(len(bag_tree.get_children())),
                            values=(iid, item.get('名字', ''), item.get('数量', 1)))

    def _collect_from_vars():
        if not current_data[0]:
            return None
        data = current_data[0]
        cd = data.setdefault('character_data', {})
        for k, v in main_vars.items():
            s = v.get().strip()
            if s != '':
                try:
                    cd[k] = int(float(s))
                except ValueError:
                    messagebox.showwarning('提示', '主属性 %s 数值无效' % k)
                    return None
        for pkey, varmap in (('player_stats', stat_vars), ('player_stats_2p', stat2_vars)):
            stats = cd.get(pkey)
            if not isinstance(stats, dict):
                stats = {}
                cd[pkey] = stats
            for k, v in varmap.items():
                s = v.get().strip()
                if s == '':
                    continue
                try:
                    f = float(s)
                except ValueError:
                    messagebox.showwarning('提示', '属性 %s 数值无效' % k)
                    return None
                stats[k] = int(f) if f == int(f) and abs(f) < 1e15 else f
        return data

    def _save():
        if not current_path[0]:
            messagebox.showwarning('提示', '请先载入存档')
            return
        data = _collect_from_vars()
        if data is None:
            return
        # 备份
        bak = current_path[0] + '.backup_手工'
        try:
            shutil.copy2(current_path[0], bak)
        except Exception:
            pass
        try:
            write_save(current_path[0], data)
        except Exception as e:
            messagebox.showerror('错误', '保存失败: %s' % e)
            return
        current_data[0] = data
        status.set('已保存（原档已备份为 %s）' % os.path.basename(bak))
        _refresh_slots()
        _refresh_bag_tree(data)

    def _export_code():
        if not current_path[0]:
            messagebox.showwarning('提示', '请先载入存档')
            return
        data = current_data[0] or load_save(current_path[0])
        raw = godot_json(data).encode('utf-8')
        code = encode_code(raw)
        if not code:
            messagebox.showerror('错误', '生成存档码失败（缺少 zstandard 库？）')
            return
        r = tk.Toplevel(root)
        r.title('存档码（复制到其他设备粘贴导入）')
        r.geometry('640x300')
        txt = tk.Text(r, wrap='char')
        txt.pack(fill='both', expand=True, padx=6, pady=6)
        txt.insert('1.0', code)
        ttk.Button(r, text='复制并关闭', command=lambda: (r.clipboard_clear(), r.clipboard_append(code), r.destroy())).pack(pady=4)

    def _import_code():
        dlg = tk.Toplevel(root)
        dlg.title('导入存档码')
        dlg.geometry('640x220')
        ttk.Label(dlg, text='粘贴 ZM1./ZM2. 开头的存档码：').pack(anchor='w', padx=8, pady=4)
        txt = tk.Text(dlg, wrap='char', height=6)
        txt.pack(fill='both', expand=True, padx=8)
        ttk.Label(dlg, text='将写入到哪个槽位？').pack(anchor='w', padx=8, pady=(6, 0))
        slot_var = tk.StringVar(value='1')
        ttk.Entry(dlg, textvariable=slot_var, width=6).pack(anchor='w', padx=8)

        def do_import():
            code = txt.get('1.0', 'end').strip()
            ok, raw = decode_code(code)
            if not ok:
                messagebox.showerror('错误', raw)
                return
            try:
                data = json.loads(raw.decode('utf-8'))
            except Exception:
                messagebox.showerror('错误', '存档码解码后不是有效 JSON')
                return
            if not isinstance(data, dict) or not isinstance(data.get('character_data'), dict):
                messagebox.showerror('错误', '存档码内容缺少 character_data，无法导入')
                return
            try:
                slot = int(slot_var.get())
            except ValueError:
                messagebox.showerror('错误', '槽位号无效')
                return
            if slot < 1 or slot > 24:
                messagebox.showerror('错误', '槽位号必须在 1-24')
                return
            d = dir_var.get()
            if not d:
                messagebox.showerror('错误', '请先选择存档目录')
                return
            path = os.path.join(d, 'save_%d.json' % slot)
            if os.path.exists(path):
                shutil.copy2(path, path + '.backup_导入前')
            # 保留原 save_uid（若存在），重算 guard
            if 'save_uid' not in data:
                data['save_uid'] = hashlib.md5(str(time.time()).encode()).hexdigest()[:32]
            write_save(path, data)
            status.set('已导入到槽位 %d' % slot)
            dlg.destroy()
            _refresh_slots()

        ttk.Button(dlg, text='导入', command=do_import).pack(pady=6)

    def _open_dir():
        d = dir_var.get()
        if d and os.path.isdir(d):
            os.startfile(d)
        else:
            messagebox.showwarning('提示', '存档目录不存在')

    def _equip_manager():
        """装备管理：浏览装备表 → 加入背包 / 直接穿上"""
        table = load_equipment_table()
        if not table:
            messagebox.showerror('错误', '装备表加载失败：\n%s\n\n（可重新解包并反编译 AllEquipment.gdc 后重试）' % EQUIP_TABLE_JSON)
            return
        if not current_data[0]:
            messagebox.showwarning('提示', '请先载入存档')
            return

        dlg = tk.Toplevel(root)
        dlg.title('装备管理（%d 件装备）' % len(table))
        dlg.geometry('980x620')
        dlg.minsize(900, 560)

        # 顶部：过滤
        topf = ttk.Frame(dlg)
        topf.pack(fill='x', padx=6, pady=4)
        ttk.Label(topf, text='搜索:').pack(side='left')
        kw_var = tk.StringVar()
        ttk.Entry(topf, textvariable=kw_var, width=24).pack(side='left', padx=4)
        type_var = tk.StringVar(value='全部')
        ttk.Combobox(topf, textvariable=type_var, values=['全部', '武器', '防具', '饰品', '法宝', '时装', '头衔', '翅膀', '道具', '消耗品', '经书'],
                     width=8, state='readonly').pack(side='left', padx=4)
        role_var = tk.StringVar(value='全部角色')
        roles = ['全部角色'] + [v for k, v in sorted(ROLE_ID_MAP.items(), key=lambda x: x[1])]
        ttk.Combobox(topf, textvariable=role_var, values=roles, width=10, state='readonly').pack(side='left', padx=4)
        ttk.Button(topf, text='搜索', command=lambda: _reload_list()).pack(side='left', padx=4)
        ttk.Label(topf, text='（提示：先「载入选中槽位」再打开本窗口，改动后点「保存修改」落盘）',
                  foreground='#666').pack(side='left', padx=8)

        # 中部：装备列表 + 详情
        mid = ttk.Frame(dlg)
        mid.pack(fill='both', expand=True, padx=6, pady=2)
        cols = ('id', 'name', 'type', 'quality', 'owner', 'power', 'price')
        tree = ttk.Treeview(mid, columns=cols, show='headings', height=18)
        heads = (('id', 'id', 90), ('name', '名字', 170), ('type', '类型', 60),
                 ('quality', '品质', 60), ('owner', '所属', 80), ('power', '攻', 50), ('price', '售价', 60))
        for c, t, w in heads:
            tree.heading(c, text=t)
            tree.column(c, width=w, anchor='w')
        tree.pack(side='left', fill='both', expand=True)
        vsb = ttk.Scrollbar(mid, orient='vertical', command=tree.yview)
        vsb.pack(side='left', fill='y')
        tree.configure(yscrollcommand=vsb.set)

        det = tk.Text(mid, width=34, wrap='char', font=('Consolas', 9))
        det.pack(side='left', fill='y', padx=(6, 0))
        det.insert('1.0', '选中装备查看详情')

        # 底部：操作
        bot = ttk.Frame(dlg)
        bot.pack(fill='x', padx=6, pady=6)
        ttk.Label(bot, text='数量:').pack(side='left')
        amt_var = tk.StringVar(value='1')
        ttk.Entry(bot, textvariable=amt_var, width=5).pack(side='left', padx=2)
        ttk.Button(bot, text='加入背包', command=lambda: _do_add(False)).pack(side='left', padx=6)
        ttk.Label(bot, text='穿给:').pack(side='left', padx=(12, 2))
        eq_role_var = tk.StringVar(value=ROLE_ID_MAP.get('wu_kong', 'wu_kong'))
        ttk.Combobox(bot, textvariable=eq_role_var, values=sorted(ROLE_ID_MAP.values()), width=10,
                     state='readonly').pack(side='left', padx=2)
        ttk.Button(bot, text='直接穿上', command=lambda: _do_add(True)).pack(side='left', padx=6)
        ttk.Button(bot, text='关闭', command=dlg.destroy).pack(side='right', padx=4)

        _current_id = [None]

        def _reload_list():
            tree.delete(*tree.get_children())
            kw = kw_var.get().strip().lower()
            ttype = type_var.get()
            trole = role_var.get()
            rid = None
            if trole != '全部角色':
                rid = ROLE_NAME_MAP.get(trole)
            for item_id, entry in sorted(table.items()):
                name = str(entry.get('名字', ''))
                itype = str(entry.get('类型', ''))
                iowner = str(entry.get('所属', ''))
                if kw and kw not in item_id.lower() and kw not in name.lower():
                    continue
                if ttype != '全部' and itype != ttype:
                    continue
                if rid is not None and iowner != ROLE_ID_MAP.get(rid, rid):
                    continue
                tree.insert('', 'end', iid=item_id, values=(
                    item_id, name, itype, entry.get('品质', ''), iowner,
                    entry.get('power', ''), entry.get('售价', '')))

        def _show_detail(_e=None):
            sel = tree.selection()
            if not sel:
                return
            iid = sel[0]
            _current_id[0] = iid
            entry = table[iid]
            det.delete('1.0', 'end')
            lines = []
            prop_names = ['名字', '类型', '品质', '所属', '颜色', '武器类型', '成长率', '五行']
            for k in prop_names:
                if k in entry:
                    lines.append('%s: %s' % (k, entry[k]))
            stat_names = ['SHp', 'SMp', 'power', 'Def', 'Mdef', 'Crit', 'Miss', 'R_hp', 'R_mp',
                          'mdignored', 'Lucky', 'dignored', 'Htarget', 'CritReduce', 'ar', 'sp']
            stats = []
            for k in stat_names:
                if k in entry:
                    v = entry[k]
                    if isinstance(v, (int, float)) and v != 0:
                        stats.append('%s=%s' % (k, v))
            lines.append('属性: ' + (' '.join(stats) if stats else '无'))
            if entry.get('强化属性'):
                lines.append('强化: %s' % godot_json(entry['强化属性']))
            if entry.get('宝石'):
                lines.append('宝石: %s' % godot_json(entry['宝石']))
            lines.append('描述: %s' % entry.get('描述', ''))
            det.insert('1.0', '\n'.join(lines))

        tree.bind('<<TreeviewSelect>>', _show_detail)

        def _do_add(equip):
            if not _current_id[0]:
                messagebox.showwarning('提示', '请先在列表中选择装备')
                return
            item_id = _current_id[0]
            entry = table[item_id]
            try:
                amount = int(amt_var.get())
            except ValueError:
                amount = 1
            if amount < 1:
                amount = 1
            if equip:
                role_id = eq_role_var.get()
                ok, msg = equip_item_to_role(current_data[0], role_id, item_id)
            else:
                ok, msg = add_equipment_to_backpack(current_data[0], item_id, amount)
            if not ok:
                messagebox.showerror('装备管理', msg)
                return
            status.set('装备管理：' + msg + '（尚未写盘，点「保存修改」保存）')
            _refresh_bag_tree(current_data[0])
            # 刷新角色下拉默认值：装备所属优先
            owner = str(entry.get('所属', ''))
            if owner in ROLE_NAME_MAP and equip:
                eq_role_var.set(ROLE_NAME_MAP[owner])
            messagebox.showinfo('装备管理', msg + '\n\n记得点主窗口「保存修改」写入存档。')

        _reload_list()

    def _json_editor():
        """高级：编辑完整 JSON（可改 missions/role_equipment/category_states 等任意字段）"""
        if not current_data[0]:
            messagebox.showwarning('提示', '请先载入存档')
            return
        dlg = tk.Toplevel(root)
        dlg.title('完整 JSON 编辑（高级）')
        dlg.geometry('760x560')
        txt = tk.Text(dlg, wrap='none', font=('Consolas', 10))
        txt.pack(fill='both', expand=True, padx=6, pady=6)
        vsb = ttk.Scrollbar(dlg, orient='vertical', command=txt.yview)
        vsb.pack(side='right', fill='y')
        txt.configure(yscrollcommand=vsb.set)
        txt.insert('1.0', godot_json(current_data[0]))
        note = ttk.Label(dlg, text='修改后点“应用”，会校验 JSON 并重算 guard。请勿删除 character_data 顶层键。',
                         foreground='#a00')
        note.pack(anchor='w', padx=8)

        def apply_json():
            try:
                parsed = json.loads(txt.get('1.0', 'end'))
            except Exception as e:
                messagebox.showerror('错误', 'JSON 解析失败: %s' % e)
                return
            if not isinstance(parsed, dict) or not isinstance(parsed.get('character_data'), dict):
                messagebox.showerror('错误', '必须保留 character_data 字典结构')
                return
            current_data[0] = parsed
            messagebox.showinfo('提示', '已应用，点「保存修改」写入文件')
            dlg.destroy()
            _refresh_bag_tree(parsed)
            status.set('JSON 已应用（尚未写盘）')

        ttk.Button(dlg, text='应用', command=apply_json).pack(pady=4)

    def _check_compliance():
        if not current_data[0]:
            messagebox.showwarning('提示', '请先载入存档')
            return
        problems = check_save_compliance(current_data[0])
        if not problems:
            messagebox.showinfo('合规检查', '所有数值都在游戏安全水位内（等级≤80、灵魂≤1万亿等）')
        else:
            r = tk.Toplevel(root)
            r.title('合规检查结果')
            r.geometry('560x320')
            txt = tk.Text(r, wrap='char')
            txt.pack(fill='both', expand=True, padx=6, pady=6)
            txt.insert('1.0', '以下数值超出游戏 SAVE_LIMITS（当前反作弊关闭不会处罚，仅供参考）：\n\n'
                      + '\n'.join('- ' + p for p in problems))
            ttk.Button(r, text='关闭', command=r.destroy).pack(pady=4)

    def _copy_slot():
        if not current_path[0]:
            messagebox.showwarning('提示', '请先载入存档')
            return
        dlg = tk.Toplevel(root)
        dlg.title('复制当前存档到其他槽位')
        dlg.geometry('360x120')
        ttk.Label(dlg, text='目标槽位（1-24）：').pack(anchor='w', padx=8, pady=6)
        var = tk.StringVar(value='2')
        ttk.Entry(dlg, textvariable=var, width=8).pack(anchor='w', padx=8)

        def do_copy():
            try:
                slot = int(var.get())
            except ValueError:
                messagebox.showerror('错误', '槽位号无效')
                return
            if slot < 1 or slot > 24:
                messagebox.showerror('错误', '槽位号必须在 1-24')
                return
            d = dir_var.get()
            if not d:
                messagebox.showerror('错误', '请先选择存档目录')
                return
            dst = os.path.join(d, 'save_%d.json' % slot)
            if os.path.exists(dst):
                shutil.copy2(dst, dst + '.backup_覆盖前')
            shutil.copy2(current_path[0], dst)
            status.set('已复制到槽位 %d（原档已备份）' % slot)
            dlg.destroy()
            _refresh_slots()

        ttk.Button(dlg, text='复制', command=do_copy).pack(pady=4)

    def _delete_slot():
        sel = slot_list.selection()
        if not sel:
            messagebox.showwarning('提示', '请先在列表中选中要删除的槽位')
            return
        slot = int(sel[0])
        d = dir_var.get()
        if not d:
            return
        path = os.path.join(d, 'save_%d.json' % slot)
        if not os.path.exists(path):
            messagebox.showwarning('提示', '槽位 %d 没有存档文件' % slot)
            return
        if not messagebox.askyesno('确认删除', '删除槽位 %d？会先备份为 save_%d.json.backup_删除' % (slot, slot)):
            return
        shutil.copy2(path, path + '.backup_删除')
        os.remove(path)
        status.set('已删除槽位 %d（备份: save_%d.json.backup_删除）' % (slot, slot))
        if current_path[0] == path:
            current_path[0] = None
            current_data[0] = None
        _refresh_slots()

    def _history_view():
        """查看并导入游戏内存档码历史（save_history.json）"""
        d = dir_var.get()
        if not d:
            messagebox.showwarning('提示', '请先选择存档目录')
            return
        hist = read_save_history(d)
        if not hist:
            messagebox.showinfo('存档码历史', '没有找到 save_history.json（需要游戏内触发过存档码记录）')
            return
        dlg = tk.Toplevel(root)
        dlg.title('存档码历史（save_history.json，最多每槽 30 条）')
        dlg.geometry('720x420')
        cols = ('idx', 'time', 'role', 'level', 'slot')
        tree = ttk.Treeview(dlg, columns=cols, show='headings', height=14)
        for c, w, t in (('idx', 50, '序号'), ('time', 160, '时间'), ('role', 200, '角色'),
                        ('level', 60, '等级'), ('slot', 60, '槽位')):
            tree.heading(c, text=t)
            tree.column(c, width=w, anchor='center')
        tree.pack(fill='both', expand=True, padx=6, pady=6)
        for i, e in enumerate(hist):
            tree.insert('', 'end', iid=str(i), values=(
                i + 1, e.get('time', ''), e.get('role', '?'), e.get('level', '?'), e.get('slot', '?')))
        info = ttk.Label(dlg, text='选中一条后点「导入到槽位」（会先解码校验存档码）')
        info.pack(anchor='w', padx=8)

        def do_import_hist():
            sel = tree.selection()
            if not sel:
                messagebox.showwarning('提示', '请先选择一条历史记录')
                return
            code = hist[int(sel[0])].get('code', '')
            if not code:
                messagebox.showerror('错误', '该记录没有存档码')
                return
            ok, raw = decode_code(code)
            if not ok:
                messagebox.showerror('错误', '存档码解码失败: %s' % raw)
                return
            try:
                data = json.loads(raw.decode('utf-8'))
            except Exception:
                messagebox.showerror('错误', '解码后不是有效 JSON')
                return
            if not isinstance(data, dict) or not isinstance(data.get('character_data'), dict):
                messagebox.showerror('错误', '内容缺少 character_data')
                return
            dst = os.path.join(d, 'save_%d.json' % int(hist[int(sel[0])].get('slot', 1)))
            if os.path.exists(dst):
                shutil.copy2(dst, dst + '.backup_历史导入前')
            if 'save_uid' not in data:
                data['save_uid'] = hashlib.md5(str(time.time()).encode()).hexdigest()[:32]
            write_save(dst, data)
            status.set('已从历史导入到槽位 %d' % int(hist[int(sel[0])].get('slot', 1)))
            dlg.destroy()
            _refresh_slots()

        ttk.Button(dlg, text='导入到原槽位', command=do_import_hist).pack(pady=4)

    def _unlock_all_roles_gui():
        """一键解锁所有角色（直接写游戏 unlock_data.json）"""
        ud = default_userdata_dir()
        if not ud:
            messagebox.showerror('错误', '未找到游戏用户数据目录\n（请先运行过一次游戏，生成存档后再试）')
            return
        if not os.path.isdir(ud):
            messagebox.showerror('错误', '游戏用户数据目录不存在：\n%s' % ud)
            return
        newly = unlock_all_roles(ud)
        data = read_unlock_data(ud)
        unlocked = get_unlocked_display_names(data)
        if newly:
            messagebox.showinfo('解锁成功', '已解锁角色：' + '、'.join(newly) +
                                '\n\n重新进入游戏的「角色选择」界面即可选用。')
        else:
            messagebox.showinfo('解锁成功', '所有角色都已解锁：\n' + '、'.join(unlocked))
        status.set('角色解锁：' + ('、'.join(unlocked) if unlocked else '（无）'))

    # 初始
    btn = ttk.Frame(frm)
    btn.pack(fill='x', pady=4)
    ttk.Button(btn, text='载入选中槽位', command=_on_slot_select).pack(side='left', padx=2)
    ttk.Button(btn, text='保存修改', command=_save).pack(side='left', padx=2)
    ttk.Button(btn, text='解锁所有角色', command=_unlock_all_roles_gui).pack(side='left', padx=2)
    ttk.Button(btn, text='装备管理…', command=_equip_manager).pack(side='left', padx=2)
    ttk.Button(btn, text='导出存档码', command=_export_code).pack(side='left', padx=2)
    ttk.Button(btn, text='导入存档码…', command=_import_code).pack(side='left', padx=2)
    btn2 = ttk.Frame(frm)
    btn2.pack(fill='x', pady=(0, 4))
    ttk.Button(btn2, text='完整JSON编辑', command=_json_editor).pack(side='left', padx=2)
    ttk.Button(btn2, text='合规检查', command=_check_compliance).pack(side='left', padx=2)
    ttk.Button(btn2, text='复制槽位…', command=_copy_slot).pack(side='left', padx=2)
    ttk.Button(btn2, text='删除槽位', command=_delete_slot).pack(side='left', padx=2)
    ttk.Button(btn2, text='存档码历史…', command=_history_view).pack(side='left', padx=2)
    ttk.Button(btn2, text='打开存档目录', command=_open_dir).pack(side='left', padx=2)
    slot_list.bind('<<TreeviewSelect>>', lambda e: _on_slot_select())
    _refresh_slots()
    root.mainloop()


if __name__ == '__main__':
    main()
