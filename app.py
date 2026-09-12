#!/usr/bin/env python3
"""
限时在线测验系统

- 老师创建考试(时长/题目/学生名单), 系统为每人生成独立链接
- 学生打开链接即开始倒计时(服务端计时), 答题自动保存
- 刷新/断网后重新打开链接可接着答, 到点强制收卷
- 重复提交幂等: 一个学生只有一条答卷记录, 刷接口也刷不出第二条

运行: python3 app.py   然后浏览器访问 http://127.0.0.1:5000/
"""
import json
import os
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
    score        INTEGER,
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


def finalize(conn, attempt, exam, status, now, answers=None):
    """收卷: 用已保存(或最后提交)的答案判分并落库。调用方需持有事务。"""
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

@app.post("/api/exams")
def create_exam():
    """创建考试。body: {title, duration_seconds, questions:[...], students:[名字,...]}"""
    data = parse_body()
    title = (data.get("title") or "").strip()
    duration = int(data.get("duration_seconds") or 0)
    questions = data.get("questions") or []
    students = [s.strip() for s in (data.get("students") or []) if s.strip()]
    if not title or duration <= 0 or not questions or not students:
        return jsonify(error="需要 title / duration_seconds / questions / students"), 400

    exam_id = uuid.uuid4().hex[:8]
    now = time.time()
    links = []
    with db() as conn:
        conn.execute(
            "INSERT INTO exams(id, title, duration_seconds, questions, created_at) VALUES(?,?,?,?,?)",
            (exam_id, title, duration, json.dumps(questions, ensure_ascii=False), now),
        )
        for name in students:
            token = secrets.token_urlsafe(8)
            conn.execute(
                "INSERT INTO attempts(token, exam_id, student_name) VALUES(?,?,?)",
                (token, exam_id, name),
            )
            links.append({"student": name, "url": f"/s/{token}"})
    return jsonify(exam_id=exam_id, teacher_url=f"/exam/{exam_id}", links=links)


@app.get("/api/exams/<exam_id>")
def exam_detail(exam_id):
    """老师视角: 考试信息 + 每个学生的状态/分数。顺便把超时未收的卷 lazy 收掉。"""
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
        return jsonify(
            exam_id=exam_id,
            title=exam["title"],
            duration_seconds=exam["duration_seconds"],
            questions=json.loads(exam["questions"]),
            attempts=[
                {
                    "student": a["student_name"],
                    "status": a["status"],
                    "score": a["score"],
                    "remaining_seconds": (
                        max(0, int(a["deadline"] - now)) if a["status"] == "in_progress" and a["deadline"] else None
                    ),
                    "url": f"/s/{a['token']}",
                }
                for a in attempts
            ],
        )


@app.get("/api/s/<token>/state")
def student_state(token):
    """学生打开/刷新页面时调用: 首次打开启动倒计时, 之后返回已存答案和剩余时间。"""
    now = time.time()
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        attempt = get_attempt(conn, token)
        exam = get_exam(conn, attempt["exam_id"])

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
        for q in questions:
            q.pop("answer", None)  # 不下发正确答案
        done = attempt["status"] in ("submitted", "expired")
        return jsonify(
            title=exam["title"],
            status=attempt["status"],
            questions=questions,
            answers=json.loads(attempt["answers"]),
            remaining_seconds=(
                max(0, int(attempt["deadline"] - now))
                if attempt["status"] == "in_progress" and attempt["deadline"]
                else 0
            ),
            score=attempt["score"] if done else None,
        )


@app.post("/api/s/<token>/answers")
def save_answers(token):
    """自动保存。仅答题中且未超时可用; 超时后拒绝并顺带强制收卷。"""
    now = time.time()
    answers = parse_body().get("answers")
    if not isinstance(answers, dict):
        return jsonify(error="answers 必须是对象"), 400
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        attempt = get_attempt(conn, token)
        if attempt["status"] != "in_progress":
            return jsonify(ok=False, status=attempt["status"]), 409
        if now >= attempt["deadline"]:
            exam = get_exam(conn, attempt["exam_id"])
            finalize(conn, attempt, exam, "expired", now)
            return jsonify(ok=False, status="expired"), 409
        conn.execute(
            "UPDATE attempts SET answers=? WHERE token=?",
            (json.dumps(answers, ensure_ascii=False), token),
        )
        return jsonify(ok=True, remaining_seconds=max(0, int(attempt["deadline"] - now)))


@app.post("/api/s/<token>/submit")
def submit(token):
    """
    交卷(幂等)。已收卷时重复调用直接返回原结果, 不会产生第二条记录。
    截止时间+宽限内: 接受本次携带的答案, 记 submitted; 之后: 只用已保存的答案, 记 expired。
    """
    now = time.time()
    answers = parse_body().get("answers")
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        attempt = get_attempt(conn, token)
        if attempt["status"] in ("submitted", "expired"):
            return jsonify(
                ok=True, duplicate=True, status=attempt["status"], score=attempt["score"]
            )
        exam = get_exam(conn, attempt["exam_id"])
        within = attempt["deadline"] is not None and now <= attempt["deadline"] + SUBMIT_GRACE_SECONDS
        if within:
            score = finalize(conn, attempt, exam, "submitted", now, answers)
            return jsonify(ok=True, duplicate=False, status="submitted", score=score)
        score = finalize(conn, attempt, exam, "expired", now)
        return jsonify(ok=True, duplicate=False, status="expired", score=score)


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
<h2>创建一场限时考试</h2>
<label>考试标题</label><input id="title" placeholder="例如: 9月安全培训测验">
<label>时长(分钟)</label><input id="duration" type="number" min="1" value="30">
<label>参加学生(每行一个名字)</label>
<textarea id="students" rows="3" placeholder="张三&#10;李四"></textarea>
<h3>题目</h3>
<div id="questions"></div>
<button type="button" onclick="addQuestion()">+ 添加题目</button>
<hr><button type="button" onclick="createExam()">创建考试</button>
<h3 id="rtitle" style="display:none">学生链接(发给对应学生)</h3>
<div id="result"></div>
<script>
let qn = 0;
function addQuestion(){
  qn++;
  const d = document.createElement('div');
  d.className = 'q';
  d.innerHTML = `
    <label>题干</label><input class="q-text" placeholder="题目内容">
    <div class="row">
      <div><label>类型</label>
        <select class="q-type" onchange="this.closest('.q').querySelector('.single-only').style.display=this.value==='single'?'block':'none'">
          <option value="single">单选题(自动判分)</option><option value="text">简答题</option>
        </select></div>
    </div>
    <div class="single-only">
      <label>选项(用逗号分隔)</label><input class="q-options" placeholder="选项A, 选项B, 选项C">
      <label>正确答案(第几个选项, 从1开始)</label><input class="q-answer" type="number" min="1" value="1">
    </div>`;
  document.getElementById('questions').appendChild(d);
}
async function createExam(){
  const questions = [];
  document.querySelectorAll('.q').forEach((el, i) => {
    const text = el.querySelector('.q-text').value.trim();
    if(!text) return;
    const type = el.querySelector('.q-type').value;
    const q = {id: 'q' + (questions.length + 1), text, type};
    if(type === 'single'){
      q.options = el.querySelector('.q-options').value.split(/[,，]/).map(s=>s.trim()).filter(Boolean);
      q.answer = Math.max(0, (parseInt(el.querySelector('.q-answer').value, 10) || 1) - 1);
    }
    questions.push(q);
  });
  const body = {
    title: document.getElementById('title').value.trim(),
    duration_seconds: Math.round(parseFloat(document.getElementById('duration').value || '0') * 60),
    questions,
    students: document.getElementById('students').value.split('\\n').map(s=>s.trim()).filter(Boolean),
  };
  const r = await fetch('/api/exams', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const data = await r.json();
  if(!r.ok){ alert(data.error || '创建失败'); return; }
  document.getElementById('rtitle').style.display = 'block';
  let out = '管理后台: ' + location.origin + data.teacher_url + '\\n\\n';
  for(const l of data.links) out += l.student + ': ' + location.origin + l.url + '\\n';
  document.getElementById('result').textContent = out;
}
addQuestion();
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

function fmt(s){ s = Math.max(0, s); const m = String(Math.floor(s/60)).padStart(2,'0'); return m + ':' + String(s%60).padStart(2,'0'); }

async function load(){
  const r = await fetch('/api/s/' + TOKEN + '/state');
  if(!r.ok){ document.body.innerHTML = '<h2>链接无效或考试不存在</h2>'; return; }
  const s = await r.json();
  document.getElementById('title').textContent = s.title;
  questions = s.questions; answers = s.answers || {}; remaining = s.remaining_seconds;
  render();
  if(s.status === 'submitted' || s.status === 'expired'){ showResult(s.status, s.score); return; }
  setInterval(tick, 1000);
  setInterval(save, 5000);                 // 每 5 秒自动保存
  window.addEventListener('beforeunload', () => {   // 关闭/刷新前兜底保存
    navigator.sendBeacon('/api/s/' + TOKEN + '/answers', JSON.stringify({answers: collect()}));
  });
}

function render(){
  const box = document.getElementById('questions');
  box.innerHTML = '';
  questions.forEach((q, i) => {
    const d = document.createElement('div');
    d.className = 'q';
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
  const r = await fetch('/api/s/' + TOKEN + '/answers', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({answers: collect()}),
  });
  if(r.ok){
    const d = await r.json();
    remaining = d.remaining_seconds;         // 以服务器剩余时间校准倒计时
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
  const d = await r.json();
  showResult(d.status, d.score);
}

function showResult(status, score){
  finished = true;
  document.getElementById('submitBtn').disabled = true;
  document.querySelectorAll('input,textarea').forEach(e => e.disabled = true);
  document.getElementById('timer').textContent = '已结束';
  document.getElementById('result').textContent =
    (status === 'expired' ? '时间到, 已强制收卷。' : '交卷成功!') +
    (score === null || score === undefined ? '' : ' 客观题得分: ' + score);
}
load();
</script></body></html>"""

TEACHER_HTML = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>考试管理</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:860px;margin:24px auto;padding:0 16px;color:#222}
 table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:8px;text-align:left}
 th{background:#f6f8fa}
</style></head><body>
<h2 id="title"></h2>
<p id="meta"></p>
<table><thead><tr><th>学生</th><th>状态</th><th>剩余时间</th><th>客观题得分</th><th>答题链接</th></tr></thead>
<tbody id="rows"></tbody></table>
<h3>题目与答案</h3><div id="qs"></div>
<script>
const EXAM_ID = "__EXAM_ID__";
const STATUS = {not_started:'未开始', in_progress:'答题中', submitted:'已交卷', expired:'超时收卷'};
function fmt(s){ if(s===null||s===undefined) return '-'; const m=String(Math.floor(s/60)).padStart(2,'0'); return m+':'+String(s%60).padStart(2,'0'); }
async function refresh(){
  const r = await fetch('/api/exams/' + EXAM_ID);
  const d = await r.json();
  document.getElementById('title').textContent = d.title;
  document.getElementById('meta').textContent = '时长 ' + Math.round(d.duration_seconds/60) + ' 分钟 · 每 4 秒自动刷新';
  document.getElementById('rows').innerHTML = d.attempts.map(a =>
    '<tr><td>' + a.student + '</td><td>' + STATUS[a.status] + '</td><td>' + fmt(a.remaining_seconds) +
    '</td><td>' + (a.score===null?'-':a.score) + '</td><td><a href="' + a.url + '">' + location.origin + a.url + '</a></td></tr>'
  ).join('');
  document.getElementById('qs').innerHTML = d.questions.map((q,i) =>
    '<p><b>' + (i+1) + '. ' + q.text + '</b>' +
    (q.type==='single' ? '<br>' + q.options.map((o,j)=> (j===q.answer?'✅ ':'　') + o).join('<br>') : ' <i>(简答题, 不自动判分)</i>') + '</p>'
  ).join('');
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
