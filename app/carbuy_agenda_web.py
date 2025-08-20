# Carbuy Coletor — HTTPServer + Playwright (resumo dinâmico por status)
# Fluxo: informar até 3 eventos (código CBY ou URL) -> login automático -> coleta -> Resumo do evento + Resumo por Status

import asyncio
import os
import re
import sys
import time
import html
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs
from http.server import BaseHTTPRequestHandler, HTTPServer

import pandas as pd
from bs4 import BeautifulSoup

try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
except ImportError:
    print("⚠ Playwright não instalado. Rode: pip install playwright && python -m playwright install")
    print("   Depois: python -m playwright install")
    sys.exit(1)

BASE_URL = "https://www.carbuy.com.br"
ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")
LOGO_FILE = os.path.join(ASSETS_DIR, "logo_carbuy.png")

DATE_REGEX = re.compile(
    r"\b(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})(?:\s*(?:às|as|,|\-|)\s*(\d{1,2}:\d{2}))?\b",
    flags=re.IGNORECASE,
)

# --------- Seletores tolerantes (login) ----------
USERNAME_SELECTORS = [
    "input[name='username']",
    "input[id='username']",
    "input[type='email']",
    "input[type='text']",
    "input[name*='user' i]",
    "input[name*='login' i]",
    "input[placeholder*='usu' i]",
    "input[placeholder*='e-mail' i]",
    "input[placeholder*='email' i]",
    "input[autocomplete='username']",
    "input[aria-label*='usu' i], input[aria-label*='email' i]",
]
PASSWORD_SELECTORS = [
    "input[type='password']",
    "input[name*='pass' i]",
    "input[name*='senha' i]",
    "input[autocomplete='current-password']",
    "input[aria-label*='senha' i]",
]
SUBMIT_SELECTORS = [
    "button[type='submit']",
    "input[type='submit']",
    "button:has-text('Entrar')",
    "button:has-text('Login')",
    "button:has-text('Acessar')",
    "form button",
]
LOGGED_HINTS = [
    "a:has-text('Sair')",
    "button:has-text('Sair')",
    "a[aria-label*='perfil' i]",
]

@dataclass
class EventHeader:
    event_url: str
    event_code: Optional[str]
    event_datetime: Optional[str]
    event_status: Optional[str]

@dataclass
class ItemRow:
    event_url: str
    model: Optional[str]
    status_text: Optional[str]
    current_value: Optional[str]

# ------------- utils -------------
def _clean_text(txt: Any) -> str:
    if not isinstance(txt, str):
        txt = str(txt) if txt is not None else ""
    return re.sub(r"\s+", " ", txt).strip()

def _parse_brl_currency(s: Optional[str]) -> float:
    """Converte 'R$ 26.000,00' -> 26000.0; inválidos -> 0.0"""
    if not s:
        return 0.0
    s = s.replace("R$", "").replace(" ", "").replace(".", "").replace(",", ".")
    try:
        m = re.search(r"-?\d+(?:\.\d+)?", s)
        return float(m.group(0)) if m else 0.0
    except Exception:
        return 0.0

def fmt_brl(x: float) -> str:
    s = f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"

def normalize_event_code(s: Optional[str]) -> Optional[str]:
    """Extrai token tipo 200825CBY; senão retorna 1º termo alfanumérico limpo."""
    if not s:
        return None
    s = _clean_text(s)
    m = re.search(r"\b([0-9]{5,}[A-Za-z]{3})\b", s)
    if m:
        return m.group(1).upper()
    m = re.search(r"\b([0-9A-Za-z\-_.]+)\b", s)
    return m.group(1).upper() if m else None

def build_event_url(x: str) -> Optional[str]:
    """Aceita URL completa ou CÓDIGO (ex.: 200825CBY). Retorna URL normalizada."""
    if not x:
        return None
    x = x.strip()
    if not x:
        return None
    if "/evento/detalhes/" in x:
        if x.startswith("http"):
            return x
        if x.startswith("/"):
            return BASE_URL + x
        return BASE_URL + "/" + x
    if re.fullmatch(r"[0-9]{5,}[A-Za-z]{3}", x):
        return f"{BASE_URL}/evento/detalhes/{x.lower()}"
    return None

async def first_visible(page, selectors: List[str], timeout: int = 6000):
    deadline = time.time() + (timeout / 1000)
    last_err = None
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=2000)
            return loc
        except Exception as e:
            last_err = e
        if time.time() > deadline:
            break
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                return loc
        except Exception as e:
            last_err = e
    if last_err:
        raise last_err
    raise PWTimeout("Nenhum seletor visível encontrado")

async def is_logged(page) -> bool:
    for sel in LOGGED_HINTS:
        try:
            if await page.locator(sel).count() > 0:
                return True
        except Exception:
            pass
    return False

async def ensure_login(context, username: str, password: str, return_after: Optional[str] = None) -> None:
    """Garante login; tenta /conta/entrar com returnUrl, fecha cookies, preenche em frames se preciso."""
    page = await context.new_page()
    try:
        target = return_after or BASE_URL
        await page.goto(target, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=12000)
        except PWTimeout:
            pass

        if await is_logged(page):
            return

        if "/conta/entrar" not in page.url and "/login" not in page.url:
            await page.goto(f"{BASE_URL}/conta/entrar?returnUrl={target.replace(BASE_URL,'')}", wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=12000)
            except PWTimeout:
                pass

        # fecha/aceita cookies
        for sel in [
            "button:has-text('Aceitar')",
            "button:has-text('Aceito')",
            "button:has-text('Concordo')",
            "#btn-accept-cookies",
            ".cookies-aceitar, .cookie-accept",
        ]:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_enabled():
                    await loc.click()
                    await page.wait_for_timeout(300)
            except Exception:
                pass

        async def find_in_all_frames(selector_list):
            for sel in selector_list:
                try:
                    loc = page.locator(sel).first
                    await loc.wait_for(state="visible", timeout=2500)
                    return loc
                except Exception:
                    pass
            for f in page.frames:
                for sel in selector_list:
                    try:
                        loc = f.locator(sel).first
                        await loc.wait_for(state="visible", timeout=2500)
                        return loc
                    except Exception:
                        pass
            return None

        # usuário
        user_loc = await find_in_all_frames(USERNAME_SELECTORS)
        if not user_loc:
            try:
                user_loc = page.get_by_label(re.compile(r"usu[aá]rio|email|e-mail|login", re.I)).first
                await user_loc.wait_for(state="visible", timeout=2500)
            except Exception:
                pass
        if user_loc:
            try:
                await user_loc.fill(username)
            except Exception:
                try:
                    await page.evaluate("""
                        (v)=>{ const e=[...document.querySelectorAll('input')].find(i=>/user|login|email/i.test(i.name||'')||/usu|email/i.test(i.placeholder||'')); if(e){e.value=v; e.dispatchEvent(new Event('input',{bubbles:true}));} }
                    """, username)
                except Exception:
                    pass

        # senha
        pass_loc = await find_in_all_frames(PASSWORD_SELECTORS)
        if not pass_loc:
            try:
                pass_loc = page.get_by_label(re.compile(r"senha|password", re.I)).first
                await pass_loc.wait_for(state="visible", timeout=2500)
            except Exception:
                pass
        if pass_loc:
            try:
                await pass_loc.fill(password)
            except Exception:
                try:
                    await page.evaluate("""
                        (v)=>{ const e=[...document.querySelectorAll('input')].find(i=>i.type==='password'||/senha|pass/i.test(i.name||'')); if(e){e.value=v; e.dispatchEvent(new Event('input',{bubbles:true}));} }
                    """, password)
                except Exception:
                    pass

        # submit
        submitted = False
        for sel in SUBMIT_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_enabled():
                    await btn.click()
                    submitted = True
                    break
            except Exception:
                pass
        if not submitted:
            try:
                await page.keyboard.press("Enter")
                submitted = True
            except Exception:
                pass

        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            pass

        if not await is_logged(page):
            raise RuntimeError("Falha no login automático (campos não visíveis ou credenciais recusadas).")

        if return_after:
            await page.goto(return_after, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=12000)
            except PWTimeout:
                pass
    finally:
        await page.close()

# ---------- parsing de /evento/detalhes ----------
async def parse_event_header(page) -> EventHeader:
    url = page.url
    html_str = await page.content()
    soup = BeautifulSoup(html_str, "lxml") if "lxml" in sys.modules else BeautifulSoup(html_str, "html.parser")

    # Evento (código)
    event_code = None
    for info in soup.select(".pgheader-info, .pgheader-event, .pghd-info, .pghd-event"):
        txt = _clean_text(info.get_text("\n"))
        m = re.search(r"Evento:\s*([^\n]+)", txt, re.I)
        if m:
            event_code = normalize_event_code(m.group(1))
            break
    if not event_code:
        event_code = normalize_event_code(_clean_text(soup.get_text(" ")))

    # Data/Horário
    event_datetime = None
    for info in soup.select(".pgheader-info, .pgheader-event"):
        txt = _clean_text(info.get_text(" "))
        m = DATE_REGEX.findall(txt)
        if m:
            d, h = m[0]
            event_datetime = f"{d} {h}".strip()
            break

    # Status do evento
    status = None
    cand = soup.select_one("#situacaoEvento")
    if cand:
        status = _clean_text(cand.get_text(" "))
    if not status:
        alltxt = _clean_text(soup.get_text(" "))
        m = re.search(r"(Evento\s+Aberto|Evento\s+Encerrado|Em\s+Andamento|Aberto)", alltxt, re.I)
        if m:
            status = _clean_text(m.group(1))

    return EventHeader(url, event_code, event_datetime, status)

async def collect_items_current_page(page, event_url: str) -> List[ItemRow]:
    rows: List[ItemRow] = []
    container = page.locator("#placeholder, #ev-list, .ev-list, .container-fluid.ev-list").first
    if await container.count() == 0:
        container = page.locator("body")

    cards_loc = container.locator(".card-anuncio, .card.text-center.h-100, div[class*='card-anuncio']")
    count = await cards_loc.count()
    indices = range(count) if count else [-1]

    for i in indices:
        if i >= 0:
            try:
                card_classes = (await cards_loc.nth(i).get_attribute("class") or "").lower()
            except Exception:
                card_classes = ""
            html_card = await cards_loc.nth(i).inner_html()
        else:
            card_classes = ""
            html_card = await container.inner_html()

        soup = BeautifulSoup(html_card, "lxml") if "lxml" in sys.modules else BeautifulSoup(html_card, "html.parser")

        # Modelo
        model = None
        for sel in (".card-title h1", "h1.card-title", "h1"):
            el = soup.select_one(sel)
            if el:
                model = _clean_text(el.get_text(" "))
                if model:
                    break

        # Situação por texto
        status_text = None
        for sel in (".situacao.shadow", ".situacao", ".card-situacaoevento", ".anuncio-icon + *"):
            el = soup.select_one(sel)
            if el:
                status_text = _clean_text(el.get_text(" "))
                if status_text:
                    break
        if not status_text:
            m = re.search(
                r"([A-Za-zÀ-ÿ ]*Vendido[A-Za-zÀ-ÿ ]*|Condicional|Aberto\s+para\s+ofertas|Encerrado|Arrematado|Em\s+Leilão|Em\s+Andamento)",
                _clean_text(soup.get_text(" ")),
                re.I,
            )
            if m:
                status_text = _clean_text(m.group(1))

        # Refino por classes CSS (ex.: 'anuncio-Vendido', 'anuncio-Condicional')
        css_classes = card_classes + " " + " ".join(
            " ".join(el.get("class", [])) if isinstance(el.get("class"), list) else (el.get("class") or "")
            for el in soup.select("[class*='anuncio-'], [class*='vend'], [class*='condic']")
        )
        css_classes = css_classes.lower()
        if "anuncio-vendido" in css_classes or "arrematado" in css_classes or re.search(r"\bvendid", css_classes):
            status_text = "Vendido"
        elif "anuncio-condicional" in css_classes or re.search(r"\bcondic", css_classes):
            status_text = "Condicional"

        # Valor Atual
        current_value = None
        for sel in (".card-valoratual h2", "h2.valor-atual", ".valor-atual", "span.valor-atual"):
            el = soup.select_one(sel)
            if el:
                val = _clean_text(el.get_text(" "))
                if "R$" in val or re.search(r"\d[\d\.\,]*", val):
                    current_value = val
                    break

        if any([model, status_text, current_value]):
            rows.append(ItemRow(event_url, model, status_text, current_value))

    return rows

async def has_next_page(page) -> Tuple[bool, Optional[str]]:
    next_locators = [
        "a[rel='next']",
        "a[aria-label*='róximo' i]",
        "button[aria-label*='róximo' i]",
        "a.page-link:has-text('Próximo')",
        "a:has-text('Próximo')",
    ]
    for sel in next_locators:
        loc = page.locator(sel).first
        try:
            if await loc.count() > 0 and await loc.is_enabled():
                return True, sel
        except Exception:
            pass
    return False, None

async def click_next(page, selector: str) -> bool:
    try:
        loc = page.locator(selector).first
        await loc.click()
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except PWTimeout:
            pass
        return True
    except Exception:
        return False

async def collect_items_with_pagination(page, event_url: str) -> List[ItemRow]:
    all_rows: List[ItemRow] = []
    all_rows.extend(await collect_items_current_page(page, event_url))

    for _ in range(100):  # limite de segurança
        has_next, sel = await has_next_page(page)
        if not has_next or not sel:
            break
        if not await click_next(page, sel):
            break
        all_rows.extend(await collect_items_current_page(page, event_url))

    # dedup
    uniq = []
    seen = set()
    for r in all_rows:
        key = (r.event_url, r.model, r.status_text, r.current_value)
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    return uniq

# ---------- agregação dinâmica por status ----------
def summarize_dynamic(header: EventHeader, items: List[ItemRow]) -> Dict[str, Any]:
    """Gera resumo por evento + breakdown dinâmico por status (contagem e soma)."""
    ev_items = [r for r in items if r.event_url == header.event_url]

    status_count: Dict[str, int] = {}
    status_sum: Dict[str, float] = {}

    for r in ev_items:
        status = _clean_text(r.status_text or "Sem status")
        value = _parse_brl_currency(r.current_value)
        status_count[status] = status_count.get(status, 0) + 1
        status_sum[status] = status_sum.get(status, 0.0) + value

    return {
        "Evento": normalize_event_code(header.event_code) or header.event_code,
        "Data/Horário": header.event_datetime,
        "Status_Evento": header.event_status,
        "Total_Anuncios": len(ev_items),
        "Status_Count": status_count,
        "Status_Sum": status_sum,
    }

# --------------- fluxo principal ---------------
async def scrape(username: str, password: str, headless: bool, event_inputs: List[str]):
    headers: List[EventHeader] = []
    items: List[ItemRow] = []

    # normaliza entradas -> URLs (até 3)
    urls: List[str] = []
    for raw in (event_inputs or []):
        u = build_event_url(raw)
        if u:
            urls.append(u)
    urls = urls[:3]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(viewport={"width": 1366, "height": 900})

        # login obrigatório — usa o 1º evento como returnUrl
        return_after = urls[0] if urls else BASE_URL
        await ensure_login(context, username, password, return_after=return_after)

        # coleta por evento
        for u in urls:
            ev = await context.new_page()
            try:
                await ev.goto(u, wait_until="domcontentloaded")
                try:
                    await ev.wait_for_load_state("networkidle", timeout=10000)
                except PWTimeout:
                    pass
                header = await parse_event_header(ev)
                headers.append(header)
                rows = await collect_items_with_pagination(ev, header.event_url)
                items.extend(rows)
            finally:
                await ev.close()

        await browser.close()

    # resumo por evento (dinâmico)
    summaries = [summarize_dynamic(h, items) for h in headers]
    return summaries  # lista de dicionários por evento

# --------------- servidor web ---------------
def _read_logo_bytes() -> Optional[bytes]:
    try:
        with open(LOGO_FILE, "rb") as f:
            return f.read()
    except Exception:
        return None

def base_html(body: str, message: str = "") -> bytes:
    page = f"""<!doctype html><html lang=pt-br>
<head>
  <meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Carbuy Coletor</title>
  <style>
    :root {{ --red:#ff1a1a; --dark:#0b0f14; --card:#111827; --border:#2a3441; --text:#e6edf3; --muted:#a6b3c2; --btn:#000 }}
    * {{ box-sizing:border-box }}
    body {{ margin:0; background:var(--dark); color:var(--text); font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial }}
    .hero {{ background:var(--red); padding:32px 16px; display:flex; justify-content:center }}
    .hero img {{ height:110px; width:auto; display:block }}
    .wrap {{ max-width:980px; margin:0 auto; padding:24px 16px }}
    .card {{ background:var(--card); border:1px solid var(--border); border-radius:14px; padding:20px; box-shadow:0 10px 30px rgba(0,0,0,.25); margin-bottom:16px }}
    h1 {{ margin:0 0 8px 0; font-size:24px }}
    h2 {{ margin:0 0 8px 0; font-size:20px }}
    p.muted {{ color:var(--muted); margin:0 0 16px 0 }}
    label {{ font-size:12px; color:var(--muted); display:block; margin-bottom:6px }}
    input,select {{ width:100%; padding:12px 14px; border-radius:10px; border:1px solid var(--border); background:#0f172a; color:var(--text); outline:none }}
    .row {{ display:grid; grid-template-columns:1fr 1fr; gap:14px }}
    .row-1 {{ display:grid; grid-template-columns:1fr; gap:14px }}
    .btn {{ width:100%; padding:12px 16px; border-radius:12px; background:var(--btn); color:#fff; border:none; cursor:pointer; font-weight:700; letter-spacing:.3px }}
    .btn:active {{ transform:translateY(1px) }}
    .footer {{ text-align:center; color:#fff; background:var(--red); padding:10px 12px; font-size:12px; margin-top:24px }}
    table.grid {{ width:100%; border-collapse:collapse; margin-top:8px }}
    table.grid th,table.grid td {{ border:1px solid var(--border); padding:6px 8px; text-align:left }}
    table.grid th {{ background:#0f172a }}
    .msg {{ margin:12px 0; padding:10px 12px; background:#1f2937; border:1px solid var(--border); border-radius:10px }}
    .actions {{ display:flex; gap:10px; margin-top:12px }}
    .link {{ color:#fff; text-decoration:none }}
    .pill {{ display:inline-block; background:#0f172a; border:1px solid var(--border); border-radius:999px; padding:3px 10px; margin-left:8px; font-size:12px; }}
  </style>
</head>
<body>
  <div class="hero"><img src="/static/logo" alt="Carbuy Logo"/></div>
  <div class="wrap">{('<div class="msg">'+message+'</div>' if message else '')}{body}</div>
  <div class="footer">Desenvolvido por Bruno Nascimento – MKT Team</div>
</body></html>"""
    return page.encode("utf-8")

def table_basic_summary(summaries: List[Dict[str, Any]]) -> str:
    if not summaries:
        return "<p class='muted'>(vazio)</p>"
    # Tabela: Evento / Data-Hora / Status_Evento / Total_Anuncios
    rows = []
    rows.append("<table class='grid'><thead><tr>"
                "<th>Evento</th><th>Data/Horário</th><th>Status_Evento</th><th>Total_Anúncios</th>"
                "</tr></thead><tbody>")
    for s in summaries:
        rows.append("<tr>"
                    f"<td>{html.escape(str(s.get('Evento') or ''))}</td>"
                    f"<td>{html.escape(str(s.get('Data/Horário') or ''))}</td>"
                    f"<td>{html.escape(str(s.get('Status_Evento') or ''))}</td>"
                    f"<td>{int(s.get('Total_Anuncios') or 0)}</td>"
                    "</tr>")
    rows.append("</tbody></table>")
    return "".join(rows)

def table_status_breakdown(summary: Dict[str, Any]) -> str:
    """Tabela dinâmica por status para 1 evento."""
    status_count: Dict[str, int] = summary.get("Status_Count", {}) or {}
    status_sum: Dict[str, float] = summary.get("Status_Sum", {}) or {}

    if not status_count:
        return "<p class='muted'>(sem anúncios)</p>"

    rows = []
    rows.append("<table class='grid'><thead><tr><th>Status</th><th>Quantidade</th><th>Soma</th></tr></thead><tbody>")
    # Ordena pelo maior volume
    for st, qtd in sorted(status_count.items(), key=lambda kv: (-kv[1], kv[0].lower())):
        soma = fmt_brl(float(status_sum.get(st, 0.0)))
        rows.append(f"<tr><td>{html.escape(st)}</td><td>{qtd}</td><td>{soma}</td></tr>")
    rows.append("</tbody></table>")
    # Badge total
    total_val = fmt_brl(sum(status_sum.values()))
    rows.append(f"<div class='pill'>Soma total: {total_val}</div>")
    return "".join(rows)

class AppHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Logo
        if self.path.startswith("/static/logo"):
            data = _read_logo_bytes()
            if not data:
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data); return

        # UI
        if self.path == "/" or self.path.startswith("/index"):
            body = """
            <div class='card'>
              <h1>Carbuy Coletor</h1>
              <p class='muted'>
                Informe <b>até 3 eventos</b>. No primeiro campo, coloque o <b>código CBY</b> (ex.: <code>200825CBY</code>) ou a <b>URL</b> completa.<br/>
                O relatório mostra um <b>Resumo do Evento</b> e, abaixo, o <b>Resumo por Status</b> (dinâmico).
              </p>
              <form method='POST' action='/run'>
                <div class='row'>
                  <div>
                    <label>Usuário</label>
                    <input name='username' value='convidado10' />
                  </div>
                  <div>
                    <label>Senha</label>
                    <input name='password' type='password' value='convidado10' />
                  </div>
                </div>

                <div class='row' style='margin-top:14px'>
                  <div>
                    <label>Evento 1 (código CBY ou URL)</label>
                    <input name='event1' placeholder='200825CBY'/>
                  </div>
                  <div>
                    <label>Evento 2 (opcional)</label>
                    <input name='event2' placeholder='200901CBY ou URL'/>
                  </div>
                </div>
                <div class='row' style='margin-top:14px'>
                  <div>
                    <label>Evento 3 (opcional)</label>
                    <input name='event3' placeholder='200915CBY ou URL'/>
                  </div>
                  <div>
                    <label>Headless</label>
                    <select name='headless'>
                      <option value='true'>Sim (oculto)</option>
                      <option value='false' selected>Não (mostrar)</option>
                    </select>
                  </div>
                </div>

                <div class='row-1' style='margin-top:18px'>
                  <button class='btn' type='submit'>Iniciar</button>
                </div>
              </form>
            </div>
            """
            self._ok(base_html(body)); return

        self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path.startswith("/run"):
            length = int(self.headers.get("Content-Length", "0"))
            data = self.rfile.read(length).decode("utf-8")
            form = {k: v[0] for k, v in parse_qs(data).items()}

            # eventos (códigos CBY ou URLs)
            event_inputs = [form.get("event1", ""), form.get("event2", ""), form.get("event3", "")]
            event_inputs = [x for x in (x.strip() for x in event_inputs) if x]

            username = form.get("username", "convidado10")
            password = form.get("password", "convidado10")
            headless = form.get("headless", "false").lower() == "true"   # padrão mostrar

            if not event_inputs:
                msg = "<b>Informe pelo menos 1 evento (código CBY ou URL).</b>"
                self._ok(base_html("", message=msg)); return

            try:
                summaries = asyncio.run(
                    scrape(username=username, password=password, headless=headless, event_inputs=event_inputs)
                )
            except Exception as e:
                msg = f"<b>Erro:</b> {html.escape(str(e))}"
                self._ok(base_html("", message=msg)); return

            # Cabeçalho geral (tabela básica)
            header_html = table_basic_summary(summaries)

            # Blocos por evento: Resumo por Status
            blocks = []
            for s in summaries:
                titulo = html.escape(str(s.get("Evento") or "Evento"))
                blocks.append(
                    f"<div class='card'><h2>Resumo por Status — {titulo}</h2>{table_status_breakdown(s)}</div>"
                )

            body = (
                "<div class='card'><h1>Resumo do Evento</h1>" + header_html + "</div>"
                + "".join(blocks) +
                "<div class='card actions'>"
                "<a class='link btn' href='/'>Voltar</a>"
                "</div>"
            )
            self._ok(base_html(body)); return

        self.send_response(404); self.end_headers()

    # helpers
    def _ok(self, content: bytes):
        self.send_response(200)
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers(); self.wfile.write(content)

# --------------- testes / CLI ---------------
def run_tests() -> str:
    out = []
    assert _clean_text(" a  b\n c ") == "a b c"; out.append("clean_text OK")
    m = DATE_REGEX.findall("Leilão 21/08/2025 às 15:00")
    assert m and m[0][0]=="21/08/2025" and m[0][1]=="15:00"; out.append("date regex OK")
    assert build_event_url("200825CBY").endswith("/evento/detalhes/200825cby"); out.append("url builder OK")
    out.append("sanidade OK")
    return "\n".join(out)

def run_cli(argv: List[str]):
    if "--test" in argv:
        print(run_tests()); return
    port = 9000
    if "--port" in argv:
        try: port = int(argv[argv.index("--port")+1])
        except: pass
    httpd = HTTPServer(("127.0.0.1", port), AppHandler)
    print(f"Servidor iniciado em http://127.0.0.1:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()

if __name__ == "__main__":
    run_cli(sys.argv[1:])
