#!/usr/bin/env python3
"""
限时在线测验系统

- 老师创建考试(时长/题目/学生名单), 系统为每人生成独立链接
- 学生打开链接即开始倒计时(服务端计时), 答题自动保存
- 刷新/断网后重新打开链接可接着答, 到点强制收卷
- 同一场考试每人题目顺序不同(发布时生成并固定), 防止邻座瞟题号; 刷新/断网回来顺序不变
- 重复提交幂等: 一个学生只有一条答卷记录, 刷接口也刷不出第二条
- 单选题交卷即自动判分; 简答题进入待评分, 由老师在管理后台逐份打分、写评语
- 成绩在老师"发布成绩"前对学生完全不可见; 发布后学生刷新自己的链接, 只能看到本人的总分/各题得分/评语

运行: python3 app.py   然后浏览器访问 http://127.0.0.1:5000/
"""
import json
import math
import os
import random
import secrets
import sqlite3
import time
import uuid

from flask import Flask, abort, jsonify, request

DB_PATH = os.environ.get("QUIZ_DB", "quiz.db")
# 到点后前端会自动发起交卷, 给这个请求留几秒网络宽限; 宽限内仍记为正常交卷
SUBMIT_GRACE_SECONDS = 5

app = Flask(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS exams (
    id               TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL,
    questions        TEXT NOT NULL,           -- JSON 数组
    status           TEXT NOT NULL DEFAULT 'draft',  -- draft/published; 发布后不可改题、不可撤回
    published_at     REAL,
    results_published_at REAL,                -- 成绩发布时间; NULL = 学生看不到任何分数
    created_at       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    token        TEXT PRIMARY KEY,            -- 一人一个 token, 主键即唯一约束
    exam_id      TEXT NOT NULL REFERENCES exams(id),
    student_name TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'not_started',  -- not_started/in_progress/submitted/expired
    started_at   REAL,
    deadline     REAL,
    answers      TEXT NOT NULL DEFAULT '{}',  -- JSON 对象 {题目id: 答案}
    answers_rev  INTEGER NOT NULL DEFAULT 0,  -- 答案版本号, 只接受更大的 rev, 防止旧保存请求覆盖新答案
    question_order TEXT,                      -- JSON 数组: 该学生看到的题目 id 顺序, 发布时固定
    score        INTEGER,                     -- 客观题得分(交卷时自动判); 简答题分数在 grading 里
    grading      TEXT NOT NULL DEFAULT '{}',  -- JSON 对象 {题目id: {"score": x, "comment": "..."}} 老师对简答题的评分
    submitted_at REAL
);
"""


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # 读写并发更稳
    return conn


def init_db():
    with db() as conn:
        conn.executescript(SCHEMA)
        # 老库补列(简单迁移); 存量考试已在用, 视为已发布
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(exams)")]
        if "status" not in cols:
            conn.execute("ALTER TABLE exams ADD COLUMN status TEXT NOT NULL DEFAULT 'draft'")
            conn.execute("UPDATE exams SET status='published'")
        if "published_at" not in cols:
            conn.execute("ALTER TABLE exams ADD COLUMN published_at REAL")
        if "results_published_at" not in cols:
            conn.execute("ALTER TABLE exams ADD COLUMN results_published_at REAL")
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(attempts)")]
        if "answers_rev" not in cols:
            conn.execute("ALTER TABLE attempts ADD COLUMN answers_rev INTEGER NOT NULL DEFAULT 0")
        if "question_order" not in cols:
            conn.execute("ALTER TABLE attempts ADD COLUMN question_order TEXT")
        if "grading" not in cols:
            conn.execute("ALTER TABLE attempts ADD COLUMN grading TEXT NOT NULL DEFAULT '{}'")


def parse_body():
    """兼容 fetch 的 JSON POST 和 sendBeacon 的 text/plain POST。"""
    if request.is_json:
        return request.get_json(silent=True) or {}
    try:
        return json.loads(request.get_data(as_text=True) or "{}")
    except ValueError:
        return {}


def grade(questions, answers):
    """客观题(单选)自动判分; 简答题不参与自动判分。"""
    score = 0
    for q in questions:
        if q.get("type") == "single" and "answer" in q:
            if answers.get(q["id"]) == q["answer"]:
                score += 1
    return score


def _is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def points_of(q):
    """简答题满分; 老数据的简答题没有 points 字段, 默认 5 分。"""
    p = q.get("points")
    return p if isinstance(p, int) and not isinstance(p, bool) and p > 0 else 5


def parse_grading(attempt):
    """读出一份答卷的简答题评分 {题目id: {score, comment}}; 老数据/脏数据一律按未评处理。"""
    try:
        g = json.loads(attempt["grading"] or "{}")
    except (ValueError, TypeError):
        g = {}
    return g if isinstance(g, dict) else {}


def total_score(attempt):
    """总分 = 客观题得分 + 已评简答题得分之和(未评的按 0 计)。"""
    obj = attempt["score"] or 0
    subj = 0
    for g in parse_grading(attempt).values():
        if isinstance(g, dict) and _is_num(g.get("score")):
            subj += g["score"]
    return obj + subj


def result_payload(attempt, questions, order):
    """
    成绩发布后给学生看的本人成绩明细: 各题得分/满分/评语。
    只含学生自己的答案和分数, 绝不含正确答案, 也不含他人信息。
    """
    grading = parse_grading(attempt)
    answers = json.loads(attempt["answers"])
    by_id = {q["id"]: q for q in questions}
    items = []
    for qid in order:
        q = by_id.get(qid)
        if not q:
            continue
        if q.get("type") == "single":
            got = answers.get(qid)
            items.append({
                "id": qid, "type": "single", "your_answer": got,
                "score": 1 if "answer" in q and got == q["answer"] else 0,
                "max_score": 1,
            })
        else:
            g = grading.get(qid) or {}
            items.append({
                "id": qid, "type": "text", "your_answer": answers.get(qid),
                "score": g.get("score"),          # None = 老师尚未评分
                "max_score": points_of(q),
                "comment": g.get("comment") or "",
            })
    obj = attempt["score"] or 0
    subj = sum(g["score"] for g in grading.values()
               if isinstance(g, dict) and _is_num(g.get("score")))
    return {"objective_score": obj, "subjective_score": subj,
            "total_score": obj + subj, "items": items}


def shuffled_question_ids(questions):
    """一份随机题目 id 顺序; 只有一道题时保持原样, 避免无谓差异。"""
    ids = [q["id"] for q in questions]
    if len(ids) > 1:
        random.SystemRandom().shuffle(ids)
    return ids


def order_for_attempt(conn, attempt, questions):
    """
    返回该学生固定的题目顺序(id 列表)。发布时已生成;
    老库/老考试等没有顺序数据的, 首次进场时补生成并落库(之后刷新不再变)。
    """
    raw = attempt["question_order"]
    if raw:
        try:
            order = json.loads(raw)
            valid = {q["id"] for q in questions}
            if set(order) == valid and len(order) == len(valid):
                return order
        except ValueError:
            pass
    order = shuffled_question_ids(questions)
    conn.execute(
        "UPDATE attempts SET question_order=? WHERE token=?",
        (json.dumps(order, ensure_ascii=False), attempt["token"]),
    )
    return order


def finalize(conn, attempt, exam, status, now, answers=None):
    """收卷: 用已保存(或最后提交)的答案判客观题并落库; 简答题留待老师评分。调用方需持有事务。"""
    final_answers = answers if answers is not None else json.loads(attempt["answers"])
    score = grade(json.loads(exam["questions"]), final_answers)
    conn.execute(
        "UPDATE attempts SET status=?, answers=?, score=?, submitted_at=? WHERE token=?",
        (status, json.dumps(final_answers, ensure_ascii=False), score, now, attempt["token"]),
    )
    return score


def get_exam(conn, exam_id):
    row = conn.execute("SELECT * FROM exams WHERE id=?", (exam_id,)).fetchone()
    if not row:
        abort(404)
    return row


def get_attempt(conn, token):
    row = conn.execute("SELECT * FROM attempts WHERE token=?", (token,)).fetchone()
    if not row:
        abort(404)
    return row


# ---------------- API ----------------

def validate_exam_payload(data):
    """返回 (title, duration, questions, students), 不合法返回 None。"""
    title = (data.get("title") or "").strip()
    try:
        duration = int(data.get("duration_seconds") or 0)
    except (TypeError, ValueError):
        return None
    questions = data.get("questions") or []
    students = [s.strip() for s in (data.get("students") or []) if s.strip()]
    if not title or duration <= 0 or not questions or not students:
        return None
    return title, duration, questions, students


@app.post("/api/exams")
def create_exam():
    """创建考试(草稿)。body: {title, duration_seconds, questions:[...], students:[名字,...]}"""
    payload = validate_exam_payload(parse_body())
    if not payload:
        return jsonify(error="需要 title / duration_seconds / questions / students"), 400
    title, duration, questions, students = payload

    exam_id = uuid.uuid4().hex[:8]
    now = time.time()
    links = []
    with db() as conn:
        conn.execute(
            "INSERT INTO exams(id, title, duration_seconds, questions, status, created_at) "
            "VALUES(?,?,?,?,'draft',?)",
            (exam_id, title, duration, json.dumps(questions, ensure_ascii=False), now),
        )
        for name in students:
            token = secrets.token_urlsafe(8)
            conn.execute(
                "INSERT INTO attempts(token, exam_id, student_name) VALUES(?,?,?)",
                (token, exam_id, name),
            )
            links.append({"student": name, "url": f"/s/{token}"})
    return jsonify(exam_id=exam_id, status="draft", teacher_url=f"/exam/{exam_id}", links=links)


@app.put("/api/exams/<exam_id>")
def update_exam(exam_id):
    """修改考试。仅草稿状态可用; 已发布的考试不能改题、加人、减人。"""
    payload = validate_exam_payload(parse_body())
    if not payload:
        return jsonify(error="需要 title / duration_seconds / questions / students"), 400
    title, duration, questions, students = payload
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        exam = get_exam(conn, exam_id)
        if exam["status"] != "draft":
            return jsonify(error="已发布的考试不能修改"), 409
        conn.execute(
            "UPDATE exams SET title=?, duration_seconds=?, questions=? WHERE id=?",
            (title, duration, json.dumps(questions, ensure_ascii=False), exam_id),
        )
        # 同步学生名单: 新增的补发 token, 移除的删掉未开始的答卷
        existing = {
            r["student_name"]: r["token"]
            for r in conn.execute("SELECT student_name, token FROM attempts WHERE exam_id=?", (exam_id,))
        }
        for name in existing:
            if name not in students:
                conn.execute(
                    "DELETE FROM attempts WHERE exam_id=? AND student_name=?", (exam_id, name)
                )
        for name in students:
            if name not in existing:
                conn.execute(
                    "INSERT INTO attempts(token, exam_id, student_name) VALUES(?,?,?)",
                    (secrets.token_urlsafe(8), exam_id, name),
                )
        links = [
            {"student": r["student_name"], "url": f"/s/{r['token']}"}
            for r in conn.execute(
                "SELECT student_name, token FROM attempts WHERE exam_id=? ORDER BY student_name",
                (exam_id,),
            )
        ]
        return jsonify(
            exam_id=exam_id, status="draft", teacher_url=f"/exam/{exam_id}", links=links
        )


@app.post("/api/exams/<exam_id>/publish")
def publish_exam(exam_id):
    """发布考试(幂等)。发布后学生链接才能进场计时; 没有对应的撤回接口。"""
    now = time.time()
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        exam = get_exam(conn, exam_id)
        if exam["status"] == "published":
            return jsonify(ok=True, already=True, status="published")
        questions = json.loads(exam["questions"])
        # 发布这一刻为每个学生固定一份不同的题目顺序; 顺序存库, 刷新/断网回来都不变
        rows = conn.execute("SELECT token FROM attempts WHERE exam_id=?", (exam_id,)).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE attempts SET question_order=? WHERE token=?",
                (json.dumps(shuffled_question_ids(questions), ensure_ascii=False), r["token"]),
            )
        conn.execute(
            "UPDATE exams SET status='published', published_at=? WHERE id=? AND status='draft'",
            (now, exam_id),
        )
        return jsonify(ok=True, already=False, status="published")


@app.get("/api/exams/<exam_id>")
def exam_detail(exam_id):
    """老师视角: 考试信息 + 每个学生的状态/客观分/总分/简答题批改情况。顺便把超时未收的卷 lazy 收掉。"""
    now = time.time()
    with db() as conn:
        exam = get_exam(conn, exam_id)
        attempts = conn.execute(
            "SELECT * FROM attempts WHERE exam_id=? ORDER BY student_name", (exam_id,)
        ).fetchall()
        for a in attempts:
            if a["status"] == "in_progress" and a["deadline"] and now >= a["deadline"]:
                finalize(conn, a, exam, "expired", now)
        attempts = conn.execute(
            "SELECT * FROM attempts WHERE exam_id=? ORDER BY student_name", (exam_id,)
        ).fetchall()
        questions = json.loads(exam["questions"])
        text_qids = [q["id"] for q in questions if q.get("type") != "single"]
        # 每题统计一律按考试原始题目顺序/原始题号聚合, 与学生看到的打乱顺序无关
        stats = []
        graded = [a for a in attempts if a["status"] in ("submitted", "expired")]
        for q in questions:
            item = {"id": q["id"], "answered": 0}
            if q.get("type") == "single":
                n_opts = len(q.get("options") or [])
                counts = [0] * n_opts
                correct = 0
                for a in graded:
                    val = json.loads(a["answers"]).get(q["id"])
                    if val is None:
                        continue
                    item["answered"] += 1
                    if isinstance(val, int) and 0 <= val < n_opts:
                        counts[val] += 1
                    if "answer" in q and val == q["answer"]:
                        correct += 1
                item["option_counts"] = counts
                item["correct"] = correct
                item["graded_count"] = len(graded)
            else:
                # 简答题: 作答份数 / 已评份数 / 平均分
                graded_n = 0
                tot = 0
                for a in graded:
                    val = json.loads(a["answers"]).get(q["id"])
                    if isinstance(val, str) and val.strip():
                        item["answered"] += 1
                    g = parse_grading(a).get(q["id"])
                    if isinstance(g, dict) and _is_num(g.get("score")):
                        graded_n += 1
                        tot += g["score"]
                item["graded"] = graded_n
                item["avg_score"] = round(tot / graded_n, 2) if graded_n else None
                item["max_score"] = points_of(q)
            stats.append(item)

        def attempt_view(a):
            done = a["status"] in ("submitted", "expired")
            grading = parse_grading(a)
            return {
                "student": a["student_name"],
                "token": a["token"],
                "status": a["status"],
                "score": a["score"],                              # 客观题得分
                "total_score": total_score(a) if done else None,  # 客观 + 已评简答
                "grading": grading,
                "answers": json.loads(a["answers"]),              # 老师要看简答题作答内容才能评分
                "pending_count": (
                    sum(1 for qid in text_qids if qid not in grading) if done else 0
                ),
                "remaining_seconds": (
                    max(0, int(a["deadline"] - now)) if a["status"] == "in_progress" and a["deadline"] else None
                ),
                "url": f"/s/{a['token']}",
            }

        return jsonify(
            exam_id=exam_id,
            title=exam["title"],
            duration_seconds=exam["duration_seconds"],
            status=exam["status"],
            published_at=exam["published_at"],
            results_published=exam["results_published_at"] is not None,
            results_published_at=exam["results_published_at"],
            questions=questions,
            attempts=[attempt_view(a) for a in attempts],
            question_stats=stats,
        )


@app.get("/api/s/<token>/state")
def student_state(token):
    """学生打开/刷新页面时调用: 首次打开启动倒计时, 之后返回已存答案和剩余时间。"""
    now = time.time()
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        attempt = get_attempt(conn, token)
        exam = get_exam(conn, attempt["exam_id"])

        if exam["status"] != "published":
            # 草稿/未发布: 学生不能进场, 更不会启动计时
            return jsonify(error="考试尚未发布", status=exam["status"]), 403

        if attempt["status"] == "not_started":
            # 首次打开: 启动计时, 截止时间以服务器为准
            deadline = now + exam["duration_seconds"]
            conn.execute(
                "UPDATE attempts SET status='in_progress', started_at=?, deadline=? "
                "WHERE token=? AND status='not_started'",
                (now, deadline, token),
            )
            attempt = get_attempt(conn, token)

        if attempt["status"] == "in_progress" and now >= attempt["deadline"]:
            # 已超时: 强制收卷(用已自动保存的答案)
            finalize(conn, attempt, exam, "expired", now)
            attempt = get_attempt(conn, token)

        questions = json.loads(exam["questions"])
        # 按该学生固定的顺序下发(发布时生成; 老数据首次进场补生成), 邻座顺序各不相同
        order = order_for_attempt(conn, attempt, questions)
        by_id = {q["id"]: q for q in questions}
        ordered = [dict(by_id[qid]) for qid in order]
        for q in ordered:
            q.pop("answer", None)  # 正确答案永不下发(成绩发布后也不给)
        done = attempt["status"] in ("submitted", "expired")
        published = exam["results_published_at"] is not None
        resp = {
            "title": exam["title"],
            "status": attempt["status"],
            "questions": ordered,
            "answers": json.loads(attempt["answers"]),
            "rev": attempt["answers_rev"],
            "remaining_seconds": (
                max(0, int(attempt["deadline"] - now))
                if attempt["status"] == "in_progress" and attempt["deadline"]
                else 0
            ),
            "results_published": published,
            "score": None,  # 成绩发布前, 任何情况下都不给学生分数
        }
        if done and published:
            # 只回传本人的成绩明细; 不含正确答案, 也不含他人信息
            res = result_payload(attempt, questions, order)
            resp["result"] = res
            resp["score"] = res["total_score"]
        return jsonify(resp)


@app.post("/api/s/<token>/answers")
def save_answers(token):
    """
    自动保存。请求必须带单调递增的 rev(版本号), 服务器只接受比已存更大的 rev;
    旧请求晚到/重发会被判为 stale 直接忽略, 不会覆盖更新的答案。
    """
    now = time.time()
    payload = parse_body()
    answers = payload.get("answers")
    rev = payload.get("rev")
    if not isinstance(answers, dict) or not isinstance(rev, int) or isinstance(rev, bool):
        return jsonify(error="需要 answers(对象) 和 rev(整数)"), 400
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        attempt = get_attempt(conn, token)
        if attempt["status"] != "in_progress":
            return jsonify(ok=False, status=attempt["status"]), 409
        if now >= attempt["deadline"]:
            exam = get_exam(conn, attempt["exam_id"])
            finalize(conn, attempt, exam, "expired", now)
            return jsonify(ok=False, status="expired"), 409
        cur = conn.execute(
            "UPDATE attempts SET answers=?, answers_rev=? WHERE token=? AND answers_rev < ?",
            (json.dumps(answers, ensure_ascii=False), rev, token, rev),
        )
        stale = cur.rowcount == 0  # 已有 >= rev 的答案落库, 本次为过期请求
        return jsonify(
            ok=True,
            stale=stale,
            rev=max(rev, attempt["answers_rev"]),
            remaining_seconds=max(0, int(attempt["deadline"] - now)),
        )


@app.post("/api/s/<token>/submit")
def submit(token):
    """
    交卷(幂等)。已收卷时重复调用直接返回原结果, 不会产生第二条记录。
    截止时间+宽限内: 接受本次携带的答案, 记 submitted; 之后: 只用已保存的答案, 记 expired。
    成绩未发布时, 响应里不带任何分数。
    """
    now = time.time()
    answers = parse_body().get("answers")
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        attempt = get_attempt(conn, token)
        exam = get_exam(conn, attempt["exam_id"])
        published = exam["results_published_at"] is not None

        def result(ok, dup, status):
            out = {"ok": ok, "duplicate": dup, "status": status, "results_published": published}
            if published and status in ("submitted", "expired"):
                out["score"] = total_score(get_attempt(conn, token))
            return jsonify(out)

        if attempt["status"] in ("submitted", "expired"):
            return result(True, True, attempt["status"])
        if attempt["status"] == "not_started":
            # 未打开过答题页就没有计时, 不允许交卷; 否则持链接者可把未开始的卷子直接作废
            return jsonify(ok=False, status="not_started", error="考试尚未开始"), 409
        within = attempt["deadline"] is not None and now <= attempt["deadline"] + SUBMIT_GRACE_SECONDS
        if within:
            # 宽限内会采用本次携带的答案, 格式必须是 {题目id: 答案};
            # 格式不对明确拒绝(400), 不判分、不改状态, 学生改好后还能重新交
            if answers is not None and not isinstance(answers, dict):
                return jsonify(
                    ok=False, status="in_progress", error="answers 必须是对象 {题目id: 答案}"
                ), 400
            finalize(conn, attempt, exam, "submitted", now, answers)
            return result(True, False, "submitted")
        # 已过宽限: 请求体里的答案不再采用, 用已自动保存的答案判分, 格式对错都照常强收
        finalize(conn, attempt, exam, "expired", now)
        return result(True, False, "expired")


@app.post("/api/exams/<exam_id>/grade")
def grade_attempt(exam_id):
    """
    老师给一份答卷的简答题评分(幂等)。body: {token, grades: {题目id: {score, comment}}}
    - 只接受本场考试的简答题; 分值不能超过题目满分(points, 老数据默认 5)
    - 重复提交相同评分是幂等 no-op; 提交不同分值视为改分, 直接覆盖
    - 成绩发布后仍可改分(学生刷新即看到更新); 未收卷的答卷一律不能评
    """
    payload = parse_body()
    token = payload.get("token")
    grades = payload.get("grades")
    if not isinstance(token, str) or not isinstance(grades, dict):
        return jsonify(error="需要 token 和 grades(对象)"), 400
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")   # 读-改-写在同一写事务里, 并发评分不会互相覆盖
        exam = get_exam(conn, exam_id)
        if exam["status"] != "published":
            return jsonify(error="考试尚未发布, 还没有答卷可评"), 409
        attempt = get_attempt(conn, token)
        if attempt["exam_id"] != exam_id:
            abort(404)
        if attempt["status"] not in ("submitted", "expired"):
            return jsonify(error="该生尚未交卷, 不能评分"), 409
        questions = json.loads(exam["questions"])
        text_qs = {q["id"]: q for q in questions if q.get("type") != "single"}
        grading = parse_grading(attempt)
        for qid, g in grades.items():
            q = text_qs.get(qid)
            if q is None:
                return jsonify(error=f"{qid} 不是本场考试的简答题"), 400
            if not isinstance(g, dict):
                return jsonify(error="grades 每项必须是 {score, comment} 对象"), 400
            score = g.get("score")
            comment = g.get("comment", "")
            maxp = points_of(q)
            if not _is_num(score) or not math.isfinite(score) or not 0 <= score <= maxp:
                return jsonify(error=f"{qid} 得分必须是 0~{maxp} 的数字"), 400
            if not isinstance(comment, str) or len(comment) > 500:
                return jsonify(error="评语必须是不超过 500 字的字符串"), 400
            grading[qid] = {"score": score, "comment": comment}
        conn.execute(
            "UPDATE attempts SET grading=? WHERE token=?",
            (json.dumps(grading, ensure_ascii=False), token),
        )
        attempt = get_attempt(conn, token)
        return jsonify(
            ok=True,
            token=token,
            grading=grading,
            objective_score=attempt["score"] or 0,
            total_score=total_score(attempt),
            pending_count=sum(1 for qid in text_qs if qid not in grading),
            results_published=exam["results_published_at"] is not None,
        )


@app.post("/api/exams/<exam_id>/publish-results")
def publish_results(exam_id):
    """发布成绩(幂等)。发布后学生刷新自己的链接即可看到本人总分/各题得分/评语。"""
    now = time.time()
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        exam = get_exam(conn, exam_id)
        if exam["status"] != "published":
            return jsonify(error="考试尚未发布, 不能发布成绩"), 409
        if exam["results_published_at"] is not None:
            return jsonify(ok=True, already=True, results_published_at=exam["results_published_at"])
        conn.execute(
            "UPDATE exams SET results_published_at=? WHERE id=? AND results_published_at IS NULL",
            (now, exam_id),
        )
        return jsonify(ok=True, already=False, results_published_at=now)


# ---------------- 页面 ----------------

INDEX_HTML = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>创建考试</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:760px;margin:24px auto;padding:0 16px;color:#222}
 input,textarea,select{width:100%;box-sizing:border-box;padding:8px;margin:4px 0 12px;border:1px solid #ccc;border-radius:6px}
 button{padding:8px 16px;border:0;border-radius:6px;background:#2563eb;color:#fff;cursor:pointer}
 .q{border:1px solid #ddd;border-radius:8px;padding:12px;margin-bottom:12px}
 .row{display:flex;gap:12px}.row>div{flex:1}
 #result{background:#f6f8fa;border-radius:8px;padding:12px;white-space:pre-wrap;word-break:break-all}
</style></head><body>
<h2 id="pageTitle">创建一场限时考试</h2>
<label>考试标题</label><input id="title" placeholder="例如: 9月安全培训测验">
<label>时长(分钟)</label><input id="duration" type="number" min="1" value="30">
<label>参加学生(每行一个名字)</label>
<textarea id="students" rows="3" placeholder="张三&#10;李四"></textarea>
<h3>题目</h3>
<div id="questions"></div>
<button type="button" onclick="addQuestion()">+ 添加题目</button>
<hr><button type="button" id="submitBtn" onclick="saveExam()">保存为草稿</button>
<h3 id="rtitle" style="display:none">学生链接(发布后才能进场)</h3>
<div id="result"></div>
<script>
let qn = 0;
const editId = new URLSearchParams(location.search).get('edit');

function addQuestion(q){
  qn++;
  const d = document.createElement('div');
  d.className = 'q';
  d.innerHTML = `
    <label>题干</label><input class="q-text" placeholder="题目内容">
    <div class="row">
      <div><label>类型</label>
        <select class="q-type" onchange="const p=this.closest('.q');p.querySelector('.single-only').style.display=this.value==='single'?'block':'none';p.querySelector('.text-only').style.display=this.value==='text'?'block':'none'">
          <option value="single">单选题(自动判分)</option><option value="text">简答题(老师评分)</option>
        </select></div>
    </div>
    <div class="single-only">
      <label>选项(用逗号分隔)</label><input class="q-options" placeholder="选项A, 选项B, 选项C">
      <label>正确答案(第几个选项, 从1开始)</label><input class="q-answer" type="number" min="1" value="1">
    </div>
    <div class="text-only" style="display:none">
      <label>满分分值(老师手动评分, 默认5分)</label><input class="q-points" type="number" min="1" value="5">
    </div>`;
  document.getElementById('questions').appendChild(d);
  if(q){
    d.querySelector('.q-text').value = q.text;
    d.querySelector('.q-type').value = q.type;
    d.querySelector('.single-only').style.display = q.type === 'single' ? 'block' : 'none';
    d.querySelector('.text-only').style.display = q.type === 'text' ? 'block' : 'none';
    if(q.type === 'single'){
      d.querySelector('.q-options').value = (q.options || []).join(', ');
      d.querySelector('.q-answer').value = (q.answer === undefined ? 0 : q.answer) + 1;
    }else if(q.points){
      d.querySelector('.q-points').value = q.points;
    }
  }
}

async function saveExam(){
  const questions = [];
  document.querySelectorAll('.q').forEach((el, i) => {
    const text = el.querySelector('.q-text').value.trim();
    if(!text) return;
    const type = el.querySelector('.q-type').value;
    const q = {id: 'q' + (questions.length + 1), text, type};
    if(type === 'single'){
      q.options = el.querySelector('.q-options').value.split(/[,，]/).map(s=>s.trim()).filter(Boolean);
      q.answer = Math.max(0, (parseInt(el.querySelector('.q-answer').value, 10) || 1) - 1);
    }else{
      q.points = Math.max(1, parseInt(el.querySelector('.q-points').value, 10) || 5);
    }
    questions.push(q);
  });
  const body = {
    title: document.getElementById('title').value.trim(),
    duration_seconds: Math.round(parseFloat(document.getElementById('duration').value || '0') * 60),
    questions,
    students: document.getElementById('students').value.split('\\n').map(s=>s.trim()).filter(Boolean),
  };
  const r = await fetch(editId ? '/api/exams/' + editId : '/api/exams', {
    method: editId ? 'PUT' : 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const data = await r.json();
  if(!r.ok){ alert(data.error || '保存失败'); return; }
  document.getElementById('rtitle').style.display = 'block';
  let out = '状态: 草稿 —— 请到管理后台校对, 确认后发布\\n管理后台: ' + location.origin + data.teacher_url + '\\n\\n';
  for(const l of data.links) out += l.student + ': ' + location.origin + l.url + '\\n';
  document.getElementById('result').textContent = out;
}

async function init(){
  if(!editId){ addQuestion(); return; }
  const r = await fetch('/api/exams/' + editId);
  const d = await r.json();
  if(d.status !== 'draft'){ alert('已发布的考试不能修改'); location.href = '/exam/' + editId; return; }
  document.getElementById('pageTitle').textContent = '编辑草稿(发布前可反复修改)';
  document.getElementById('submitBtn').textContent = '保存修改';
  document.getElementById('title').value = d.title;
  document.getElementById('duration').value = d.duration_seconds / 60;
  document.getElementById('students').value = d.attempts.map(a => a.student).join('\\n');
  document.getElementById('questions').innerHTML = '';
  d.questions.forEach(q => addQuestion(q));
}
init();
</script></body></html>"""

STUDENT_HTML = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>在线考试</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:760px;margin:24px auto;padding:0 16px;color:#222}
 #bar{position:sticky;top:0;background:#fff;border-bottom:1px solid #eee;padding:10px 0;display:flex;justify-content:space-between;align-items:center}
 #timer{font-size:20px;font-weight:600}#timer.low{color:#dc2626}
 #savestate{color:#6b7280;font-size:13px}
 .q{border:1px solid #ddd;border-radius:8px;padding:12px;margin:12px 0}
 .opt{display:block;margin:6px 0;font-weight:normal}
 textarea{width:100%;box-sizing:border-box;padding:8px;border:1px solid #ccc;border-radius:6px}
 button{padding:10px 24px;border:0;border-radius:6px;background:#2563eb;color:#fff;font-size:16px;cursor:pointer}
 button:disabled{background:#9ca3af;cursor:default}
</style></head><body>
<div id="bar">
  <div><b id="title"></b> <span id="savestate"></span></div>
  <div id="timer">--:--</div>
</div>
<div id="questions"></div>
<button id="submitBtn" onclick="submitExam()">交卷</button>
<div id="result" style="margin:16px 0;font-size:18px"></div>
<script>
const TOKEN = "__TOKEN__";
let questions = [], answers = {}, remaining = 0, finished = false, submitting = false;
let rev = 0;  // 答案版本号, 每次保存递增, 服务器据此丢弃晚到的旧请求

function fmt(s){ s = Math.max(0, s); const m = String(Math.floor(s/60)).padStart(2,'0'); return m + ':' + String(s%60).padStart(2,'0'); }

async function load(){
  const r = await fetch('/api/s/' + TOKEN + '/state');
  if(r.status === 403){ document.body.innerHTML = '<h2>考试尚未发布, 请等老师通知后再打开本链接</h2>'; return; }
  if(!r.ok){ document.body.innerHTML = '<h2>链接无效或考试不存在</h2>'; return; }
  const s = await r.json();
  document.getElementById('title').textContent = s.title;
  questions = s.questions; answers = s.answers || {}; remaining = s.remaining_seconds;
  rev = s.rev || 0;
  render();
  if(s.status === 'submitted' || s.status === 'expired'){ finishUI(s); return; }
  setInterval(tick, 1000);
  setInterval(save, 5000);                 // 每 5 秒自动保存
  window.addEventListener('beforeunload', () => {   // 关闭/刷新前兜底保存
    // beacon 也必须消耗一个 rev(++rev): 若只读 rev+1, 卸载瞬间恰好启动的定时保存会撞同一版本号,
    // 服务器对同 rev 只认先到者, 携带更新快照的一方可能被判 stale 丢弃
    navigator.sendBeacon('/api/s/' + TOKEN + '/answers', JSON.stringify({answers: collect(), rev: ++rev}));
  });
}

function render(){
  const box = document.getElementById('questions');
  box.innerHTML = '';
  questions.forEach((q, i) => {
    const d = document.createElement('div');
    d.className = 'q';
    d.dataset.qid = q.id;
    let h = '<b>' + (i+1) + '. ' + escapeHtml(q.text) + '</b><br>';
    if(q.type === 'single'){
      q.options.forEach((op, j) => {
        const checked = answers[q.id] === j ? ' checked' : '';
        h += '<label class="opt"><input type="radio" name="' + q.id + '" value="' + j + '"' + checked + ' onchange="onChange()"> ' + escapeHtml(op) + '</label>';
      });
    } else {
      h += '<textarea rows="3" id="ans-' + q.id + '" oninput="onChange()">' + escapeHtml(answers[q.id] || '') + '</textarea>';
    }
    d.innerHTML = h;
    box.appendChild(d);
  });
}

function escapeHtml(s){ return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

function collect(){
  questions.forEach(q => {
    if(q.type === 'single'){
      const el = document.querySelector('input[name="' + q.id + '"]:checked');
      if(el) answers[q.id] = parseInt(el.value, 10);
    } else {
      const el = document.getElementById('ans-' + q.id);
      if(el) answers[q.id] = el.value;
    }
  });
  return answers;
}

let savePending = null;
function onChange(){
  if(finished) return;
  document.getElementById('savestate').textContent = '有未保存修改…';
  clearTimeout(savePending);
  savePending = setTimeout(save, 800);       // 改动后 0.8 秒内保存
}

async function save(){
  if(finished) return;
  rev++;
  let r;
  try{
    r = await fetch('/api/s/' + TOKEN + '/answers', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({answers: collect(), rev: rev}),
    });
  }catch(e){
    // 断网/超时: rev 已消耗(允许跳号, 服务端只要求递增), 下一次定时保存带完整快照重试
    document.getElementById('savestate').textContent = '保存失败, 网络恢复后自动重试…';
    return;
  }
  if(r.ok){
    const d = await r.json();
    if(d.stale) rev = Math.max(rev, d.rev);   // 服务器上有更新的进度, 本地计数跟上
    remaining = d.remaining_seconds;          // 以服务器剩余时间校准倒计时
    document.getElementById('savestate').textContent = '已自动保存 ' + new Date().toLocaleTimeString();
  } else if(r.status === 409){
    const d = await r.json();
    if(d.status === 'expired') location.reload();
  }
}

function tick(){
  if(finished) return;
  remaining--;
  const t = document.getElementById('timer');
  t.textContent = fmt(remaining);
  if(remaining <= 60) t.classList.add('low');
  if(remaining <= 0) submitExam(true);       // 到点自动交卷
}

async function submitExam(auto){
  if(finished || submitting) return;
  if(!auto && !confirm('确定交卷吗?')) return;
  submitting = true;
  const r = await fetch('/api/s/' + TOKEN + '/submit', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({answers: collect()}),
  });
  if(r.ok){ location.reload(); return; }   // 交卷后刷新, 由 state 决定显示"待发布"还是本人成绩
  submitting = false;
  alert('交卷失败, 请检查网络后重试');
}

function finishUI(s){
  finished = true;
  document.getElementById('submitBtn').disabled = true;
  document.querySelectorAll('input,textarea').forEach(e => e.disabled = true);
  document.getElementById('timer').textContent = '已结束';
  const head = s.status === 'expired' ? '时间到, 已强制收卷。' : '交卷成功!';
  const box = document.getElementById('result');
  if(!s.results_published || !s.result){
    // 成绩发布前: 一个分数都不显示
    box.textContent = head + ' 成绩待老师发布后, 刷新本页即可查看。';
    return;
  }
  const r = s.result;
  box.innerHTML = escapeHtml(head) + ' 总分: <b>' + r.total_score + '</b> 分' +
    ' <span style="color:#6b7280;font-size:14px">(客观题 ' + r.objective_score + ' 分 + 简答题 ' + r.subjective_score + ' 分)</span>';
  // 每题下方标注本题得分; 简答题附老师评语
  const byId = {};
  r.items.forEach(it => byId[it.id] = it);
  document.querySelectorAll('.q').forEach(el => {
    const it = byId[el.dataset.qid];
    if(!it) return;
    const line = document.createElement('div');
    line.style.cssText = 'margin-top:8px;color:#2563eb';
    let txt;
    if(it.type === 'single'){
      txt = '本题得分: ' + it.score + ' / ' + it.max_score;
    }else if(it.score === null || it.score === undefined){
      txt = '简答题(满分 ' + it.max_score + ' 分): 老师尚未评分';
    }else{
      txt = '本题得分: ' + it.score + ' / ' + it.max_score + (it.comment ? ' · 老师评语: ' + it.comment : '');
    }
    line.textContent = txt;
    el.appendChild(line);
  });
}
load();
</script></body></html>"""

TEACHER_HTML = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>考试管理</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:960px;margin:24px auto;padding:0 16px;color:#222}
 table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:8px;text-align:left}
 th{background:#f6f8fa}
 button{padding:6px 14px;border:0;border-radius:6px;background:#2563eb;color:#fff;cursor:pointer}
 .panel{border:1px solid #ddd;border-radius:8px;padding:12px;margin-bottom:12px}
 .ans{background:#f6f8fa;border-radius:6px;padding:8px;margin:6px 0;white-space:pre-wrap;word-break:break-all}
 input[type=number]{width:90px}
</style></head><body>
<h2 id="title"></h2>
<p id="meta"></p>
<p id="draftBar" style="display:none;background:#fef3c7;border-radius:8px;padding:10px">
  <b>草稿状态:</b> 学生链接暂时无法进场。校对无误后
  <button onclick="publishExam()" style="background:#16a34a">发布考试</button>
  <a id="editLink" href="#">返回编辑草稿</a>
  (发布后不能再修改)
</p>
<p id="resultBar" style="display:none;background:#ecfdf5;border-radius:8px;padding:10px"></p>
<table><thead><tr><th>学生</th><th>状态</th><th>剩余时间</th><th>客观题</th><th>简答题</th><th>总分</th><th>待评</th><th>答题链接</th></tr></thead>
<tbody id="rows"></tbody></table>
<h3 id="gradingTitle" style="display:none">简答题批改</h3>
<div id="grading"></div>
<h3>题目与答案</h3><div id="qs"></div>
<h3>每题统计</h3><div id="qstats"></div>
<script>
const EXAM_ID = "__EXAM_ID__";
const STATUS = {not_started:'未开始', in_progress:'答题中', submitted:'已交卷', expired:'超时收卷'};
let EXAM = null;
let gradingDirty = false;   // 老师正在输入时, 自动刷新不重置批改面板
function fmt(s){ if(s===null||s===undefined) return '-'; const m=String(Math.floor(s/60)).padStart(2,'0'); return m+':'+String(s%60).padStart(2,'0'); }
function escapeHtml(s){ return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function pointsOf(q){ return (typeof q.points === 'number' && q.points > 0) ? q.points : 5; }

async function refresh(){
  const r = await fetch('/api/exams/' + EXAM_ID);
  const d = await r.json();
  EXAM = d;
  document.getElementById('title').textContent = d.title;
  document.getElementById('meta').textContent =
    '时长 ' + Math.round(d.duration_seconds/60) + ' 分钟 · 状态: ' +
    (d.status === 'draft' ? '草稿(未发布)' : '已发布') + ' · 每 4 秒自动刷新';
  document.getElementById('draftBar').style.display = d.status === 'draft' ? 'block' : 'none';
  document.getElementById('editLink').href = '/?edit=' + EXAM_ID;

  // 成绩发布条: 未发布前学生看不到任何分数
  const bar = document.getElementById('resultBar');
  if(d.status === 'published'){
    const pending = d.attempts.reduce((n, a) => n + (a.pending_count || 0), 0);
    bar.style.display = 'block';
    bar.innerHTML = d.results_published
      ? '<b>成绩已发布</b> · 学生刷新自己的链接即可看到本人成绩' +
        (pending ? ' · 还有 <b>' + pending + '</b> 道简答题未评分(暂按 0 计入总分), 在下方评分后学生刷新即可看到更新' : '')
      : '<b>成绩未发布</b> · 学生现在看不到任何分数' +
        (pending ? ' · 还有 <b>' + pending + '</b> 道简答题待评分' : '') +
        ' <button onclick="publishResults()">发布成绩</button>';
  } else {
    bar.style.display = 'none';
  }

  document.getElementById('rows').innerHTML = d.attempts.map(a => {
    const done = a.status === 'submitted' || a.status === 'expired';
    const subj = done ? (a.total_score - (a.score || 0)) : '-';
    return '<tr><td>' + escapeHtml(a.student) + '</td><td>' + STATUS[a.status] + '</td><td>' + fmt(a.remaining_seconds) +
      '</td><td>' + (a.score === null ? '-' : a.score) + '</td><td>' + subj + '</td>' +
      '<td><b>' + (done ? a.total_score : '-') + '</b></td><td>' + (a.pending_count || 0) + '</td>' +
      '<td><a href="' + a.url + '">' + location.origin + a.url + '</a></td></tr>';
  }).join('');

  if(!gradingDirty) renderGrading(d);

  document.getElementById('qs').innerHTML = d.questions.map((q,i) =>
    '<p><b>' + (i+1) + '. ' + escapeHtml(q.text) + '</b>' +
    (q.type==='single' ? '<br>' + q.options.map((o,j)=> (j===q.answer?'✅ ':'　') + escapeHtml(o)).join('<br>')
                       : ' <i>(简答题, 满分 ' + pointsOf(q) + ' 分, 人工评分)</i>') + '</p>'
  ).join('');

  // 每题统计按原始题号(第 N 题即建卷时的顺序), 与学生端各自的打乱顺序无关
  const statById = {};
  (d.question_stats || []).forEach(st => statById[st.id] = st);
  document.getElementById('qstats').innerHTML = d.questions.map((q,i) => {
    const st = statById[q.id] || {};
    let body;
    if(q.type === 'single'){
      const n = st.graded_count || 0;
      const rate = n ? Math.round((st.correct||0) / n * 100) + '%' : '-';
      body = q.options.map((o,j) => '　' + (j===q.answer?'✅':'　') + ' ' + escapeHtml(o) + ': <b>' + ((st.option_counts||[])[j]||0) + '</b> 人' +
             (j===q.answer ? '（正确）' : '')).join('<br>') +
             '<br>已答 ' + (st.answered||0) + '/' + n + ' 份 · 正确率 ' + rate;
    } else {
      body = '已交卷中作答 ' + (st.answered||0) + ' 份 · 已评 ' + (st.graded||0) + ' 份' +
             (st.avg_score === null || st.avg_score === undefined ? '' : ' · 平均 ' + st.avg_score + ' 分');
    }
    return '<p style="border-top:1px solid #eee;padding-top:8px"><b>第 ' + (i+1) + ' 题</b> ' + escapeHtml(q.text) + '<br>' + body + '</p>';
  }).join('');
}

function renderGrading(d){
  const textQs = d.questions.filter(q => q.type !== 'single');
  document.getElementById('gradingTitle').style.display = textQs.length ? 'block' : 'none';
  const box = document.getElementById('grading');
  if(!textQs.length){ box.innerHTML = ''; return; }
  let html = '';
  d.attempts.forEach(a => {
    if(a.status !== 'submitted' && a.status !== 'expired') return;
    html += '<div class="panel"><b>' + escapeHtml(a.student) + '</b> ' +
      '<span style="color:#6b7280">(' + STATUS[a.status] + ' · 客观题 ' + (a.score || 0) + ' 分 · 当前总分 ' + a.total_score + ')</span>';
    textQs.forEach(q => {
      const g = (a.grading || {})[q.id] || {};
      const ans = (a.answers || {})[q.id];
      html += '<div style="border-top:1px solid #eee;margin-top:8px;padding-top:8px">' +
        '<div><b>' + escapeHtml(q.text) + '</b>(满分 ' + pointsOf(q) + ' 分)</div>' +
        '<div class="ans">' + (ans ? escapeHtml(ans) : '<i>未作答</i>') + '</div>' +
        '得分 <input type="number" min="0" max="' + pointsOf(q) + '" id="sc-' + a.token + '-' + q.id + '"' +
          ' value="' + (g.score === undefined ? '' : g.score) + '" oninput="gradingDirty=true"> ' +
        '评语 <input type="text" id="cm-' + a.token + '-' + q.id + '" style="width:55%"' +
          ' value="' + escapeHtml(g.comment || '') + '" oninput="gradingDirty=true"></div>';
    });
    html += '<div style="margin-top:8px"><button onclick="saveGrades(\\'' + a.token + '\\')">保存该生评分</button> ' +
      '<span id="msg-' + a.token + '" style="color:#dc2626"></span></div></div>';
  });
  box.innerHTML = html || '<p>还没有已收卷的答卷。</p>';
}

async function saveGrades(token){
  const textQs = EXAM.questions.filter(q => q.type !== 'single');
  const grades = {};
  for(const q of textQs){
    const sc = document.getElementById('sc-' + token + '-' + q.id).value;
    const cm = document.getElementById('cm-' + token + '-' + q.id).value;
    if(sc === '' && !cm) continue;              // 没填的题保持原样
    if(sc === ''){ alert('请给「' + q.text + '」填得分(0~' + pointsOf(q) + ')'); return; }
    grades[q.id] = {score: parseFloat(sc), comment: cm};
  }
  if(!Object.keys(grades).length){ alert('没有需要保存的评分'); return; }
  const r = await fetch('/api/exams/' + EXAM_ID + '/grade', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({token: token, grades: grades}),
  });
  const d = await r.json();
  if(!r.ok){ alert(d.error || '评分失败'); return; }
  gradingDirty = false;
  refresh();
}

async function publishExam(){
  if(!confirm('发布后学生即可进场, 且题目不能再修改。确认发布?')) return;
  const r = await fetch('/api/exams/' + EXAM_ID + '/publish', {method: 'POST'});
  if(r.ok) refresh(); else alert('发布失败');
}

async function publishResults(){
  if(!confirm('发布后学生即可看到各自的总分/各题得分/评语。确认发布成绩?')) return;
  const r = await fetch('/api/exams/' + EXAM_ID + '/publish-results', {method: 'POST'});
  if(r.ok) refresh(); else alert('发布失败');
}
refresh(); setInterval(refresh, 4000);
</script></body></html>"""


@app.get("/")
def index_page():
    return INDEX_HTML


@app.get("/s/<token>")
def student_page(token):
    with db() as conn:
        get_attempt(conn, token)  # 无效 token 直接 404
    return STUDENT_HTML.replace("__TOKEN__", token)


@app.get("/exam/<exam_id>")
def teacher_page(exam_id):
    with db() as conn:
        get_exam(conn, exam_id)
    return TEACHER_HTML.replace("__EXAM_ID__", exam_id)


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
