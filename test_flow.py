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

print()
failed = [n for n, ok, _ in results if not ok]
print(f"共 {len(results)} 项检查, 通过 {len(results) - len(failed)}, 失败 {len(failed)}")
raise SystemExit(1 if failed else 0)
