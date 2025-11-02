from __future__ import annotations
"""
app_mobile.py — Interfaz móvil (Streamlit) para emitir comprobantes POS en el teléfono
- UI adaptada a celular (controles grandes, flujo simple)
- Genera PDF «Documento Equivalente POS»
- Guarda cada comprobante en invoices_store.json
- Link público opcional si configuras AWS S3 (presignado)
- Envío por correo (SMTP) con PDF adjunto
- Inicio de sesión con Google o Microsoft (opcional, recomendado para enviar correos)

Dependencias nuevas:
    pip install streamlit reportlab boto3 msal google-auth-oauthlib

Ejecuta:
    streamlit run app_mobile.py
"""

import os
import re
import json
from dataclasses import dataclass, asdict
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import List, Literal

import streamlit as st

# --- Email / PDF / Cloud ---
import smtplib
from email.message import EmailMessage

import boto3
from botocore.exceptions import BotoCoreError, ClientError

# --- OAuth ---
from google_auth_oauthlib.flow import Flow as GoogleFlow
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests
import msal

# =================== Configuración base ===================
st.set_page_config(
    page_title="Comprobantes Móvil — POS",
    page_icon="📱",
    layout="centered",
    initial_sidebar_state="expanded",
)

COMPANY_NAME = os.getenv("COMPANY_NAME", "UNISABANA DINING S.A.S")
COMPANY_NIT = os.getenv("COMPANY_NIT", "900.123.456-7")
IVA_RATE = float(os.getenv("IVA_RATE", "0.19"))

# Archivos locales
ROOT = Path(__file__).parent
INVOICES_PATH = ROOT / "invoices_store.json"
PDF_DIR = ROOT / "invoices_pdfs"
PDF_DIR.mkdir(exist_ok=True)

# AWS S3 (opcional para link público)
AWS_S3_BUCKET = os.getenv("AWS_S3_BUCKET", "")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
PRESIGNED_TTL_SECONDS = int(os.getenv("PRESIGNED_TTL_SECONDS", "3600"))

# SMTP (para correo formal)
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")

# Google OAuth
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "")  # ej: https://tuapp.streamlit.app
GOOGLE_OAUTH_SCOPES = ["openid", "email", "profile"]

# Microsoft OAuth (Entra ID)
MS_CLIENT_ID = os.getenv("MS_CLIENT_ID", "")
MS_CLIENT_SECRET = os.getenv("MS_CLIENT_SECRET", "")
MS_TENANT_ID = os.getenv("MS_TENANT_ID", "common")
MS_REDIRECT_URI = os.getenv("MS_REDIRECT_URI", "")
MS_AUTHORITY = f"https://login.microsoftonline.com/{MS_TENANT_ID}"
MS_SCOPES = ["User.Read", "openid", "email", "profile"]

# =================== Estado ===================
if "cart" not in st.session_state:
    st.session_state.cart = {}
if "user" not in st.session_state:
    st.session_state.user = None  # {provider, email, name}

# =================== Modelos & utilidades ===================
Status = Literal["NUEVO", "EMITIDO"]

@dataclass
class OrderItem:
    product_id: str
    name: str
    price: float
    quantity: int

@dataclass
class Invoice:
    id: int
    code: str
    items: List[OrderItem]
    customer_name: str
    customer_email: str
    customer_phone: str
    subtotal: float
    iva: float
    total: float
    medio_pago: str
    created_at: str  # ISO
    pdf_path: str | None
    s3_url: str | None
    status: Status


def _ensure_store():
    if not INVOICES_PATH.exists():
        INVOICES_PATH.write_text(json.dumps({"invoices": [], "meta": {"last_id": 0}}, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_store() -> dict:
    _ensure_store()
    try:
        return json.loads(INVOICES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"invoices": [], "meta": {"last_id": 0}}


def _write_store(data: dict):
    tmp = INVOICES_PATH.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(INVOICES_PATH)


def next_invoice_id_and_code() -> tuple[int, str]:
    data = _read_store()
    last_id = int(data["meta"].get("last_id", 0)) + 1
    data["meta"]["last_id"] = last_id
    _write_store(data)
    return last_id, f"DPOS-{last_id:06d}"


def peso(valor: float) -> str:
    return f"$ {valor:,.0f}".replace(",", ".")


def compute_subtotal(items: list[OrderItem]) -> float:
    return sum(i.quantity * i.price for i in items)


def compute_iva(subtotal: float) -> float:
    return round(subtotal * IVA_RATE)


def normalize_co_phone(raw: str) -> str:
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


def build_invoice_message(inv: Invoice) -> str:
    dt = datetime.fromisoformat(inv.created_at)
    fecha = dt.strftime("%d/%m/%Y")
    hour12 = dt.hour % 12 or 12
    suf = "a.m." if dt.hour < 12 else "p.m."
    hora = f"{hour12}:{dt.strftime('%M')} {suf}"

    lines = [
        f"🍽 {COMPANY_NAME.split(' S.A.S')[0]} – Documento Equivalente Electrónico POS",
        f"NIT: {COMPANY_NIT}",
        f"Fecha: {fecha} – {hora}",
        f"Consecutivo: {inv.code}",
        "",
    ]
    for it in inv.items:
        linea_precio = peso(it.price * it.quantity)
        lines.append(f"• {'%d x ' % it.quantity if it.quantity>1 else ''}{it.name} – {linea_precio}")
    lines += [
        f"Subtotal: {peso(inv.subtotal)}",
        f"IVA (19 %): {peso(inv.iva)}",
        f"Total: 💲{str(int(inv.total)).replace(',', '.')}",
        f"Medio de pago: {inv.medio_pago}",
        "",
        f"Comprobante electrónico generado por {COMPANY_NAME.split(' S.A.S')[0]} conforme a la Resolución DIAN 000165 de 2023.",
        "Será transmitido electrónicamente a la DIAN conforme a los plazos del Anexo Técnico 1.9.",
    ]
    return "\n".join(lines)


# ===== PDF =====

def build_invoice_pdf_bytes(inv: Invoice) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import mm
    from reportlab.lib.utils import simpleSplit

    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    left = 18 * mm
    top = height - 18 * mm

    text = build_invoice_message(inv)

    c.setFont("Helvetica-Bold", 14)
    c.drawString(left, top, f"{COMPANY_NAME} — Documento Equivalente POS")
    c.setFont("Helvetica", 10)
    y = top - 10 * mm

    for line in text.split("\n"):
        wrapped = simpleSplit(line, "Helvetica", 10, width - 2 * left)
        for w in wrapped:
            c.drawString(left, y, w)
            y -= 6 * mm
            if y < 20 * mm:
                c.showPage()
                c.setFont("Helvetica", 10)
                y = top

    c.showPage()
    c.save()
    return buffer.getvalue()


# ===== S3 opcional =====

def s3_upload_and_get_link(key: str, data: bytes) -> str | None:
    if not AWS_S3_BUCKET:
        return None
    try:
        s3 = boto3.client("s3", region_name=AWS_REGION)
        s3.put_object(Bucket=AWS_S3_BUCKET, Key=key, Body=data, ContentType="application/pdf")
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": AWS_S3_BUCKET, "Key": key},
            ExpiresIn=PRESIGNED_TTL_SECONDS,
        )
        return url
    except (BotoCoreError, ClientError) as e:
        st.warning(f"No se pudo subir a S3: {e}")
        return None


# ===== Email (SMTP) =====

def send_email_with_pdf(to_email: str, subject: str, body_text: str, pdf_bytes: bytes, filename: str):
    host = st.session_state.get("SMTP_HOST", SMTP_HOST)
    port = int(st.session_state.get("SMTP_PORT", SMTP_PORT))
    user = st.session_state.get("SMTP_USER", SMTP_USER)
    pwd = st.session_state.get("SMTP_PASS", SMTP_PASS)
    if not (host and user and pwd):
        raise RuntimeError("Faltan credenciales SMTP (HOST/USER/PASS). Configúralas en el panel." )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_email
    msg.set_content(body_text)
    msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=filename)

    with smtplib.SMTP(host, port) as s:
        s.starttls()
        s.login(user, pwd)
        s.send_message(msg)


# ===== WA link =====
import urllib.parse

def wa_me_link(phone_e164: str, text: str) -> str:
    enc = urllib.parse.quote(text)
    digits = re.sub(r"\D", "", phone_e164)
    return f"https://wa.me/{digits}?text={enc}" if digits else f"https://wa.me/?text={enc}"


# ===== OAuth =====

def google_login_button():
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_REDIRECT_URI):
        st.info("Configura GOOGLE_CLIENT_ID/SECRET/REDIRECT_URI para activar Google Login.")
        return
    if st.button("Iniciar sesión con Google"):
        flow = GoogleFlow.from_client_config(
            {
                "web": {
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [GOOGLE_REDIRECT_URI],
                }
            },
            scopes=GOOGLE_OAUTH_SCOPES,
        )
        auth_url, state = flow.authorization_url(prompt="consent", include_granted_scopes="true")
        st.session_state["oauth_state"] = state
        st.session_state["oauth_provider"] = "google"
        st.markdown(f"[Continuar con Google]({auth_url})")


def handle_google_callback():
    params = st.experimental_get_query_params()
    if params.get("state") and params.get("code") and st.session_state.get("oauth_provider") == "google":
        flow = GoogleFlow.from_client_config(
            {
                "web": {
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [GOOGLE_REDIRECT_URI],
                }
            },
            scopes=GOOGLE_OAUTH_SCOPES,
        )
        # Nota: Streamlit Cloud usa la URL actual como redirect; algunos despliegues requieren pasarla explícita.
        flow.fetch_token(authorization_response=os.environ.get("OAUTH_REDIRECT_FULL", ""))
        creds = flow.credentials
        try:
            idinfo = google_id_token.verify_oauth2_token(creds.id_token, google_requests.Request(), GOOGLE_CLIENT_ID)
            st.session_state.user = {
                "provider": "google",
                "email": idinfo.get("email", ""),
                "name": idinfo.get("name", ""),
                "picture": idinfo.get("picture", ""),
            }
            st.success(f"Sesión iniciada: {st.session_state.user['email']}")
        except Exception as e:
            st.error(f"Error validando Google ID token: {e}")


def microsoft_login_button():
    if not (MS_CLIENT_ID and MS_CLIENT_SECRET and MS_REDIRECT_URI):
        st.info("Configura MS_CLIENT_ID/SECRET/REDIRECT_URI para Microsoft Login.")
        return
    if st.button("Iniciar sesión con Microsoft"):
        app = msal.ConfidentialClientApplication(
            MS_CLIENT_ID, authority=MS_AUTHORITY, client_credential=MS_CLIENT_SECRET
        )
        auth_url = app.get_authorization_request_url(MS_SCOPES, redirect_uri=MS_REDIRECT_URI)
        st.session_state["oauth_provider"] = "microsoft"
        st.markdown(f"[Continuar con Microsoft]({auth_url})")


def handle_microsoft_callback():
    params = st.experimental_get_query_params()
    if params.get("code") and st.session_state.get("oauth_provider") == "microsoft":
        app = msal.ConfidentialClientApplication(
            MS_CLIENT_ID, authority=MS_AUTHORITY, client_credential=MS_CLIENT_SECRET
        )
        result = app.acquire_token_by_authorization_code(
            params["code"][0], scopes=MS_SCOPES, redirect_uri=MS_REDIRECT_URI
        )
        if "id_token_claims" in result:
            claims = result["id_token_claims"]
            st.session_state.user = {
                "provider": "microsoft",
                "email": claims.get("preferred_username", ""),
                "name": claims.get("name", ""),
            }
            st.success(f"Sesión iniciada: {st.session_state.user['email']}")
        else:
            st.error(f"Error autenticando con Microsoft: {result.get('error_description')}")


# ===== Estilos (mobile-first) =====
MOBILE_CSS = """
<style>
:root{ --bg:#0b1220; --panel:#0f1b36; --accent:#0e4bc9; --text:#fff; --muted:#c7d6ff; }
html, body, [data-testid="stAppViewContainer"]{ background:var(--bg)!important; color:var(--text)!important; }
.stButton button, .stTextInput input, .stNumberInput input, .stTextArea textarea{ font-size:20px !important; }
.stButton button{ min-height:56px !important; font-weight:800 !important; border-radius:14px !important; background:var(--accent)!important; color:#fff!important; }
.card{ background:linear-gradient(180deg, #0f1b36, #0b1530); border:2px solid #21305a; border-radius:16px; padding:16px; }
.hr{ height:1px; background:linear-gradient(90deg, transparent, #3552ad, transparent); border:0; margin:12px 0 16px 0; }
.label{ color:var(--muted); font-size:14px; margin-bottom:6px; }
.total{ font-size:30px; font-weight:1000; margin-top:6px; }
</style>
"""
st.markdown(MOBILE_CSS, unsafe_allow_html=True)

# =================== Sidebar (Login + SMTP + S3) ===================
st.sidebar.markdown("### 🔐 Autenticación")
if st.session_state.user:
    st.sidebar.success(f"Conectado: {st.session_state.user.get('email','')}")
    if st.sidebar.button("Cerrar sesión"):
        st.session_state.user = None
        st.experimental_set_query_params()
else:
    google_login_button()
    microsoft_login_button()
    handle_google_callback()
    handle_microsoft_callback()

st.sidebar.markdown("---")
st.sidebar.markdown("### ✉️ SMTP (para correo)")
st.session_state["SMTP_HOST"] = st.sidebar.text_input("SMTP_HOST", value=SMTP_HOST)
st.session_state["SMTP_PORT"] = st.sidebar.number_input("SMTP_PORT", value=SMTP_PORT, step=1)
st.session_state["SMTP_USER"] = st.sidebar.text_input("SMTP_USER", value=SMTP_USER)
st.session_state["SMTP_PASS"] = st.sidebar.text_input("SMTP_PASS", value=SMTP_PASS, type="password")

st.sidebar.markdown("### ☁️ AWS S3 (opcional)")
st.session_state["AWS_S3_BUCKET"] = st.sidebar.text_input("AWS_S3_BUCKET", value=AWS_S3_BUCKET)
st.session_state["AWS_REGION"] = st.sidebar.text_input("AWS_REGION", value=AWS_REGION)
st.session_state["PRESIGNED_TTL_SECONDS"] = st.sidebar.number_input("PRESIGNED_TTL_SECONDS", value=PRESIGNED_TTL_SECONDS, step=60)

# =================== Encabezado ===================
st.title("📱 Comprobantes Móvil")
st.caption("Emitir PDF · WhatsApp · Correo · Guardado local/S3")
st.markdown("<hr class='hr' />", unsafe_allow_html=True)

# =================== Formulario rápido (ítems + cliente) ===================
left, right = st.columns([1, 1])
with left:
    st.subheader("🛒 Ítems")
    with st.container(border=True):
        c1, c2, c3, c4 = st.columns([1.2, 1, 1, 1])
        pname = c1.text_input("Producto", key="it_name", placeholder="Ej: Hamburguesa")
        pprice = c2.number_input("Precio", key="it_price", min_value=0, step=500)
        pqty = c3.number_input("Cant.", key="it_qty", min_value=1, step=1, value=1)
        pid = c4.text_input("ID", key="it_id", placeholder="Opcional")
        if st.button("Agregar ítem ➕", use_container_width=True):
            if not pname or pprice <= 0:
                st.warning("Completa nombre y precio positivos.")
            else:
                st.session_state.cart[pid or f"X{len(st.session_state.cart)+1}"] = {
                    "product_id": pid or f"X{len(st.session_state.cart)+1}",
                    "name": pname,
                    "price": float(pprice),
                    "quantity": int(pqty),
                }
                st.success("Ítem agregado.")

    if st.session_state.cart:
        st.markdown("**Detalle**")
        for k, row in list(st.session_state.cart.items()):
            col1, col2, col3, col4 = st.columns([1.8, 1, 1, 0.6])
            col1.write(row["name"])
            col2.write(peso(row["price"]))
            col3.write(f"x {row['quantity']}")
            if col4.button("🗑️", key=f"del_{k}"):
                st.session_state.cart.pop(k, None)
        st.markdown("<hr class='hr' />", unsafe_allow_html=True)
    else:
        st.info("Sin ítems todavía.")

with right:
    st.subheader("👤 Cliente")
    customer_name = st.text_input("Nombre", placeholder="Juan Pérez")
    customer_email = st.text_input("Correo (para PDF)", placeholder="cliente@correo.com")
    customer_phone_raw = st.text_input("Celular (WhatsApp)", placeholder="3001234567")
    medio_pago = st.selectbox("Medio de pago", ["Tarjeta", "Efectivo", "Transferencia", "Otro"])

# =================== Totales ===================
items = [
    OrderItem(product_id=v["product_id"], name=v["name"], price=float(v["price"]), quantity=int(v["quantity"]))
    for v in st.session_state.cart.values()
]
subtotal = compute_subtotal(items)
iva = compute_iva(subtotal)
total = subtotal + iva
st.markdown("<div class='card'>", unsafe_allow_html=True)
st.markdown("**Resumen**")
st.write(f"Subtotal: {peso(subtotal)}")
st.write(f"IVA ({int(IVA_RATE*100)}%): {peso(iva)}")
st.markdown(f"<div class='total'>Total: {peso(total)}</div>", unsafe_allow_html=True)
st.markdown("</div>", unsafe_allow_html=True)

# =================== Acciones: Emitir ===================
colA, colB = st.columns(2)

if colA.button("🧾 Emitir comprobante (PDF)", use_container_width=True):
    if not items:
        st.error("Agrega al menos un ítem.")
    elif not customer_name:
        st.error("Ingresa el nombre del cliente.")
    else:
        try:
            inv_id, code = next_invoice_id_and_code()
            inv = Invoice(
                id=inv_id,
                code=code,
                items=items,
                customer_name=customer_name.strip(),
                customer_email=customer_email.strip(),
                customer_phone=normalize_co_phone(customer_phone_raw),
                subtotal=subtotal,
                iva=iva,
                total=total,
                medio_pago=medio_pago,
                created_at=datetime.now().isoformat(timespec="seconds"),
                pdf_path=None,
                s3_url=None,
                status="EMITIDO",
            )
            pdf_bytes = build_invoice_pdf_bytes(inv)
            pdf_name = f"{code}.pdf"
            # Guarda PDF local
            local_path = PDF_DIR / pdf_name
            local_path.write_bytes(pdf_bytes)
            inv.pdf_path = str(local_path)

            # S3 opcional
            bucket = st.session_state.get("AWS_S3_BUCKET") or AWS_S3_BUCKET
            region = st.session_state.get("AWS_REGION") or AWS_REGION
            ttl = int(st.session_state.get("PRESIGNED_TTL_SECONDS") or PRESIGNED_TTL_SECONDS)
            if bucket:
                os.environ["AWS_S3_BUCKET"] = bucket
                os.environ["AWS_REGION"] = region
                os.environ["PRESIGNED_TTL_SECONDS"] = str(ttl)
                key = f"comprobantes/{inv.created_at[:10].replace('-', '')}/{inv.code}.pdf"
                link = s3_upload_and_get_link(key, pdf_bytes)
                inv.s3_url = link

            # Persistencia
            data = _read_store()
            row = asdict(inv)
            data.setdefault("invoices", []).append(row)
            # actualiza last_id si fuera necesario
            data.setdefault("meta", {}).setdefault("last_id", inv_id)
            if int(data["meta"]["last_id"]) < inv_id:
                data["meta"]["last_id"] = inv_id
            _write_store(data)

            st.success(f"Comprobante emitido: {inv.code}")
            # Mostrar descarga + WA + correo
            with st.expander("📄 Descargar / Compartir"):
                st.download_button("⬇️ Descargar PDF", data=pdf_bytes, file_name=pdf_name, mime="application/pdf")
                msg = build_invoice_message(inv)
                if inv.s3_url:
                    msg += f"\n\nDescargar comprobante en PDF:\n{inv.s3_url}"
                wa = wa_me_link(inv.customer_phone, msg)
                st.markdown(f"[📲 Abrir WhatsApp con mensaje]({wa})")

                # Envío por correo (si hay sesión y SMTP configurado)
                if st.session_state.user:
                    to_mail = inv.customer_email or st.text_input("Correo del cliente", key=f"mail_{inv.id}")
                    if st.button("Enviar por correo ✉️", key=f"send_email_{inv.id}"):
                        try:
                            subject = f"Comprobante {inv.code} — {COMPANY_NAME}"
                            body = (
                                f"Hola {inv.customer_name},\n\nAdjuntamos tu comprobante en PDF.\n\n{build_invoice_message(inv)}\n\n"
                                + (f"Link de descarga: {inv.s3_url}\n\n" if inv.s3_url else "")
                                + "Gracias por tu compra."
                            )
                            send_email_with_pdf(to_mail, subject, body, pdf_bytes, filename=pdf_name)
                            st.success("Correo enviado correctamente.")
                        except Exception as e:
                            st.error(f"No se pudo enviar el correo: {e}")
                else:
                    st.info("Inicia sesión con Google o Microsoft para habilitar el envío por correo.")

            # Limpia carrito para siguiente comprobante
            st.session_state.cart = {}
        except Exception as e:
            st.error(f"No se pudo emitir el comprobante: {e}")


if colB.button("🧹 Limpiar formulario", use_container_width=True):
    st.session_state.cart = {}
    st.experimental_rerun()

# =================== Historial rápido ===================
st.markdown("<hr class='hr' />", unsafe_allow_html=True)
st.subheader("🗂️ Últimos comprobantes")
try:
    data = _read_store()
    invs = list(reversed(data.get("invoices", [])))[:10]
    if not invs:
        st.caption("(sin historial)")
    else:
        for row in invs:
            code = row.get("code")
            created = row.get("created_at", "")
            total_v = row.get("total", 0)
            s3_url = row.get("s3_url")
            pdf_path = row.get("pdf_path")
            line = f"{code} · {created} · Total {peso(float(total_v))}"
            st.markdown(f"- {line}")
            if s3_url:
                st.markdown(f"  • Link: {s3_url}")
            elif pdf_path and Path(pdf_path).exists():
                # botón de descarga de nuevo
                try:
                    pdf_bytes = Path(pdf_path).read_bytes()
                    st.download_button("⬇️ PDF", data=pdf_bytes, file_name=f"{code}.pdf", mime="application/pdf", key=f"dl_{code}")
                except Exception:
                    pass
except Exception as e:
    st.warning(f"No se pudo cargar el historial: {e}")
