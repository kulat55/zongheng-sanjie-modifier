# -*- coding: utf-8 -*-
# 存档编辑器 v2 新功能端到端测试
import importlib.util, json, os, tempfile, shutil
spec = importlib.util.spec_from_file_location("se", r"E:\baidu-game\纵横三界修改器\save_editor.py")
se = importlib.util.module_from_spec(spec)
spec.loader.exec_module(se)

tmp = tempfile.mkdtemp(prefix="se2_test_")

# ---- 合规检查 ----
good = {"character_data": {"level": 5, "soul_count": 100, "player_stats": {"attack": 50},
                           "backpack_items": [{"id": "a", "数量": 3}], "level_clears": {}}}
bad = {"character_data": {"level": 999, "soul_count": 10 ** 15, "last_world_map": 1,
                          "player_stats": {"level": 5, "attack": 10 ** 7},
                          "backpack_items": [{"id": "a", "数量": 99999999}], "level_clears": {}}}
good_p = se.check_save_compliance(good)
bad_p = se.check_save_compliance(bad)
assert not good_p, good_p
assert any('等级=999' in p for p in bad_p), bad_p
assert any('灵魂' in p for p in bad_p), bad_p
assert any('attack' in p for p in bad_p), bad_p
assert any('数量' in p for p in bad_p), bad_p
print("COMPLIANCE_CHECK_OK  good=%d bad=%d" % (len(good_p), len(bad_p)))

# ---- 存档码历史 ----
hist = [{"slot": 1, "minute": 123, "time": "2026-10-11 10:00", "role": "唐僧", "level": 3,
         "code": se.encode_code(se.godot_json(good).encode('utf-8'))}]
with open(os.path.join(tmp, "save_history.json"), "w", encoding="utf-8") as f:
    f.write(se.godot_json(hist))
got = se.read_save_history(tmp)
assert len(got) == 1 and got[0]["role"] == "唐僧"
ok, raw = se.decode_code(got[0]["code"])
assert ok and json.loads(raw.decode("utf-8"))["character_data"]["level"] == 5
print("HISTORY_READ_OK")

# ---- 完整 JSON 编辑往返：改 missions 等任意字段 ----
save = {"character_data": {"level": 1, "experience": 0, "gold": 0, "backpack_items": [],
                           "player_stats": {"level": 1, "Hp": 50, "max_Hp": 50, "Mp": 50,
                                            "max_Mp": 50, "Exp": 0, "max_Exp": 135, "attack": 36,
                                            "defense": 0, "magic_defense": 0, "critical_rate": 0.1,
                                            "critical_damage": 2, "lucky": 5, "dodge": 5,
                                            "hp_regen": 1, "mp_regen": 3},
                           "missions": {"main_1": {"done": True, "rewards_taken": True}},
                           "role_equipment": {"current_role": "tang_seng", "roles": {}}},
        "category_states": {"equipment": [{}, {}, {}], "item": [{}, {}, {}],
                            "fashion": [{}, {}, {}], "book": [{}, {}, {}]},
        "timestamp_str": "2026-10-11 10:00:00", "timestamp": 1750000000,
        "save_uid": "x" * 32, "guard": {}}
p = os.path.join(tmp, "save_2.json")
with open(p, "w", encoding="utf-8") as f:
    f.write(se.godot_json(save))
data = se.load_save(p)
# 模拟“完整JSON编辑”流程：文本 → 解析 → 应用 → 保存
text = se.godot_json(data)
edited = json.loads(text)
edited["character_data"]["missions"]["main_1"]["done"] = False
edited["character_data"]["wushuang_energy"] = 100
edited["character_data"]["skill_cooldowns"] = {"BLB": 0, "XBZ": 0}
se.write_save(p, edited)
back = se.load_save(p)
assert back["character_data"]["missions"]["main_1"]["done"] is False
assert back["character_data"]["wushuang_energy"] == 100
assert back["character_data"]["skill_cooldowns"] == {"BLB": 0, "XBZ": 0}
g = back["guard"]
assert g["level"] == se.guard_of(1) and g["soul"] == se.guard_of(0)
print("JSON_EDIT_ROUNDTRIP_OK")

# ---- 槽位复制 ----
dst = os.path.join(tmp, "save_3.json")
shutil.copy2(p, dst)
d3 = se.load_save(dst)
assert d3["character_data"]["wushuang_energy"] == 100
print("SLOT_COPY_OK")

shutil.rmtree(tmp)
print("EDITOR_V2_ALL_TESTS_PASSED")
