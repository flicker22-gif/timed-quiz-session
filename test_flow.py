#!/usr/bin/env python3
"""端到端验收: 建一场考试, 两个学生并发答完交卷, 并覆盖续答/幂等/超时强收。"""
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
            {"id": "q3", "text": "简述你所在岗位的疏散路线。", "type": "text"},
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


# ---------- 场景1: 两个学生并发答题、刷新续答、交卷且重复提交幂等 ----------
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
    ("李四", {"q1": 0, "q2": 1, "q3": "坐电梯"}, ),
]]
for t in threads: t.start()
for t in threads: t.join()

check("两个学生并发完成答题交卷", len(outcomes) == 2)
check("张三全对得 2 分", outcomes["张三"][0].get("score") == 2, str(outcomes["张三"]))
check("李四答错一题得 1 分", outcomes["李四"][0].get("score") == 1, str(outcomes["李四"]))
check("重复提交返回 duplicate 且分数不变",
      all(o[1].get("duplicate") and o[1].get("score") == o[0].get("score") for o in outcomes.values()))

with db() as conn:
    rows = conn.execute("SELECT token, status, score FROM attempts").fetchall()
check("数据库中每人只有一条答卷记录", len(rows) == 2, str([dict(r) for r in rows]))
check("两条记录均为已交卷", all(r["status"] == "submitted" for r in rows))

r = client.get(f"/api/exams/{exam['exam_id']}").get_json()
check("老师后台看到 2 份已交卷",
      sorted(a["status"] for a in r["attempts"]) == ["submitted", "submitted"], str(r["attempts"]))

# ---------- 场景2: 到点强制收卷, 超时后不能再改答案 ----------
exam2 = make_exam(client, duration=2, students=("王五",))
tok = token_of(exam2, "王五")
c = app.test_client()
c.get(f"/api/s/{tok}/state")
c.post(f"/api/s/{tok}/answers", json={"answers": {"q1": 1, "q2": 1, "q3": "没答完"}, "rev": 1})
time.sleep(2.5)
s = c.get(f"/api/s/{tok}/state").get_json()
check("超时后自动强制收卷", s["status"] == "expired", s["status"])
check("强收用已保存答案判分(2分)", s["score"] == 2, str(s.get("score")))
r = c.post(f"/api/s/{tok}/answers", json={"answers": {"q1": 0}, "rev": 2})
check("超时后保存答案被拒绝", r.status_code == 409)
r = c.post(f"/api/s/{tok}/submit", json={"answers": {"q1": 0, "q2": 0}})
check("超时后补交不能改答案(幂等返回原结果)",
      r.get_json().get("duplicate") and r.get_json().get("score") == 2, str(r.get_json()))
with db() as conn:
    n = conn.execute("SELECT COUNT(*) c FROM attempts WHERE exam_id=?", (exam2["exam_id"],)).fetchone()["c"]
check("超时场景也只有一条记录", n == 1)

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
check("开考后正常交卷判分(2分)", r.get("status") == "submitted" and r.get("score") == 2, str(r))

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

# ---------- 场景5: 草稿 -> 校对 -> 发布, 发布后不可改 ----------
exam5 = make_exam(client, students=("周九",), publish=False)
tok5 = token_of(exam5, "周九")
c = app.test_client()
r = c.get(f"/api/s/{tok5}/state")
check("草稿期学生无法进场(403)", r.status_code == 403, f"{r.status_code}")
with db() as conn:
    st = conn.execute("SELECT status FROM attempts WHERE token=?", (tok5,)).fetchone()["status"]
check("草稿期打开链接不会启动计时", st == "not_started", st)
check("草稿期交卷被拒绝", c.post(f"/api/s/{tok5}/submit", json={"answers": {}}).status_code == 409)

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
check("发布后交卷判分照常(1分)", r.get("status") == "submitted" and r.get("score") == 1, str(r))

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
# 构造"全对": 按每题在该学生页面上的位置选正确选项
def correct_answers_in_display_order(s):
    text_to_correct = {"题一": 0, "题二": 1, "题三": 0, "题四": 1}
    return {q["id"]: text_to_correct[q["text"]] for q in s["questions"]}

s = states["丙"]
ans = correct_answers_in_display_order(s)
client.post(f"/api/s/{token_of(exam6, '丙')}/answers", json={"answers": ans, "rev": 1})
sub = client.post(f"/api/s/{token_of(exam6, '丙')}/submit", json={"answers": ans}).get_json()
check("题序打乱后按页面作答判分仍正确(4分)", sub.get("score") == 4, str(sub))

# 乙故意全错 -> 0 分; 丁只答对两题 -> 2 分
s = states["乙"]
wrong = {q["id"]: 1 - {"题一": 0, "题二": 1, "题三": 0, "题四": 1}[q["text"]] for q in s["questions"]}
client.post(f"/api/s/{token_of(exam6, '乙')}/submit", json={"answers": wrong})
s = states["丁"]
all_correct = {"题一": 0, "题二": 1, "题三": 0, "题四": 1}
half = {q["id"]: (all_correct[q["text"]] if q["text"] in ("题一", "题二") else 1 - all_correct[q["text"]])
        for q in s["questions"]}
client.post(f"/api/s/{token_of(exam6, '丁')}/submit", json={"answers": half})

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
# 改成合法格式重新交卷 -> 正常判分
r1 = c.post(f"/api/s/{tok}/submit", json={"answers": {"q1": 1, "q2": 1}}).get_json()
check("修正格式后正常交卷判分(2分)", r1.get("status") == "submitted" and r1.get("score") == 2, str(r1))
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
check("超时后非法交卷体不影响强收(200/expired/2分)",
      r.status_code == 200 and d.get("status") == "expired" and d.get("score") == 2,
      f"{r.status_code} {d}")

# 缺省 answers 字段仍允许: 用已自动保存的答案交卷(老行为不变)
exam8c = make_exam(client, students=("褚十三",))
tok = token_of(exam8c, "褚十三")
c = app.test_client()
c.get(f"/api/s/{tok}/state")
c.post(f"/api/s/{tok}/answers", json={"answers": {"q1": 1, "q2": 1}, "rev": 1})
r = c.post(f"/api/s/{tok}/submit", json={})
check("不带 answers 交卷沿用已保存答案(2分)",
      r.status_code == 200 and r.get_json().get("score") == 2, f"{r.status_code} {r.get_json()}")

print()
failed = [n for n, ok, _ in results if not ok]
print(f"共 {len(results)} 项检查, 通过 {len(results) - len(failed)}, 失败 {len(failed)}")
raise SystemExit(1 if failed else 0)
