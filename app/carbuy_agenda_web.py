# Carbuy Coletor — HTTPServer + Playwright (paginação por URL estável + Excel + WhatsApp)
# Fluxo: informar até 3 eventos (código CBY ou URL) -> login automático -> coleta
# -> Resumo do evento + Resumo por Status + Tabela de Lotes + Exportar Excel + WhatsApp

import asyncio
import os
import re
import sys
import time
import html
import traceback
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse, parse_qsl, urlunparse, quote
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
LOGO_FILE = os.path.join(ASSETS_DIR, "logo_carbuytransparent.png")

# ---- export (memória)
EXPORT_BYTES: Optional[bytes] = None
EXPORT_NAME: str = "carbuy_export.xlsx"

DATE_REGEX = re.compile(
    r"\b(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})(?:\s*(?:às|as|,|\-|)\s*(\d{1,2}:\d{2}))?\b",
    flags=re.IGNORECASE,
)

# ---- NOVO: Regex e helper para capturar ano no card (ex.: '2019/2020' ou '2020')
YEAR_PAIR_RE   = re.compile(r'(?:19|20)\d{2}\s*/\s*(?:19|20)\d{2}')
YEAR_SINGLE_RE = re.compile(r'(?:19|20)\d{2}')

def _extract_year_text_from_card(soup) -> Optional[str]:
    """
    Tenta achar '2019/2020' ou um único ano dentro da área do título do card;
    se não achar, cai pro conteúdo inteiro do card.
    """
    for sel in (".card-title h2", ".card-title", "h2"):
        el = soup.select_one(sel)
        if el:
            t = _clean_text(el.get_text(" "))
            m2 = YEAR_PAIR_RE.search(t)
            if m2:
                return re.sub(r'\s+', '', m2.group(0))  # '2019 / 2020' -> '2019/2020'
            m1 = YEAR_SINGLE_RE.search(t)
            if m1:
                return m1.group(0)
    # fallback varrendo o card todo
    t_all = _clean_text(soup.get_text(" "))
    m2 = YEAR_PAIR_RE.search(t_all)
    if m2:
        return re.sub(r'\s+', '', m2.group(0))
    m1 = YEAR_SINGLE_RE.search(t_all)
    return m1.group(0) if m1 else None

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
    lot_url: Optional[str]
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

def _update_query(url: str, key: str, value: str) -> str:
    parts = list(urlparse(url))
    q = dict(parse_qsl(parts[4]))
    q[key] = value
    parts[4] = urlencode(q, doseq=True)
    return urlunparse(parts)

# ---------- helpers de navegação "suave" ----------
CARD_SELECTORS = ".card-anuncio, .card.text-center.h-100, div[class*='card-anuncio']"

async def wait_list_ready(page, timeout_ms: int = 8000):
    """Espera suave pela lista de cards sem depender de 'networkidle'."""
    try:
        await page.wait_for_selector(CARD_SELECTORS, timeout=timeout_ms)
    except Exception:
        try:
            await page.wait_for_timeout(600)
        except Exception:
            pass

async def safe_goto(page, url: str, timeout_nav: int = 6000) -> bool:
    """Navega por URL com tolerância a fechamento/contexto."""
    try:
        await page.goto(url, wait_until="domcontentloaded")
        await wait_list_ready(page, timeout_ms=timeout_nav)
        return True
    except Exception:
        return False

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
    """Garante login sem esperar networkidle e sem navegar para o evento (evita abrir duas vezes)."""
    page = await context.new_page()
    try:
        # Sempre começa pela home
        ok = await safe_goto(page, BASE_URL, timeout_nav=7000)
        if not ok:
            raise RuntimeError("Navegador fechado durante o login (safe_goto falhou no alvo inicial).")

        if await is_logged(page):
            return

        # força /conta/entrar
        await safe_goto(page, f"{BASE_URL}/conta/entrar", timeout_nav=7000)

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

    finally:
        try:
            await page.close()
        except Exception:
            pass


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

# ---------- itens da página corrente + link do lote ----------
async def collect_items_current_page(page, event_url: str) -> List[ItemRow]:
    rows: List[ItemRow] = []

    container = page.locator("#placeholder, #ev-list, .ev-list, .container-fluid.ev-list").first
    if await container.count() == 0:
        container = page.locator("body")

    cards = container.locator(CARD_SELECTORS)
    count = await cards.count()
    for i in range(count):
        card = cards.nth(i)
        html_card = await card.inner_html()
        soup = BeautifulSoup(html_card, "lxml") if "lxml" in sys.modules else BeautifulSoup(html_card, "html.parser")

        # Modelo
        model = None
        for sel in (".card-title h1", "h1.card-title", "h1", ".card-title h2", "h2.card-title", "h2"):
            el = soup.select_one(sel)
            if el:
                model = _clean_text(el.get_text(" "))
                if model:
                    break

        # NOVO: Ano e anexar ao modelo
        year_text = _extract_year_text_from_card(soup)
        if model and year_text and year_text not in model:
            model = f"{model} ({year_text})"

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

        # Valor Atual
        current_value = None
        for sel in (".card-valoratual h2", "h2.valor-atual", ".valor-atual", "span.valor-atual"):
            el = soup.select_one(sel)
            if el:
                val = _clean_text(el.get_text(" "))
                if "R$" in val or re.search(r"\d[\d\.\,]*", val):
                    current_value = val
                    break

        # Link do lote
        lot_url = None
        try:
            anchor = card.locator("a[href*='/evento/anuncio/'], a[href*='/anuncio/'], a[href*='/lote/'], a.card, a").first
            if await anchor.count() > 0:
                href = await anchor.get_attribute("href")
                if href:
                    lot_url = href if href.startswith("http") else (BASE_URL + href if href.startswith("/") else BASE_URL + "/" + href)
        except Exception:
            for a in soup.select("a[href]"):
                href = a.get("href") or ""
                if "/evento/anuncio/" in href or "/anuncio/" in href or "/lote/" in href:
                    lot_url = href if href.startswith("http") else (BASE_URL + href if href.startswith("/") else BASE_URL + "/" + href)
                    break

        if any([model, status_text, current_value, lot_url]):
            rows.append(ItemRow(event_url, lot_url, model, status_text, current_value))

    return rows

# ---------- paginação: SOMENTE por URL (sem cliques) ----------
def _fallback_next_url(url: str) -> Optional[str]:
    # incrementa pageNumber/page/pagina/lotePage preservando outros parâmetros (ex.: handler=pesquisar)
    for key in ("pageNumber", "page", "pagina", "lotePage"):
        parts = list(urlparse(url))
        q = dict(parse_qsl(parts[4]))
        if key in q:
            cur = re.sub(r"\D", "", q[key] or "")
            nxt = str(int(cur) + 1) if cur.isdigit() else "2"
            q[key] = nxt
            parts[4] = urlencode(q, doseq=True)
            return urlunparse(parts)
    # se não tinha nada, cria pageNumber=2
    parts = list(urlparse(url))
    q = dict(parse_qsl(parts[4]))
    q["pageNumber"] = "2"
    parts[4] = urlencode(q, doseq=True)
    return urlunparse(parts)

async def collect_items_with_pagination(page, event_url: str, max_pages: int = 200) -> List[ItemRow]:
    all_rows: List[ItemRow] = []
    seen_urls = set()
    prev_total = -1
    no_progress = 0

    while True:
        if page.is_closed():
            break

        cur_url = page.url
        seen_urls.add(cur_url)

        # coleta da página atual
        try:
            page_rows = await collect_items_current_page(page, event_url)
        except Exception:
            page_rows = []
        if page_rows:
            all_rows.extend(page_rows)

        # progresso?
        if len(all_rows) == prev_total:
            no_progress += 1
        else:
            no_progress = 0
            prev_total = len(all_rows)

        # paradas seguras
        if no_progress >= 2:
            break
        if len(seen_urls) >= max_pages:
            break

        # SEM CLIQUES: força URL da próxima página
        next_url = _fallback_next_url(cur_url)
        if not next_url or next_url in seen_urls:
            break

        ok = await safe_goto(page, next_url, timeout_nav=7000)
        if not ok:
            break

    # dedup
    uniq: List[ItemRow] = []
    seen = set()
    for r in all_rows:
        key = (r.event_url, r.lot_url, r.model, r.status_text, r.current_value)
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
        "Total_Anúncios": len(ev_items),
        "Status_Count": status_count,
        "Status_Sum": status_sum,
        "Event_URL": header.event_url,
    }

# -------- Excel / WhatsApp helpers --------
def _items_to_dataframe(items: Optional[List[ItemRow]]) -> pd.DataFrame:
    items = items or []
    rows = []
    for it in items:
        rows.append({
            "Evento_URL": it.event_url,
            "Modelo": it.model or "",
            "Status": it.status_text or "",
            "Valor_Atual": it.current_value or "",
            "URL_Anuncio": it.lot_url or "",
        })
    return pd.DataFrame(rows)

def _make_excel_bytes(df: pd.DataFrame, summaries: Optional[List[Dict[str, Any]]]) -> bytes:
    """Tenta openpyxl → xlsxwriter → engine automático, sem derrubar o servidor."""
    summaries = summaries or []
    from io import BytesIO
    bio = BytesIO()

    def _write_with_engine(engine_name: Optional[str]):
        with pd.ExcelWriter(bio, engine=engine_name) as writer:
            df.to_excel(writer, index=False, sheet_name="Lotes")
            for i, s in enumerate(summaries, start=1):
                s = s or {}
                sheet = f"Resumo_{i}"
                res_df = pd.DataFrame([
                    {"Status": st, "Quantidade": qtd, "Soma": fmt_brl(float(s.get("Status_Sum", {}).get(st, 0.0)))}
                    for st, qtd in sorted((s.get("Status_Count") or {}).items(), key=lambda kv: (-kv[1], kv[0].lower()))
                ])
                if res_df.empty:
                    res_df = pd.DataFrame([{"Status": "", "Quantidade": 0, "Soma": "R$ 0,00"}])
                res_df.to_excel(writer, index=False, sheet_name=sheet)

    # tenta openpyxl
    try:
        _write_with_engine("openpyxl")
        return bio.getvalue()
    except ModuleNotFoundError:
        pass

    # fallback: xlsxwriter
    try:
        _write_with_engine("xlsxwriter")
        return bio.getvalue()
    except ModuleNotFoundError:
        pass

    # último recurso: engine automático (se houver algum disponível)
    _write_with_engine(None)
    return bio.getvalue()

def build_whatsapp_text(summaries: Optional[List[Dict[str, Any]]]) -> str:
    summaries = summaries or []
    parts = []
    for s in summaries:
        s = s or {}
        code = s.get("Evento") or ""
        parts.append(f"Resumo por Status — {code}")
        parts.append("Status\tQuantidade\tSoma")
        status_count = s.get("Status_Count", {}) or {}
        status_sum = s.get("Status_Sum", {}) or {}
        for st, qtd in sorted(status_count.items(), key=lambda kv: (-kv[1], kv[0].lower())):
            soma = fmt_brl(float(status_sum.get(st, 0.0)))
            parts.append(f"{st}\t{qtd}\t{soma}")
        parts.append("")
    text = "\n".join(parts).strip()
    return f"https://wa.me/?text={quote(text)}"

# --------------- fluxo principal ---------------
async def _auto_close_if_popup(p):
    """Fecha páginas abertas via window.open/target=_blank (tem 'opener')."""
    try:
        op = None
        try:
            op = await p.opener()
        except Exception:
            try:
                op = p.opener
            except Exception:
                op = None
        if op:
            await p.close()
    except Exception:
        pass

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

        # Fecha popups automaticamente (apenas se tiver 'opener')
        context.on("page", lambda page: asyncio.create_task(_auto_close_if_popup(page)))

        # Faz login sem abrir o evento (evita abrir duas vezes)
        await ensure_login(context, username, password, return_after=None)

        # coleta por evento
        for u in urls:
            if len(context.pages) > 20:  # hard cap defensivo
                break
            ev = await context.new_page()
            try:
                ok = await safe_goto(ev, u, timeout_nav=8000)
                if not ok:
                    break
                header = await parse_event_header(ev)
                headers.append(header)

                page_items = await collect_items_with_pagination(ev, header.event_url)
                items.extend(page_items or [])
            except Exception:
                # se navegador/ctx fechar, aborta loop
                break
            finally:
                try:
                    await ev.close()
                except Exception:
                    pass

        # fecha tudo antes do browser
        for p_ in context.pages:
            try:
                await p_.close()
            except Exception:
                pass
        try:
            await context.close()
        except Exception:
            pass
        try:
            await browser.close()
        except Exception:
            pass

    # resumo por evento (dinâmico)
    summaries = [summarize_dynamic(h, items) for h in headers]
    return headers, summaries, items

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
    :root {{ 
      --red:#ff1a1a; 
      --dark:#fff;
      --card:#f9f9f9;
      --border:#ddd;
      --text:#000;
      --muted:#555;
      --btn:var(--red);
    }}
    * {{ box-sizing:border-box }}
    body {{ margin:0; background:var(--dark); color:var(--text); font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial }}
    .hero {{ background:var(--red); padding:32px 16px; display:flex; justify-content:center }}
    .hero img {{ height:110px; width:auto; display:block }}
    .wrap {{ max-width:980px; margin:0 auto; padding:24px 16px }}
    .card {{ background:var(--card); border:1px solid var(--border); border-radius:14px; padding:20px; box-shadow:0 4px 12px rgba(0,0,0,.1); margin-bottom:16px }}
    h1 {{ margin:0 0 8px 0; font-size:24px }}
    h2 {{ margin:0 0 8px 0; font-size:20px }}
    p.muted {{ color:var(--muted); margin:0 0 16px 0 }}
    label {{ font-size:12px; color:var(--muted); display:block; margin-bottom:6px }}
    input,select {{ width:100%; padding:12px 14px; border-radius:10px; border:1px solid var(--border); background:#fff; color:#000; outline:none }}
    .row {{ display:grid; grid-template-columns:1fr 1fr; gap:14px }}
    .row-1 {{ display:grid; grid-template-columns:1fr; gap:14px }}
    .btn {{ width:100%; padding:12px 16px; border-radius:12px; background:var(--btn); color:#fff; border:none; cursor:pointer; font-weight:700; letter-spacing:.3px; text-align:center }}
    .btn:active {{ transform:translateY(1px) }}
    .footer {{ text-align:center; color:#fff; background:var(--red); padding:10px 12px; font-size:12px; margin-top:24px }}
    table.grid {{ width:100%; border-collapse:collapse; margin-top:8px }}
    table.grid th,table.grid td {{ border:1px solid var(--border); padding:6px 8px; text-align:left }}
    table.grid th {{ background:#f0f0f0 }}
    .msg {{ margin:12px 0; padding:10px 12px; background:#f8f8f8; border:1px solid var(--border); border-radius:10px; color:#000 }}
    .actions {{ display:flex; gap:10px; margin-top:12px; flex-wrap:wrap }}
    .link {{ color:#fff; text-decoration:none }}
    .pill {{ display:inline-block; background:#eee; border:1px solid var(--border); border-radius:999px; padding:3px 10px; margin-left:8px; font-size:12px; color:#000 }}
  </style>
</head>
<body>
  <div class="hero"><img src="/static/logo" alt="Carbuy Logo"/></div>
  <div class="wrap">{('<div class="msg">'+message+'</div>' if message else '')}{body}</div>
  <div class="footer">Desenvolvido por Bruno Nascimento – MKT Team</div>
</body></html>"""
    return page.encode("utf-8")

def table_basic_summary(summaries: Optional[List[Dict[str, Any]]]) -> str:
    summaries = summaries or []
    if not summaries:
        return "<p class='muted'>(vazio)</p>"
    rows = []
    rows.append("<table class='grid'><thead><tr>"
                "<th>Evento</th><th>Data/Horário</th><th>Status_Evento</th><th>Total_Anúncios</th>"
                "</tr></thead><tbody>")
    for s in summaries:
        s = s or {}
        rows.append("<tr>"
                    f"<td>{html.escape(str(s.get('Evento') or ''))}</td>"
                    f"<td>{html.escape(str(s.get('Data/Horário') or ''))}</td>"
                    f"<td>{html.escape(str(s.get('Status_Evento') or ''))}</td>"
                    f"<td>{int(s.get('Total_Anúncios') or s.get('Total_Anuncios') or 0)}</td>"
                    "</tr>")
    rows.append("</tbody></table>")
    return "".join(rows)

def table_status_breakdown(summary: Dict[str, Any]) -> str:
    """Tabela dinâmica por status para 1 evento."""
    summary = summary or {}
    status_count: Dict[str, int] = summary.get("Status_Count", {}) or {}
    status_sum: Dict[str, float] = summary.get("Status_Sum", {}) or {}

    if not status_count:
        return "<p class='muted'>(sem anúncios)</p>"

    rows = []
    rows.append("<table class='grid'><thead><tr><th>Status</th><th>Quantidade</th><th>Soma</th></tr></thead><tbody>")
    for st, qtd in sorted(status_count.items(), key=lambda kv: (-kv[1], kv[0].lower())):
        soma = fmt_brl(float(status_sum.get(st, 0.0)))
        rows.append(f"<tr><td>{html.escape(st)}</td><td>{qtd}</td><td>{soma}</td></tr>")
    rows.append("</tbody></table>")
    total_val = fmt_brl(sum(status_sum.values()))
    rows.append(f"<div class='pill'>Soma total: {total_val}</div>")
    return "".join(rows)

def table_items(event_url: str, items: Optional[List[ItemRow]]) -> str:
    items = items or []
    ev_items = [r for r in items if r.event_url == event_url]
    if not ev_items:
        return "<p class='muted'>(sem lotes)</p>"
    rows = ["<table class='grid'><thead><tr><th>Modelo</th><th>Status</th><th>Valor Atual</th></tr></thead><tbody>"]
    for it in ev_items:
        rows.append("<tr>"
                    f"<td>{html.escape(it.model or '')}</td>"
                    f"<td>{html.escape(it.status_text or '')}</td>"
                    f"<td>{html.escape(it.current_value or '')}</td>"
                    "</tr>")
    rows.append("</tbody></table>")
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

        # Download do Excel
        if self.path.startswith("/export.xlsx"):
            global EXPORT_BYTES, EXPORT_NAME
            if not EXPORT_BYTES:
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type","application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", f'attachment; filename="{EXPORT_NAME}"')
            self.send_header("Content-Length", str(len(EXPORT_BYTES)))
            self.end_headers(); self.wfile.write(EXPORT_BYTES); return

        # UI
        if self.path == "/" or self.path.startswith("/index"):
            body = """
            <div class='card'>
              <h1>Carbuy Coletor</h1>
              <p class='muted'>
                Informe <b>até 3 eventos</b>. No primeiro campo, coloque o <b>código CBY</b> (ex.: <code>200825CBY</code>) ou a <b>URL</b> completa.<br/>
                O relatório mostra um <b>Resumo do Evento</b>, o <b>Resumo por Status</b> e a <b>Tabela de Lotes</b>.
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

            # leitura robusta do form (evita NoneType)
            form_raw = parse_qs(data) or {}
            form: Dict[str, str] = {}
            for k, v in form_raw.items():
                if isinstance(v, list) and len(v) > 0:
                    form[k] = v[0] if v[0] is not None else ""
                else:
                    form[k] = ""

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
                headers, summaries, items = asyncio.run(
                    scrape(username=username, password=password, headless=headless, event_inputs=event_inputs)
                )
            except Exception as e:
                tb = traceback.format_exc()
                msg = f"<b>Erro:</b> {html.escape(str(e))}<br/><pre style='white-space:pre-wrap'>{html.escape(tb)}</pre>"
                self._ok(base_html("", message=msg)); return

            # normalizações à prova de None
            headers = headers or []
            summaries = summaries or []
            items = items or []

            # Excel export (tolerante)
            df = _items_to_dataframe(items)
            global EXPORT_BYTES, EXPORT_NAME
            export_ok = True
            export_err = ""
            try:
                EXPORT_BYTES = _make_excel_bytes(df, summaries or [])
                export_code = (summaries[0].get("Evento") if summaries else "carbuy")
                EXPORT_NAME = f"carbuy_{export_code}.xlsx"
            except Exception as ex:
                EXPORT_BYTES = None
                export_ok = False
                export_err = str(ex)

            # Cabeçalho geral (tabela básica)
            header_html = table_basic_summary(summaries)

            # Blocos por evento: Resumo por Status + Tabela de Lotes
            blocks = []
            if headers and summaries:
                for i in range(min(len(headers), len(summaries))):
                    h = headers[i]
                    s = summaries[i] or {}
                    titulo = html.escape(str(s.get("Evento") or "Evento"))
                    blocks.append(f"<div class='card'><h2>Resumo por Status — {titulo}</h2>{table_status_breakdown(s)}</div>")
                    blocks.append(f"<div class='card'><h2>Lotes — {titulo}</h2>{table_items(h.event_url, items)}</div>")
            else:
                blocks.append("<div class='card'><h2>Resumo por Status</h2><p class='muted'>(sem dados)</p></div>")

            wa_link = build_whatsapp_text(summaries or [])

            actions_html = (
                "<div class='card actions'>"
                + (f"<a class='link btn' href='/export.xlsx'>Exportar para Excel</a>" if export_ok
                   else "<div class='btn' style='opacity:.6;pointer-events:none' title='Instale openpyxl ou XlsxWriter para habilitar exportação'>Exportar para Excel (indisponível)</div>")
                + f"<a class='link btn' href='{wa_link}' target='_blank' rel='noopener'>Enviar no WhatsApp</a>"
                + "<a class='link btn' href='/'>Voltar</a>"
                + "</div>"
                + (f"<div class='msg'><b>Aviso:</b> Exportação desabilitada: {html.escape(export_err)}</div>" if not export_ok else "")
            )

            body = "<div class='card'><h1>Resumo do Evento</h1>" + header_html + "</div>" + "".join(blocks) + actions_html
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
    # testes básicos do ano
    assert _extract_year_text_from_card(BeautifulSoup("<h2>2019 / 2020</h2>", "html.parser")) == "2019/2020"; out.append("year pair OK")
    assert _extract_year_text_from_card(BeautifulSoup("<h2>2020</h2>", "html.parser")) == "2020"; out.append("year single OK")
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
