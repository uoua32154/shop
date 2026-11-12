import sqlite3
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

app = FastAPI()

# 1. Инициализация базы данных SQLite (создается автоматически)
def init_db():
    conn = sqlite3.connect("orders.db")
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT UNIQUE,
                    password TEXT
                )""")
    c.execute("""CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_email TEXT,
                    title TEXT,
                    description TEXT,
                    price INTEGER,
                    status TEXT
                )""")
    conn.commit()
    conn.close()

init_db()

# 2. Менеджер уведомлений в реальном времени (WebSocket)
class ConnectionManager:
    def __init__(self):
        self.active_connections = list()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in list(self.active_connections):
            try:
                await connection.send_text(json.dumps(message))
            except Exception:
                pass

manager = ConnectionManager()

# 3. Модели запросов
class AuthRequest(BaseModel):
    email: str
    password: str

class OrderCreate(BaseModel):
    client_email: str
    title: str
    description: str
    price: int

class StatusUpdate(BaseModel):
    order_id: int
    status: str

# 4. API: Вход и регистрация клиента
@app.post("/api/login")
def login_or_register(data: AuthRequest):
    conn = sqlite3.connect("orders.db")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT id, email, password FROM users WHERE email = ?", (data.email,))
    user = c.fetchone()
    if not user:
        c.execute("INSERT INTO users (email, password) VALUES (?, ?)", (data.email, data.password))
        conn.commit()
    elif user["password"] != data.password:
        conn.close()
        raise HTTPException(status_code=400, detail="Неверный пароль")
    conn.close()
    return {"status": "ok", "email": data.email}

# 5. API: Получение списка всех заказов
@app.get("/api/orders")
def get_orders():
    conn = sqlite3.connect("orders.db")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT id, client_email, title, description, price, status FROM orders ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()
    return list(dict(r) for r in rows)

# 6. API: Создание и оплата нового заказа
@app.post("/api/orders")
async def create_order(order: OrderCreate):
    conn = sqlite3.connect("orders.db")
    c = conn.cursor()
    status_text = "Оплачен (Ждет принятия)"
    c.execute(
        "INSERT INTO orders (client_email, title, description, price, status) VALUES (?, ?, ?, ?, ?)",
        (order.client_email, order.title, order.description, order.price, status_text)
    )
    order_id = c.lastrowid
    conn.commit()
    conn.close()

    new_order_data = {
        "event": "new_order",
        "id": order_id,
        "client_email": order.client_email,
        "title": order.title,
        "description": order.description,
        "price": order.price,
        "status": status_text
    }
    await manager.broadcast(new_order_data)
    return new_order_data

# 7. API: Изменение статуса заказа администратором
@app.post("/api/orders/status")
async def update_status(data: StatusUpdate):
    conn = sqlite3.connect("orders.db")
    c = conn.cursor()
    c.execute("UPDATE orders SET status = ? WHERE id = ?", (data.status, data.order_id))
    conn.commit()
    conn.close()

    await manager.broadcast({"event": "status_updated", "id": data.order_id, "status": data.status})
    return {"status": "ok"}

# 8. Канал связи в реальном времени
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# 9. Веб-сайт (Клиентский кабинет + Встроенная панель Администратора)
HTML_PAGE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <title>Биржа заказа программ</title>
    <style>
        body { font-family: Arial, sans-serif; background: #0f172a; color: #f8fafc; max-width: 850px; margin: 20px auto; padding: 15px; }
        .tabs { display: flex; gap: 10px; margin-bottom: 20px; }
        .tab-btn { background: #1e293b; border: 1px solid #334155; color: #94a3b8; padding: 10px 20px; border-radius: 8px; cursor: pointer; font-weight: bold; }
        .tab-btn.active { background: #3b82f6; color: white; border-color: #3b82f6; }
        .card { background: #1e293b; padding: 20px; border-radius: 12px; margin-bottom: 20px; border: 1px solid #334155; }
        input, textarea { width: 100%; padding: 10px; margin: 8px 0; border-radius: 6px; border: 1px solid #475569; background: #0f172a; color: white; box-sizing: border-box; }
        button { background: #3b82f6; color: white; border: none; padding: 10px 16px; border-radius: 6px; cursor: pointer; font-weight: bold; margin-right: 6px; }
        button:hover { background: #2563eb; }
        .btn-green { background: #10b981; }
        .btn-green:hover { background: #059669; }
        .badge { padding: 4px 10px; border-radius: 6px; background: #059669; font-size: 13px; font-weight: bold; }
        .toast { position: fixed; bottom: 20px; right: 20px; background: #10b981; color: white; padding: 15px 20px; border-radius: 8px; display: none; font-weight: bold; box-shadow: 0 4px 12px rgba(0,0,0,0.4); }
    </style>
</head>
<body>
    <h2>💻 Сайт продажи программ на заказ</h2>

    <div class="tabs">
        <button id="tab-client" class="tab-btn active" onclick="switchMode('client')">👤 Кабинет Клиента</button>
        <button id="tab-admin" class="tab-btn" onclick="switchMode('admin')">🛠 Панель Разработчика (Прием заказов)</button>
    </div>

    <!-- РЕЖИМ КЛИЕНТА -->
    <div id="view-client">
        <div id="auth-box" class="card">
            <h3>Вход / Регистрация аккаунта</h3>
            <input type="email" id="email" placeholder="Введите ваш Email (например: client@mail.ru)">
            <input type="password" id="password" placeholder="Пароль">
            <button onclick="login()">Войти в аккаунт</button>
        </div>

        <div id="client-area" style="display:none;">
            <div class="card">
                <h3>Новый заказ (<span id="user-label"></span>)</h3>
                <input type="text" id="title" placeholder="Что нужно разработать? (например: Чат-бот или парсер)">
                <textarea id="desc" rows="3" placeholder="Опишите функции программы..."></textarea>
                <input type="number" id="price" placeholder="Сумма оплаты в ₽ (например: 5000)">
                <button class="btn-green" onclick="createOrder()">💳 Оплатить и отправить заказ</button>
            </div>

            <div class="card">
                <h3>Мои заказы (Live-обновление)</h3>
                <div id="client-orders"></div>
            </div>
        </div>
    </div>

    <!-- РЕЖИМ АДМИНИСТРАТОРА (ДЛЯ ПК И ANDROID БРАУЗЕРА) -->
    <div id="view-admin" style="display:none;">
        <div class="card">
            <h3>📬 Все входящие заказы от клиентов</h3>
            <div id="admin-orders"></div>
        </div>
    </div>

    <div id="toast" class="toast"></div>

    <script>
        let currentEmail = "";
        const ws = new WebSocket(`ws://${location.host}/ws`);

        ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            if (data.event === "new_order") {
                showToast(`🔔 НОВЫЙ ЗАКАЗ: ${data.title} (${data.price} ₽) от ${data.client_email}!`);
            }
            loadOrders();
        };

        function showToast(text) {
            const t = document.getElementById('toast');
            t.innerText = text;
            t.style.display = 'block';
            setTimeout(() => { t.style.display = 'none'; }, 5000);
        }

        function switchMode(mode) {
            document.getElementById('view-client').style.display = mode === 'client' ? 'block' : 'none';
            document.getElementById('view-admin').style.display = mode === 'admin' ? 'block' : 'none';
            document.getElementById('tab-client').className = mode === 'client' ? 'tab-btn active' : 'tab-btn';
            document.getElementById('tab-admin').className = mode === 'admin' ? 'tab-btn active' : 'tab-btn';
            loadOrders();
        }

        async function login() {
            const email = document.getElementById('email').value.trim();
            const password = document.getElementById('password').value.trim();
            if (!email || !password) return alert("Введите Email и пароль!");

            const res = await fetch('/api/login', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({email, password})
            });
            if (res.ok) {
                currentEmail = email;
                document.getElementById('auth-box').style.display = 'none';
                document.getElementById('client-area').style.display = 'block';
                document.getElementById('user-label').innerText = email;
                loadOrders();
            } else {
                alert('Ошибка: неверный пароль для этого аккаунта!');
            }
        }

        async function createOrder() {
            const title = document.getElementById('title').value;
            const description = document.getElementById('desc').value;
            const price = parseInt(document.getElementById('price').value || "0");
            if (!title || !price) return alert("Укажите название программы и бюджет!");

            await fetch('/api/orders', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({client_email: currentEmail, title, description, price})
            });
            document.getElementById('title').value = '';
            document.getElementById('desc').value = '';
            document.getElementById('price').value = '';
            loadOrders();
        }

        async function setStatus(orderId, status) {
            await fetch('/api/orders/status', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({order_id: orderId, status: status})
            });
            loadOrders();
        }

        async function loadOrders() {
            const res = await fetch('/api/orders');
            const orders = await res.json();

            if (currentEmail) {
                const myOrders = orders.filter(o => o.client_email === currentEmail);
                document.getElementById('client-orders').innerHTML = myOrders.map(o => `
                    <div style="border-bottom: 1px solid #334155; padding: 12px 0;">
                        <b>Заказ #${o.id} — ${o.title}</b> (${o.price} ₽)<br>
                        <small style="color:#94a3b8">${o.description}</small><br><br>
                        Статус: <span class="badge">${o.status}</span>
                    </div>
                `).join('') || "У вас пока нет заказов.";
            }

            document.getElementById('admin-orders').innerHTML = orders.map(o => `
                <div style="border-bottom: 1px solid #334155; padding: 14px 0;">
                    <b>Заказ #${o.id}: ${o.title}</b> — <span style="color:#34d399; font-weight:bold;">${o.price} ₽</span><br>
                    <small>Клиент: ${o.client_email}</small><br>
                    <p style="margin: 6px 0; color:#cbd5e1;">ТЗ: ${o.description}</p>
                    <div style="margin-bottom: 10px;">Текущий статус: <span class="badge">${o.status}</span></div>
                    <button class="btn-green" onclick="setStatus(${o.id}, 'Принят в работу 🛠')">✅ Принять заказ</button>
                    <button onclick="setStatus(${o.id}, 'Готов! Ссылка отправлена 🚀')">🚀 Завершить и сдать</button>
                </div>
            `).join('') || "Входящих заказов пока нет.";
        }
    </script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
def index():
    return HTML_PAGE

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
