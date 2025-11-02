# app.py — Comandas Virtuales (POS & Cocina + Notificador) — Rediseño UI sin imágenes
# ---------------------------------------------------------------------------------
# Cambios clave de la interfaz (intuitiva, accesible y clara):
# - Elimina IMÁGENES en todo el flujo (menú, carrito, cocina, notificador).
# - Tipografía, color y espaciado como jerarquía: títulos/acciones/total más grandes.
# - Tarjetas/filas de productos MINIMALISTAS: nombre, precio, acción (Agregar).
# - Botones grandes (mín. 48px de alto) y táctiles; foco visible y contraste alto.
# - Distribución por columnas limpias; listas de lectura rápida sin sobrecarga visual.
# - Mantiene tema oscuro profesional (azules + blancos) con alto contraste.
#
# Ejecuta:
#   pip install streamlit python-dotenv requests
#   streamlit run app.py

from __future__ import annotations
import json
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Literal
from datetime import datetime
import urllib.parse
import re
import os
import requests
from requests.auth import HTTPBasicAuth

import streamlit as st

# --------------------------- Configuración App ---------------------------
st.set_page_config(
    page_title="Comandas Virtuales — POS & Cocina",
    page_icon="🍽️",
    layout="wide",
)

def _safe_rerun():
    try:
        return st.rerun()
    except AttributeError:
        return st.experimental_rerun()

# --------------------------- Constantes de negocio ---------------------------
COMPANY_NAME = "UNISABANA DINING S.A.S"
COMPANY_NIT = "900.123.456-7"
IVA_RATE = 0.19  # 19%

# --------------------------- Persistencia ---------------------------
ORDERS_PATH = Path(__file__).parent / "orders_store.json"
LOCK = threading.Lock()

def _ensure_store():
    if not ORDERS_PATH.exists():
        with LOCK:
            payload = {"orders": [], "meta": {"last_id": 0}}
            ORDERS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

def _read_store() -> dict:
    _ensure_store()
    with LOCK:
        try:
            raw = ORDERS_PATH.read_text(encoding="utf-8")
            data = json.loads(raw)
            if "orders" not in data or "meta" not in data:
                data = {"orders": [], "meta": {"last_id": 0}}
            return data
        except (json.JSONDecodeError, OSError) as e:
            data = {"orders": [], "meta": {"last_id": 0}}
            try:
                ORDERS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError:
                pass
            st.error(f"Se detectó un problema leyendo el almacenamiento y se reinició: {e}")
            return data

def _write_store(data: dict):
    tmp_path = ORDERS_PATH.with_suffix(".tmp.json")
    with LOCK:
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(ORDERS_PATH)

def load_orders() -> list[dict]:
    return _read_store().get("orders", [])

def save_orders(orders: list[dict], last_id: int | None = None):
    data = _read_store()
    data["orders"] = orders
    if last_id is not None:
        data["meta"]["last_id"] = int(last_id)
    _write_store(data)

def next_order_id_and_code() -> tuple[int, str]:
    data = _read_store()
    last_id = int(data["meta"].get("last_id", 0)) + 1
    data["meta"]["last_id"] = last_id
    _write_store(data)
    return last_id, f"#{last_id:03d}"

# --------------------------- Modelos ---------------------------
Status = Literal["NUEVO"]
OrderType = Literal["MESA", "PARA LLEVAR"]

@dataclass
class OrderItem:
    product_id: str
    name: str
    price: float
    quantity: int

@dataclass
class Order:
    id: int
    code: str
    items: List[OrderItem]
    type: OrderType
    table: str
    notes: str
    customer_name: str
    customer_phone: str
    subtotal: float
    iva: float
    total: float
    status: Status
    created_at: str  # ISO timestamp

# --------------------------- Utilidades ---------------------------

def peso(valor: float) -> str:
    return f"$ {valor:,.0f}".replace(",", ".")

def compute_subtotal(items: list[OrderItem]) -> float:
    return sum(i.quantity * i.price for i in items)

def compute_iva(subtotal: float) -> float:
    return round(subtotal * IVA_RATE)

def order_to_dict(order: Order) -> dict:
    d = asdict(order)
    d["items"] = [asdict(i) for i in order.items]
    return d

def dict_to_order(d: dict) -> Order:
    return Order(
        id=int(d["id"]),
        code=d.get("code", f"#{int(d['id']):03d}"),
        items=[OrderItem(**i) for i in d.get("items", [])],
        type=d.get("type", "MESA"),
        table=d.get("table", ""),
        notes=d.get("notes", ""),
        customer_name=d.get("customer_name", ""),
        customer_phone=d.get("customer_phone", ""),
        subtotal=float(d.get("subtotal", d.get("total", 0.0))),
        iva=float(d.get("iva", 0.0)),
        total=float(d.get("total", 0.0)),
        status=d.get("status", "NUEVO"),
        created_at=d.get("created_at", datetime.now().isoformat(timespec="seconds")),
    )

def add_order(order: Order):
    data = _read_store()
    orders = data.get("orders", [])
    orders.append(order_to_dict(order))
    save_orders(orders)

def delete_order(order_id: int):
    data = _read_store()
    orders = [o for o in data.get("orders", []) if int(o["id"]) != int(order_id)]
    save_orders(orders, data["meta"]["last_id"])

def clear_all_orders():
    data = _read_store()
    save_orders([], data["meta"]["last_id"])

def normalize_co_phone(raw: str) -> str:
    """Normaliza a E.164. Si parece celular CO (10 dígitos que empiezan por 3), antepone +57."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return ""
    if digits.startswith("57") and len(digits) == 12:
        return f"+{digits}"
    if digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if len(digits) == 10 and digits[0] == "3":
        return f"+57{digits}"
    return f"+{digits}" if not raw.strip().startswith("+") else raw.strip()

def format_date_ddmmyyyy(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str)
    except Exception:
        dt = datetime.now()
    return dt.strftime("%d/%m/%Y")


def build_invoice_message(o: Order) -> str:
    """Mensaje de factura estilo DIAN-like para WhatsApp."""
    inv_num = f"{o.id:04d}"
    exp_date = format_date_ddmmyyyy(o.created_at)
    hello_name = o.customer_name.strip() or "Cliente"

    lines: List[str] = [
        f"Hola {hello_name}, 👋",
        "",
        f"Gracias por tu compra. Te compartimos los datos de tu Factura de Venta No. {inv_num} emitida por {COMPANY_NAME} – NIT {COMPANY_NIT}.",
        "",
        f"📅 Fecha de expedición: {exp_date}",
        "",
        "🍽 Detalle de tu pedido:",
    ]

    # Detalle de los ítems
    for it in o.items:
        lines.append(f"{it.name}  ..............  {peso(it.price * it.quantity)}")

    # Totales y cierre
    lines += [
        "",
        f"IVA (19%) ............................ {peso(o.iva)}",
        f"💰 Total a pagar: {peso(o.total)}",
        "",
        f"⚖ {COMPANY_NAME.split(' S.A.S')[0]} es responsable del IVA y agente retenedor.",
        "",
        "Si necesitas imprimir, personalizar o descargar tu factura, ve al siguiente link:",
        "🔗 https://www.figma.com/proto/niEz42XWpMPuWMeNgQbiPR/SabanaHack?node-id=6-3&p=f&m=dev&scaling=scale-down&content-scaling=fixed&page-id=0%3A1",
        "",
        "Gracias por preferirnos. ¡Esperamos que disfrutes tu pedido! 😊😊",
    ]

    return "\n".join(lines)

def wa_me_link(phone_e164: str, text: str) -> str:
    enc = urllib.parse.quote(text)
    digits = re.sub(r"\D", "", phone_e164)
    return f"https://wa.me/{digits}?text={enc}" if digits else f"https://wa.me/?text={enc}"

# --------------------------- Twilio vía requests ---------------------------

def get_twilio_config():
    # Lee de entorno o del sidebar
    sid = os.getenv("TWILIO_ACCOUNT_SID", st.session_state.get("tw_sid", ""))
    tok = os.getenv("TWILIO_AUTH_TOKEN", st.session_state.get("tw_token", ""))
    wa_from = os.getenv("TWILIO_WA_FROM", st.session_state.get("tw_wa_from", "whatsapp:+14155238886"))
    return sid.strip(), tok.strip(), wa_from.strip()

def send_whatsapp_twilio_requests(to_e164: str, body: str) -> str:
    """Envía WhatsApp usando API de Twilio con requests (Sandbox/Prod).
    Retorna el SID del mensaje si fue 201."""
    sid, tok, wa_from = get_twilio_config()
    if not (sid and tok):
        raise RuntimeError("Faltan credenciales de Twilio (Account SID / Auth Token) en Ajustes o variables de entorno.")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data = {
        "From": wa_from,                     # ej: whatsapp:+14155238886 (Sandbox)
        "To": f"whatsapp:{to_e164}",         # destino normalizado envuelto en 'whatsapp:'
        "Body": body,
    }
    resp = requests.post(url, data=data, auth=HTTPBasicAuth(sid, tok), timeout=30)
    if resp.status_code == 201:
        return resp.json().get("sid", "ok")
    else:
        raise RuntimeError(f"{resp.status_code} {resp.text}")

# --------------------------- Menú (sin imágenes) ---------------------------
MENU = {
    "Comidas": [
        {"id": "C1", "name": "Hamburguesa Clásica", "price": 18000},
        {"id": "C2", "name": "Perro Caliente", "price": 12000},
        {"id": "C3", "name": "Wrap de Pollo", "price": 16000},
        {"id": "C4", "name": "Ensalada César", "price": 15000},
        {"id": "C5", "name": "Porción de Papas", "price": 7000},
        {"id": "C6", "name": "Pizza (slice)", "price": 10000},
    ],
    "Bebidas": [
        {"id": "B1", "name": "Gaseosa 400ml", "price": 5000},
        {"id": "B2", "name": "Limonada Natural", "price": 7000},
        {"id": "B3", "name": "Agua Botella", "price": 4000},
        {"id": "B4", "name": "Café Americano", "price": 4500},
    ],
    "Postres": [
        {"id": "D1", "name": "Brownie", "price": 7000},
        {"id": "D2", "name": "Cheesecake", "price": 9000},
    ],
    "Combos": [
        {"id": "K1", "name": "Combo Burger + Gaseosa", "price": 21000},
        {"id": "K2", "name": "Combo Perro + Gaseosa", "price": 16000},
        {"id": "K3", "name": "Combo Wrap + Limonada", "price": 22000},
    ],
}

# --------------------------- Estado UI ---------------------------
if "cart" not in st.session_state:
    st.session_state.cart = {}
if "order_type" not in st.session_state:
    st.session_state.order_type = "MESA"
if "order_table" not in st.session_state:
    st.session_state.order_table = ""
if "order_notes" not in st.session_state:
    st.session_state.order_notes = ""
if "confirm_clear_all" not in st.session_state:
    st.session_state.confirm_clear_all = False
if "active_category" not in st.session_state:
    st.session_state.active_category = "Todas"
if "customer_name" not in st.session_state:
    st.session_state.customer_name = ""
if "customer_phone" not in st.session_state:
    st.session_state.customer_phone = ""
# Ajustes Twilio (sidebar)
for key, default in [("tw_sid", os.getenv("TWILIO_ACCOUNT_SID", "")),
                     ("tw_token", os.getenv("TWILIO_AUTH_TOKEN", "")),
                     ("tw_wa_from", os.getenv("TWILIO_WA_FROM", "whatsapp:+14155238886"))]:
    if key not in st.session_state:
        st.session_state[key] = default

# --------------------------- Estilos (tema + componentes) ---------------------------
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;700;900&display=swap');
</style>
""", unsafe_allow_html=True)


THEME_CSS = """
<style>
:root{
  --bg-dark:#0b1220; --panel:#0f1b36; --panel-2:#0b1530; --panel-border:#1d2a4d;
  --accent:#0e4bc9; --accent-2:#4f80ff; --text:#ffffff; --text-muted:#c7d6ff;
  --white:#ffffff; --card-border:#7aa2ff; --shadow:0 8px 22px rgba(10,20,60,.25);
  --shadow-soft:0 4px 14px rgba(10,20,60,.18);
}
html, body, [data-testid="stAppViewContainer"]{
  font-family: "Inter", "Segoe UI", Roboto, Arial, sans-serif !important;
}
  font-family: "Inter", "Segoe UI", Roboto, Arial, sans-serif !important;
  font-size: 20px !important; line-height: 1.6; cursor: default;
}
[data-testid="stHeader"]{ background: linear-gradient(180deg, rgba(14,25,55,.5), transparent) !important; }

/* Tipografía jerárquica */
h1, .h1 { font-size: 2.8rem; font-weight: 900; letter-spacing: .3px; margin-bottom: 0.4em; }
h2, .h2 { font-size: 2.2rem; font-weight: 800; margin-bottom: 0.3em; }
h3, .h3 { font-size: 1.8rem; font-weight: 700; margin-bottom: 0.25em; }
.section-title { font-weight: 900; font-size: 1.6rem; letter-spacing: .3px; margin: 6px 0 10px 0; }
.big-total { font-size: 2.8rem; font-weight: 1000; letter-spacing: .4px; margin-top: 10px; }
.big-action button { font-size: 1.4rem !important; font-weight: 900 !important; cursor: pointer; }

/* Entradas */
.stTextInput input, .stTextArea textarea, .stNumberInput input{
  background: var(--white) !important; color: #0b1220 !important; font-size: 1.2rem !important;
  border: 3px solid #dbe3ff !important; border-radius: 16px !important; padding: 16px 18px !important; cursor: text;
  caret-color: #000000; /* caret negro para mayor legibilidad */
}
.stTextArea textarea{ min-height: 110px !important; }

.stButton button{
  background: var(--accent) !important; color: var(--white) !important;
  border-radius: 16px !important; border: 0 !important; padding: 18px 22px !important; min-height: 58px !important;
  font-size: 1.3rem !important; font-weight: 900 !important;
  box-shadow: var(--shadow-soft); transition: all .2s ease-in-out; cursor: pointer;
}
.stButton button:hover{ filter: brightness(1.1); transform: translateY(-1px); }
.stButton button:focus{ outline: 4px solid #bcd1ff; outline-offset: 2px; }

/* Tabs */
div[role="tablist"] > div[role="tab"]{
  color: var(--text-muted) !important; border-bottom: 3px solid transparent !important;
  padding-bottom: 14px !important; font-weight: 800; font-size: 1.2rem; cursor: pointer; }
div[role="tablist"] > div[aria-selected="true"]{
  color: var(--text) !important; border-bottom: 4px solid var(--accent) !important;
}

/* Paneles */
.panel{ background: linear-gradient(180deg, var(--panel), var(--panel-2));
  border: 2px solid var(--panel-border); border-radius: 20px; padding: 20px; box-shadow: var(--shadow-soft); cursor: default; }
.sticky{ position: sticky; top: 12px; }
.hr{ height:1px; background: linear-gradient(90deg, transparent, #3552ad, transparent); border:0; margin:14px 0 20px 0; }
.small-muted{ color: var(--text-muted) !important; font-size: 1.1rem; }

/* MATRIZ de productos */
.grid{ display:grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap:20px; }
.card{
  background: rgba(255,255,255,.05); border:2px solid rgba(186,209,255,.3); border-radius:18px; padding:18px;
  display:flex; flex-direction:column; justify-content:space-between; box-shadow: var(--shadow-soft);
  transition: all .2s ease-in-out; cursor: pointer;
}
.card:hover{ background: rgba(255,255,255,.08); transform: translateY(-2px); }
.pname{ font-weight:900; font-size:1.5rem; letter-spacing:.3px; margin-bottom:6px; }
.pprice{ font-weight:800; font-size:1.3rem; color:#bcd1ff; margin-bottom:10px; }
.card button{ margin-top:auto; font-size:1.2rem; font-weight:900; border-radius:12px; background:var(--accent); color:var(--white); border:0; padding:14px; cursor:pointer; }
.card button:hover{ filter: brightness(1.1); }

/* Cursor negro en superficies claras específicas */
.qty .btn,
.card button{
  cursor: url('data:image/svg+xml;utf8,<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"24\" height=\"24\" viewBox=\"0 0 24 24\"><path fill=\"black\" d=\"M3 2l15 7-6 2 4 9-3 1-4-9-5 2z\"/></svg>') 0 0, pointer;
}

/* Carrito / resumen */
.line{ display:grid; grid-template-columns: 1fr 240px 160px 120px; gap:14px; align-items:center;
  padding:12px 0; border-bottom:1px dashed rgba(186,209,255,.25); }
.qty{ display:flex; align-items:center; gap:12px; }
.qty .btn{ padding:12px 16px; border-radius:12px; border:3px solid #aac3ff; background:#fff; color:#0b1220; font-weight:900; min-width:50px; text-align:center; font-size:1.2rem; }
.qty .count{ font-weight:1000; font-size:1.25rem; }
.linetotal{ text-align:right; font-weight:1000; font-size:1.3rem; }

/* Tarjetas cocina / notificador */
.kcard{ border-radius:18px; padding:18px; border:2px solid var(--card-border); background:#0f1b36; margin-bottom:16px; box-shadow: var(--shadow-soft); }
.krow{ display:flex; justify-content:space-between; align-items:center; cursor: default; }
.kbadge{ display:inline-block; padding:6px 14px; border-radius:999px; font-size:1rem; font-weight:1000; background:#e8f0ff; color:#0b1220; }
.ktitle{ font-weight:1000; font-size:1.5rem; letter-spacing:.3px; }
.kmuted{ color:#cbd6ff; font-size:1.1rem; }
.klist{ margin:8px 0 2px 0; padding-left:20px; }
.klist li{ margin-bottom:4px; font-size:1.2rem; }

:focus-visible{ outline:4px solid #bcd1ff; outline-offset:2px; }
</style>
"""

st.markdown(THEME_CSS, unsafe_allow_html=True)

STATUS_COLORS = {"NUEVO": "#ffffff"}
STATUS_BORDERS = {"NUEVO": "#a7c1ff"}

# --------------------------- Sidebar Ajustes Twilio ---------------------------
with st.sidebar:
    st.markdown("### ⚙️ Ajustes Twilio")
    st.caption("Usa tu **WhatsApp Sandbox** o un remitente aprobado.")
    st.session_state.tw_sid = st.text_input("Account SID", value=st.session_state.tw_sid, type="password")
    st.session_state.tw_token = st.text_input("Auth Token", value=st.session_state.tw_token, type="password")
    st.session_state.tw_wa_from = st.text_input("WhatsApp From (Twilio)", value=st.session_state.tw_wa_from,
                                                help="Ej: whatsapp:+14155238886 (Sandbox)")
    st.caption("El destinatario debe estar unido al Sandbox (enviar el código a tu número de sandbox).")

# --------------------------- Encabezado ---------------------------
try:
    active_count = len(load_orders())
except Exception as e:
    active_count = 0
    st.error(f"No se pudo cargar el conteo de pedidos: {e}")

c1, c2 = st.columns([0.75, 0.25])
with c1:
    st.markdown("### 🍽️ Comandas Virtuales")
    st.markdown("<div class='small-muted'>POS · Cocina · Notificador</div>", unsafe_allow_html=True)
with c2:
    st.metric("Pedidos activos", active_count)

st.markdown("<hr class='hr'/>", unsafe_allow_html=True)

# --------------------------- Pestañas ---------------------------
tab_pos, tab_kitchen, tab_notify = st.tabs(["🧾 POS (Mesero)", "👩‍🍳 Cocina (Kanban)", "🔔 Notificador de Entregas"])

# =========================== TAB POS ===========================
with tab_pos:
    st.markdown("<div class='section-title'>Panel del Mesero</div>", unsafe_allow_html=True)

    # Buscador + chips
    top_l, top_r = st.columns([0.62, 0.38])
    with top_l:
        q = st.text_input("Buscar producto", placeholder="Ej: gaseosa, burger, wrap…")
    with top_r:
        st.caption("Tip: escribe y presiona Enter para filtrar")

    cats = ["Todas"] + list(MENU.keys())
    chip_cols = st.columns(len(cats))
    for idx, c in enumerate(cats):
        with chip_cols[idx]:
            if st.button(("● " if st.session_state.active_category == c else "○ ") + c, key=f"chip_{c}"):
                st.session_state.active_category = c
    st.markdown("<hr class='hr'/>", unsafe_allow_html=True)

    left, right = st.columns([0.62, 0.38])

    # Lista Menú (texto)
    # Matriz Menú (tarjetas en 3 columnas)
with left:
    st.markdown("<div class='panel'>", unsafe_allow_html=True)
    st.markdown("**Menú**")

    # Construye resultados según búsqueda/categoría
    results = []
    for cat, items in MENU.items():
        if st.session_state.active_category not in ("Todas", cat):
            continue
        for it in items:
            if q and q.strip().lower() not in it["name"].lower():
                continue
            results.append((cat, it))

    if not results:
        st.info("No hay resultados para tu búsqueda/filtrado.")
    else:
        # 3 columnas: más productos a la vista
        cols = st.columns(3, gap="medium")
        for i, (cat, it) in enumerate(results):
            n = it["name"]; p = float(it["price"]); pid = it["id"]
            with cols[i % 3]:
                st.markdown(
                    f"""
                    <div class='card'>
                      <div class='pname'>{n}</div>
                      <div class='pprice'>{peso(p)}</div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )
                # Botón de acción (ancho completo)
                if st.button("Agregar", key=f"add_{pid}", use_container_width=True):
                    row = st.session_state.cart.get(
                        pid, {"product_id": pid, "name": n, "price": p, "quantity": 0}
                    )
                    row["quantity"] += 1
                    row["price"] = p
                    row["name"] = n
                    st.session_state.cart[pid] = row
                    st.toast(f"Agregado: {n}", icon="➕")

    st.markdown("</div>", unsafe_allow_html=True)

    # Carrito + datos cliente
    with right:
        st.markdown("<div class='panel sticky'>", unsafe_allow_html=True)
        st.markdown("**🛒 Pedido actual**")
        st.session_state.order_type = st.radio("Tipo de pedido", ["MESA", "PARA LLEVAR"], horizontal=True)
        st.session_state.order_table = st.text_input("Mesa o nombre", value=st.session_state.order_table, placeholder="Ej: Mesa 5 / Ana")
        st.session_state.customer_name = st.text_input("Nombre del cliente", value=st.session_state.customer_name, placeholder="Ej: Juan Pérez")
        st.session_state.customer_phone = st.text_input("Celular (WhatsApp)", value=st.session_state.customer_phone, placeholder="Ej: 3001234567")
        st.session_state.order_notes = st.text_area("Notas para cocina (opcional)", value=st.session_state.order_notes, placeholder="Sin cebolla, al punto…")

        if st.session_state.cart:
            subtotal = 0.0
            # Encabezado de líneas (sólo texto)
            st.caption("\n**Ítems**")
            for pid, row in list(st.session_state.cart.items()):
                qtty = int(row["quantity"]); pr = float(row["price"]); nm = row["name"]
                if qtty <= 0:
                    st.session_state.cart.pop(pid, None)
                    continue
                subtotal += qtty * pr
                # Fila: nombre | cantidad +/- | total línea | eliminar
                st.markdown("<div class='line'>", unsafe_allow_html=True)
                st.markdown(f"<div class='pname'>{nm}</div>", unsafe_allow_html=True)
                # Controles de cantidad
                qc1, qc2, qc3 = st.columns([0.35, 0.30, 0.35])
                with qc1:
                    if st.button("−", key=f"minus_{pid}"):
                        row["quantity"] = max(0, qtty - 1); st.session_state.cart[pid] = row
                with qc2:
                    st.markdown(f"<div class='qty'><span class='count'>x {qtty}</span></div>", unsafe_allow_html=True)
                with qc3:
                    if st.button("+", key=f"plus_{pid}"):
                        row["quantity"] = qtty + 1; st.session_state.cart[pid] = row
                st.markdown(f"<div class='linetotal'>{peso(qtty*pr)}</div>", unsafe_allow_html=True)
                del_col = st.columns([1])[0]
                if del_col.button("🗑️", key=f"del_{pid}"):
                    st.session_state.cart.pop(pid, None); st.toast(f"Eliminado: {nm}", icon="🗑️")
                st.markdown("</div>", unsafe_allow_html=True)

            iva = compute_iva(subtotal); total = subtotal + iva
            st.markdown("---")
            st.write(f"**Subtotal :  ** {peso(subtotal)}")
            st.write(f"**IVA (19%) :  ** {peso(iva)}")
            st.markdown(f"<div class='big-total'>Total: {peso(total)}</div>", unsafe_allow_html=True)

            colf1, colf2 = st.columns(2)
            with colf1:
                if st.button("Enviar a cocina 🚀", use_container_width=True):
                    items = [
                        OrderItem(product_id=pid, name=v["name"], price=float(v["price"]), quantity=int(v["quantity"]))
                        for pid, v in st.session_state.cart.items() if int(v["quantity"]) > 0
                    ]
                    if not items:
                        st.warning("No hay cantidades válidas.")
                    else:
                        try:
                            oid, code = next_order_id_and_code()
                            order = Order(
                                id=oid, code=code, items=items,
                                type=st.session_state.order_type, table=st.session_state.order_table.strip(),
                                notes=st.session_state.order_notes.strip(),
                                customer_name=st.session_state.customer_name.strip(),
                                customer_phone=normalize_co_phone(st.session_state.customer_phone),
                                subtotal=subtotal, iva=iva, total=total,
                                status="NUEVO", created_at=datetime.now().isoformat(timespec="seconds"),
                            )
                            add_order(order)
                            st.success(f"Pedido {order.code} enviado a cocina ✅")
                            st.session_state.cart = {}; st.session_state.order_table = ""; st.session_state.order_notes = ""
                            _safe_rerun()
                        except Exception as e:
                            st.error(f"No fue posible guardar el pedido: {e}")
            with colf2:
                if st.button("Limpiar carrito", use_container_width=True):
                    st.session_state.cart = {}; st.info("Carrito reiniciado.")
        else:
            st.info("El carrito está vacío. Agrega productos del menú.")
        st.markdown("</div>", unsafe_allow_html=True)

# =========================== TAB COCINA ===========================
with tab_kitchen:
    st.markdown("<div class='section-title'>Pantalla de Cocina</div>", unsafe_allow_html=True)
    a, b = st.columns([0.6, 0.4])
    with a:
        if st.button("Recargar ahora 🔄", use_container_width=True):
            _safe_rerun()
    with b:
        with st.popover("🧹 Limpiar todos los pedidos", help="Requiere confirmación"):
            st.write("Esta acción elimina **todos** los pedidos del sistema.")
            st.session_state.confirm_clear_all = st.checkbox("Entiendo y deseo continuar", value=False, key="confirm_clear_all_chk")
            if st.button("Eliminar definitivamente", type="primary", disabled=not st.session_state.confirm_clear_all):
                clear_all_orders(); st.warning("Se borraron todos los pedidos."); _safe_rerun()

    st.markdown("<hr class='hr'/>", unsafe_allow_html=True)

    try:
        orders = [dict_to_order(o) for o in load_orders()]
    except Exception as e:
        orders = []; st.error(f"No se pudieron cargar pedidos: {e}")

    st.markdown("<div class='panel'>", unsafe_allow_html=True)
    st.markdown("**🟦 En cocina (Pedidos activos)**")
    if not orders:
        st.caption("Sin pedidos.")
    else:
        for o in sorted(orders, key=lambda x: x.id):
            border = STATUS_BORDERS["NUEVO"]
            label = ("Mesa" if o.type == "MESA" else "Para llevar") + (f" {o.table}" if o.table else "")
            st.markdown(
                f"""
                <div class="kcard" style="--border-color:{border}">
                  <div class="krow">
                    <div class="ktitle">{o.code}</div>
                    <div class="kbadge">Nuevo</div>
                  </div>
                  <div class="kmuted">{label} · {o.created_at}</div>
                """, unsafe_allow_html=True
            )
            items_html = ""
            for it in o.items:
                items_html += f'<li>{it.quantity} x {it.name} — {peso(it.price)}</li>'
            st.markdown(f"<ul class='klist'>{items_html}</ul>", unsafe_allow_html=True)
            if o.notes:
                st.markdown(f"<div class='kmuted'>📝 {o.notes}</div>", unsafe_allow_html=True)
            st.markdown(f"**Subtotal:** {peso(o.subtotal)} · **IVA (19%):** {peso(o.iva)} · **Total:** {peso(o.total)}")
            st.markdown("</div>", unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

# =========================== TAB NOTIFICADOR ===========================
with tab_notify:
    st.markdown("<div class='section-title'>Notificador de Entregas</div>", unsafe_allow_html=True)
    st.caption("⚠️ Esta pantalla la opera quien entrega al cliente. La cocina no debe usarla.")
    if st.button("Recargar ahora 🔄", use_container_width=True, key="reload_notify"):
        _safe_rerun()

    st.markdown("<hr class='hr'/>", unsafe_allow_html=True)

    try:
        active_orders = [dict_to_order(o) for o in load_orders()]
    except Exception as e:
        active_orders = []; st.error(f"No se pudieron cargar pedidos: {e}")

    st.markdown("<div class='panel'>", unsafe_allow_html=True)
    if not active_orders:
        st.info("No hay pedidos para entregar.")
    else:
        for o in sorted(active_orders, key=lambda z: z.id):
            border = STATUS_BORDERS["NUEVO"]
            label = ("Mesa" if o.type == "MESA" else "Para llevar") + (f" {o.table}" if o.table else "")
            st.markdown(
                f"""
                <div class="kcard" style="--border-color:{border}">
                  <div class="krow">
                    <div class="ktitle">{o.code}</div>
                    <div class="kbadge">Listo para enviar</div>
                  </div>
                  <div class="kmuted">{label} · {o.created_at}</div>
                """, unsafe_allow_html=True
            )
            items_html = ""
            for it in o.items:
                items_html += f'<li>{it.quantity} x {it.name} — {peso(it.price)}</li>'
            st.markdown(f"<ul class='klist'>{items_html}</ul>", unsafe_allow_html=True)

            # Mensaje factura + acciones
            msg_text = build_invoice_message(o)
            phone_e164 = normalize_co_phone(o.customer_phone)
            wa_link = wa_me_link(phone_e164, msg_text)

            a1, a2, a3, a4 = st.columns([0.44, 0.18, 0.20, 0.18])
            with a1:
                st.caption(f"Cliente: **{o.customer_name or '—'}** · Cel: **{phone_e164 or '—'}**")
                st.code(msg_text, language=None)
            with a2:
                st.markdown(f"[📲 WhatsApp Web/App]({wa_link})", unsafe_allow_html=True)
            with a3:
                if st.button("Enviar WhatsApp (Twilio API)", use_container_width=True, key=f"wa_{o.id}"):
                    try:
                        if not phone_e164:
                            raise RuntimeError("Número de celular vacío o inválido.")
                        sid = send_whatsapp_twilio_requests(phone_e164, msg_text)
                        st.success(f"WhatsApp enviado ✅ SID: {sid}")
                    except Exception as e:
                        st.error(f"Error enviando WhatsApp: {e}")
            with a4:
                st.download_button(
                    "⬇️ Descargar SMS.txt",
                    data=msg_text.encode("utf-8"),
                    file_name=f"Factura_{o.id:04d}.txt",
                    mime="text/plain",
                    use_container_width=True
                )

            # Entregar / Eliminar
            c1, c2 = st.columns([0.70, 0.30])
            with c1:
                st.markdown("<span class='small-muted'>Después de notificar al cliente, elimina el pedido para limpiar Cocina.</span>", unsafe_allow_html=True)
            with c2:
                if st.button("Entregar / Eliminar ✅", use_container_width=True, key=f"deliver_{o.id}"):
                    try:
                        delete_order(o.id); st.success(f"Pedido {o.code} entregado y eliminado."); _safe_rerun()
                    except Exception as e:
                        st.error(f"No fue posible eliminar: {e}")
            st.markdown("</div>", unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)
