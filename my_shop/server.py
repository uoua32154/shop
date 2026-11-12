import os
import sqlite3
import json
import smtplib
import random
import string
import requests
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

app = FastAPI()

# ================= ВАШИ ДАННЫЕ =================
MAIL_USER = "aperture_portal_2@mail.ru"
MAIL_PASS = "ij2ivBPg8vhyScaT8vJD"
DEFAULT_PHONE = "+7 909 996-29-68"
WORKER_TOKEN = "ai_secure_access_777" # Пароль для вашего домашнего ПК
# ===============================================

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# --- ЛОГИКА БАЗЫ ДАННЫХ (SQLite для тестов / PostgreSQL для облака) ---
DATABASE_URL = os.environ.get("DATABASE_URL")

def get_db_connection():
    # Если мы в облаке и есть DATABASE_URL, используем PostgreSQL
    if DATABASE_URL:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return conn
    # Иначе локально используем SQLite
    conn = sqlite3.connect("local.db")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    # SQL команды одинаковы для обоих типов БД (базовый синтаксис)
    c.execute("CREATE TABLE IF NOT EXISTS users (email TEXT PRIMARY KEY, role TEXT, verify_code TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("""CREATE TABLE IF NOT EXISTS orders (
        id SERIAL PRIMARY KEY if NOT EXISTS id integer PRIMARY KEY, 
        client_email TEXT, category TEXT, title TEXT, description TEXT,
        price INTEGER, pay_method TEXT, pay_status TEXT DEFAULT 'Ожидает',
        order_status TEXT DEFAULT 'Оформлен', ai_state TEXT DEFAULT 'active',
        ai_pending_req TEXT DEFAULT '', result_link TEXT DEFAULT '',
        chat_json TEXT DEFAULT '[]', created_at TEXT
    )""")
    # SQLite fix (SERIAL не работает в sqlite) - упростим для универсальности
    try:
        c.execute("INSERT INTO settings (key, value) VALUES ('soft_min', '500') ON CONFLICT DO NOTHING")
        c.execute("INSERT INTO settings (key, value) VALUES ('soft_max', '2000') ON CONFLICT DO NOTHING")
        c.execute("INSERT INTO settings (key, value) VALUES ('pay_phone', %s) ON CONFLICT DO NOTHING", (DEFAULT_PHONE,))
    except: pass
    conn.commit()
    conn.close()

init_db()

# --- ОТПРАВКА ПИСЕМ ---
def send_mail(to, code):
    try:
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        msg = MIMEMultipart()
        msg["From"], msg["To"], msg["Subject"] = MAIL_USER, to, "Код DevMarket"
        msg.attach(MIMEText(f"Ваш код входа: {code}", "plain"))
        server = smtplib.SMTP_SSL("smtp.mail.ru", 465, timeout=10)
        server.login(MAIL_USER, MAIL_PASS)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        print(f"Mail error: {e}")
        return False

# --- REAL-TIME ---
class WSManager:
    def __init__(self): self.conns = []
    async def connect(self, ws: WebSocket): await ws.accept(); self.conns.append(ws)
    def disconnect(self, ws: WebSocket): self.conns.remove(ws)
    async def broadcast(self, data: dict):
        for c in self.conns:
            try: await c.send_text(json.dumps(data))
            except: pass
manager = WSManager()

# --- API ---
class AuthReq(BaseModel): email: str
class VerifyReq(BaseModel): email: str; code: str
class OrderReq(BaseModel): client_email: str; category: str; title: str; description: str; price: int; pay_method: str
class WorkerUpdate(BaseModel): token: str; order_id: int; status: str; result_link: str = ""

@app.post("/api/auth/send_code")
def api_code(data: AuthReq):
    code = "".join(random.choices(string.digits, k=6))
    role = "admin" if data.email.lower() == MAIL_USER.lower() else "client"
    conn = get_db_connection(); c = conn.cursor()
    # Универсальный запрос для сохранения кода
    try:
        c.execute("DELETE FROM users WHERE email=%s", (data.email.lower(),))
        c.execute("INSERT INTO users (email, role, verify_code) VALUES (%s, %s, %s)", (data.email.lower(), role, code))
    except:
        c.execute("INSERT OR REPLACE INTO users (email, role, verify_code) VALUES (?, ?, ?)", (data.email.lower(), role, code))
    conn.commit(); conn.close()
    if send_mail(data.email, code): return {"status": "ok"}
    return {"status": "error", "msg": f"Ошибка почты. Код (для теста): {code}"}

@app.post("/api/auth/verify")
def api_verify(data: VerifyReq):
    conn = get_db_connection(); c = conn.cursor()
    try: c.execute("SELECT role, verify_code FROM users WHERE email=%s", (data.email.lower(),))
    except: c.execute("SELECT role, verify_code FROM users WHERE email=?", (data.email.lower(),))
    row = c.fetchone()
    conn.close()
    if row and str(row[1]) == data.code.strip(): return {"status": "ok", "role": row[0], "email": data.email}
    return {"status": "error"}

@app.get("/api/orders")
def api_get_orders():
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT * FROM orders ORDER BY id DESC")
    # Обработка разных форматов ответа БД
    try: res = [dict(r) for r in c.fetchall()]
    except: res = [] # Для упрощения в этом примере
    conn.close(); return res

@app.post("/api/orders/create")
async def api_create(data: OrderReq):
    now = datetime.now().strftime("%d.%m %H:%M")
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("INSERT INTO orders (client_email, category, title, description, price, pay_method, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (data.client_email, data.category, data.title, data.description, data.price, data.pay_method, now))
    except:
        cur.execute("INSERT INTO orders (client_email, category, title, description, price, pay_method, created_at) VALUES (?,?,?,?,?,?,?)",
                (data.client_email, data.category, data.title, data.description, data.price, data.pay_method, now))
    conn.commit(); conn.close()
    await manager.broadcast({"event": "new_order"})
    return {"status": "ok"}

@app.websocket("/ws")
async def websocket_ep(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True: await ws.receive_text()
    except WebSocketDisconnect: manager.disconnect(ws)

# --- HTML ИНТЕРФЕЙС (Упрощенный для стабильности) ---
HTML_PAGE = """
<!DOCTYPE html>
<html>
<head><title>DevMarket Cloud</title><meta charset="utf-8"></head>
<body style="background:#0f172a; color:white; font-family:sans-serif; padding:20px;">
    <h2>🚀 DevMarket LIVE</h2>
    <div id="auth">
        <input id="em" placeholder="Email"><button onclick="send()">Код</button><br>
        <input id="cd" placeholder="Код"><button onclick="ver()">Войти</button>
    </div>
    <div id="main" style="display:none;">
        <h3>Кабинет <span id="user"></span></h3>
        <button onclick="create()">Создать тестовый заказ</button>
        <div id="list"></div>
    </div>
    <script>
        let user = null;
        async function send(){ await fetch('/api/auth/send_code', {method:'POST', body:JSON.stringify({email:document.getElementById('em').value}), headers:{'Content-Type':'application/json'}}); alert('Отправлено'); }
        async function ver(){ 
            const r = await fetch('/api/auth/verify', {method:'POST', body:JSON.stringify({email:document.getElementById('em').value, code:document.getElementById('cd').value}), headers:{'Content-Type':'application/json'}});
            const d = await r.json();
            if(d.status=='ok'){ user=d; document.getElementById('auth').style.display='none'; document.getElementById('main').style.display='block'; document.getElementById('user').innerText=d.email; }
        }
        async function create(){
            await fetch('/api/orders/create', {method:'POST', body:JSON.stringify({client_email:user.email, category:'Программа', title:'Тест', description:'ТЗ', price:1000, pay_method:'sbp'}), headers:{'Content-Type':'application/json'}});
        }
    </script>
</body>
</html>
"""

@app.get("/")
def root(): return HTMLResponse(HTML_PAGE)

if __name__ == "__main__":
    # Render передает PORT автоматически
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

