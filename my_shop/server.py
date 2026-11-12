import os
import json
import smtplib
import random
import string
import psycopg2
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Данные владельца
MAIL_USER = "aperture_portal_2@mail.ru"
MAIL_PASS = "ij2ivBPg8vhyScaT8vJD"
DATABASE_URL = os.environ.get("DATABASE_URL")

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    try:
        conn = get_db_connection()
        c = conn.cursor()
        # Чистый SQL для PostgreSQL (Neon)
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
            pay_status TEXT DEFAULT 'Ожидает',
            order_status TEXT DEFAULT 'Оформлен',
            created_at TEXT
        )""")
        conn.commit()
        conn.close()
        print("✅ База данных готова")
    except Exception as e:
        print(f"❌ Ошибка базы: {e}")

init_db()

def send_mail(to, code):
    try:
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        msg = MIMEMultipart()
        msg["From"], msg["To"], msg["Subject"] = MAIL_USER, to, "Код входа DevMarket"
        msg.attach(MIMEText(f"Ваш код входа: {code}", "plain"))
        server = smtplib.SMTP_SSL("smtp.mail.ru", 465, timeout=10)
        server.login(MAIL_USER, MAIL_PASS)
        server.send_message(msg)
        server.quit()
        return True
    except: return False

class AuthReq(BaseModel): email: str
class VerifyReq(BaseModel): email: str; code: str
class OrderReq(BaseModel): client_email: str; category: str; title: str; description: str; price: int; pay_method: str

@app.post("/api/auth/send_code")
def api_code(data: AuthReq):
    code = "".join(random.choices(string.digits, k=6))
    role = "admin" if data.email.lower() == MAIL_USER.lower() else "client"
    conn = get_db_connection(); c = conn.cursor()
    c.execute("INSERT INTO users (email, role, verify_code) VALUES (%s, %s, %s) ON CONFLICT (email) DO UPDATE SET verify_code = EXCLUDED.verify_code", (data.email.lower(), role, code))
    conn.commit(); conn.close()
    if send_mail(data.email, code): return {"status": "ok"}
    return {"status": "error", "msg": f"Ошибка почты. Ваш код для теста: {code}"}

@app.post("/api/auth/verify")
def api_verify(data: VerifyReq):
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT role FROM users WHERE email=%s AND verify_code=%s", (data.email.lower(), data.code.strip()))
    row = c.fetchone()
    conn.close()
    if row: return {"status": "ok", "role": row[0], "email": data.email}
    return {"status": "error"}

@app.get("/api/orders")
def api_get_orders():
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT * FROM orders ORDER BY id DESC")
    cols = [d[0] for d in c.description]
    res = [dict(zip(cols, r)) for r in c.fetchall()]
    conn.close(); return res

@app.post("/api/orders/create")
async def api_create(data: OrderReq):
    now = datetime.now().strftime("%d.%m %H:%M")
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("INSERT INTO orders (client_email, category, title, description, price, pay_method, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (data.client_email, data.category, data.title, data.description, data.price, data.pay_method, now))
    conn.commit(); conn.close()
    return {"status": "ok"}

@app.get("/")
def home():
    return HTMLResponse("<html><body style='background:#0f172a;color:white;font-family:sans-serif;padding:50px;text-align:center;'><h1>🚀 Магазин DevMarket Online запущен!</h1><p>База данных Neon подключена. Используйте приложение для входа.</p></body></html>")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

