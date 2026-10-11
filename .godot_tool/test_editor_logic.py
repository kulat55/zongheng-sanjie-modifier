# -*- coding: utf-8 -*-
# 存档编辑器核心逻辑端到端测试（临时目录，不碰游戏存档）
import importlib.util, json, os, tempfile, hashlib
spec = importlib.util.spec_from_file_location("se", r"E:\baidu-game\纵横三界修改器\save_editor.py")
se = importlib.util.module_from_spec(spec)
spec.loader.exec_module(se)

tmp = tempfile.mkdtemp(prefix="save_edit_test_")
save = {
    "character_data": {
        "level": 1, "experience": 0, "gold": 0, "backpack_items": [],
        "backpack_items_2p": [], "player_stats": {
            "level": 1, "Hp": 50, "max_Hp": 50, "Mp": 50, "max_Mp": 50, "Exp": 0,
            "max_Exp": 135, "attack": 36, "defense": 0, "magic_defense": 0,
            "critical_rate": 0.1, "critical_damage": 2, "lucky": 5, "dodge": 5,
            "hp_regen": 1, "mp_regen": 3},
        "player_stats_2p": {"level": 1, "Hp": 50, "max_Hp": 50, "Mp": 50, "max_Mp": 50,
            "Exp": 0, "max_Exp": 135, "attack": 36, "defense": 0, "magic_defense": 0,
            "critical_rate": 0.1, "critical_damage": 2, "lucky": 5, "dodge": 5,
            "hp_regen": 1, "mp_regen": 3},
        "soul_count": 0, "soul_count_2p": 0, "max_soul_count": 99999999,
        "skill_cooldowns": {"BLB": 0}, "skill_cooldown_timers": {"BLB": 0},
        "wushuang_energy": 0, "unlocked_level": 1, "last_world_map": 1,
        "level_clears": {}, "missions": {}, "global_spawn_speed": 1,
        "game_difficulty": 1, "is_fashion_showing_1": False,
        "is_fashion_showing_2": False, "show_level_detail": True,
        "show_player_name": True, "selected_loading_bgs": [0, 1],
        "role_equipment": {"current_role": "tang_seng", "roles": {}}},
    "category_states": {"equipment": [{}, {}, {}], "item": [{}, {}, {}],
                        "fashion": [{}, {}, {}], "book": [{}, {}, {}]},
    "category_states_2p": {"equipment": [{}, {}, {}], "item": [{}, {}, {}],
                           "fashion": [{}, {}, {}], "book": [{}, {}, {}]},
    "timestamp_str": "2026-10-11 10:00:00", "timestamp": 1750000000,
    "save_uid": "abcdef0123456789abcdef0123456789",
    "guard": {}
}
p = os.path.join(tmp, "save_1.json")
with open(p, "w", encoding="utf-8") as f:
    f.write(se.godot_json(save))

slots = se.list_slots(tmp)
assert [(s, os.path.basename(pp)) for s, pp in slots] == [(1, "save_1.json")], slots
data = se.load_save(p)
pre = se.slot_preview(data)
print("PREVIEW:", pre)

cd = data["character_data"]
cd["level"] = 50
cd["gold"] = 999999
cd["soul_count"] = 12345678
cd["player_stats"]["attack"] = 123456
cd["backpack_items"] = [{"id": "zly", "名字": "紫灵玉", "数量": 5}]
data["guard"] = se.make_save_guard(data)
se.write_save(p, data)

data2 = se.load_save(p)
g = data2["guard"]
assert isinstance(g, dict) and len(g) == 4, g
assert g["level"] == se.guard_of(50), (g["level"], se.guard_of(50))
assert g["soul"] == se.guard_of(12345678)
assert g["soul2p"] == se.guard_of(0)
assert g["stats"] == se.guard_of(data2["character_data"]["player_stats"])
assert data2["character_data"]["backpack_items"][0]["数量"] == 5
assert data2["timestamp"] > 1750000000
ref = hashlib.md5(("50.0|zhxys-0.67-save-guard").encode()).hexdigest()
assert g["level"] == ref, (g["level"], ref)
print("GUARD_LEVEL_REF_CHECK:", g["level"] == ref)

raw = open(p, "rb").read()
code = se.encode_code(raw)
ok, back = se.decode_code(code)
assert ok and back == raw
print("CODEC_ROUNDTRIP: OK len=%d" % len(raw))

d = se.default_save_dir()
print("AUTO_SAVE_DIR:", d)
import shutil; shutil.rmtree(tmp)
print("ALL_LOGIC_TESTS_PASSED")
