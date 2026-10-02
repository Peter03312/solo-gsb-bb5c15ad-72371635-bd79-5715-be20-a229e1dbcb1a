"""现场出口清点集成测试。

覆盖：
* 登记受理：仅已按固定路线抵达该出口（scheduled/fixed）或已由人工救援
  实际送达该出口（rescued，采用实际出口/时刻）的组可登记；
* 清点分钟窗口：不早于相应到达分钟、不晚于当前分钟；
* 幂等：同流水号同内容重试返回原记录（200, replayed=true），异内容 409；
* 累计：同组全部记录的当前人数累计不得超过组人数；
* 更正：凭原流水号+原版本+非空原因更正为非负人数，保留原值与历史，
  版本 +1，按更正后的累计检查上限；版本失配/同版本重复更正/超额均整次
  拒绝；
* 查询：按组返回应到、已清点、未清点与逐条记录；
* 清点与更正不改编排、不产生修订；场景重置清空当前清点记录。
"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_tmpdir = tempfile.mkdtemp(prefix="evac-test-headcount-")
os.environ["EVAC_DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["EVAC_SEED"] = "false"

from app.main import app  # noqa: E402


def setup_two_exits(c):
    """组1(10人)：1->2(edge1,2min)->6(edge2,2min) 到出口6，第4分钟到达；
    组2(8人)：3->5(edge3,1min) 到出口5，第1分钟到达。"""
    resp = c.post("/scenario", json={
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 2, "capacity": 10},
            {"id": 2, "src": 2, "dst": 6, "minutes": 2, "capacity": 10},
            {"id": 3, "src": 3, "dst": 5, "minutes": 1, "capacity": 10},
        ],
        "exits": [5, 6], "closures": [],
        "groups": [
            {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 30},
            {"id": 2, "size": 8, "origin": 3, "earliest_departure": 0,
             "priority": 5, "deadline": 30}]})
    assert resp.status_code == 200
    return resp.json()


@pytest.fixture()
def client():
    with TestClient(app) as c:
        setup_two_exits(c)
        yield c


def revs(c):
    return c.get("/revisions").json()["revisions"]


def submit(c, serial, group_id, exit_id, count, count_minute):
    return c.post("/headcounts", json={
        "serial": serial, "group_id": group_id, "exit_id": exit_id,
        "count": count, "count_minute": count_minute})


def correct(c, serial, version, count, reason):
    return c.post(f"/headcounts/{serial}/corrections", json={
        "version": version, "count": count, "reason": reason})


# --------------------------------------------------------------------------
# 登记受理
# --------------------------------------------------------------------------

def test_submit_for_scheduled_group_before_departure_rejected(client):
    # 当前分钟 0：组 1 虽已排定第 4 分钟到达出口 6，但在清点分钟 0 尚未
    # 到达 -> 清点分钟早于相应到达分钟，整次拒绝
    resp = submit(client, "s1", 1, 6, 5, 0)
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "HEADCOUNT_NOT_ACCEPTABLE"
    conflict = body["conflicts"][0]
    assert conflict["reason"] == "COUNT_MINUTE_BEFORE_ARRIVAL"
    assert conflict["arrival_type"] == "planned_arrival"
    assert conflict["arrival_minute"] == 4


def test_submit_at_arrival_minute_accepted_scheduled(client):
    # 组 2 第 1 分钟到达出口 5；当前分钟 1，清点分钟取到达分钟，仍受理
    client.post("/tick", json={"minutes": 1})
    resp = submit(client, "s2", 2, 5, 8, 1)
    assert resp.status_code == 201, resp.text
    rec = resp.json()
    assert rec["serial"] == "s2"
    assert rec["count"] == 8
    assert rec["version"] == 1
    assert rec["replayed"] is False
    assert rec["arrival_type"] == "planned_arrival"
    assert rec["arrival_minute"] == 1
    assert rec["count_minute"] == 1
    assert rec["registered_minute"] == 1
    assert rec["corrections"] == []


def test_submit_for_fixed_group_accepted(client):
    # 推进到第 4 分钟：组 1 已固定并抵达出口 6
    client.post("/tick", json={"minutes": 4})
    g1 = {g["group_id"]: g for g in client.get("/plan").json()["groups"]}[1]
    assert g1["status"] == "fixed" and g1["arrival_minute"] == 4
    resp = submit(client, "s1", 1, 6, 6, 4)
    assert resp.status_code == 201
    rec = resp.json()
    assert rec["arrival_type"] == "planned_arrival"
    assert rec["arrival_minute"] == 4


def test_submit_wrong_exit_rejected(client):
    # 组 2 只抵达出口 5；报出口 6（既有出口）仍拒绝
    client.post("/tick", json={"minutes": 1})
    resp = submit(client, "x1", 2, 6, 1, 1)
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "GROUP_NOT_ARRIVED"


def test_submit_unknown_group_or_exit_422(client):
    assert submit(client, "x", 999, 6, 1, 0).status_code == 422
    assert submit(client, "x", 1, 99, 1, 0).status_code == 422


def test_invalid_payload_422(client):
    # 空流水号 / 非正人数 / 负清点分钟
    assert submit(client, "", 1, 6, 1, 0).status_code == 422
    assert submit(client, "s", 1, 6, 0, 0).status_code == 422
    assert submit(client, "s", 1, 6, 1, -1).status_code == 422


def test_non_arrived_statuses_rejected(client):
    # 构造一个 failed 组（人数超容量）与 blocked 组，均不可登记
    client.post("/groups", json=[
        {"id": 9, "size": 200, "origin": 1, "earliest_departure": 0,
         "priority": 1, "deadline": 10}])
    assert submit(client, "f1", 9, 6, 1, 1).status_code == 409
    # blocked：组10 以失败组 9 为先导
    client.post("/groups", json=[
        {"id": 10, "size": 1, "origin": 3, "earliest_departure": 0,
         "priority": 1, "deadline": 30}])
    client.post("/group-prerequisites", json={
        "group_id": 10, "leader_group_id": 9, "release_gap": 0})
    assert submit(client, "b1", 10, 5, 1, 1).status_code == 409


# --------------------------------------------------------------------------
# 清点分钟窗口
# --------------------------------------------------------------------------

def test_count_minute_window(client):
    client.post("/tick", json={"minutes": 4})
    # 早于到达分钟（组1第4分钟到达）
    resp = submit(client, "early", 1, 6, 1, 3)
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == \
        "COUNT_MINUTE_BEFORE_ARRIVAL"
    # 晚于当前分钟
    resp = submit(client, "future", 1, 6, 1, 5)
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "COUNT_MINUTE_IN_FUTURE"
    # 边界：等于到达分钟与等于当前分钟均受理
    assert submit(client, "edge1", 1, 6, 1, 4).status_code == 201
    assert submit(client, "edge2", 1, 6, 1, 4).status_code == 201


def test_late_idempotent_replay_after_tick_returns_original(client):
    # 第 4 分钟登记，之后推进到第 8 分钟：同内容重试仍返回原记录
    client.post("/tick", json={"minutes": 4})
    first = submit(client, "r1", 1, 6, 4, 4)
    assert first.status_code == 201
    client.post("/tick", json={"minutes": 4})
    retry = submit(client, "r1", 1, 6, 4, 4)
    assert retry.status_code == 200
    rec = retry.json()
    assert rec["replayed"] is True
    assert rec["registered_minute"] == 4
    # 不产生第二条记录
    report = client.get("/headcounts").json()
    recs = [r for g in report["groups"] for r in g["records"]]
    assert len([r for r in recs if r["serial"] == "r1"]) == 1


def test_same_serial_different_content_rejected(client):
    client.post("/tick", json={"minutes": 4})
    assert submit(client, "dup", 1, 6, 4, 4).status_code == 201
    # 改人数
    resp = submit(client, "dup", 1, 6, 3, 4)
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "HEADCOUNT_CONFLICT"
    # 改组/出口/分钟同样拒绝
    assert submit(client, "dup", 1, 5, 4, 4).status_code == 409
    # 原记录内容保持
    rec = client.get("/headcounts").json()
    orig = [r for g in rec["groups"] for r in g["records"]
            if r["serial"] == "dup"][0]
    assert orig["count"] == 4 and orig["exit_id"] == 6


# --------------------------------------------------------------------------
# 累计上限
# --------------------------------------------------------------------------

def test_cumulative_count_must_not_exceed_group_size(client):
    client.post("/tick", json={"minutes": 4})
    assert submit(client, "c1", 1, 6, 6, 4).status_code == 201
    # 同组再清点 5 人：6+5 > 10
    resp = submit(client, "c2", 1, 6, 5, 4)
    assert resp.status_code == 409
    conflict = resp.json()["conflicts"][0]
    assert conflict["reason"] == "GROUP_COUNT_EXCEEDED"
    assert conflict["group_size"] == 10
    assert conflict["already_counted"] == 6
    assert conflict["new_total"] == 11
    # 恰好不超可登记（6+4=10 已满）；同组再清点任意正数都拒绝
    assert submit(client, "c3", 1, 6, 4, 4).status_code == 201
    assert submit(client, "c4", 1, 6, 1, 4).status_code == 409


def test_counts_across_different_exits_also_cumulative_per_group(client):
    # 组 1 仅抵达出口 6；先在出口 6 清点部分人数，随后（即便出口字段不同）
    # 任何新登记都纳入同组累计——出口不同本身会先被 GROUP_NOT_ARRIVED
    # 拦截，这里验证组人数口径是按组而非按出口
    client.post("/tick", json={"minutes": 4})
    submit(client, "c1", 1, 6, 7, 4)
    # 报出口 5：组 1 从未抵达出口 5 -> GROUP_NOT_ARRIVED（而非超额）
    resp = submit(client, "c2", 1, 5, 4, 4)
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "GROUP_NOT_ARRIVED"


# --------------------------------------------------------------------------
# 更正
# --------------------------------------------------------------------------

def test_correction_keeps_history_and_bumps_version(client):
    client.post("/tick", json={"minutes": 4})
    submit(client, "k1", 1, 6, 6, 4)
    # 6 -> 3（释放 3 个名额），版本 1 -> 2
    resp = correct(client, "k1", 1, 3, "重复清点，扣减 3 人")
    assert resp.status_code == 200
    rec = resp.json()
    assert rec["count"] == 3
    assert rec["version"] == 2
    assert rec["replayed"] is False
    assert len(rec["corrections"]) == 1
    corr = rec["corrections"][0]
    assert corr["from_version"] == 1 and corr["to_version"] == 2
    assert corr["old_count"] == 6 and corr["new_count"] == 3
    assert corr["reason"] == "重复清点，扣减 3 人"
    assert corr["corrected_minute"] == 4

    # 再更正 2 -> 9，历史追加、原值保留
    client.post("/tick", json={"minutes": 1})
    resp = correct(client, "k1", 2, 9, "补充找回 6 人")
    assert resp.status_code == 200
    rec = resp.json()
    assert rec["count"] == 9 and rec["version"] == 3
    assert [c["old_count"] for c in rec["corrections"]] == [6, 3]
    assert [c["new_count"] for c in rec["corrections"]] == [3, 9]
    assert rec["corrections"][1]["corrected_minute"] == 5


def test_correction_to_zero_allowed(client):
    client.post("/tick", json={"minutes": 4})
    submit(client, "z1", 1, 6, 5, 4)
    resp = correct(client, "z1", 1, 0, "整笔误报，清零")
    assert resp.status_code == 200
    assert resp.json()["count"] == 0
    report = client.get("/headcounts").json()
    g1 = next(g for g in report["groups"] if g["group_id"] == 1)
    assert g1["counted_count"] == 0


def test_correction_version_mismatch_rejected(client):
    client.post("/tick", json={"minutes": 4})
    submit(client, "v1", 1, 6, 6, 4)
    correct(client, "v1", 1, 3, "第一次更正")
    # 拿从未存在过的版本 9 更正 -> VERSION_MISMATCH
    resp = correct(client, "v1", 9, 2, "基于不存在的版本")
    assert resp.status_code == 409
    conflict = resp.json()["conflicts"][0]
    assert conflict["reason"] == "VERSION_MISMATCH"
    assert conflict["submitted_version"] == 9
    assert conflict["current_version"] == 2
    # 当前值不被改动
    report = client.get("/headcounts").json()
    rec = [r for g in report["groups"] for r in g["records"]
           if r["serial"] == "v1"][0]
    assert rec["count"] == 3 and rec["version"] == 2


def test_correction_same_version_twice_rejected(client):
    client.post("/tick", json={"minutes": 4})
    submit(client, "v2", 1, 6, 2, 4)
    # 版本已是 2；用版本 2 再更正一次成功
    assert correct(client, "v2", 1, 3, "更正").status_code == 200
    assert correct(client, "v2", 2, 4, "再更正").status_code == 200
    # 再拿旧版本 2（当前版本已为 3）顺序提交：该版本已被消费，重复更正
    resp = correct(client, "v2", 2, 1, "拿已消费的旧版本更正")
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "ALREADY_CORRECTED"
    # 拿一个从未存在过的版本 9：版本失配
    resp = correct(client, "v2", 9, 1, "基于不存在的版本")
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "VERSION_MISMATCH"
    # 历史只有两条，当前版本 3
    report = client.get("/headcounts").json()
    rec = [r for g in report["groups"] for r in g["records"]
           if r["serial"] == "v2"][0]
    assert rec["version"] == 3 and len(rec["corrections"]) == 2


def test_concurrent_correction_same_version_only_one_wins(client):
    """两个客户端持同一版本并发更正：一个成功，另一个 ALREADY_CORRECTED。"""
    from concurrent.futures import ThreadPoolExecutor

    client.post("/tick", json={"minutes": 4})
    submit(client, "cc", 1, 6, 2, 4)
    payload = {"version": 1, "count": 1, "reason": "并发更正"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(
            lambda _: client.post("/headcounts/cc/corrections",
                                  json=payload), range(2)))
    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200, 409]
    loser = next(r for r in responses if r.status_code == 409)
    assert loser.json()["conflicts"][0]["reason"] == "ALREADY_CORRECTED"
    report = client.get("/headcounts").json()
    rec = [r for g in report["groups"] for r in g["records"]
           if r["serial"] == "cc"][0]
    assert rec["version"] == 2 and len(rec["corrections"]) == 1


def test_correction_over_limit_rejected(client):
    client.post("/tick", json={"minutes": 4})
    submit(client, "o1", 1, 6, 6, 4)
    submit(client, "o2", 1, 6, 3, 4)  # 累计 9
    # 把 o1 从 6 更正为 8：8+3=11 > 10 -> 整次拒绝
    resp = correct(client, "o1", 1, 8, "多记")
    assert resp.status_code == 409
    conflict = resp.json()["conflicts"][0]
    assert conflict["reason"] == "GROUP_COUNT_EXCEEDED"
    assert conflict["group_size"] == 10
    assert conflict["new_total"] == 11
    # 原值保留、无新增历史
    report = client.get("/headcounts").json()
    rec = [r for g in report["groups"] for r in g["records"]
           if r["serial"] == "o1"][0]
    assert rec["count"] == 6 and rec["version"] == 1
    # 更正到 7（7+3=10）允许
    assert correct(client, "o1", 1, 7, "补记一人").status_code == 200


def test_correction_can_free_capacity_for_new_submission(client):
    client.post("/tick", json={"minutes": 4})
    submit(client, "f1", 1, 6, 10, 4)
    assert submit(client, "f2", 1, 6, 1, 4).status_code == 409
    # 减记到 6，腾出 4 个名额
    correct(client, "f1", 1, 6, "扣减")
    resp = submit(client, "f2", 1, 6, 4, 4)
    assert resp.status_code == 201
    g1 = next(g for g in client.get("/headcounts").json()["groups"]
              if g["group_id"] == 1)
    assert g1["counted_count"] == 10


def test_correction_unknown_serial_404_and_blank_reason_422(client):
    client.post("/tick", json={"minutes": 4})
    submit(client, "b1", 1, 6, 1, 4)
    assert client.post("/headcounts/nope/corrections",
                       json={"version": 1, "count": 1,
                             "reason": "x"}).status_code == 404
    assert correct(client, "b1", 1, 1, "   ").status_code == 422
    # 负人数 422（pydantic）
    assert client.post("/headcounts/b1/corrections",
                       json={"version": 1, "count": -1,
                             "reason": "x"}).status_code == 422


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------

def test_headcount_report_per_group(client):
    client.post("/tick", json={"minutes": 4})
    submit(client, "q1", 1, 6, 6, 4)
    correct(client, "q1", 1, 5, "扣 1")
    submit(client, "q2", 1, 6, 2, 4)
    report = client.get("/headcounts").json()
    assert report["current_minute"] == 4
    g1 = next(g for g in report["groups"] if g["group_id"] == 1)
    g2 = next(g for g in report["groups"] if g["group_id"] == 2)
    assert g1["expected_count"] == 10
    assert g1["counted_count"] == 7  # 5（更正后）+ 2
    assert g1["remaining_count"] == 3
    assert [r["serial"] for r in g1["records"]] == ["q1", "q2"]
    # 组 2 尚无记录，也在汇总中
    assert g2["expected_count"] == 8
    assert g2["counted_count"] == 0
    assert g2["remaining_count"] == 8
    assert g2["records"] == []


def test_single_group_headcount_query(client):
    client.post("/tick", json={"minutes": 4})
    submit(client, "q1", 1, 6, 3, 4)
    resp = client.get("/groups/1/headcounts")
    assert resp.status_code == 200
    groups = resp.json()["groups"]
    assert [g["group_id"] for g in groups] == [1]
    assert groups[0]["records"][0]["serial"] == "q1"
    assert client.get("/groups/999/headcounts").status_code == 404


# --------------------------------------------------------------------------
# 不改编排与修订 / 场景重置
# --------------------------------------------------------------------------

def test_headcount_and_correction_change_neither_plan_nor_revisions(client):
    client.post("/tick", json={"minutes": 4})
    plan_before = client.get("/plan").json()
    n0 = len(revs(client))
    submit(client, "p1", 1, 6, 6, 4)
    correct(client, "p1", 1, 4, "扣减")
    assert client.get("/plan").json() == plan_before
    assert len(revs(client)) == n0


def test_scenario_reset_clears_headcounts(client):
    client.post("/tick", json={"minutes": 4})
    submit(client, "h1", 1, 6, 6, 4)
    correct(client, "h1", 1, 5, "扣减")
    revisions_before = revs(client)
    setup_two_exits(client)  # 重置场景
    report = client.get("/headcounts").json()
    assert all(g["records"] == [] for g in report["groups"])
    assert client.post("/headcounts/h1/corrections",
                       json={"version": 1, "count": 1,
                             "reason": "x"}).status_code == 404
    # 同流水号重置后可重新登记；旧修订保留
    client.post("/tick", json={"minutes": 4})
    assert submit(client, "h1", 1, 6, 1, 4).status_code == 201
    assert revs(client)[:len(revisions_before)] == revisions_before


# --------------------------------------------------------------------------
# 人工救援实际送达：采用实际出口与时刻
# --------------------------------------------------------------------------

def make_awaiting_rescue_and_rescue(c, *, rescue_exit=6, delivery_minute=None):
    """构造待救援组1，注册队1并派遣；返回任务号（不结案）。"""
    c.post("/scenario", json={
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
            {"id": 2, "src": 2, "dst": 6, "minutes": 1, "capacity": 10},
        ],
        "exits": [6],
        "closures": [{"edge_id": 2, "start_minute": 5, "end_minute": 30}],
        "groups": [
            {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 30}]})
    c.post("/tick", json={"minutes": 4})
    c.post("/groups/1/delay-report",
           json={"node_id": 2, "recovery_minute": 6})
    c.post("/rescue-teams", json={"team_id": 1, "capacity": 10})
    task = c.post("/rescue-tasks", json={
        "group_id": 1, "team_id": 1, "node_id": 2,
        "observed_minute": 4}).json()["task_id"]
    if delivery_minute is None:
        delivery_minute = 4
    if delivery_minute > 4:
        c.post("/tick", json={"minutes": delivery_minute - 4})
    c.post(f"/rescue-tasks/{task}/completion", json={
        "exit_id": rescue_exit, "delivery_minute": delivery_minute})
    return task


def test_rescued_group_headcount_uses_actual_exit_and_minute(client):
    make_awaiting_rescue_and_rescue(client, rescue_exit=6,
                                   delivery_minute=6)
    g1 = {g["group_id"]: g for g in client.get("/plan").json()["groups"]}[1]
    assert g1["status"] == "rescued"
    # 规划到达字段为空；清点以实际送达分钟 6 为锚点
    assert g1["exit_id"] is None and g1["arrival_minute"] is None

    # 清点分钟早于实际送达分钟 -> COUNT_MINUTE_BEFORE_DELIVERY
    resp = submit(client, "r1", 1, 6, 5, 5)
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == \
        "COUNT_MINUTE_BEFORE_DELIVERY"
    # 以实际送达分钟登记受理
    resp = submit(client, "r1", 1, 6, 10, 6)
    assert resp.status_code == 201
    rec = resp.json()
    assert rec["arrival_type"] == "actual_delivery"
    assert rec["arrival_minute"] == 6
    assert rec["count_minute"] == 6


def test_rescuing_group_not_arrived(client):
    make_awaiting_rescue_and_rescue(client)  # 结案至 rescued
    # 另构造一个仍处于 rescuing 的组
    client.post("/scenario", json={
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
            {"id": 2, "src": 2, "dst": 6, "minutes": 1, "capacity": 10},
        ],
        "exits": [6],
        "closures": [{"edge_id": 2, "start_minute": 5, "end_minute": 30}],
        "groups": [
            {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 30}]})
    client.post("/tick", json={"minutes": 4})
    client.post("/groups/1/delay-report",
                json={"node_id": 2, "recovery_minute": 6})
    client.post("/rescue-teams", json={"team_id": 1, "capacity": 10})
    client.post("/rescue-tasks", json={
        "group_id": 1, "team_id": 1, "node_id": 2, "observed_minute": 4})
    # 救援中、尚未送达：不可登记
    resp = submit(client, "ing1", 1, 6, 10, 4)
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "GROUP_NOT_ARRIVED"


def test_awaiting_rescue_group_not_arrived(client):
    client.post("/scenario", json={
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
            {"id": 2, "src": 2, "dst": 6, "minutes": 1, "capacity": 10},
        ],
        "exits": [6],
        "closures": [{"edge_id": 2, "start_minute": 5, "end_minute": 30}],
        "groups": [
            {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 30}]})
    client.post("/tick", json={"minutes": 4})
    client.post("/groups/1/delay-report",
                json={"node_id": 2, "recovery_minute": 6})
    resp = submit(client, "a1", 1, 6, 10, 4)
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "GROUP_NOT_ARRIVED"
