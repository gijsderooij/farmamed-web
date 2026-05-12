"""
Farmamed Recept Agent — FastAPI backend
"""

from __future__ import annotations
import os
import json
import base64
from pathlib import Path

from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.requests import Request
from fastapi.responses import JSONResponse, HTMLResponse
from dotenv import load_dotenv
import requests as http_requests

load_dotenv()

app = FastAPI(title="Farmamed Recept Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).parent
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-5"


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = BASE_DIR / "templates" / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/recept-preview")
async def recept_preview(bestand: UploadFile = File(...)):
    """
    Converteert eerste pagina van PDF naar JPEG voor weergave in browser.
    Geeft base64-gecodeerde afbeelding terug.
    """
    inhoud = await bestand.read()
    bestandsnaam = bestand.filename or ""

    if bestandsnaam.lower().endswith(".pdf"):
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(stream=inhoud, filetype="pdf")
            pagina = doc[0]
            mat = fitz.Matrix(1.5, 1.5)
            pix = pagina.get_pixmap(matrix=mat, alpha=False)
            img_bytes = pix.tobytes("jpeg")
            b64 = base64.standard_b64encode(img_bytes).decode()
            return JSONResponse(content={"preview": f"data:image/jpeg;base64,{b64}", "type": "image"})
        except Exception as e:
            return JSONResponse(content={"fout": str(e)})
    else:
        # Afbeelding direct teruggeven
        b64 = base64.standard_b64encode(inhoud).decode()
        mt = "image/png" if bestandsnaam.lower().endswith(".png") else "image/jpeg"
        return JSONResponse(content={"preview": f"data:{mt};base64,{b64}", "type": "image"})


@app.post("/api/test-upload")
async def test_upload(bestand: UploadFile = File(...)):
    """Test endpoint om te controleren of uploads werken."""
    inhoud = await bestand.read()
    return JSONResponse(content={
        "ok": True,
        "bestandsnaam": bestand.filename,
        "grootte_kb": round(len(inhoud) / 1024, 1),
        "type": bestand.content_type,
    })


@app.post("/api/analyseer-recept")
async def analyseer_recept(bestand: UploadFile = File(...)):
    if not ANTHROPIC_API_KEY:
        return JSONResponse(content={"fout": "API-sleutel niet geconfigureerd"})

    try:
        inhoud = await bestand.read()
    except Exception as e:
        return JSONResponse(content={"fout": f"Upload mislukt: {str(e)}"})

    if not inhoud:
        return JSONResponse(content={"fout": "Leeg bestand ontvangen"})

    bestandsnaam = bestand.filename or "recept"
    grootte_kb = len(inhoud) / 1024
    print(f"Upload ontvangen: {bestandsnaam} ({grootte_kb:.0f} KB)")

    if bestandsnaam.lower().endswith(".pdf"):
        media_type = "application/pdf"
    elif bestandsnaam.lower().endswith(".png"):
        media_type = "image/png"
    else:
        media_type = "image/jpeg"

    b64 = base64.standard_b64encode(inhoud).decode("utf-8")

    if media_type == "application/pdf":
        document_blok = {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": b64}
        }
    else:
        document_blok = {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64}
        }

    prompt = """Analyseer dit recept en extraheer de velden als JSON.
Geef ALLEEN JSON terug, geen uitleg of markdown.

BELANGRIJK voor adres en naam:
- Het patiëntblok staat meestal in deze volgorde: voor- en achternaam, straatnaam + huisnummer, postcode + woonplaats
- Lees het ADRESBLOK van de PATIËNT uit, NIET het adres van de apotheek of voorschrijver
- De "straat" is de straatnaam + huisnummer van de PATIËNT
- De "postcode_plaats" is de postcode + woonplaats van de PATIËNT
- NIET de naam van de voorschrijver of arts gebruiken als woonplaats (bijv. "van Coevorden" is een achternaam, geen stad)
- De voorschrijver staat apart vermeld als arts/huisarts/specialist, NIET als woonplaats van de patiënt

{
  "recept_datum": "DD-MM-YYYY of null",
  "medicijn": "volledige naam inclusief concentratie, bijv. Tretinoïne 0.02% crème",
  "hoeveelheid": "bijv. 30 gram of 3 tubes van 30 gram",
  "iter": "aantal herhalingen of null, bijv. 2x iter",
  "gebruiksaanwijzing": "volledige instructie na S: of Sig:",
  "patient_naam": "volledige voor- en achternaam van de PATIËNT",
  "geboortedatum": "DD-MM-YYYY of null",
  "bsn": "9-cijferig BSN-nummer van de patiënt of null",
  "straat": "straatnaam + huisnummer van de PATIËNT (niet van arts of apotheek)",
  "postcode_plaats": "postcode + woonplaats van de PATIËNT (niet van arts of apotheek)",
  "email": "emailadres van de patiënt of null",
  "telefoon": "telefoonnummer van de patiënt of null",
  "voorschrijver": "naam van de arts/voorschrijver (niet de patiënt)",
  "agb_code": "AGB-code van de arts of null",
  "big_nummer": "BIG-nummer van de arts of null",
  "geldig": true,
  "vertrouwen": 85
}"""

    try:
        response = http_requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 1000,
                "messages": [
                    {
                        "role": "user",
                        "content": [document_blok, {"type": "text", "text": prompt}]
                    }
                ],
            },
            timeout=60,
        )
        response.raise_for_status()
        tekst = response.json()["content"][0]["text"].strip()
        tekst = tekst.replace("```json", "").replace("```", "").strip()
        return JSONResponse(content=json.loads(tekst))
    except Exception as e:
        return JSONResponse(content={"fout": str(e)})


@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    berichten = body.get("berichten", [])
    recept_context = body.get("recept_context", {})

    if not ANTHROPIC_API_KEY:
        return JSONResponse(content={"antwoord": "API-sleutel niet geconfigureerd"})

    systeem = f"""Je bent een vriendelijke apotheekassistent van Farmamed Bereidingsapotheek.
Je helpt klanten hun recept te verwerken en een bestelling te plaatsen.

Receptgegevens:
{json.dumps(recept_context, ensure_ascii=False, indent=2)}

Richtlijnen:
- Spreek de klant aan bij naam als je die weet
- Bevestig welk medicijn je hebt gevonden
- Controleer of het recept geldig is
- Verwijs naar farmamed.nl voor de bestelling
- Antwoord altijd in het Nederlands
- Houd antwoorden kort (max 3-4 zinnen)"""

    try:
        response = http_requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 500,
                "system": systeem,
                "messages": berichten,
            },
            timeout=30,
        )
        response.raise_for_status()
        antwoord = response.json()["content"][0]["text"]
        return JSONResponse(content={"antwoord": antwoord})
    except Exception as e:
        return JSONResponse(content={"antwoord": f"Er is een fout opgetreden: {str(e)}"})


@app.post("/api/maak-order")
async def maak_order(request: Request):
    """Maak een WooCommerce bestelling aan op basis van de receptgegevens."""
    data = await request.json()

    wc_url = os.getenv("WC_URL", "")
    wc_key = os.getenv("WC_KEY", "")
    wc_secret = os.getenv("WC_SECRET", "")

    if not all([wc_url, wc_key, wc_secret]):
        return JSONResponse(content={"fout": "WooCommerce niet geconfigureerd"})

    # Zoek product op basis van medicijnnaam
    medicijn = data.get("medicijn", "")
    product_id = await _zoek_product_id(medicijn, wc_url, wc_key, wc_secret)

    naam_delen = (data.get("patient_naam") or "").split(" ", 1)
    voornaam = naam_delen[0] if naam_delen else ""
    achternaam = naam_delen[1] if len(naam_delen) > 1 else ""

    order_payload = {
        "status": "processing",
        "billing": {
            "first_name": voornaam,
            "last_name": achternaam,
            "email": data.get("email") or "onbekend@farmamed.nl",
            "phone": data.get("telefoon") or "",
        },
        "line_items": [
            {"product_id": product_id, "quantity": 1}
        ] if product_id else [],
        "meta_data": [
            {"key": "geboortedatum", "value": data.get("geboortedatum") or ""},
            {"key": "bsn", "value": data.get("bsn") or ""},
            {"key": "voorschrijver", "value": data.get("voorschrijver") or ""},
            {"key": "agb_code", "value": data.get("agb_code") or ""},
            {"key": "big_nummer", "value": data.get("big_nummer") or ""},
            {"key": "recept_datum", "value": data.get("recept_datum") or ""},
            {"key": "medicijn_ocr", "value": data.get("medicijn") or ""},
            {"key": "gebruiksaanwijzing", "value": data.get("gebruiksaanwijzing") or ""},
            {"key": "iter", "value": data.get("iter") or ""},
        ],
        "customer_note": f"Recept ingediend via webformulier. Medicijn: {medicijn}",
    }

    try:
        response = http_requests.post(
            f"{wc_url}/wp-json/wc/v3/orders",
            auth=(wc_key, wc_secret),
            json=order_payload,
            timeout=20,
        )
        response.raise_for_status()
        order = response.json()
        return JSONResponse(content={"order_id": order["id"], "status": order["status"]})
    except Exception as e:
        return JSONResponse(content={"fout": str(e)})


async def _zoek_product_id(medicijn_naam: str, wc_url: str, wc_key: str, wc_secret: str) -> int | None:
    """Zoek het WooCommerce product-ID op basis van de medicijnnaam."""
    if not medicijn_naam:
        return None
    try:
        response = http_requests.get(
            f"{wc_url}/wp-json/wc/v3/products",
            auth=(wc_key, wc_secret),
            params={"search": medicijn_naam[:30], "per_page": 5},
            timeout=10,
        )
        producten = response.json()
        if producten and isinstance(producten, list):
            return producten[0]["id"]
    except Exception:
        pass
    return None


# ------------------------------------------------------------------
# Orders pagina
# ------------------------------------------------------------------

@app.get("/orders", response_class=HTMLResponse)
async def orders_pagina():
    html_path = BASE_DIR / "templates" / "orders.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/api/orders")
async def haal_orders_op():
    """Haalt openstaande WooCommerce orders op."""
    wc_url = os.getenv("WC_URL", "")
    wc_key = os.getenv("WC_KEY", "")
    wc_secret = os.getenv("WC_SECRET", "")

    if not all([wc_url, wc_key, wc_secret]):
        return JSONResponse(content={"fout": "WooCommerce niet geconfigureerd"})

    try:
        response = http_requests.get(
            f"{wc_url}/wp-json/wc/v3/orders",
            auth=(wc_key, wc_secret),
            params={"status": "processing", "per_page": 20, "orderby": "date", "order": "desc"},
            headers={"Accept": "application/json"},
            timeout=15,
        )
        response.raise_for_status()
        orders_raw = response.json()

        orders = []
        for o in orders_raw:
            billing = o.get("billing", {})
            meta = {m["key"]: m["value"] for m in o.get("meta_data", [])}
            items = o.get("line_items", [])
            medicijn = items[0]["name"] if items else "Onbekend"
            naam = f"{billing.get('first_name','')} {billing.get('last_name','')}".strip()
            geboortedatum = meta.get("billing_birth") or meta.get("_billing_birth", "")

            # Recept URL via plugin
            recept_url = o.get("recept_url") or meta.get("recept_url", "")

            orders.append({
                "id": o["id"],
                "status": o.get("status", ""),
                "datum": o.get("date_created", "")[:10],
                "klant_naam": naam,
                "email": billing.get("email", ""),
                "telefoon": billing.get("phone", ""),
                "adres": f"{billing.get('address_1','')} {billing.get('postcode','')} {billing.get('city','')}".strip(),
                "geboortedatum": _amerikaans_naar_nederlands(geboortedatum),
                "medicijn": medicijn,
                "hoeveelheid": float(items[0].get("quantity", 1)) * 30 if items else 30,
                "aantal": int(items[0].get("quantity", 1)) if items else 1,
                "totaal": o.get("total", "0"),
                "heeft_recept": bool(recept_url),
                "recept_url": recept_url,
            })

        return JSONResponse(content={"orders": orders})

    except Exception as e:
        return JSONResponse(content={"fout": str(e)})


@app.post("/api/recept-preview-url")
async def recept_preview_url(request: Request):
    """Haalt recept op via URL en converteert naar afbeelding voor weergave."""
    body = await request.json()
    url = body.get("url", "")

    if not url:
        return JSONResponse(content={"fout": "Geen URL opgegeven"})

    try:
        response = http_requests.get(url, timeout=20)
        response.raise_for_status()
        inhoud = response.content

        if url.lower().endswith(".pdf") or response.headers.get("content-type", "").startswith("application/pdf"):
            import fitz
            doc = fitz.open(stream=inhoud, filetype="pdf")
            pagina = doc[0]
            mat = fitz.Matrix(1.5, 1.5)
            pix = pagina.get_pixmap(matrix=mat, alpha=False)
            img_bytes = pix.tobytes("jpeg")
            b64 = base64.standard_b64encode(img_bytes).decode()
            return JSONResponse(content={"preview": f"data:image/jpeg;base64,{b64}"})
        else:
            b64 = base64.standard_b64encode(inhoud).decode()
            ct = response.headers.get("content-type", "image/jpeg").split(";")[0]
            return JSONResponse(content={"preview": f"data:{ct};base64,{b64}"})

    except Exception as e:
        return JSONResponse(content={"fout": str(e)})


@app.post("/api/analyseer-order")
async def analyseer_order(request: Request):
    """Analyseert recept van een WooCommerce order en vergelijkt met besteldata."""
    body = await request.json()
    order_id = body.get("order_id")
    recept_url = body.get("recept_url", "")

    if not recept_url:
        return JSONResponse(content={"fout": "Geen recept-URL"})

    # Download recept
    try:
        resp = http_requests.get(recept_url, timeout=20)
        resp.raise_for_status()
        recept_bytes = resp.content
    except Exception as e:
        return JSONResponse(content={"fout": f"Kon recept niet downloaden: {str(e)}"})

    # Analyseer met Claude Vision
    b64 = base64.standard_b64encode(recept_bytes).decode()
    is_pdf = recept_url.lower().endswith(".pdf")
    document_blok = {
        "type": "document" if is_pdf else "image",
        "source": {
            "type": "base64",
            "media_type": "application/pdf" if is_pdf else "image/jpeg",
            "data": b64
        }
    }

    prompt = """Analyseer dit recept en extraheer de velden als JSON.
Geef ALLEEN JSON terug, geen uitleg of markdown.

BELANGRIJK: Lees het adresblok van de PATIËNT uit (niet van de arts).
Volgorde adresblok: naam → straat + huisnummer → postcode + woonplaats.
Verwar de achternaam van de arts NIET met een woonplaats.

{
  "recept_datum": "DD-MM-YYYY of null",
  "medicijn": "volledige naam inclusief concentratie",
  "hoeveelheid": "bijv. 30 gram",
  "iter": "herhalingen of null",
  "gebruiksaanwijzing": "instructie na S:",
  "patient_naam": "voor- en achternaam patiënt",
  "geboortedatum": "DD-MM-YYYY of null",
  "bsn": "BSN-nummer of null",
  "straat": "straat + huisnummer patiënt",
  "postcode_plaats": "postcode + woonplaats patiënt",
  "email": "email patiënt of null",
  "telefoon": "telefoon patiënt of null",
  "voorschrijver": "naam arts",
  "agb_code": "AGB-code of null",
  "big_nummer": "BIG-nummer of null",
  "geldig": true,
  "vertrouwen": 85
}"""

    try:
        api_resp = http_requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": [document_blok, {"type": "text", "text": prompt}]}],
            },
            timeout=60,
        )
        api_resp.raise_for_status()
        tekst = api_resp.json()["content"][0]["text"].strip().replace("```json","").replace("```","").strip()
        recept_data = json.loads(tekst)
    except Exception as e:
        return JSONResponse(content={"fout": f"OCR mislukt: {str(e)}"})

    # Haal WooCommerce order op voor vergelijking
    wc_url = os.getenv("WC_URL", "")
    wc_key = os.getenv("WC_KEY", "")
    wc_secret = os.getenv("WC_SECRET", "")
    wc_order = {}

    try:
        wc_resp = http_requests.get(
            f"{wc_url}/wp-json/wc/v3/orders/{order_id}",
            auth=(wc_key, wc_secret),
            timeout=15,
        )
        wc_order = wc_resp.json()
    except Exception:
        pass

    # Vergelijking
    vergelijking = _vergelijk_order_recept(wc_order, recept_data)

    return JSONResponse(content={"recept": recept_data, "vergelijking": vergelijking})


def _normaliseer_hoeveelheid(tekst: str) -> str:
    """
    Normaliseert hoeveelheden voor betere vergelijking.
    - g, G, gram, grammen → gram
    - mg, milligram → milligram
    - 1 stuk, 1 tube, 1x 30g → 30 gram
    - 2 stuks, 2 tubes → 60 gram
    - Verwijdert spaties tussen getal en eenheid
    """
    import re

    tekst = tekst.lower().strip()

    # Tubes/stuks omzetten naar gram (1 tube = 30 gram)
    tube_match = re.search(r'(\d+)\s*(?:tube[s]?|stuk[s]?|stuks?|x)', tekst)
    gram_match = re.search(r'(\d+)\s*(?:g|gram)', tekst)

    if tube_match and gram_match:
        # "3 tubes van 30 gram" → "90 gram"
        aantal = int(tube_match.group(1))
        gram = int(gram_match.group(1))
        return f"{aantal * gram} gram"
    elif tube_match and not gram_match:
        # "2 tubes" → "60 gram" (1 tube = 30 gram)
        aantal = int(tube_match.group(1))
        return f"{aantal * 30} gram"
    elif re.search(r'^\d+\s*(?:stuk[s]?|tube[s]?)$', tekst):
        aantal = int(re.search(r'(\d+)', tekst).group(1))
        return f"{aantal * 30} gram"

    # Normaliseer eenheden
    tekst = re.sub(r'(\d+)\s*(?:gram|grammen|gr|g)', r' gram', tekst)
    tekst = re.sub(r'(\d+)\s*(?:milligram|mg)', r' milligram', tekst)

    # Verwijder extra spaties
    tekst = re.sub(r'\s+', ' ', tekst).strip()

    return tekst


def _normaliseer_naam(naam: str) -> str:
    """
    Normaliseert patiëntnamen voor betere vergelijking.
    - Verwijdert tussenvoegsels (van, de, den, der, het, 't)
    - Verwijdert voorletters (J. of J.M.)
    - Verwijdert koppeltekens tussen dubbele namen
    - Zet om naar lowercase
    - Verwijdert mevrouw/dhr/de heer titels
    """
    import re

    naam = naam.lower().strip()

    # Verwijder titels
    naam = re.sub(r'\b(mw\.?|dhr\.?|de heer|mevrouw|drs\.?|dr\.?|mr\.?)\s*', '', naam)

    # Verwijder voorletters (bijv. "J." of "J.M.")
    naam = re.sub(r'\b[a-z]\.(?:[a-z]\.)*\s*', '', naam)

    # Vervang koppeltekens door spatie (meisjesnaam koppeling)
    naam = naam.replace('-', ' ')

    # Verwijder tussenvoegsels
    tussenvoegsels = r'\b(van|de|den|der|het|ten|ter|\'t|d\'|von|van der|van den|van de)\b'
    naam = re.sub(tussenvoegsels, '', naam)

    # Verwijder extra spaties
    naam = re.sub(r'\s+', ' ', naam).strip()

    return naam


def _normaliseer_medicijn(naam: str) -> str:
    """
    Verwijdert ruis uit medicijnnamen voor betere vergelijking.
    - Verwijdert: FNA, SAW creme, tegen huidveroudering, (saw-creme), in saw creme
    - Converteert mg/g naar % (bijv. 0.5 mg/g -> 0.05%)
    - Verwijdert extra spaties
    """
    import re

    naam = naam.lower().strip()

    # Verwijder bekende suffixen en toevoegingen
    te_verwijderen = [
        r'fna',
        r'\(saw[\s\-]?creme?\)',
        r'in\s+saw[\s\-]?creme?',
        r'saw[\s\-]?creme?',
        r'tegen\s+huidveroudering',
        r'tegen\s+acne',
        r'\(?\d+\s*gram\)?',      # (30 gram) weglaten
        r'crème',
        r'creme',
        r'zalf',
        r'gel',
        r'oplossing',
    ]
    for patroon in te_verwijderen:
        naam = re.sub(patroon, '', naam, flags=re.IGNORECASE)

    # Converteer mg/g naar % (0.5 mg/g = 0.05%)
    def mgpg_naar_procent(match):
        waarde = float(match.group(1).replace(',', '.'))
        procent = waarde / 10
        return f"{procent:g}%"

    naam = re.sub(r'(\d+[.,]?\d*)\s*mg/g', mgpg_naar_procent, naam, flags=re.IGNORECASE)

    # Normaliseer decimalen (0,02 en 0.02 zijn hetzelfde)
    naam = naam.replace(',', '.')

    # Verwijder extra spaties
    naam = re.sub(r'\s+', ' ', naam).strip()

    return naam


def _amerikaans_naar_nederlands(datum: str) -> str:
    """Converteert YYYY-MM-DD naar DD-MM-YYYY."""
    if not datum:
        return ""
    try:
        from datetime import datetime
        # Probeer YYYY-MM-DD formaat
        if len(datum) == 10 and datum[4] == '-':
            dt = datetime.strptime(datum, "%Y-%m-%d")
            return dt.strftime("%d-%m-%Y")
    except ValueError:
        pass
    return datum  # Geef origineel terug als conversie mislukt


def _vergelijk_order_recept(wc_order: dict, recept: dict) -> dict:
    """Vergelijkt WooCommerce besteldata met OCR-receptdata."""
    from rapidfuzz import fuzz

    billing = wc_order.get("billing", {})
    meta = {m["key"]: m["value"] for m in wc_order.get("meta_data", [])}
    items = wc_order.get("line_items", [])

    wc_naam = f"{billing.get('first_name','')} {billing.get('last_name','')}".strip()
    wc_medicijn = items[0]["name"] if items else ""
    wc_geboortedatum_raw = meta.get("billing_birth") or meta.get("_billing_birth", "")
    wc_geboortedatum = _amerikaans_naar_nederlands(wc_geboortedatum_raw)
    wc_aantal = int(items[0].get("quantity", 1)) if items else 1
    wc_hoeveelheid_gram = wc_aantal * 30
    wc_hoeveelheid = f"{wc_hoeveelheid_gram} gram ({wc_aantal}x 30g)" if wc_aantal > 1 else "30 gram"
    wc_order["_hoeveelheid"] = wc_hoeveelheid_gram

    velden = []
    aandachtspunten = []

    def vergelijk_veld(naam, wc_waarde, recept_waarde):
        wc_str = str(wc_waarde or "").strip().lower()
        rec_str = str(recept_waarde or "").strip().lower()
        if not wc_str or not rec_str:
            score = 40
        else:
            score = fuzz.token_sort_ratio(wc_str, rec_str)
        return {"veld": naam, "wc_waarde": wc_waarde or "—", "recept_waarde": recept_waarde or "—", "score": score}

    wc_naam_norm = _normaliseer_naam(wc_naam)
    recept_naam_norm = _normaliseer_naam(recept.get("patient_naam") or "")
    veld_naam = vergelijk_veld("Naam", wc_naam, recept.get("patient_naam"))
    from rapidfuzz import fuzz as _fuzz2
    veld_naam["score"] = _fuzz2.token_sort_ratio(wc_naam_norm, recept_naam_norm)
    velden.append(veld_naam)
    velden.append(vergelijk_veld("Geboortedatum", wc_geboortedatum, recept.get("geboortedatum")))
    wc_medicijn_norm = _normaliseer_medicijn(wc_medicijn)
    recept_medicijn_norm = _normaliseer_medicijn(recept.get("medicijn") or "")
    veld = vergelijk_veld("Medicijn", wc_medicijn, recept.get("medicijn"))
    # Herbereken score op genormaliseerde waarden
    from rapidfuzz import fuzz as _fuzz
    veld["score"] = _fuzz.token_sort_ratio(wc_medicijn_norm, recept_medicijn_norm)
    velden.append(veld)
    wc_hoev_norm = _normaliseer_hoeveelheid(wc_hoeveelheid)
    recept_hoev_norm = _normaliseer_hoeveelheid(recept.get("hoeveelheid") or "")
    veld_hoev = vergelijk_veld("Hoeveelheid", wc_hoeveelheid, recept.get("hoeveelheid"))
    from rapidfuzz import fuzz as _fuzz3
    veld_hoev["score"] = _fuzz3.token_sort_ratio(wc_hoev_norm, recept_hoev_norm)
    velden.append(veld_hoev)

    # Receptdatum geldigheid
    recept_datum = recept.get("recept_datum", "")
    geldig = recept.get("geldig", True)
    if not geldig:
        aandachtspunten.append("⛔ Recept mogelijk verlopen — ouder dan 1 jaar")
        velden.append({"veld": "Receptdatum", "wc_waarde": "Geldig", "recept_waarde": recept_datum, "score": 0})
    else:
        velden.append({"veld": "Receptdatum", "wc_waarde": "Geldig", "recept_waarde": recept_datum, "score": 100})

    scores = [v["score"] for v in velden]
    totaal = round(sum(scores) / len(scores)) if scores else 0

    if totaal < 60:
        aandachtspunten.append("⚠ Lage overeenkomst tussen bestelling en recept")

    return {"velden": velden, "totaal_score": totaal, "aandachtspunten": aandachtspunten}


@app.post("/api/order-status")
async def update_order_status(request: Request):
    """Werkt WooCommerce orderstatus bij na beslissing apotheker."""
    body = await request.json()
    order_id = body.get("order_id")
    status = body.get("status", "completed")

    wc_url = os.getenv("WC_URL", "")
    wc_key = os.getenv("WC_KEY", "")
    wc_secret = os.getenv("WC_SECRET", "")

    try:
        resp = http_requests.put(
            f"{wc_url}/wp-json/wc/v3/orders/{order_id}",
            auth=(wc_key, wc_secret),
            json={"status": status},
            timeout=15,
        )
        return JSONResponse(content={"ok": resp.status_code == 200})
    except Exception as e:
        return JSONResponse(content={"fout": str(e)})


# ------------------------------------------------------------------
# E-mail pagina en IMAP endpoints
# ------------------------------------------------------------------

@app.get("/emails", response_class=HTMLResponse)
async def emails_pagina():
    html_path = BASE_DIR / "templates" / "emails.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


def _imap_verbinding():
    """Maakt IMAP-verbinding met de geconfigureerde mailbox."""
    import imaplib
    imap_server = os.getenv("IMAP_SERVER", "")
    imap_port = int(os.getenv("IMAP_PORT", "993"))
    imap_user = os.getenv("IMAP_USER", "")
    imap_pass = os.getenv("IMAP_PASS", "")

    if not all([imap_server, imap_user, imap_pass]):
        raise ValueError("IMAP niet geconfigureerd — voeg IMAP_SERVER, IMAP_USER en IMAP_PASS toe")

    conn = imaplib.IMAP4_SSL(imap_server, imap_port)
    conn.login(imap_user, imap_pass)
    return conn


def _classificeer_email(onderwerp: str, body: str) -> str:
    """Classificeert het type e-mail op basis van onderwerp en inhoud."""
    tekst = (onderwerp + " " + body).lower()
    herhaal_termen = ["herhaalrecept", "herhaling", "iter", "verlenging", "opnieuw", "nogmaals", "herhaal"]
    recept_termen = ["recept", "voorschrift", "medicijn", "medicatie", "bijlage", "zie bijlage"]

    if any(t in tekst for t in herhaal_termen):
        return "herhaalrecept"
    elif any(t in tekst for t in recept_termen):
        return "nieuw_recept"
    return "overig"


# In-memory opslag van ontvangen e-mails (wordt gevuld door lokale poller)
_email_cache: list[dict] = []


@app.post("/api/email-inkomend")
async def email_inkomend(request: Request):
    """Ontvangt een e-mail van de lokale poller en slaat hem op."""
    global _email_cache
    data = await request.json()
    # Voeg toe als nog niet aanwezig (op basis van uid)
    uids = {e["uid"] for e in _email_cache}
    if data.get("uid") not in uids:
        _email_cache.insert(0, data)
        _email_cache = _email_cache[:50]  # max 50 bewaren
    return JSONResponse(content={"ok": True, "totaal": len(_email_cache)})


@app.get("/api/emails")
async def haal_emails_op():
    """Geeft opgeslagen e-mails terug (gevuld door lokale poller)."""
    if not _email_cache:
        return JSONResponse(content={"emails": [], "info": "Nog geen e-mails ontvangen — start de lokale email_poller.py"})
    # Geef emails terug zonder bijlage-data (die is groot)
    emails_zonder_data = []
    for e in _email_cache:
        email_slim = {k: v for k, v in e.items() if k != "bijlagen"}
        email_slim["bijlagen"] = [{"naam": b["naam"], "type": b["type"]} for b in e.get("bijlagen", [])]
        emails_zonder_data.append(email_slim)
    return JSONResponse(content={"emails": emails_zonder_data})

    try:
        conn.select("INBOX")
        # Haal alle e-mails op (ongelezen + gelezen, max 30 nieuwste)
        _, berichten = conn.search(None, "ALL")
        uids = berichten[0].split()
        uids = uids[-30:]  # laatste 30

        emails = []
        for uid in reversed(uids):
            try:
                _, data = conn.fetch(uid, "(RFC822)")
                msg = email_lib.message_from_bytes(data[0][1])

                # Onderwerp decoderen
                onderwerp_raw = msg.get("Subject", "")
                onderwerp_parts = decode_header(onderwerp_raw)
                onderwerp = ""
                for part, enc in onderwerp_parts:
                    if isinstance(part, bytes):
                        onderwerp += part.decode(enc or "utf-8", errors="replace")
                    else:
                        onderwerp += str(part)

                # Afzender
                afzender = msg.get("From", "")
                afzender_naam = ""
                if "<" in afzender:
                    afzender_naam = afzender.split("<")[0].strip().strip('"')
                    afzender_email = afzender.split("<")[1].rstrip(">")
                else:
                    afzender_email = afzender

                # Datum
                datum = msg.get("Date", "")[:25]

                # Body uitlezen
                body = ""
                bijlagen = []
                for part in msg.walk():
                    ct = part.get_content_type()
                    cd = str(part.get("Content-Disposition", ""))

                    if ct == "text/plain" and "attachment" not in cd:
                        try:
                            body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                        except Exception:
                            body = ""
                    elif "attachment" in cd or ct in ("application/pdf", "image/jpeg", "image/png"):
                        naam = part.get_filename() or f"bijlage_{len(bijlagen)+1}"
                        bijlagen.append({"naam": naam, "type": ct})

                # Ongelezen check
                _, flags_data = conn.fetch(uid, "(FLAGS)")
                ongelezen = b"\\Seen" not in flags_data[0]

                emails.append({
                    "uid": uid.decode(),
                    "onderwerp": onderwerp,
                    "afzender": afzender_email,
                    "afzender_naam": afzender_naam,
                    "datum": datum,
                    "body": body[:500],  # eerste 500 tekens
                    "bijlagen": bijlagen,
                    "heeft_bijlage": len(bijlagen) > 0,
                    "ongelezen": ongelezen,
                    "type": _classificeer_email(onderwerp, body),
                })
            except Exception:
                continue

        conn.logout()
        return JSONResponse(content={"emails": emails})

    except Exception as e:
        try:
            conn.logout()
        except Exception:
            pass
        return JSONResponse(content={"fout": str(e)})


@app.post("/api/open-email")
async def open_email(request: Request):
    """Haalt volledige e-mailinhoud op uit de cache."""
    body_req = await request.json()
    email_uid = body_req.get("email_uid", "")

    email = next((e for e in _email_cache if e["uid"] == email_uid), None)
    if not email:
        return JSONResponse(content={"fout": "E-mail niet gevonden in cache"})

    return JSONResponse(content={
        "body": email.get("body", ""),
        "bijlagen": [{"naam": b["naam"], "type": b["type"]} for b in email.get("bijlagen", [])],
        "type": email.get("type", "overig"),
    })


@app.post("/api/lees-bijlage")
async def lees_bijlage(request: Request):
    """Haalt bijlage op uit e-mailcache, genereert preview en leest recept uit."""
    body = await request.json()
    email_uid = body.get("email_uid", "")
    bijlage_index = body.get("bijlage_index", 0)

    email = next((e for e in _email_cache if e["uid"] == email_uid), None)
    if not email:
        return JSONResponse(content={"fout": "E-mail niet gevonden in cache"})

    bijlagen = email.get("bijlagen", [])
    if bijlage_index >= len(bijlagen):
        return JSONResponse(content={"fout": "Bijlage niet gevonden"})

    bijlage = bijlagen[bijlage_index]
    try:
        inhoud = base64.b64decode(bijlage["data"])
    except Exception:
        return JSONResponse(content={"fout": "Bijlagedata onleesbaar"})

    ct = bijlage.get("type", "application/pdf")
    naam = bijlage.get("naam", "recept")

    # Preview genereren
    preview = None
    if ct == "application/pdf" or naam.lower().endswith(".pdf"):
        try:
            import fitz
            doc = fitz.open(stream=inhoud, filetype="pdf")
            pix = doc[0].get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
            preview = f"data:image/jpeg;base64,{base64.standard_b64encode(pix.tobytes('jpeg')).decode()}"
        except Exception:
            pass
    else:
        preview = f"data:{ct};base64,{base64.standard_b64encode(inhoud).decode()}"

    # OCR via Claude
    is_pdf = ct == "application/pdf" or naam.lower().endswith(".pdf")
    b64 = base64.standard_b64encode(inhoud).decode()
    document_blok = {
        "type": "document" if is_pdf else "image",
        "source": {
            "type": "base64",
            "media_type": "application/pdf" if is_pdf else ct,
            "data": b64
        }
    }

    prompt = """Analyseer dit recept en extraheer de velden als JSON.
Geef ALLEEN JSON terug, geen uitleg of markdown.

BELANGRIJK: Lees het adresblok van de PATIËNT (niet van de arts).
Volgorde: naam → straat + huisnummer → postcode + woonplaats.

{
  "recept_datum": "DD-MM-YYYY of null",
  "medicijn": "volledige naam inclusief concentratie",
  "hoeveelheid": "bijv. 30 gram",
  "iter": "herhalingen of null",
  "gebruiksaanwijzing": "instructie na S:",
  "patient_naam": "voor- en achternaam patiënt",
  "geboortedatum": "DD-MM-YYYY of null",
  "bsn": "BSN-nummer of null",
  "straat": "straat + huisnummer patiënt",
  "postcode_plaats": "postcode + woonplaats patiënt",
  "email": "email patiënt of null",
  "telefoon": "telefoon patiënt of null",
  "voorschrijver": "naam arts",
  "agb_code": "AGB-code of null",
  "big_nummer": "BIG-nummer of null",
  "geldig": true,
  "vertrouwen": 85
}"""

    try:
        api_resp = http_requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": [document_blok, {"type": "text", "text": prompt}]}],
            },
            timeout=60,
        )
        api_resp.raise_for_status()
        tekst = api_resp.json()["content"][0]["text"].strip().replace("```json", "").replace("```", "").strip()
        recept_data = json.loads(tekst)
        return JSONResponse(content={"preview": preview, "recept": recept_data})
    except Exception as e:
        return JSONResponse(content={"preview": preview, "fout": str(e)})
