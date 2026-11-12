import os
import json
import smtplib
import random
import string
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# --- НАСТРОЙКИ ВЛАДЕЛЬЦА ---
MAIL_USER = "aperture_portal_2@mail.ru"
MAIL_PASS = "ij2ivBPg8vhyScaT8vJD"
DATABASE_URL = os.environ.get("DATABASE_URL")

# --- ПОДКЛЮЧЕНИЕ К POSTGRES (NEON) ---
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS users (email TEXT PRIMARY KEY, role TEXT, verify_code TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("""CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            client_email TEXT,
            category TEXT,
            title TEXT,
            description TEXT,
            price INTEGER,
            pay_method TEXT,
            pay_status TEXT DEFAULT 'Ожидает проверки',
            order_status TEXT DEFAULT 'Оформлен',
            chat_json TEXT DEFAULT '[]',
            created_at TEXT
        )""")
        # Начальные настройки цен
        c.execute("INSERT INTO settings (key, value) VALUES ('soft_min', '500') ON CONFLICT DO NOTHING")
        c.execute("INSERT INTO settings (key, value) VALUES ('soft_max', '2000') ON CONFLICT DO NOTHING")
        c.execute("INSERT INTO settings (key, value) VALUES ('pay_phone', '+7 909 996-29-68') ON CONFLICT DO NOTHING")
        conn.commit()
        conn.close()
        print("✅ База данных Neon полностью готова")
    except Exception as e:
        print(f"❌ Ошибка инициализации базы: {e}")

init_db()

# --- ОТПРАВКА КОДА MAIL.RU ---
def send_mail(to, code):
    try:
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        msg = MIMEMultipart()
        msg["From"], msg["To"], msg["Subject"] = MAIL_USER, to, "Код входа DevMarket"
        msg.attach(MIMEText(f"Ваш проверочный код: {code}", "plain"))
        server = smtplib.SMTP_SSL("smtp.mail.ru", 465, timeout=10)
        server.login(MAIL_USER, MAIL_PASS)
        server.send_message(msg)
        server.quit()
        return True
    except: return False

# --- REAL-TIME ---
class WSManager:
    def __init__(self): self.conns = []
    async def connect(self, ws: WebSocket): await ws.accept(); self.conns.append(ws)
    def disconnect(self, ws: WebSocket): 
        if ws in self.conns: self.conns.remove(ws)
    async def broadcast(self, data: dict):
        for c in list(self.conns):
            try: await c.send_text(json.dumps(data))
            except: pass
manager = WSManager()

# --- API MODELS ---
class AuthReq(BaseModel): email: str
class VerifyReq(BaseModel): email: str; code: str
class OrderReq(BaseModel): client_email: str; category: str; title: str; description: str; price: int; pay_method: str
class StatusUpdate(BaseModel): admin_email: str; order_id: int; status: str; pay_status: str

# --- API ENDPOINTS ---
@app.post("/api/auth/send_code")
def api_code(data: AuthReq):
    email = data.email.lower().strip()
    code = "".join(random.choices(string.digits, k=6))
    role = "admin" if email == MAIL_USER.lower() else "client"
    conn = get_db_connection(); c = conn.cursor()
    c.execute("INSERT INTO users (email, role, verify_code) VALUES (%s, %s, %s) ON CONFLICT (email) DO UPDATE SET verify_code = EXCLUDED.verify_code", (email, role, code))
    conn.commit(); conn.close()
    if send_mail(email, code): return {"status": "ok"}
    return {"status": "error", "msg": "Ошибка почты"}

@app.post("/api/auth/verify")
def api_verify(data: VerifyReq):
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT role, verify_code FROM users WHERE email=%s", (data.email.lower(),))
    row = c.fetchone()
    conn.close()
    if row and row[1] == data.code.strip(): return {"status": "ok", "role": row[0], "email": data.email}
    return {"status": "error"}

@app.get("/api/orders")
def api_get_orders():
    conn = get_db_connection(); c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute("SELECT * FROM orders ORDER BY id DESC")
    res = c.fetchall()
    conn.close(); return res

@app.post("/api/orders/create")
async def api_create(data: OrderReq):
    now = datetime.now().strftime("%d.%m %H:%M")
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("INSERT INTO orders (client_email, category, title, description, price, pay_method, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
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

# --- ПОЛНЫЙ ИНТЕРФЕЙС МАРКЕТПЛЕЙСА ---
HTML_PAGE = """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8"><title>DevMarket Cloud</title>
<style>
    :root { --bg: #0b0f19; --card: #1e293b; --text: #f8fafc; --acc: #3b82f6; --border: #334155; }
    body { background: var(--bg); color: var(--text); font-family: sans-serif; margin: 0; padding: 20px; }
    .card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 20px; margin-bottom: 20px; }
    input, textarea, select { width: 100%; padding: 12px; margin: 8px 0; border-radius: 8px; border: 1px solid var(--border); background: var(--bg); color: white; box-sizing: border-box; }
    button { background: var(--acc); color: white; border: none; padding: 12px; border-radius: 8px; cursor: pointer; font-weight: bold; width: 100%; }
    .tracker { display: flex; justify-content: space-between; font-size: 12px; margin: 15px 0; color: #94a3b8; }
    .step.active { color: #10b981; font-weight: bold; }
    .badge { padding: 4px 8px; border-radius: 4px; background: #3b82f6; font-size: 12px; }
</style>
</head>
<body>
    <div id="v-login" class="card">
        <h2>🔐 Вход в DevMarket</h2>
        <input id="em" type="email" placeholder="Введите ваш Email">
        <button id="btn-code" onclick="sendCode()">Получить код на почту</button>
        <div id="code-box" style="display:none; margin-top:15px;">
            <input id="cd" type="text" placeholder="Код из письма">
            <button onclick="verify()">Войти</button>
        </div>
    </div>

    <div id="v-main" style="display:none;">
        <div class="card">
            <h3 id="u-label">Личный кабинет</h3>
            <div id="client-form">
                <select id="c-cat"><option value="Программа">Программа (500-2000₽)</option><option value="Игра">Игра (от 90₽)</option></select>
                <input id="c-title" placeholder="Название проекта">
                <textarea id="c-desc" placeholder="Описание ТЗ..."></textarea>
                <input id="c-price" type="number" placeholder="Бюджет">
                <button onclick="createOrder()">🚀 Оформить заказ</button>
            </div>
        </div>
        <div id="orders-list"></div>
    </div>

    <script>
        let user = null;
        const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
        ws.onmessage = () => renderOrders();

        async function sendCode() {
            const em = document.getElementById('em').value;
            await fetch('/api/auth/send_code', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({email:em})});
            document.getElementById('code-box').style.display = 'block';
        }
        async function verify() {
            const em = document.getElementById('em').value;
            const cd = document.getElementById('cd').value;
            const r = await fetch('/api/auth/verify', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({email:em, code:cd})});
            const d = await r.json();
            if(d.status === 'ok') { 
                user = d; document.getElementById('v-login').style.display='none'; document.getElementById('v-main').style.display='block';
                document.getElementById('u-label').innerText = d.role === 'admin' ? "👑 ПАНЕЛЬ ВЛАДЕЛЬЦА" : "👤 КАБИНЕТ КЛИЕНТА";
                if(d.role === 'admin') document.getElementById('client-form').style.display = 'none';
                renderOrders();
            }
        }
        async function createOrder() {
            await fetch('/api/orders/create', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({
                client_email: user.email, category: document.getElementById('c-cat').value,
                title: document.getElementById('c-title').value, description: document.getElementById('c-desc').value,
                price: parseInt(document.getElementById('c-price').value), pay_method: 'sbp'
            })});
            renderOrders();
        }
        async function renderOrders() {
            const r = await fetch('/api/orders'); const list = await r.json();
            const my = user.role === 'admin' ? list : list.filter(o => o.client_email === user.email);
            const steps = ['Оформлен', 'В работе', 'Готов', 'Доставлен'];
            document.getElementById('orders-list').innerHTML = my.map(o => `
                <div class="card">
                    <div style="display:flex; justify-content:space-between">
                        <b>#${o.id} ${o.title}</b> <span style="color:#10b981">${o.price}₽</span>
                    </div>
                    <div class="tracker">${steps.map(s => `<span class="step ${o.order_status === s ? 'active' : ''}">${s}</span>`).join(' → ')}</div>
                    <p style="font-size:13px; color:#94a3b8">${o.description}</p>
                    <div style="font-size:12px">Статус оплаты: <span class="badge">${o.pay_status}</span></div>
                    ${user.role === 'admin' ? `<div style="margin-top:10px; color:gold">Клиент: ${o.client_email}</div>` : ''}
                </div>
            `).join('');
        }
    </script>
</body>
</html>
"""

@app.get("/")
def root(): return HTMLResponse(HTML_PAGE)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

