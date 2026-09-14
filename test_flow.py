#!/usr/bin/env python3
"""端到端验收: 混合题型(单选+简答)考试全流程 —— 交卷即判客观题、老师评简答、发布成绩后学生查分。"""
import json
import os
import tempfile

os.environ["QUIZ_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")

import threading
import time

from app import app, db

PASS, FAIL = "✅", "❌"
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"{PASS if cond else FAIL} {name}" + (f"  -- {detail}" if detail and not cond else ""))


def make_exam(client, duration=60, students=("张三", "李四"), publish=True):
    r = client.post("/api/exams", json={
        "title": "9月安全培训测验",
        "duration_seconds": duration,
        "questions": [
            {"id": "q1", "text": "灭火器压力表指针在哪个区域正常?", "type": "single",
             "options": ["红色", "绿色", "黄色"], "answer": 1},
            {"id": "q2", "text": "发生火灾时能否乘坐电梯逃生?", "type": "single",
             "options": ["能", "不能"], "answer": 1},
            {"id": "q3", "text": "简述你所在岗位的疏散路线。", "type": "text", "points": 5},
        ],
        "students": list(students),
    })
    assert r.status_code == 200, r.get_json()
    exam = r.get_json()
    assert exam["status"] == "draft", "新创建的考试应为草稿"
    if publish:
        r = client.post(f"/api/exams/{exam['exam_id']}/publish")
        assert r.get_json()["status"] == "published"
    return exam


def token_of(exam, name):
    return next(l["url"].split("/s/")[1] for l in exam["links"] if l["student"] == name)


def release_results(client, exam_id):
    r = client.post(f"/api/exams/{exam_id}/publish-results")
    assert r.status_code == 200, r.get_json()
    return r.get_json()


# ---------- 场景1: 两人并发答题交卷 -> 老师评简答 -> 发布成绩 -> 学生各看各的 ----------
client = app.test_client()
exam = make_exam(client)
tokens = {n: token_of(exam, n) for n in ("张三", "李四")}
outcomes = {}


def student_flow(name, final_answers):
    c = app.test_client()  # 每个"浏览器"独立 client
    # 1. 打开链接 -> 开始倒计时
    s1 = c.get(f"/api/s/{tokens[name]}/state").get_json()
    assert s1["status"] == "in_progress" and s1["remaining_seconds"] > 0
    assert "answer" not in str(s1["questions"]), "正确答案不应下发给学生"
    # 2. 答一部分, 自动保存
    c.post(f"/api/s/{tokens[name]}/answers", json={"answers": {"q1": final_answers["q1"]}, "rev": s1["rev"] + 1})
    # 3. 模拟刷新: 重新拉状态, 应带着已存答案继续
    s2 = c.get(f"/api/s/{tokens[name]}/state").get_json()
    assert s2["answers"].get("q1") == final_answers["q1"], "刷新后答案丢失"
    # 4. 答完交卷
    r1 = c.post(f"/api/s/{tokens[name]}/submit", json={"answers": final_answers}).get_json()
    # 5. 重复提交(网络重试/双击/刷新再交)
    r2 = c.post(f"/api/s/{tokens[name]}/submit", json={"answers": final_answers}).get_json()
    outcomes[name] = (r1, r2)


threads = [threading.Thread(target=student_flow, args=(n, a)) for n, a in [
    ("张三", {"q1": 1, "q2": 1, "q3": "走东侧楼梯下楼到广场集合"}),
    ("李四", {"q1": 0, "q2": 1, "q3": "坐电梯"}),
]]
for t in threads: t.start()
for t in threads: t.join()

check("两个学生并发完成答题交卷", len(outcomes) == 2)
check("交卷响应不提前泄露分数(成绩未发布)",
      all("score" not in o[0] and "score" not in o[1] for o in outcomes.values()), str(outcomes))
check("重复提交返回 duplicate", all(o[1].get("duplicate") for o in outcomes.values()), str(outcomes))

with db() as conn:
    rows = conn.execute("SELECT token, status, score FROM attempts").fetchall()
check("数据库中每人只有一条答卷记录", len(rows) == 2, str([dict(r) for r in rows]))
check("两条记录均为已交卷", all(r["status"] == "submitted" for r in rows))

# 成绩发布前: 学生刷新自己的链接也看不到任何分数
s = client.get(f"/api/s/{tokens['张三']}/state").get_json()
check("发布成绩前学生端看不到分数和明细", s.get("score") is None and "result" not in s, str(s.get("score")))

# 老师后台: 客观题已判, 简答题待评, 能看到学生简答内容
d = client.get(f"/api/exams/{exam['exam_id']}").get_json()
att = {a["student"]: a for a in d["attempts"]}
check("老师看到客观题得分(张三2/李四1)", att["张三"]["score"] == 2 and att["李四"]["score"] == 1, str(att))
check("简答题待评分各 1 道", att["张三"]["pending_count"] == 1 and att["李四"]["pending_count"] == 1)
check("老师能看到学生简答内容以便评分",
      att["张三"]["answers"].get("q3") == "走东侧楼梯下楼到广场集合", str(att["张三"]["answers"]))
check("成绩未发布标记正确", d.get("results_published") is False)

# 老师评分+评语; 重复评分幂等; 改分直接覆盖
grade1 = lambda tok, grades: client.post(f"/api/exams/{exam['exam_id']}/grade",
                                         json={"token": tok, "grades": grades})
r1 = grade1(tokens["张三"], {"q3": {"score": 5, "comment": "路线清晰"}}).get_json()
r2 = grade1(tokens["张三"], {"q3": {"score": 5, "comment": "路线清晰"}}).get_json()
check("评分成功且重复评分幂等(张三 2+5=7 不变)",
      r1.get("total_score") == 7 and r2.get("total_score") == 7 and r2.get("ok"), f"{r1} {r2}")
r3 = grade1(tokens["李四"], {"q3": {"score": 2, "comment": "不能坐电梯"}}).get_json()
r4 = grade1(tokens["李四"], {"q3": {"score": 3, "comment": "不能坐电梯, 扣两分"}}).get_json()
check("改分直接覆盖(李四 1+2=3 -> 1+3=4)", r3.get("total_score") == 3 and r4.get("total_score") == 4,
      f"{r3} {r4}")

# 评完分但没发布成绩, 学生仍然看不到
s = client.get(f"/api/s/{tokens['张三']}/state").get_json()
check("评分后未发布学生仍看不到分数", s.get("score") is None and "result" not in s)

# 发布成绩(重复发布幂等)
p1 = release_results(client, exam["exam_id"])
p2 = release_results(client, exam["exam_id"])
check("发布成绩成功且重复发布幂等", p1.get("ok") and not p1.get("already") and p2.get("already"), f"{p1} {p2}")

# 学生刷新自己的链接: 看到本人总分/各题得分/评语
s = client.get(f"/api/s/{tokens['张三']}/state").get_json()
check("发布后张三看到总分 7", s.get("score") == 7, str(s.get("score")))
items = {it["id"]: it for it in s["result"]["items"]}
check("各题得分正确(客观1/1+1/1, 简答5/5)",
      items["q1"]["score"] == 1 and items["q2"]["score"] == 1 and items["q3"]["score"] == 5
      and items["q1"]["max_score"] == 1 and items["q3"]["max_score"] == 5, str(items))
check("张三看到老师评语", items["q3"]["comment"] == "路线清晰", str(items["q3"]))
check("成绩明细不含正确答案", all("answer" not in it for it in s["result"]["items"]))
check("发布后下发题目仍不含正确答案", all("answer" not in q for q in s["questions"]))
s4 = client.get(f"/api/s/{tokens['李四']}/state").get_json()
check("李四看到自己总分 4", s4.get("score") == 4, str(s4.get("score")))
check("张三的成绩页不含李四的任何信息", "李四" not in json.dumps(s, ensure_ascii=False))
r = client.post(f"/api/s/{tokens['张三']}/submit", json={"answers": {}}).get_json()
check("发布后重复交卷幂等返回本人总分", r.get("duplicate") and r.get("score") == 7, str(r))

# ---------- 场景2: 到点强制收卷, 客观题即判, 简答补评后发布 ----------
exam2 = make_exam(client, duration=2, students=("王五",))
tok = token_of(exam2, "王五")
c = app.test_client()
c.get(f"/api/s/{tok}/state")
c.post(f"/api/s/{tok}/answers", json={"answers": {"q1": 1, "q2": 1, "q3": "没答完"}, "rev": 1})
time.sleep(2.5)
s = c.get(f"/api/s/{tok}/state").get_json()
check("超时后自动强制收卷", s["status"] == "expired", s["status"])
check("强收后未发布成绩学生看不到分", s.get("score") is None and "result" not in s)
r = c.post(f"/api/s/{tok}/answers", json={"answers": {"q1": 0}, "rev": 2})
check("超时后保存答案被拒绝", r.status_code == 409)
r = c.post(f"/api/s/{tok}/submit", json={"answers": {"q1": 0, "q2": 0}})
check("超时后补交幂等且不泄露分数",
      r.get_json().get("duplicate") and "score" not in r.get_json(), str(r.get_json()))
with db() as conn:
    n = conn.execute("SELECT COUNT(*) c FROM attempts WHERE exam_id=?", (exam2["exam_id"],)).fetchone()["c"]
check("超时场景也只有一条记录", n == 1)

d = client.get(f"/api/exams/{exam2['exam_id']}").get_json()
a = d["attempts"][0]
check("强收卷客观题已判(2分)且简答待评", a["score"] == 2 and a["pending_count"] == 1, str(a))
r = client.post(f"/api/exams/{exam2['exam_id']}/grade",
                json={"token": tok, "grades": {"q3": {"score": 4, "comment": "基本正确"}}})
check("超时强收的卷子仍可补评简答(2+4=6)",
      r.status_code == 200 and r.get_json()["total_score"] == 6, f"{r.status_code} {r.get_json()}")
release_results(client, exam2["exam_id"])
s = c.get(f"/api/s/{tok}/state").get_json()
items = {it["id"]: it for it in s["result"]["items"]}
check("发布后王五看到总分 6 和评语",
      s.get("score") == 6 and items["q3"]["comment"] == "基本正确", str(s.get("score")))
r = c.post(f"/api/s/{tok}/submit", json={"answers": {"q1": 0}}).get_json()
check("发布后重复交卷返回总分 6", r.get("duplicate") and r.get("score") == 6, str(r))

# ---------- 场景3: 未开始的考试不能被"强制超时收卷" ----------
exam3 = make_exam(client, students=("赵六",))
tok3 = token_of(exam3, "赵六")
c = app.test_client()
r = c.post(f"/api/s/{tok3}/submit", json={"answers": {"q1": 0}})  # 从未打开页面直接交卷
check("未开始时交卷被拒绝(409)", r.status_code == 409 and r.get_json().get("status") == "not_started",
      f"{r.status_code} {r.get_json()}")
s = c.get(f"/api/s/{tok3}/state").get_json()
check("之后首次打开仍能正常开考", s["status"] == "in_progress" and s["remaining_seconds"] > 0, str(s["status"]))
r = c.post(f"/api/s/{tok3}/submit", json={"answers": {"q1": 1, "q2": 1}}).get_json()
check("开考后正常交卷(响应不含分数)", r.get("status") == "submitted" and "score" not in r, str(r))
release_results(client, exam3["exam_id"])
s = c.get(f"/api/s/{tok3}/state").get_json()
items = {it["id"]: it for it in s["result"]["items"]}
check("发布后看到总分(客观2分, 简答未评按0)",
      s.get("score") == 2 and items["q3"]["score"] is None, str(s.get("score")))

# ---------- 场景4: 自动保存乱序到达, 旧请求不能覆盖新答案 ----------
exam4 = make_exam(client, students=("孙七",))
tok4 = token_of(exam4, "孙七")
c = app.test_client()
s = c.get(f"/api/s/{tok4}/state").get_json()
assert s["rev"] == 0
save = lambda rev, ans: c.post(f"/api/s/{tok4}/answers", json={"answers": ans, "rev": rev})

r1 = save(1, {"q1": 0}).get_json()                 # 先发出: 选了红色
r2 = save(2, {"q1": 1, "q2": 1}).get_json()        # 又改了两题, 新请求先落库
check("正常递增保存生效", r1.get("ok") and r2.get("ok") and not r2.get("stale"), f"{r1} {r2}")
r3 = save(1, {"q1": 0}).get_json()                 # 旧请求晚到(网络乱序/重发)
check("旧 rev 请求被判为 stale", r3.get("stale") is True, str(r3))
s = c.get(f"/api/s/{tok4}/state").get_json()
check("刷新后新答案未被旧请求覆盖",
      s["answers"] == {"q1": 1, "q2": 1} and s["rev"] == 2, str(s["answers"]))
r4 = save(2, {"q1": 0}).get_json()                 # 相同 rev 重发也不算更新
check("相同 rev 重发被忽略", r4.get("stale") is True, str(r4))
r5 = save(3, {"q1": 0, "q2": 0}).get_json()        # 真正更新的保存仍正常生效
s = c.get(f"/api/s/{tok4}/state").get_json()
check("更高 rev 保存正常生效", not r5.get("stale") and s["answers"] == {"q1": 0, "q2": 0}, str(s["answers"]))
r6 = c.post(f"/api/s/{tok4}/answers", json={"answers": {"q1": 1}})  # 缺 rev
check("缺少 rev 的保存被拒绝(400)", r6.status_code == 400)

# ---------- 场景5: 草稿 -> 校对 -> 发布; 草稿不能评分/发成绩 ----------
exam5 = make_exam(client, students=("周九",), publish=False)
tok5 = token_of(exam5, "周九")
c = app.test_client()
r = c.get(f"/api/s/{tok5}/state")
check("草稿期学生无法进场(403)", r.status_code == 403, f"{r.status_code}")
with db() as conn:
    st = conn.execute("SELECT status FROM attempts WHERE token=?", (tok5,)).fetchone()["status"]
check("草稿期打开链接不会启动计时", st == "not_started", st)
check("草稿期交卷被拒绝", c.post(f"/api/s/{tok5}/submit", json={"answers": {}}).status_code == 409)
check("草稿不能发布成绩(409)",
      c.post(f"/api/exams/{exam5['exam_id']}/publish-results").status_code == 409)
r = c.post(f"/api/exams/{exam5['exam_id']}/grade", json={"token": tok5, "grades": {}})
check("草稿不能评分(409)", r.status_code == 409, f"{r.status_code}")

# 校对发现问题: 改题 + 加学生(草稿可改)
r = c.put(f"/api/exams/{exam5['exam_id']}", json={
    "title": "9月安全培训测验(校对版)", "duration_seconds": 60,
    "questions": [{"id": "q1", "text": "1+1=?", "type": "single", "options": ["2", "3"], "answer": 0}],
    "students": ["周九", "吴十"]})
check("草稿可修改(改题/加学生)", r.status_code == 200 and len(r.get_json()["links"]) == 2, f"{r.status_code}")
d = c.get(f"/api/exams/{exam5['exam_id']}").get_json()
check("修改已生效", d["title"].endswith("(校对版)") and d["questions"][0]["text"] == "1+1=?", d["title"])

r1 = c.post(f"/api/exams/{exam5['exam_id']}/publish").get_json()
r2 = c.post(f"/api/exams/{exam5['exam_id']}/publish").get_json()
check("发布成功且重复发布幂等", r1.get("ok") and r2.get("already"), f"{r1} {r2}")

r = c.put(f"/api/exams/{exam5['exam_id']}", json={
    "title": "被篡改", "duration_seconds": 1,
    "questions": [{"id": "q1", "text": "被篡改", "type": "text"}], "students": ["周九"]})
check("发布后改题被拒绝(409)", r.status_code == 409, f"{r.status_code}")
d = c.get(f"/api/exams/{exam5['exam_id']}").get_json()
check("题目未被悄悄改动", d["questions"][0]["text"] == "1+1=?" and d["title"].endswith("(校对版)"), d["title"])

# 发布后: 进场计时/自动保存/交卷照常
s = c.get(f"/api/s/{tok5}/state").get_json()
check("发布后学生正常进场计时", s["status"] == "in_progress" and s["remaining_seconds"] > 0, s["status"])
check("发布后自动保存照常",
      c.post(f"/api/s/{tok5}/answers", json={"answers": {"q1": 0}, "rev": 1}).get_json().get("ok") is True)
r = c.post(f"/api/s/{tok5}/submit", json={"answers": {"q1": 0}}).get_json()
check("交卷成功但响应不含分数", r.get("status") == "submitted" and "score" not in r, str(r))
release_results(client, exam5["exam_id"])
s = c.get(f"/api/s/{tok5}/state").get_json()
check("纯客观考试发布成绩后学生看到 1 分", s.get("score") == 1, str(s.get("score")))

# ---------- 场景6: 每人题目顺序不同, 但自己刷新不变; 老师视角按原题号 ----------
import random as _random

r = client.post("/api/exams", json={
    "title": "防瞟题序测验",
    "duration_seconds": 60,
    "questions": [
        {"id": "q1", "text": "题一", "type": "single", "options": ["A", "B"], "answer": 0},
        {"id": "q2", "text": "题二", "type": "single", "options": ["A", "B"], "answer": 1},
        {"id": "q3", "text": "题三", "type": "single", "options": ["A", "B"], "answer": 0},
        {"id": "q4", "text": "题四", "type": "single", "options": ["A", "B"], "answer": 1},
    ],
    "students": ["甲", "乙", "丙", "丁", "戊", "己"],
})
exam6 = r.get_json()
assert r.status_code == 200, exam6
# 用确定性 RNG 替身: 同一种子连续产出的排列必然不全相同, 避免概率性断言
_rng = _random.Random(20240913)
app_globals = __import__("app").random
_orange_method = app_globals.SystemRandom
class _SeededSystemRandom(_random.SystemRandom):
    def shuffle(self, x):
        _rng.shuffle(x)
app_globals.SystemRandom = _SeededSystemRandom
try:
    client.post(f"/api/exams/{exam6['exam_id']}/publish")
finally:
    app_globals.SystemRandom = _orange_method

states, orders = {}, {}
for name in ("甲", "乙", "丙", "丁", "戊", "己"):
    tok = token_of(exam6, name)
    s = client.get(f"/api/s/{tok}/state").get_json()
    states[name] = s
    orders[name] = [q["id"] for q in s["questions"]]
    check(f"{name}拿到的是完整 4 题", set(orders[name]) == {"q1", "q2", "q3", "q4"}, str(orders[name]))

check("同场考生题目顺序不都一样(防邻座瞟题)", len(set(map(tuple, orders.values()))) > 1, str(orders))

# 学生自己刷新/断网重连: 顺序必须稳定
for name in ("甲", "乙"):
    tok = token_of(exam6, name)
    again = client.get(f"/api/s/{tok}/state").get_json()
    check(f"{name}刷新后题序不变", [q["id"] for q in again["questions"]] == orders[name],
          f"{orders[name]} vs {[q['id'] for q in again['questions']]}")

# 按各自打乱后的显示位置作答, 判分仍按题目 id 走, 不能对错号
def correct_answers_in_display_order(s):
    text_to_correct = {"题一": 0, "题二": 1, "题三": 0, "题四": 1}
    return {q["id"]: text_to_correct[q["text"]] for q in s["questions"]}

s = states["丙"]
ans = correct_answers_in_display_order(s)
client.post(f"/api/s/{token_of(exam6, '丙')}/answers", json={"answers": ans, "rev": 1})
sub = client.post(f"/api/s/{token_of(exam6, '丙')}/submit", json={"answers": ans}).get_json()
check("交卷响应不含分数(未发布成绩)", "score" not in sub, str(sub))

# 乙故意全错 -> 0 分; 丁只答对两题 -> 2 分
s = states["乙"]
wrong = {q["id"]: 1 - {"题一": 0, "题二": 1, "题三": 0, "题四": 1}[q["text"]] for q in s["questions"]}
client.post(f"/api/s/{token_of(exam6, '乙')}/submit", json={"answers": wrong})
s = states["丁"]
all_correct = {"题一": 0, "题二": 1, "题三": 0, "题四": 1}
half = {q["id"]: (all_correct[q["text"]] if q["text"] in ("题一", "题二") else 1 - all_correct[q["text"]])
        for q in s["questions"]}
client.post(f"/api/s/{token_of(exam6, '丁')}/submit", json={"answers": half})

release_results(client, exam6["exam_id"])
s = client.get(f"/api/s/{token_of(exam6, '丙')}/state").get_json()
check("题序打乱后按页面作答判分仍正确(4分)", s.get("score") == 4, str(s.get("score")))
check("丙的成绩明细顺序与本人题序一致",
      [it["id"] for it in s["result"]["items"]] == orders["丙"], str(orders["丙"]))
s = client.get(f"/api/s/{token_of(exam6, '乙')}/state").get_json()
check("乙全错 0 分", s.get("score") == 0, str(s.get("score")))
s = client.get(f"/api/s/{token_of(exam6, '丁')}/state").get_json()
check("丁答对两题 2 分", s.get("score") == 2, str(s.get("score")))

d = client.get(f"/api/exams/{exam6['exam_id']}").get_json()
check("老师后台题目仍是原始题号顺序",
      [q["text"] for q in d["questions"]] == ["题一", "题二", "题三", "题四"])
stats = {st["id"]: st for st in d["question_stats"]}
# 已收卷 3 人(丙/乙/丁); q1: 丙对 乙错 丁对 -> 正确 2; q2 同理 2; q3: 丙对 乙错 丁错 -> 1; q4 同 -> 1
check("每题统计按原始题号聚合(q1 正确2人)", stats["q1"]["correct"] == 2, str(stats["q1"]))
check("每题统计按原始题号聚合(q3 正确1人)", stats["q3"]["correct"] == 1, str(stats["q3"]))
check("每题选项计数正确(q2 选B为正确项 2 人)", stats["q2"]["option_counts"] == [1, 2], str(stats["q2"]))
check("统计分母为已收卷人数(3)", stats["q1"]["graded_count"] == 3 and stats["q1"]["answered"] == 3)

# 学生端仍然拿不到正确答案
check("打乱下发时正确答案依然不下发", all("answer" not in q for q in states["甲"]["questions"]))

# ---------- 场景7: 单题考试不需要打乱, 顺序就是原题序 ----------
r = client.post("/api/exams", json={
    "title": "单题测验", "duration_seconds": 60,
    "questions": [{"id": "q1", "text": "唯一一题", "type": "single", "options": ["对", "错"], "answer": 0}],
    "students": ["钱一"],
})
exam7 = r.get_json()
client.post(f"/api/exams/{exam7['exam_id']}/publish")
tok7 = token_of(exam7, "钱一")
s = client.get(f"/api/s/{tok7}/state").get_json()
check("单题考试顺序固定为原题序", [q["id"] for q in s["questions"]] == ["q1"])

# ---------- 场景8: 交卷答案格式不对 -> 4xx 且不改状态; 之后正常/超时/重复交卷照常 ----------
exam8 = make_exam(client, students=("冯十一", "陈十二"))
bad_cases = [
    ["q1", 1],                       # 数组
    "q1=1",                          # 字符串
    42,                              # 数字
    True,                            # 布尔
]
for bad in bad_cases:
    tok = token_of(exam8, "冯十一")
    c = app.test_client()
    c.get(f"/api/s/{tok}/state")     # 开考
    r = c.post(f"/api/s/{tok}/submit", json={"answers": bad})
    check(f"交卷答案格式错误({type(bad).__name__})返回 400 而非 500",
          r.status_code == 400, f"{r.status_code} {r.data[:120]!r}")
    s = c.get(f"/api/s/{tok}/state").get_json()
    check("格式错误后仍是答题中, 可重新交", s["status"] == "in_progress", str(s["status"]))

# 先保存正确答案, 再用错误格式交卷被拒, 已存进度不受影响
tok = token_of(exam8, "冯十一")
c = app.test_client()
c.get(f"/api/s/{tok}/state")
c.post(f"/api/s/{tok}/answers", json={"answers": {"q1": 1, "q2": 1}, "rev": 1})
r = c.post(f"/api/s/{tok}/submit", json={"answers": ["非法"]})
check("非法交卷被拒(400)", r.status_code == 400, f"{r.status_code}")
s = c.get(f"/api/s/{tok}/state").get_json()
check("被拒后已保存的答案仍在", s["answers"] == {"q1": 1, "q2": 1}, str(s["answers"]))
# 改成合法格式重新交卷 -> 正常收卷
r1 = c.post(f"/api/s/{tok}/submit", json={"answers": {"q1": 1, "q2": 1}}).get_json()
check("修正格式后正常交卷", r1.get("status") == "submitted" and "score" not in r1, str(r1))
release_results(client, exam8["exam_id"])
s = c.get(f"/api/s/{tok}/state").get_json()
check("发布成绩后查到 2 分", s.get("score") == 2, str(s.get("score")))
# 再重复交(即便这次带着非法答案)也应幂等返回原结果, 不报错、不改分
r2 = c.post(f"/api/s/{tok}/submit", json={"answers": ["非法"]}).get_json()
check("收卷后重复交卷幂等(非法体也不改结果)",
      r2.get("duplicate") and r2.get("status") == "submitted" and r2.get("score") == 2, str(r2))

# 超时收卷: 即使交卷请求体格式非法, 也照常按已保存答案强收, 不报 500
exam8b = make_exam(client, duration=2, students=("陈十二",))
tok = token_of(exam8b, "陈十二")
c = app.test_client()
c.get(f"/api/s/{tok}/state")
c.post(f"/api/s/{tok}/answers", json={"answers": {"q1": 1, "q2": 1, "q3": "疏散"}, "rev": 1})
time.sleep(2.5)
import app as _app_mod
_app_mod.SUBMIT_GRACE_SECONDS = 0   # 模拟已过交卷宽限, 走超时强收分支
try:
    r = c.post(f"/api/s/{tok}/submit", json={"answers": ["非法"]})
finally:
    _app_mod.SUBMIT_GRACE_SECONDS = 5
d = r.get_json()
check("超时后非法交卷体不影响强收(200/expired/不泄露分数)",
      r.status_code == 200 and d.get("status") == "expired" and "score" not in d,
      f"{r.status_code} {d}")
release_results(client, exam8b["exam_id"])
s = c.get(f"/api/s/{tok}/state").get_json()
check("强收卷发布后查到客观 2 分", s.get("score") == 2, str(s.get("score")))

# 缺省 answers 字段仍允许: 用已自动保存的答案交卷(老行为不变)
exam8c = make_exam(client, students=("褚十三",))
tok = token_of(exam8c, "褚十三")
c = app.test_client()
c.get(f"/api/s/{tok}/state")
c.post(f"/api/s/{tok}/answers", json={"answers": {"q1": 1, "q2": 1}, "rev": 1})
r = c.post(f"/api/s/{tok}/submit", json={})
check("不带 answers 交卷沿用已保存答案", r.status_code == 200 and r.get_json().get("status") == "submitted",
      f"{r.status_code} {r.get_json()}")
release_results(client, exam8c["exam_id"])
s = c.get(f"/api/s/{tok}/state").get_json()
check("发布后查到 2 分", s.get("score") == 2, str(s.get("score")))

# ---------- 场景9: 简答题评分校验 / 成绩发布边界 / 学生间隔离 ----------
exam9 = make_exam(client, students=("小明", "小红"))
tok_m, tok_h = token_of(exam9, "小明"), token_of(exam9, "小红")
gid = exam9["exam_id"]
grade9 = lambda tok, grades: client.post(f"/api/exams/{gid}/grade", json={"token": tok, "grades": grades})

r = grade9(tok_m, {"q3": {"score": 3}})
check("未交卷不能评分(409)", r.status_code == 409, f"{r.status_code} {r.get_json()}")
r = grade9("不存在的token", {"q3": {"score": 3}})
check("未知答卷评分返回 404", r.status_code == 404, f"{r.status_code}")

c = app.test_client()
c.get(f"/api/s/{tok_m}/state")
c.post(f"/api/s/{tok_m}/submit", json={"answers": {"q1": 1, "q2": 1, "q3": "走消防通道"}})

bad_grades = [
    ({"q3": {"score": 6}}, "超过满分"),
    ({"q3": {"score": -1}}, "负分"),
    ({"q3": {"score": "4"}}, "分数是字符串"),
    ({"q3": {"score": True}}, "分数是布尔"),
    ({"q1": {"score": 1}}, "给单选题评分"),
    ({"q9": {"score": 1}}, "不存在的题"),
    ({"q3": {"score": 4, "comment": 123}}, "评语不是字符串"),
    ({"q3": "4"}, "评分项不是对象"),
]
for grades, desc in bad_grades:
    r = grade9(tok_m, grades)
    check(f"非法评分被拒({desc}, 400)", r.status_code == 400, f"{r.status_code} {r.get_json()}")

r = grade9(tok_m, {"q3": {"score": 4, "comment": "要点齐全"}}).get_json()
check("正常评分(客观2+简答4=6)", r.get("total_score") == 6 and r.get("pending_count") == 0, str(r))
r = grade9(tok_m, {"q3": {"score": 4, "comment": "要点齐全"}}).get_json()
check("重复评分幂等(总分仍 6)", r.get("total_score") == 6, str(r))
r = grade9(tok_m, {"q3": {"score": 5, "comment": "复评上调"}}).get_json()
check("改分覆盖(总分 7)", r.get("total_score") == 7, str(r))

# 另一份答卷的评分互不影响
c2 = app.test_client()
c2.get(f"/api/s/{tok_h}/state")
c2.post(f"/api/s/{tok_h}/submit", json={"answers": {"q1": 0, "q2": 0, "q3": "不知道"}})
r = grade9(tok_h, {"q3": {"score": 1, "comment": "疏散常识薄弱"}}).get_json()
check("各答卷评分互不影响(小红 0+1=1)", r.get("total_score") == 1, str(r))

# 未发布成绩: 两个学生都看不到任何分数
ok_gate = True
for t in (tok_m, tok_h):
    s = app.test_client().get(f"/api/s/{t}/state").get_json()
    ok_gate = ok_gate and s.get("score") is None and "result" not in s
check("未发布前所有学生都看不到分数", ok_gate)

p1 = release_results(client, gid)
p2 = release_results(client, gid)
check("发布成绩幂等", p1.get("ok") and not p1.get("already") and p2.get("already"), f"{p1} {p2}")

s = app.test_client().get(f"/api/s/{tok_m}/state").get_json()
check("小明看到自己总分 7", s.get("score") == 7, str(s.get("score")))
check("小明的页面不含小红的信息", "小红" not in json.dumps(s, ensure_ascii=False))
s = app.test_client().get(f"/api/s/{tok_h}/state").get_json()
check("小红看到自己总分 1", s.get("score") == 1, str(s.get("score")))
check("小红的页面不含小明的信息", "小明" not in json.dumps(s, ensure_ascii=False))

# 发布后再改分, 学生刷新看到更新
r = grade9(tok_m, {"q3": {"score": 3, "comment": "复核后调整"}}).get_json()
check("发布后仍可改分(总分 5)", r.get("total_score") == 5, str(r))
s = app.test_client().get(f"/api/s/{tok_m}/state").get_json()
items = {it["id"]: it for it in s["result"]["items"]}
check("学生刷新看到更新后的分数和评语",
      s.get("score") == 5 and items["q3"]["comment"] == "复核后调整", str(s.get("score")))

# ---------- 场景10: 旧版数据库兼容(无新列的老库迁移后照常工作) ----------
import sqlite3 as _sqlite3

legacy_db = os.path.join(tempfile.mkdtemp(), "legacy.db")
conn = _sqlite3.connect(legacy_db)
conn.executescript("""
CREATE TABLE exams (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, duration_seconds INTEGER NOT NULL,
    questions TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'published',
    published_at REAL, created_at REAL NOT NULL
);
CREATE TABLE attempts (
    token TEXT PRIMARY KEY, exam_id TEXT NOT NULL, student_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'not_started', started_at REAL, deadline REAL,
    answers TEXT NOT NULL DEFAULT '{}', answers_rev INTEGER NOT NULL DEFAULT 0,
    question_order TEXT, score INTEGER, submitted_at REAL
);
""")
now = time.time()
conn.execute("INSERT INTO exams VALUES(?,?,?,?,?,?,?)",
             ("legacy01", "旧版考试", 60, json.dumps([
                 {"id": "q1", "text": "1+1=?", "type": "single", "options": ["2", "3"], "answer": 0},
                 {"id": "q2", "text": "简述疏散路线", "type": "text"},   # 老数据: 没有 points 字段
             ], ensure_ascii=False), "published", now, now))
conn.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
             ("legtok1", "legacy01", "老生", "submitted", now - 600, now - 540,
              json.dumps({"q1": 0, "q2": "走楼梯"}, ensure_ascii=False), 3, None, 1, now - 540))
conn.commit()
conn.close()

import app as app_mod
orig_db = app_mod.DB_PATH
app_mod.DB_PATH = legacy_db
try:
    app_mod.init_db()   # 老库补列迁移, 不应报错
    lc = app.test_client()
    s = lc.get("/api/s/legtok1/state").get_json()
    check("旧库迁移后学生端正常返回", s.get("status") == "submitted", str(s))
    check("旧考试未发布成绩前学生看不到分", s.get("score") is None and "result" not in s, str(s.get("score")))
    check("旧考试学生端仍拿不到正确答案", all("answer" not in q for q in s["questions"]))
    d = lc.get("/api/exams/legacy01").get_json()
    a = d["attempts"][0]
    check("旧答卷客观分/总分/待评兼容",
          a["score"] == 1 and a["total_score"] == 1 and a["pending_count"] == 1, str(a))
    r = lc.post("/api/exams/legacy01/grade",
                json={"token": "legtok1", "grades": {"q2": {"score": 4, "comment": "不错"}}})
    check("旧考试的简答题可补评(默认满分5, 总分 1+4=5)",
          r.status_code == 200 and r.get_json()["total_score"] == 5, f"{r.status_code} {r.get_json()}")
    r = lc.post("/api/exams/legacy01/grade", json={"token": "legtok1", "grades": {"q2": {"score": 6}}})
    check("旧简答题按默认满分 5 校验(6 分被拒)", r.status_code == 400, f"{r.status_code}")
    lc.post("/api/exams/legacy01/publish-results")
    s = lc.get("/api/s/legtok1/state").get_json()
    check("旧考试发布成绩后学生看到总分 5", s.get("score") == 5, str(s.get("score")))
    items = {it["id"]: it for it in s["result"]["items"]}
    check("旧简答题按默认满分 5 展示",
          items["q2"]["max_score"] == 5 and items["q2"]["score"] == 4, str(items))
finally:
    app_mod.DB_PATH = orig_db

# ---------- 场景10: 并发自动保存乱序到达(可重复), 刷新后必须是最新答案 ----------
exam10 = make_exam(client, students=("乱序生",))
tok10 = token_of(exam10, "乱序生")
c = app.test_client()
s = c.get(f"/api/s/{tok10}/state").get_json()
assert s["rev"] == 0

# 模拟"连续改几道题, 多个自动保存同时在飞": rev 1..6 同时发出,
# 每个请求按 rev 倒序错开 50ms, 旧请求稳定晚到新请求之后(确定性乱序, 可重复)
N = 6
snapshots = {i: {"q1": i % 3, "q2": i % 2, "q3": f"第{i}次保存"} for i in range(1, N + 1)}
responses = {}


def delayed_save(i):
    cc = app.test_client()
    time.sleep((N - i) * 0.05)      # rev 越大越早到, 强制乱序
    r = cc.post(f"/api/s/{tok10}/answers", json={"answers": snapshots[i], "rev": i})
    responses[i] = r.get_json()


threads = [threading.Thread(target=delayed_save, args=(i,)) for i in range(1, N + 1)]
for t in threads: t.start()
for t in threads: t.join()

check("并发乱序保存都返回 ok", all(responses[i].get("ok") for i in responses), str(responses))
check("晚到的旧 rev 全部判 stale, 仅最新 rev 生效",
      all(responses[i].get("stale") for i in range(1, N)) and responses[N].get("stale") is False,
      str(responses))
s = c.get(f"/api/s/{tok10}/state").get_json()
check("刷新后是最新一次保存的答案(rev 6)",
      s["answers"] == snapshots[N] and s["rev"] == N, str(s["answers"]))

# sendBeacon(text/plain) 与 fetch 乱序: beacon 带新快照先到, 旧 fetch 晚到必须被丢弃
r = c.post(f"/api/s/{tok10}/answers",
           data=json.dumps({"answers": {"q1": 0, "q2": 0, "q3": "beacon最新"}, "rev": N + 1}),
           content_type="text/plain")
check("sendBeacon(text/plain)保存正常生效",
      r.get_json().get("ok") and r.get_json().get("stale") is False, str(r.get_json()))
r = c.post(f"/api/s/{tok10}/answers", json={"answers": {"q1": 2}, "rev": N})
check("beacon之后晚到的旧fetch被丢弃", r.get_json().get("stale") is True, str(r.get_json()))
s = c.get(f"/api/s/{tok10}/state").get_json()
check("刷新后仍是 beacon 保存的最新答案",
      s["answers"]["q3"] == "beacon最新" and s["rev"] == N + 1, str(s["answers"]))

# rev 为布尔值按格式错误拒绝(true 在 Python 里是 int 1, 不能蒙混为合法版本号)
r = c.post(f"/api/s/{tok10}/answers", json={"answers": {"q1": 1}, "rev": True})
check("布尔 rev 被拒绝(400)", r.status_code == 400, f"{r.status_code}")

# 已提交答卷: 更高 rev 的迟到保存也绝不能回写
c.post(f"/api/s/{tok10}/submit", json={"answers": {"q1": 1, "q2": 1, "q3": "最终交卷"}})
r = c.post(f"/api/s/{tok10}/answers", json={"answers": {"q1": 0, "q3": "迟到篡改"}, "rev": 999})
check("交卷后迟到保存(更高 rev)被拒(409)", r.status_code == 409, f"{r.status_code}")
s = c.get(f"/api/s/{tok10}/state").get_json()
check("已提交答卷未被迟到请求回写",
      s["status"] == "submitted" and s["answers"] == {"q1": 1, "q2": 1, "q3": "最终交卷"},
      str(s["answers"]))

# 已过期答卷: 迟到保存(更高 rev)同样不能回写, 强收用的仍是已保存答案
exam10b = make_exam(client, duration=2, students=("过期生",))
tok10b = token_of(exam10b, "过期生")
cb = app.test_client()
cb.get(f"/api/s/{tok10b}/state")
cb.post(f"/api/s/{tok10b}/answers", json={"answers": {"q1": 1, "q2": 1, "q3": "已保存进度"}, "rev": 1})
time.sleep(2.5)
r = cb.post(f"/api/s/{tok10b}/answers", json={"answers": {"q1": 0, "q2": 0, "q3": "迟到篡改"}, "rev": 2})
check("超时后迟到保存被拒(409/expired)",
      r.status_code == 409 and r.get_json().get("status") == "expired", f"{r.status_code} {r.get_json()}")
s = cb.get(f"/api/s/{tok10b}/state").get_json()
check("过期答卷仍是已保存的答案, 未被回写",
      s["answers"] == {"q1": 1, "q2": 1, "q3": "已保存进度"}, str(s["answers"]))

print()
failed = [n for n, ok, _ in results if not ok]
print(f"共 {len(results)} 项检查, 通过 {len(results) - len(failed)}, 失败 {len(failed)}")
raise SystemExit(1 if failed else 0)
