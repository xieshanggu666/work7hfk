"""多章远征：创建/章节交接携带成长/战败结算更新解锁/防重复开章与重复结算/整程回放。

测试策略：经 monkeypatch 替换敌人注册表（弱敌必胜、强敌必败），全程只走合法
动作驱动状态推进，因此动作日志自洽、整程回放的逐步校验点必须逐位一致。
"""
import pytest

from app import db, mapgen, service
from app import enemies as enemies_mod
from app.service import _apply_action, _new_run_state

ENEMY_IDS = ["goblin", "wolf", "brute", "maggot", "vampire", "echo_knight",
             "elite_warlord", "boss_ancient"]


def _enemy(eid, hp, dmg, boss=False):
    return {
        "id": eid, "name": eid, "hp": hp, "tier": "basic",
        "kind": "boss" if boss else "normal",
        "reward_cards": [], "reward_gold": 30,
        "skills": [{"name": "攻击", "hint": "", "effects": [
            {"type": "damage", "value": dmg, "target": "player", "tags": []}]}],
        "boss": boss, "phases": [],
    }


@pytest.fixture
def weak_enemies(monkeypatch):
    """所有敌人 6 血 1 攻打不过玩家：任何战斗都能被简单 AI 终结。"""
    for eid in ENEMY_IDS:
        monkeypatch.setitem(enemies_mod.ENEMIES, eid,
                            _enemy(eid, 6, 1, boss=(eid == "boss_ancient")))


@pytest.fixture
def strong_enemies(monkeypatch):
    """所有敌人 300 血 50 攻：玩家很快战败。"""
    for eid in ENEMY_IDS:
        monkeypatch.setitem(enemies_mod.ENEMIES, eid,
                            _enemy(eid, 300, 50, boss=(eid == "boss_ancient")))


def _act(client, rid, **action):
    return client.post(f"/api/runs/{rid}/act", json=action)


def _resume(client, rid):
    return client.get(f"/api/runs/{rid}/resume").json()


def _auto_win_battle(client, rid):
    """简单 AI：能出就出费用最低的手牌，否则结束回合，直到战斗结束。"""
    for _ in range(120):
        v = _resume(client, rid)
        if not v["in_battle"]:
            return v
        hand = [h for h in v["battle"]["hand"] if isinstance(h, dict)]
        energy = v["battle"]["energy"]
        playable = sorted((h for h in hand if h["cost"] <= energy), key=lambda h: h["cost"])
        moved = False
        if playable:
            moved = _act(client, rid, action="play", card=playable[0]["uid"]).status_code == 200
        if not moved:
            _act(client, rid, action="end_turn")
    raise AssertionError("battle did not finish")


def _auto_lose_battle(client, rid):
    """空过回合直到战败。"""
    for _ in range(60):
        v = _resume(client, rid)
        if not v["in_battle"]:
            return v
        _act(client, rid, action="end_turn")
    raise AssertionError("battle did not finish")


def _walk_to_chapter_end(client, rid):
    """从当前位置推进到本章首领并击败（敌人须为弱版）。

    返回首领被击败后的视口：非终章 pending_next=True；终章 status=won。
    """
    for _ in range(16):
        v = _resume(client, rid)
        if v["status"] != "in_progress":
            return v
        exp = v.get("expedition")
        if exp and exp.get("pending_next"):
            return v
        if v["in_battle"]:
            _auto_win_battle(client, rid)
            continue
        if not v["reward_claimed"] and v["reward_options"]:
            gi = next((i for i, o in enumerate(v["reward_options"]) if o["kind"] == "gold"), 0)
            _act(client, rid, action="claim_reward", option=gi)
            continue
        reachable = v["reachable"]
        assert reachable, "章节未结束但已无可走节点"
        nxt = next((n for n in reachable if n["type"] in ("encounter", "elite", "boss")),
                   reachable[0])
        r = _act(client, rid, action="choose_node", node=nxt["id"])
        assert r.status_code == 200, r.json()
    raise AssertionError("did not reach chapter end")


# ---------- 创建 ----------
def test_create_expedition_view_and_validation(client):
    r = client.post("/api/expeditions", json={"seed": 1234, "chapters": 3})
    assert r.status_code == 200
    v = r.json()
    exp = v["expedition"]
    assert exp["enabled"] is True
    assert (exp["chapter"], exp["total_chapters"]) == (1, 3)
    assert exp["pending_next"] is False and exp["settled"] is False
    assert exp["history"] == []
    assert v["status"] == "in_progress" and v["position"] == "start"
    # 默认 3 章
    v2 = client.post("/api/expeditions", json={}).json()
    assert v2["expedition"]["total_chapters"] == 3
    # 非法章节数
    assert client.post("/api/expeditions", json={"chapters": 1}).status_code == 400
    assert client.post("/api/expeditions", json={"chapters": 10}).status_code == 400
    # 普通局不带远征字段
    v3 = client.post("/api/runs", json={"seed": 1}).json()
    assert v3["expedition"] is None


# ---------- 章节交接：携带成长资产 ----------
def test_chapter_clear_and_transition_carries_growth(client, weak_enemies):
    rid = client.post("/api/expeditions", json={"seed": 2024, "chapters": 2}).json()["run_id"]
    v = _walk_to_chapter_end(client, rid)
    exp = v["expedition"]
    # 首领已击败：待开下一章，run 不结束
    assert v["status"] == "in_progress"
    assert exp["pending_next"] is True and exp["chapter"] == 1
    assert exp["history"][0]["result"] == "cleared"
    deck_before = [c["uid"] for c in v["deck"]]
    gold_before, hp_before = v["gold"], v["health"]
    map_before = v["map"]["nodes"]

    # 待开章期间不能在旧地图上继续走点
    assert _act(client, rid, action="choose_node", node="0-0").status_code == 400

    # 开下一章（带幂等令牌）：成长资产全部携带，章节奖励交接
    r = _act(client, rid, action="next_chapter", request_id="adv-1")
    assert r.status_code == 200
    adv = next(x["chapter_advanced"] for x in r.json()["log"] if "chapter_advanced" in x)
    assert adv["chapter"] == 2 and adv["gold_bonus"] == 45 and adv["heal"] == 30
    v = r.json()["run"]
    exp = v["expedition"]
    assert (exp["chapter"], exp["pending_next"]) == (2, False)
    assert v["position"] == "start" and v["in_battle"] is False
    assert [c["uid"] for c in v["deck"]] == deck_before      # 牌组（含锻造实例）携带
    assert v["relics"] is not None
    assert v["gold"] == gold_before + 45                     # 通关金币交接
    assert v["health"] == min(v["max_health"], hp_before + 30)  # 营地休整
    assert v["map"]["nodes"] != map_before                   # 新章节地图（种子派生）
    assert exp["history"][0]["gold_bonus"] == 45 and exp["history"][0]["heal"] == 30

    # 幂等：同一 request_id 重试返回首次响应，不重复开章/不重复发金币
    r2 = _act(client, rid, action="next_chapter", request_id="adv-1")
    assert r2.json()["duplicate"] is True
    assert r2.json()["run"]["expedition"]["chapter"] == 2
    assert r2.json()["run"]["gold"] == gold_before + 45
    # 防重复开章：无待开章时重复请求 -> 409，金币不变
    r3 = _act(client, rid, action="next_chapter")
    assert r3.status_code == 409
    assert _resume(client, rid)["gold"] == gold_before + 45


def test_next_chapter_requires_pending(client):
    rid = client.post("/api/expeditions", json={"seed": 7, "chapters": 2}).json()["run_id"]
    # 未击败首领就开章 -> 409
    assert _act(client, rid, action="next_chapter").status_code == 409
    # 普通局没有远征 -> 400
    rid2 = client.post("/api/runs", json={"seed": 7}).json()["run_id"]
    assert _act(client, rid2, action="next_chapter").status_code == 400


def test_chapter_handoff_preserves_forge_and_relics():
    """纯推演：锻造成长/遗物/金币/生命交接的确定性（含重复开章拦截）。"""
    run = _new_run_state(99, expedition_chapters=3)
    m1 = mapgen.generate_map(99)
    uid = run["deck"][0]
    run["gold"] = 100
    run["forge_claimed"] = False
    _apply_action(run, "forge", {"action": "forge", "card": uid, "branch": "sharpen"}, m1)
    run["relics"]["power_up"] = 1
    run["health"] = 40
    run["expedition"]["pending_next"] = True

    log = _apply_action(run, "next_chapter", {"action": "next_chapter"}, m1)
    adv = log[0]["chapter_advanced"]
    assert adv["chapter"] == 2 and adv["gold_bonus"] == 45 and adv["heal"] == 30
    assert run["card_instances"][uid]["forges"] == ["sharpen"]   # 锻造成长携带
    assert run["relics"] == {"power_up": 1}                      # 遗物携带
    assert run["gold"] == 100 - 25 + 45                          # 锻造扣款 + 通关金币
    assert run["health"] == 70                                   # 40 + 休整 30
    assert run["expedition"]["chapter"] == 2
    assert run["position"] == "start" and run["battle"] is None
    # 第 2 章地图与第 1 章不同，且按种子确定
    assert service._run_map(run, m1)["nodes"] != m1["nodes"]
    assert service._run_map(run, m1) == service._run_map(run, m1)
    # 重复开章 -> 409 语义（DuplicateReward），状态不变
    with pytest.raises(service.DuplicateReward):
        _apply_action(run, "next_chapter", {"action": "next_chapter"}, m1)


# ---------- 整程通关：只结算一次 ----------
def test_expedition_full_clear_settles_once(client, weak_enemies):
    rid = client.post("/api/expeditions", json={"seed": 555, "chapters": 2}).json()["run_id"]
    _walk_to_chapter_end(client, rid)
    assert _act(client, rid, action="next_chapter").status_code == 200
    v = _walk_to_chapter_end(client, rid)
    exp = v["expedition"]
    assert v["status"] == "won"
    assert exp["settled"] is True
    assert [h["result"] for h in exp["history"]] == ["cleared", "cleared"]
    # 已结算：任何行动都被拒绝，不会重复结算
    assert _act(client, rid, action="next_chapter").status_code == 400
    assert _act(client, rid, action="choose_node", node="0-0").status_code == 400
    # 通关不解锁新卡
    assert db.get_profile() is None


# ---------- 战败结算：更新解锁 ----------
def test_expedition_loss_unlocks_per_cleared_chapter(client, weak_enemies, monkeypatch):
    rid = client.post("/api/expeditions", json={"seed": 909, "chapters": 3}).json()["run_id"]
    # 连过两章
    _walk_to_chapter_end(client, rid)
    _act(client, rid, action="next_chapter")
    _walk_to_chapter_end(client, rid)
    _act(client, rid, action="next_chapter")
    assert _resume(client, rid)["expedition"]["chapter"] == 3
    # 第 3 章换成强敌：走进第一场战斗并战败
    for eid in ENEMY_IDS:
        monkeypatch.setitem(enemies_mod.ENEMIES, eid,
                            _enemy(eid, 300, 50, boss=(eid == "boss_ancient")))
    v = _resume(client, rid)
    node = next(n for n in v["reachable"] if n["type"] in ("encounter", "elite"))
    _act(client, rid, action="choose_node", node=node["id"])
    v = _auto_lose_battle(client, rid)

    exp = v["expedition"]
    assert v["status"] == "lost"
    assert exp["settled"] is True
    assert [h["result"] for h in exp["history"]] == ["cleared", "cleared", "lost"]
    # 解锁数 = 已通关章节数（2）
    prof = db.get_profile()
    assert prof is not None
    assert len(prof["unlocked"]) == len(service.START_DECK) + 2
    # 结算只发生一次：后续行动一律 400，profile 不再变化
    assert _act(client, rid, action="end_turn").status_code == 400
    assert db.get_profile() == prof


def test_expedition_loss_in_chapter_one_unlocks_one(client, strong_enemies):
    rid = client.post("/api/expeditions", json={"seed": 313, "chapters": 2}).json()["run_id"]
    v = _resume(client, rid)
    node = next(n for n in v["reachable"] if n["type"] in ("encounter", "elite"))
    _act(client, rid, action="choose_node", node=node["id"])
    v = _auto_lose_battle(client, rid)
    assert v["status"] == "lost"
    assert v["expedition"]["settled"] is True
    assert [h["result"] for h in v["expedition"]["history"]] == ["lost"]
    prof = db.get_profile()
    assert len(prof["unlocked"]) == len(service.START_DECK) + 1


# ---------- 整程回放 ----------
def test_expedition_replay_full_course_verified(client, weak_enemies):
    """整程回放：跨章逐步重建，所有校验点逐位一致；回放只读隔离。"""
    rid = client.post("/api/expeditions", json={"seed": 777, "chapters": 2}).json()["run_id"]
    _walk_to_chapter_end(client, rid)
    _act(client, rid, action="next_chapter")
    _walk_to_chapter_end(client, rid)
    assert _resume(client, rid)["status"] == "won"

    events_before = db.load_events(rid)
    rep = client.get(f"/api/runs/{rid}/replay").json()
    # 校验点全程通过（跨章地图派生与在线一致）
    ver = rep["verification"]
    assert ver["mismatch"] == 0 and ver["error"] == 0
    assert ver["final_match"] is True
    # 开章步骤：类型/标题/摘要齐全，帧视口进入第 2 章
    adv = next(s for s in rep["steps"] if s["action"] == "next_chapter")
    assert adv["kind"] == "chapter"
    assert "第 2" in adv["title"]
    assert adv["view"]["expedition"]["chapter"] == 2
    assert adv["view"]["position"] == "start"
    # 章节通关步：战斗结果事件带 chapter_clear
    clear = next(s for s in rep["steps"]
                 if any(e.get("result") == "chapter_clear" for e in s["events"]))
    assert clear["result"] == "chapter_clear"
    # 两章地图不同；第 2 章的帧视口用新地图
    ch1_step = next(s for s in rep["steps"]
                    if s["action"] == "choose_node" and s["view"]["expedition"]["chapter"] == 1)
    ch2_step = next(s for s in rep["steps"]
                    if s["action"] == "choose_node" and s["view"]["expedition"]["chapter"] == 2)
    assert ch1_step["view"]["map"]["nodes"] != ch2_step["view"]["map"]["nodes"]
    # 终局视口：won + 已结算
    assert rep["final_view"]["status"] == "won"
    assert rep["final_view"]["expedition"]["settled"] is True
    # 只读隔离：回放不写日志、不改解锁
    assert db.load_events(rid) == events_before
    assert db.get_profile() is None
    assert rep["isolated"] is True


def test_expedition_replay_loss_does_not_double_settle(client, strong_enemies):
    """战败远征的回放：grant_unlocks=False，重放战败步不重复结算/发解锁。"""
    rid = client.post("/api/expeditions", json={"seed": 414, "chapters": 2}).json()["run_id"]
    v = _resume(client, rid)
    node = next(n for n in v["reachable"] if n["type"] in ("encounter", "elite"))
    _act(client, rid, action="choose_node", node=node["id"])
    _auto_lose_battle(client, rid)
    prof_after_loss = db.get_profile()
    assert prof_after_loss is not None

    rep = client.get(f"/api/runs/{rid}/replay").json()
    assert rep["final_view"]["status"] == "lost"
    assert rep["final_view"]["expedition"]["settled"] is True
    # 回放后 profile 与战败结算时完全一致（没有第二次解锁）
    assert db.get_profile() == prof_after_loss
