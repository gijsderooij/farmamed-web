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
    wc_key = os.getenv("WC_KEY", "")
    wc_secret = os.getenv("WC_SECRET", "")
    return {
        "status": "ok",
        "wc_key_prefix": wc_key[:8] + "..." if wc_key else "LEEG",
        "wc_secret_prefix": wc_secret[:8] + "..." if wc_secret else "LEEG",
        "wc_url": os.getenv("WC_URL", "LEEG"),
    }


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

    # Adres splitsen
    straat = data.get("straat") or ""
    postcode_plaats = data.get("postcode_plaats") or ""
    # Splits "1234AB Amsterdam" in postcode en stad
    import re as _re
    pc_match = _re.match(r"(\d{4}\s*[A-Za-z]{2})\s*(.*)", postcode_plaats.strip())
    postcode = pc_match.group(1).strip() if pc_match else ""
    stad = pc_match.group(2).strip() if pc_match else postcode_plaats

    # Bron bepalen
    bron = data.get("bron", "balie")
    if bron == "email":
        oorsprong = "E-Mail"
    elif bron == "balie":
        oorsprong = "Balie"
    else:
        oorsprong = "Balie"

    billing = {
        "first_name": voornaam,
        "last_name": achternaam,
        "address_1": straat,
        "city": stad,
        "postcode": postcode,
        "country": "NL",
        "email": data.get("email") or "onbekend@farmamed.nl",
        "phone": data.get("telefoon") or "",
    }

    # Aantal tubes bepalen uit hoeveelheid (bijv. "60 gram" = 2 tubes van 30g)
    import re as _re2
    hoeveelheid_str = str(data.get("hoeveelheid") or "30")
    gram_match = _re2.search(r"[\d.]+", hoeveelheid_str.replace(",", "."))
    gram_totaal = float(gram_match.group(0)) if gram_match else 30.0
    aantal_tubes = max(1, round(gram_totaal / 30))

    # Line items: medicijn + WMG tarief
    line_items = []
    if product_id:
        line_items.append({"product_id": product_id, "quantity": aantal_tubes})
    line_items.append({"product_id": 1139, "quantity": 1})  # WMG tarief

    order_payload = {
        "status": "pending",
        "billing": billing,
        "shipping": billing,
        "line_items": line_items,
        "meta_data": [
            {"key": "geboortedatum",    "value": data.get("geboortedatum") or ""},
            {"key": "_geboortedatum",   "value": data.get("geboortedatum") or ""},
            {"key": "billing_birth",    "value": _nl_naar_amerikaans(data.get("geboortedatum") or "")},
            {"key": "_billing_birth",   "value": _nl_naar_amerikaans(data.get("geboortedatum") or "")},
            {"key": "bsn",              "value": data.get("bsn") or ""},
            {"key": "voorschrijver",    "value": data.get("voorschrijver") or ""},
            {"key": "agb_code",         "value": data.get("agb_code") or ""},
            {"key": "big_nummer",       "value": data.get("big_nummer") or ""},
            {"key": "recept_datum",     "value": data.get("recept_datum") or ""},
            {"key": "medicijn_ocr",     "value": data.get("medicijn") or ""},
            {"key": "gebruiksaanwijzing","value": data.get("gebruiksaanwijzing") or ""},
            {"key": "iter",             "value": data.get("iter") or ""},
            {"key": "oorsprong",        "value": oorsprong},
            {"key": "_created_via_farmamed", "value": "Farmamed_apotheek"},
            {"key": "Oorsprong bestelling", "value": oorsprong},
        ],
        "customer_note": f"Recept ingediend via {oorsprong}. Medicijn: {medicijn}",
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

        # Genereer EDIFACT voor dit verstrekkingsverzoek
        edifact = _genereer_edifact({
            "id": order["id"],
            "medicijn": data.get("medicijn", ""),
            "hoeveelheid": data.get("hoeveelheid", "30"),
            "patient_naam": data.get("patient_naam", ""),
        }, data)

        # Sla order_id op bij e-mail maar markeer NIET als verwerkt
        # Verwerkt wordt pas gezet als apotheker op "E-mail verwerkt" klikt
        if data.get("bron") == "email" and data.get("email_uid"):
            email_cached = _zoek_email_op_uid(data["email_uid"])
            if email_cached:
                email_cached["order_id"] = order["id"]
                _sla_email_op(email_cached)

        return JSONResponse(content={
            "order_id": order["id"],
            "status": order["status"],
            "edifact": edifact,
        })
    except Exception as e:
        return JSONResponse(content={"fout": str(e)})


# Volledige productcatalogus Farmamed
FARMAMED_PRODUCTEN = [
    (3430, "Tretinoïne crème 0.1% FNA - tegen huidveroudering (30 gram)"),
    (2760, "Tadalafil 5mg tabletten op recept (Sandoz)"),
    (2734, "Oxybutynine"),
    (2418, "Tretinoïne crème 0.05% FNA - tegen huidveroudering (90 gram)"),
    (2416, "Tretinoïne crème 0.02% FNA - tegen huidveroudering (90 gram)"),
    (1994, "Isoso in Lidocaine"),
    (1947, "Gabapentine crème 10% (SAW-crème)"),
    (1666, "Tretinoïne crème 0.05% FNA - tegen huidveroudering (30 gram)"),
    (1658, "Clonidine crème 0,1% (SAW-crème)"),
    (1185, "Proefbehandeling pijnstillende crèmes"),
    (1167, "Tretinoïne crème 0.02% FNA - tegen huidveroudering (30 gram)"),
    (1157, "Naltrexon (Low Dose) - LDN 1.0 - 4.5 mg"),
    (946,  "Clonidine crème 0,2% (SAW-crème)"),
    (945,  "Ketamine crème 10% (SAW-crème)"),
    (944,  "Baclofen crème 5% (SAW-crème)"),
    (943,  "Fenytoïne crème 5% (SAW-crème)"),
    (942,  "Fenytoïne crème 10% (SAW-crème)"),
    (940,  "Fenytoïne crème 20% (SAW-crème)"),
    (939,  "Amitriptyline crème 5% (SAW-crème)"),
    (937,  "Amitriptyline crème 10% (SAW-crème)"),
]


async def _zoek_product_id(medicijn_naam: str, wc_url: str, wc_key: str, wc_secret: str) -> int | None:
    """
    Zoek het beste WooCommerce product-ID op basis van medicijnnaam.
    Gebruikt Claude om slim te matchen op werkzame stof, concentratie en hoeveelheid.
    """
    if not medicijn_naam:
        return None

    # Bouw productlijst op als tekst voor Claude
    producten_tekst = "\n".join([f"ID {pid}: {naam}" for pid, naam in FARMAMED_PRODUCTEN])

    prompt = f"""Je bent een farmaceutisch assistent. Zoek het best passende product-ID uit de lijst.

Uitgelezen medicijn van recept: "{medicijn_naam}"

Beschikbare producten:
{producten_tekst}

Regels:
- Match op werkzame stof (bijv. tretinoïne = tretinoine = retinoïnezuur)
- Match op concentratie (0.02% = 0,02% = 0.2 mg/g)
- Bij meerdere hoeveelheden (30g vs 90g): kies 30 gram tenzij recept anders aangeeft
- Geef ALLEEN het getal van het product-ID terug, niets anders
- Als er geen match is geef je: 0"""

    try:
        resp = http_requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 10,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=15,
        )
        resp.raise_for_status()
        product_id_str = resp.json()["content"][0]["text"].strip()
        product_id = int(product_id_str)
        if product_id > 0:
            return product_id
    except Exception:
        pass

    # Fallback: fuzzy matching
    from rapidfuzz import fuzz
    beste_score = 0
    beste_id = None
    medicijn_norm = _normaliseer_medicijn(medicijn_naam)
    for pid, naam in FARMAMED_PRODUCTEN:
        naam_norm = _normaliseer_medicijn(naam)
        score = fuzz.token_sort_ratio(medicijn_norm, naam_norm)
        if score > beste_score:
            beste_score = score
            beste_id = pid
    return beste_id if beste_score > 50 else None


# ------------------------------------------------------------------
# Orders pagina
# ------------------------------------------------------------------

@app.get("/bestellingen")
async def haal_bestellingen_op():
    headers={"Accept": "application/json"},
    from datetime import datetime, timedelta
    """Haalt openstaande WooCommerce orders op met betaal- en verzendstatus."""
    wc_url = os.getenv("WC_URL", "")
    wc_key = os.getenv("WC_KEY", "")
    wc_secret = os.getenv("WC_SECRET", "")
    sc_public = os.getenv("SENDCLOUD_PUBLIC_KEY", "")
    sc_secret = os.getenv("SENDCLOUD_SECRET_KEY", "")
    paynl_token = os.getenv("PAYNL_API_TOKEN", "")
    paynl_service = os.getenv("PAYNL_SERVICE_ID", "")

    if not all([wc_url, wc_key, wc_secret]):
        return JSONResponse(content={"fout": "WooCommerce niet geconfigureerd"})

    # Haal openstaande orders op
    try:
        # Haal alle pagina's op (WooCommerce max 100 per pagina)
        orders_raw = []
        from datetime import datetime as _dt, timedelta as _td
        twee_weken_geleden = (_dt.now() - _td(weeks=2)).strftime("%Y-%m-%dT00:00:00")
        pagina = 1
        while True:
            resp = http_requests.get(
                f"{wc_url}/wp-json/wc/v3/orders",
                auth=(wc_key, wc_secret),
                headers={"Accept": "application/json"},
                params={"status": "processing,pending,on-hold", "per_page": 100, "orderby": "date", "order": "desc", "page": pagina, "after": twee_weken_geleden},
                timeout=20,
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            orders_raw.extend(batch)
            if len(batch) < 100:
                break
            pagina += 1
    except Exception as e:
        return JSONResponse(content={"fout": str(e)})

    # Haal SendCloud zendingen op als credentials beschikbaar
    sendcloud_zendingen = {}
    if sc_public and sc_secret:
        try:
            sc_resp = http_requests.get(
                "https://panel.sendcloud.sc/api/v2/parcels",
                auth=(sc_public, sc_secret),
                params={"after": twee_weken_geleden,},
                timeout=10,
            )
            if sc_resp.status_code == 200:
                for p in sc_resp.json().get("parcels", []):
                    ref = str(p.get("order_number") or p.get("external_reference") or "")
                    if ref:
                        sendcloud_zendingen[ref] = {
                            "status": p.get("status", {}).get("message", ""),
                            "tracking": p.get("tracking_number", ""),
                            "tracking_url": p.get("tracking_url", ""),
                        }
        except Exception:
            pass

    # Verwerk orders
    orders = []
    for o in orders_raw:
        billing = o.get("billing", {})
        meta = {m["key"]: m["value"] for m in o.get("meta_data", [])}
        items = o.get("line_items", [])
        naam = f"{billing.get('first_name','')} {billing.get('last_name','')}".strip()
        medicijn = items[0]["name"] if items else "—"
        order_id = str(o["id"])

        # Betaalstatus
        betaal_methode = o.get("payment_method", "")
        betaal_titel = o.get("payment_method_title", "")
        datum_betaald = o.get("date_paid")

        if datum_betaald and o.get("transaction_id"):
            if "pay" in betaal_methode.lower() or "paynl" in betaal_methode.lower():
                betaal_status = "Pay ✓"
                betaal_type = "pay"
            elif "bank" in betaal_methode.lower() or "transfer" in betaal_methode.lower() or "bacs" in betaal_methode.lower():
                betaal_status = "Bank ✓"
                betaal_type = "bank"
            else:
                betaal_status = f"Betaald ({betaal_titel or betaal_methode})"
                betaal_type = "anders"
        else:
            betaal_status = "Niet betaald"
            betaal_type = "onbetaald"

        # Verzendstatus: haal ordernotities op voor SendCloud tracking
        zending = sendcloud_zendingen.get(order_id) or sendcloud_zendingen.get(f"#{order_id}")
        verzend_status = "Niet verzonden"
        tracking_url = ""
        tracking_nr = ""

        if zending:
            verzend_status = zending["status"]
            tracking_url = zending["tracking_url"]
            tracking_nr = zending["tracking"]
        else:
            pass  # Ordernotities worden niet opgehaald voor snelheid
            
        # Oorsprong
        oorsprong = meta.get("oorsprong", "")
        if not oorsprong:
            via = o.get("created_via", "")
            oorsprong = "Webshop" if via == "checkout" else "API"

        # Haal ordernotities op
        o["order_notes"] = []

        orders.append({
            "id": o["id"],
            "status": o.get("status", ""),
            "datum": o.get("date_created", "")[:10],
            "klant_naam": naam,
            "medicijn": medicijn[:40],
            "totaal": o.get("total", "0"),
            "betaal_status": betaal_status,
            "betaal_type": betaal_type,
            "verzend_status": verzend_status,
            "tracking_url": tracking_url,
            "tracking_nr": tracking_nr,
            "oorsprong": oorsprong,
        })

    return JSONResponse(content={"orders": orders})


@app.post("/api/verwerk-mt940")
async def verwerk_mt940(bestand: UploadFile = File(...), request: Request = None):
    """
    Parseert een MT940 bankbestand en matcht betalingen aan openstaande WooCommerce orders.
    """
    import re as _re
    from rapidfuzz import fuzz

    inhoud = await bestand.read()
    tekst = inhoud.decode("utf-8", errors="replace")

    # MT940 parser
    betalingen = []
    transacties = _re.split(r':61:', tekst)

    for i, blok in enumerate(transacties[1:], 1):
        try:
            # Datum (YYMMDD)
            datum_match = _re.match(r'(\d{6})', blok)
            datum = ""
            if datum_match:
                d = datum_match.group(1)
                datum = f"20{d[:2]}-{d[2:4]}-{d[4:6]}"

            # Bedrag (C=credit/inkomend, D=debet/uitgaand)
            bedrag_match = _re.search(r'[CD](\d+),(\d*)', blok)
            if not bedrag_match:
                continue
            richting = 'C' if 'C' in blok[:20] else 'D'
            if richting != 'C':
                continue  # Alleen inkomende betalingen
            bedrag = float(f"{bedrag_match.group(1)}.{bedrag_match.group(2) or '00'}")

            # Omschrijving uit :86: tag
            omschrijving = ""
            naam = ""
            iban = ""
            omschrijving_match = _re.search(r':86:(.*?)(?=:6[12]:|$)', blok, _re.DOTALL)
            if omschrijving_match:
                omschrijving_raw = omschrijving_match.group(1).strip()
                omschrijving = omschrijving_raw.replace('\n', ' ').replace('\r', '')

                # Naam tegenhanger
                naam_match = _re.search(r'/NAME/([^/]+)', omschrijving_raw) or _re.search(r'(?:naam|name)[:\s]+([^/]+)', omschrijving_raw, _re.IGNORECASE)
                if naam_match:
                    naam = naam_match.group(1).strip()

                # IBAN
                iban_match = _re.search(r'[A-Z]{2}\d{2}[A-Z0-9]{4}\d{7,}', omschrijving_raw)
                if iban_match:
                    iban = iban_match.group(0)

            betalingen.append({
                "datum": datum,
                "bedrag": bedrag,
                "naam": naam,
                "iban": iban,
                "omschrijving": omschrijving[:200],
            })
        except Exception:
            continue

    if not betalingen:
        return JSONResponse(content={"fout": "Geen betalingen gevonden in MT940 bestand"})

    # Haal openstaande orders op
    wc_url = os.getenv("WC_URL", "")
    wc_key = os.getenv("WC_KEY", "")
    wc_secret = os.getenv("WC_SECRET", "")

    try:
        resp = http_requests.get(
            f"{wc_url}/wp-json/wc/v3/orders",
            auth=(wc_key, wc_secret),
            headers={"Accept": "application/json"},
            params={"status": "pending", "per_page": 50},
            timeout=15,
        )
        orders = resp.json() if resp.status_code == 200 else []
    except Exception:
        orders = []

    # Match betalingen aan orders
    matches = []
    for betaling in betalingen:
        beste_match = None
        beste_score = 0

        for order in orders:
            billing = order.get("billing", {})
            wc_naam = f"{billing.get('first_name','')} {billing.get('last_name','')}".strip()
            wc_bedrag = float(order.get("total", 0))
            wc_id = str(order["id"])

            score = 0

            # Bedrag match (zwaarst gewogen)
            if abs(betaling["bedrag"] - wc_bedrag) < 0.02:
                score += 50

            # Naam match
            if betaling["naam"]:
                naam_score = fuzz.token_sort_ratio(
                    _normaliseer_naam(betaling["naam"]),
                    _normaliseer_naam(wc_naam)
                )
                score += int(naam_score * 0.4)

            # Ordernummer directe match (zwaarste gewicht)
            if betaling.get("order_nr") == wc_id:
                score += 60
            elif wc_id in betaling["omschrijving"]:
                score += 30

            if score > beste_score and score >= 50:
                beste_score = score
                beste_match = order

        matches.append({
            "betaling": betaling,
            "order": {
                "id": beste_match["id"],
                "klant_naam": f"{beste_match['billing']['first_name']} {beste_match['billing']['last_name']}".strip(),
                "totaal": beste_match.get("total"),
                "status": beste_match.get("status"),
            } if beste_match else None,
            "score": beste_score,
            "gematcht": beste_match is not None and beste_score >= 50,
        })

    return JSONResponse(content={
        "betalingen": len(betalingen),
        "gematcht": sum(1 for m in matches if m["gematcht"]),
        "matches": matches,
    })


@app.post("/api/betalingen-verwerken")
async def betalingen_verwerken(request: Request):
    """Markeert geselecteerde orders als betaald in WooCommerce."""
    body = await request.json()
    order_ids = body.get("order_ids", [])
    wc_url = os.getenv("WC_URL", "")
    wc_key = os.getenv("WC_KEY", "")
    wc_secret = os.getenv("WC_SECRET", "")

    resultaten = []
    for oid in order_ids:
        try:
            resp = http_requests.put(
                f"{wc_url}/wp-json/wc/v3/orders/{oid}",
                auth=(wc_key, wc_secret),
                json={
                    "set_paid": True,
                    "status": "processing",
                },
                timeout=10,
            )
            resultaten.append({"order_id": oid, "ok": resp.status_code == 200})
        except Exception as e:
            resultaten.append({"order_id": oid, "ok": False, "fout": str(e)})

    return JSONResponse(content={"resultaten": resultaten})


@app.post("/api/verzend-status")
async def haal_verzend_status(request: Request):
    """Haalt verzendstatus op uit ordernotities voor een lijst order IDs."""
    body = await request.json()
    order_ids = body.get("order_ids", [])
    wc_url = os.getenv("WC_URL", "")
    wc_key = os.getenv("WC_KEY", "")
    wc_secret = os.getenv("WC_SECRET", "")
    import re as _re

    resultaten = {}
    for oid in order_ids:
        try:
            resp = http_requests.get(
                f"{wc_url}/wp-json/wc/v3/orders/{oid}/notes",
                auth=(wc_key, wc_secret),
                headers={"Accept": "application/json"},
                timeout=5,
            )
            if resp.status_code == 200:
                for note in resp.json():
                    note_tekst = note.get("note", "")
                    if "sendcloud" in note_tekst.lower() or "postnl" in note_tekst.lower() or "tracking" in note_tekst.lower():
                        url_match = _re.search(r'(https?://\S+)', note_tekst)
                        if url_match:
                            resultaten[str(oid)] = {
                                "verzonden": True,
                                "tracking_url": url_match.group(1).replace("&amp;", "&").rstrip(".")
                            }
                            break
                if str(oid) not in resultaten:
                    resultaten[str(oid)] = {"verzonden": False, "tracking_url": ""}
        except Exception:
            resultaten[str(oid)] = {"verzonden": False, "tracking_url": ""}

    return JSONResponse(content={"resultaten": resultaten})


@app.post("/api/order-afronden")
async def order_afronden(request: Request):
    """Markeert een of meerdere WooCommerce orders als completed."""
    body = await request.json()
    order_ids = body.get("order_ids", [])
    wc_url = os.getenv("WC_URL", "")
    wc_key = os.getenv("WC_KEY", "")
    wc_secret = os.getenv("WC_SECRET", "")

    resultaten = []
    for oid in order_ids:
        try:
            resp = http_requests.put(
                f"{wc_url}/wp-json/wc/v3/orders/{oid}",
                auth=(wc_key, wc_secret),
                json={"status": "completed"},
                timeout=10,
            )
            resultaten.append({"order_id": oid, "ok": resp.status_code == 200})
        except Exception as e:
            resultaten.append({"order_id": oid, "ok": False, "fout": str(e)})

    return JSONResponse(content={"resultaten": resultaten})


@app.get("/status", response_class=HTMLResponse)
async def status_pagina():
    html_path = BASE_DIR / "templates" / "status.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/orders", response_class=HTMLResponse)
async def orders_pagina():
    html_path = BASE_DIR / "templates" / "orders.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


def _parseer_edifact(tekst: str) -> dict:
    """
    Parseert een inkomend EDIFACT-bericht (verstrekkingsverzoek van arts).
    Extraheert patiënt- en medicijngegevens voor WooCommerce order.
    """
    import re
    data = {}

    # Patiëntnaam uit NAD segment
    nad = re.search(r"NAD\+PAT\+([^+']+)", tekst)
    if nad:
        data["patient_naam"] = nad.group(1).strip().replace(":", " ")

    # Geboortedatum
    dob = re.search(r"DTM\+329:(\d{8})", tekst)
    if dob:
        d = dob.group(1)
        data["geboortedatum"] = f"{d[6:8]}-{d[4:6]}-{d[0:4]}"

    # Medicijn uit LIN of IMD segment
    imd = re.search(r"IMD\+F\+\+\+([^']+)", tekst)
    if imd:
        data["medicijn"] = imd.group(1).strip()

    # Hoeveelheid uit QTY segment
    qty = re.search(r"QTY\+21:(\d+):GRM", tekst)
    if qty:
        data["hoeveelheid"] = f"{qty.group(1)} gram"

    # Voorschrijver uit NAD+PrescribingDoctor of PRE segment
    prs = re.search(r"NAD\+PRS\+([^+']+)", tekst)
    if prs:
        data["voorschrijver"] = prs.group(1).strip().replace(":", " ")

    # Receptdatum
    rdt = re.search(r"DTM\+137:(\d{8}):102", tekst)
    if rdt:
        d = rdt.group(1)
        data["recept_datum"] = f"{d[6:8]}-{d[4:6]}-{d[0:4]}"

    # BSN uit PNA of GIN segment
    bsn = re.search(r"GIN\+BSN\+(\d{8,9})", tekst)
    if bsn:
        data["bsn"] = bsn.group(1)

    return data


@app.post("/api/zoek-herhaalorder")
async def zoek_herhaalorder(request: Request):
    """
    Zoekt een eerdere WooCommerce order op basis van e-mailadres en/of naam
    uit de e-mailbody. Geeft de beste match terug als kloonvoorstel.
    """
    body = await request.json()
    afzender_email = body.get("afzender_email", "")
    afzender_naam = body.get("afzender_naam", "")
    email_body = body.get("email_body", "")

    wc_url = os.getenv("WC_URL", "")
    wc_key = os.getenv("WC_KEY", "")
    wc_secret = os.getenv("WC_SECRET", "")

    if not all([wc_url, wc_key, wc_secret]):
        return JSONResponse(content={"fout": "WooCommerce niet geconfigureerd"})

    # Laat Claude de echte afzender en inhoud uit de (doorgestuurde) mail extraheren
    prompt = f"""Analyseer deze e-mail. Het kan een doorgestuurde (forwarded) mail zijn.
Geef ALLEEN JSON terug, geen uitleg.

Directe afzender: {afzender_email}
Naam directe afzender: {afzender_naam}
Volledige e-mailinhoud:
{email_body[:2000]}

Instructies:
- Als dit een doorgestuurde mail is, gebruik dan het e-mailadres en naam van de ORIGINELE afzender (niet de doorsturende partij zoals info@farmamed.nl)
- De originele afzender staat meestal na "Van:", "From:", "Afzender:" of "-------- Oorspronkelijk bericht --------"
- Extraheer het medicijn en eventuele hoeveelheid uit de gehele inhoud inclusief het doorgestuurde deel

{{
  "patient_naam": "naam van de originele patient/arts",
  "patient_email": "e-mailadres van de originele afzender",
  "medicijn": "gevraagd medicijn of null",
  "hoeveelheid": "hoeveelheid of null",
  "is_herhaalverzoek": true of false,
  "is_doorgestuurd": true of false,
  "notitie": "korte samenvatting van het verzoek"
}}"""

    try:
        resp = http_requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=15,
        )
        resp.raise_for_status()
        tekst = resp.json()["content"][0]["text"].strip().replace("```json","").replace("```","").strip()
        email_data = json.loads(tekst)
    except Exception as e:
        email_data = {"patient_naam": afzender_naam, "is_herhaalverzoek": True}

    # Gebruik het echte e-mailadres (uit doorgestuurde mail indien van toepassing)
    zoek_email = email_data.get("patient_email") or afzender_email
    zoek_naam = email_data.get("patient_naam") or afzender_naam

    # Zoek eerdere orders op e-mailadres
    beste_order = None
    beste_score = 0

    try:
        # Zoek op e-mailadres van de originele afzender
        if zoek_email and "@" in zoek_email:
            resp = http_requests.get(
                f"{wc_url}/wp-json/wc/v3/orders",
                auth=(wc_key, wc_secret),
                headers={"Accept": "application/json"},
                params={"search": zoek_email, "per_page": 5, "orderby": "date", "order": "desc"},
                timeout=10,
            )
            orders = resp.json() if resp.status_code == 200 else []
            for order in (orders if isinstance(orders, list) else []):
                billing = order.get("billing", {})
                if billing.get("email", "").lower() == zoek_email.lower():
                    beste_order = order
                    beste_score = 100
                    break

        # Fallback: zoek op naam
        if not beste_order:
            naam = zoek_naam
            achternaam = naam.split()[-1] if naam.split() else ""
            if achternaam and len(achternaam) >= 3:
                from rapidfuzz import fuzz
                resp = http_requests.get(
                    f"{wc_url}/wp-json/wc/v3/orders",
                    auth=(wc_key, wc_secret),
                    headers={"Accept": "application/json"},
                    params={"search": achternaam, "per_page": 10, "orderby": "date", "order": "desc"},
                    timeout=10,
                )
                orders = resp.json() if resp.status_code == 200 else []
                for order in (orders if isinstance(orders, list) else []):
                    billing = order.get("billing", {})
                    wc_naam = f"{billing.get('first_name','')} {billing.get('last_name','')}".strip()
                    score = fuzz.token_sort_ratio(_normaliseer_naam(naam), _normaliseer_naam(wc_naam))
                    if score > beste_score:
                        beste_score = score
                        beste_order = order

    except Exception as e:
        return JSONResponse(content={"fout": str(e), "email_data": email_data})

    if not beste_order or beste_score < 60:
        return JSONResponse(content={
            "gevonden": False,
            "email_data": email_data,
            "bericht": "Geen eerdere order gevonden — vul handmatig in",
        })

    # Bouw kloonvoorstel op
    billing = beste_order.get("billing", {})
    meta = {m["key"]: m["value"] for m in beste_order.get("meta_data", [])}
    items = beste_order.get("line_items", [])
    medicijn = items[0]["name"] if items else ""
    product_id = items[0]["product_id"] if items else None

    # Gebruik productnaam uit catalogus
    productnaam = medicijn
    for pid, naam in FARMAMED_PRODUCTEN:
        if pid == product_id:
            productnaam = naam
            break

    kloon = {
        "order_id": beste_order["id"],
        "match_score": beste_score,
        "patient_naam": f"{billing.get('first_name','')} {billing.get('last_name','')}".strip(),
        "email": billing.get("email", ""),
        "telefoon": billing.get("phone", ""),
        "straat": billing.get("address_1", ""),
        "postcode_plaats": f"{billing.get('postcode','')} {billing.get('city','')}".strip(),
        "geboortedatum": _amerikaans_naar_nederlands(meta.get("billing_birth") or meta.get("_billing_birth") or ""),
        "bsn": meta.get("bsn", ""),
        "medicijn": productnaam,
        "hoeveelheid": "30 gram",
        "product_id": product_id,
        "iter": "1x iter",
        "gebruiksaanwijzing": meta.get("gebruiksaanwijzing", ""),
        "voorschrijver": meta.get("voorschrijver", ""),
        "agb_code": meta.get("agb_code", ""),
    }

    return JSONResponse(content={
        "gevonden": True,
        "kloon": kloon,
        "email_data": email_data,
        "bericht": f"Eerdere order #{beste_order['id']} gevonden (match: {beste_score}%)",
    })


@app.post("/api/kloon-order")
async def kloon_order(request: Request):
    """Maakt een nieuwe WooCommerce order aan op basis van een gekloonde order."""
    data = await request.json()
    data["bron"] = "email"
    # Hergebruik maak-order logica
    return await maak_order(request.__class__(request._scope, request._receive))


@app.post("/api/verwerk-edifact-bijlage")
async def verwerk_edifact_bijlage(request: Request):
    """
    Stroom 4: verwerkt een inkomend EDIFACT-bestand van een arts.
    Parseert de gegevens en maakt een WooCommerce order aan.
    """
    body = await request.json()
    email_uid = body.get("email_uid", "")
    bijlage_index = body.get("bijlage_index", 0)

    email = _zoek_email_op_uid(email_uid)
    if not email:
        return JSONResponse(content={"fout": "E-mail niet gevonden"})

    bijlagen = email.get("bijlagen", [])
    if bijlage_index >= len(bijlagen):
        return JSONResponse(content={"fout": "Bijlage niet gevonden"})

    bijlage = bijlagen[bijlage_index]
    try:
        inhoud_bytes = base64.b64decode(bijlage["data"])
        edifact_tekst = inhoud_bytes.decode("utf-8", errors="replace")
    except Exception as e:
        return JSONResponse(content={"fout": f"Kon bijlage niet lezen: {e}"})

    # Parseer EDIFACT
    gegevens = _parseer_edifact(edifact_tekst)
    if not gegevens:
        return JSONResponse(content={"fout": "Geen EDIFACT-gegevens gevonden in bijlage"})

    # Maak WooCommerce order aan
    wc_url = os.getenv("WC_URL", "")
    wc_key = os.getenv("WC_KEY", "")
    wc_secret = os.getenv("WC_SECRET", "")

    naam_delen = (gegevens.get("patient_naam") or "").split(" ", 1)
    order_payload = {
        "status": "processing",
        "billing": {
            "first_name": naam_delen[0] if naam_delen else "",
            "last_name": naam_delen[1] if len(naam_delen) > 1 else "",
        },
        "meta_data": [
            {"key": "geboortedatum", "value": gegevens.get("geboortedatum", "")},
            {"key": "bsn", "value": gegevens.get("bsn", "")},
            {"key": "voorschrijver", "value": gegevens.get("voorschrijver", "")},
            {"key": "recept_datum", "value": gegevens.get("recept_datum", "")},
            {"key": "medicijn_ocr", "value": gegevens.get("medicijn", "")},
            {"key": "bron", "value": "edifact_email"},
        ],
        "customer_note": f"Order aangemaakt vanuit EDIFACT-bijlage. Medicijn: {gegevens.get('medicijn', '')}",
    }

    try:
        resp = http_requests.post(
            f"{wc_url}/wp-json/wc/v3/orders",
            auth=(wc_key, wc_secret),
            json=order_payload,
            timeout=20,
        )
        resp.raise_for_status()
        order = resp.json()
        return JSONResponse(content={
            "order_id": order["id"],
            "gegevens": gegevens,
            "bericht": f"Order #{order['id']} aangemaakt vanuit EDIFACT-bijlage",
        })
    except Exception as e:
        return JSONResponse(content={"fout": str(e), "gegevens": gegevens})


@app.get("/api/orders")
async def haal_orders_op(toon_alle: bool = False):
    """Haalt openstaande WooCommerce orders op (pending + processing)."""
    wc_url = os.getenv("WC_URL", "")
    wc_key = os.getenv("WC_KEY", "")
    wc_secret = os.getenv("WC_SECRET", "")

    if not all([wc_url, wc_key, wc_secret]):
        return JSONResponse(content={"fout": "WooCommerce niet geconfigureerd"})

    try:
        orders_raw = []
        pagina = 1
        while True:
            response = http_requests.get(
                f"{wc_url}/wp-json/wc/v3/orders",
                auth=(wc_key, wc_secret),
                params={"status": "pending,processing", "per_page": 100, "orderby": "date", "order": "desc", "page": pagina},
                headers={"Accept": "application/json"},
                timeout=20,
            )
            if response.status_code != 200:
                fout_tekst = response.text[:500]
                print(f"[ORDERS] HTTP {response.status_code}: {fout_tekst}")
                return JSONResponse(content={"fout": f"WooCommerce fout {response.status_code}: {fout_tekst}"})
            batch = response.json()
            if not batch:
                break
            orders_raw.extend(batch)
            if len(batch) < 100:
                break
            pagina += 1

        orders = []
        for o in orders_raw:
            billing = o.get("billing", {})
            meta = {m["key"]: m["value"] for m in o.get("meta_data", [])}
            items = o.get("line_items", [])
            medicijn = items[0]["name"] if items else "Onbekend"
            naam = f"{billing.get('first_name','')} {billing.get('last_name','')}".strip()
            geboortedatum = meta.get("billing_birth") or meta.get("_billing_birth", "")
            recept_url = o.get("recept_url") or meta.get("recept_url", "")
            heeft_verstrekking = meta.get("_farmamed_verstrekking", "") == "1"
            created_via = o.get("created_via", "")

            # Admin-orders altijd als verstrekt markeren
            if created_via == "admin":
                heeft_verstrekking = True

            # Filter admin-orders tenzij toon_alle
            if created_via == "admin" and not toon_alle:
                continue

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
                "heeft_verstrekking": heeft_verstrekking,
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


async def _verrijk_met_woocommerce(recept: dict, wc_url: str, wc_key: str, wc_secret: str) -> dict:
    """
    Zoekt eerdere WooCommerce orders van dezelfde patiënt en vult
    ontbrekende velden aan vanuit die orders.
    Geeft verrijkt recept-dict terug met bronvermelding per veld.
    """
    if not all([wc_url, wc_key, wc_secret]):
        return recept

    verrijkt = dict(recept)
    verrijkt["_verrijking"] = {}  # bijhoudt welke velden verrijkt zijn

    # Zoekterm: achternaam van patiënt
    naam = recept.get("patient_naam") or ""
    achternaam = naam.split()[-1] if naam.split() else ""
    if not achternaam or len(achternaam) < 3:
        return verrijkt

    try:
        # Zoek orders op naam
        resp = http_requests.get(
            f"{wc_url}/wp-json/wc/v3/orders",
            auth=(wc_key, wc_secret),
            headers={"Accept": "application/json"},
            params={"search": achternaam, "per_page": 10, "orderby": "date", "order": "desc"},
            timeout=10,
        )
        resp.raise_for_status()
        orders = resp.json()

        if not orders or not isinstance(orders, list):
            return verrijkt

        # Zoek beste match op geboortedatum of naam
        from rapidfuzz import fuzz
        beste_order = None
        beste_score = 0
        huidige_order_id = str(body.get("huidige_order_id", ""))

        for order in orders:
            # Sla huidige order zelf over
            if str(order.get("id")) == huidige_order_id:
                continue
            billing = order.get("billing", {})
            meta = {m["key"]: m["value"] for m in order.get("meta_data", [])}

            # Naam vergelijken
            wc_naam = f"{billing.get('first_name','')} {billing.get('last_name','')}".strip()
            naam_score = fuzz.token_sort_ratio(
                _normaliseer_naam(naam),
                _normaliseer_naam(wc_naam)
            )

            # Geboortedatum vergelijken (extra zekerheid)
            geb_score = 0
            recept_geb = recept.get("geboortedatum") or ""
            wc_geb = _amerikaans_naar_nederlands(meta.get("billing_birth") or meta.get("_billing_birth") or "")
            if recept_geb and wc_geb and recept_geb == wc_geb:
                geb_score = 50  # bonus bij exacte match

            totaal = naam_score + geb_score
            if totaal > beste_score and naam_score >= 70:
                beste_score = totaal
                beste_order = order

        if not beste_order:
            return verrijkt

        # Verrijk ontbrekende velden
        billing = beste_order.get("billing", {})
        meta = {m["key"]: m["value"] for m in beste_order.get("meta_data", [])}
        order_id = beste_order.get("id")
        bron = f"WooCommerce order #{order_id}"

        def vul_aan(veld_recept, waarde, label):
            if not recept.get(veld_recept) and waarde:
                verrijkt[veld_recept] = waarde
                verrijkt["_verrijking"][label] = f"{waarde} (uit {bron})"

        vul_aan("email",    billing.get("email"), "E-mail")
        vul_aan("telefoon", billing.get("phone"), "Telefoon")
        vul_aan("bsn",      meta.get("bsn"), "BSN")
        vul_aan("geboortedatum",
                _amerikaans_naar_nederlands(meta.get("billing_birth") or meta.get("_billing_birth") or ""),
                "Geboortedatum")

        # Adres
        adres1 = billing.get("address_1", "")
        postcode = billing.get("postcode", "")
        stad = billing.get("city", "")
        if adres1:
            vul_aan("straat", adres1, "Straat")
        if postcode and stad:
            vul_aan("postcode_plaats", f"{postcode} {stad}", "Postcode & plaats")

        verrijkt["_match_score"] = beste_score
        verrijkt["_match_order"] = order_id

    except Exception as e:
        verrijkt["_verrijking_fout"] = str(e)

    return verrijkt


def _genereer_edifact(order_data: dict, recept_data: dict = None) -> str:
    """
    Genereert een EDIFACT ORDERS D96A verstrekkingsverzoek.
    Werkt voor alle drie werkstromen.
    """
    from datetime import datetime
    nu = datetime.now()
    datum = nu.strftime("%y%m%d")
    tijd = nu.strftime("%H%M")
    order_id = str(order_data.get("id") or order_data.get("order_id") or "0")
    ctrl = order_id.zfill(5)

    medicijn = order_data.get("medicijn", "ONBEKEND")
    medicijn_code = medicijn.upper().replace(" ", "")[:20]
    try:
        hoev_raw = str(order_data.get("hoeveelheid", 30))
        hoev_raw = hoev_raw.replace(",", ".").replace(" gram", "").replace("gram", "").replace(" g", "").replace("G", "").replace("g", "").strip()
        # Neem alleen het eerste getal
        import re as _re
        hoev_match = _re.search(r"[\d.]+", hoev_raw)
        hoeveelheid = int(float(hoev_match.group(0))) if hoev_match else 30
    except Exception:
        hoeveelheid = 30

    naam = order_data.get("patient_naam") or order_data.get("klant_naam") or ""
    voorschrijver = ""
    recept_datum = ""
    if recept_data:
        voorschrijver = recept_data.get("voorschrijver") or ""
        recept_datum = recept_data.get("recept_datum") or ""

    if voorschrijver and recept_datum:
        recept_ref = f"RECEPT-{voorschrijver[:12].upper().replace(' ','-')}-{recept_datum}"
    else:
        recept_ref = f"BESTELLING-{order_id}"

    regels = [
        f"UNB+UNOA:2+FARMAMED+GROOTHANDEL+{datum}:{tijd}+{ctrl}'",
        f"UNH+1+ORDERS:D:96A:UN'",
        f"BGM+220+{order_id}+9'",
        f"DTM+137:{nu.strftime('%Y%m%d')}:102'",
        f"NAD+BY+FARMAMED:::Farmamed BV'",
        f"NAD+SU+GROOTHANDEL:::Groothandel Farma NL'",
        f"NAD+DP+{naam[:35]}'",
        f"LIN+1++{medicijn_code}:BP'",
        f"IMD+F+++{medicijn[:35]}'",
        f"QTY+21:{hoeveelheid}:GRM'",
        f"RFF+PD:{recept_ref}'",
        f"UNT+11+1'",
        f"UNZ+1+{ctrl}'",
    ]
    return "\n".join(regels)


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


def _nl_naar_amerikaans(datum: str) -> str:
    """Converteert DD-MM-YYYY naar YYYY-MM-DD voor WooCommerce billing_birth."""
    if not datum:
        return ""
    try:
        from datetime import datetime
        if len(datum) == 10 and datum[2] == '-':
            dt = datetime.strptime(datum, "%d-%m-%Y")
            return dt.strftime("%Y-%m-%d")
    except ValueError:
        pass
    return datum


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


# SQLite persistente e-mailopslag
import sqlite3 as _sqlite3

_DB_PAD = "/app/data/emails.db"

def _init_email_db():
    """Maak database aan als die nog niet bestaat."""
    import os
    os.makedirs("/app/data", exist_ok=True)
    conn = _sqlite3.connect(_DB_PAD)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS emails (
            uid TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            ontvangen TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()

def _sla_email_op(email_data: dict):
    """Sla e-mail op in SQLite."""
    _init_email_db()
    conn = _sqlite3.connect(_DB_PAD)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO emails (uid, data) VALUES (?, ?)",
            (email_data["uid"], json.dumps(email_data))
        )
        conn.commit()
    finally:
        conn.close()

def _haal_emails_op_db(limit: int = 50) -> list:
    """Haal e-mails op uit SQLite, nieuwste eerst."""
    _init_email_db()
    conn = _sqlite3.connect(_DB_PAD)
    try:
        rows = conn.execute(
            "SELECT data FROM emails ORDER BY ontvangen DESC LIMIT ?", (limit,)
        ).fetchall()
        return [json.loads(r[0]) for r in rows]
    finally:
        conn.close()

def _zoek_email_op_uid(uid: str) -> dict:
    """Zoek één e-mail op uid."""
    _init_email_db()
    conn = _sqlite3.connect(_DB_PAD)
    try:
        row = conn.execute("SELECT data FROM emails WHERE uid = ?", (uid,)).fetchone()
        return json.loads(row[0]) if row else None
    finally:
        conn.close()

# Backwards compat helper
def _email_cache_get(uid: str):
    return _zoek_email_op_uid(uid)


@app.post("/api/email-inkomend")
async def email_inkomend(request: Request):
    """Ontvangt een e-mail van de lokale poller en slaat hem op in SQLite."""
    data = await request.json()
    if not data.get("uid"):
        return JSONResponse(content={"fout": "Geen UID"})
    _sla_email_op(data)
    alle = _haal_emails_op_db()
    return JSONResponse(content={"ok": True, "totaal": len(alle)})


from fastapi.responses import StreamingResponse
import io

@app.post("/api/download-recept")
async def download_recept(request: Request):
    """
    Haalt recept op via WordPress plugin endpoint en stuurt als download.
    Gebruikt het beveiligde farmamed/v1/download-recept endpoint.
    """
    body = await request.json()
    order_id = body.get("order_id")
    bestandsnaam = body.get("bestandsnaam", "recept.pdf")

    wc_url = os.getenv("WC_URL", "")
    wc_key = os.getenv("WC_KEY", "")
    wc_secret = os.getenv("WC_SECRET", "")

    if not order_id or not wc_url:
        return JSONResponse(content={"fout": "Onvoldoende gegevens"}, status_code=400)

    try:
        # Gebruik het WordPress plugin endpoint
        download_url = f"{wc_url}/wp-json/farmamed/v1/download-recept"
        resp = http_requests.get(
            download_url,
            params={"order_id": order_id, "bestandsnaam": bestandsnaam},
            auth=(wc_key, wc_secret),
            timeout=30,
            stream=True,
        )
        resp.raise_for_status()
        inhoud = resp.content

        # Bepaal media type
        ct = resp.headers.get("content-type", "application/pdf").split(";")[0]

        return StreamingResponse(
            io.BytesIO(inhoud),
            media_type=ct,
            headers={
                "Content-Disposition": f'attachment; filename="{bestandsnaam}"',
                "Content-Length": str(len(inhoud)),
            }
        )
    except Exception as e:
        return JSONResponse(content={"fout": str(e)}, status_code=500)


@app.post("/api/download-bijlage")
async def download_bijlage(request: Request):
    """Haalt een e-mailbijlage op uit de cache en stuurt het terug als download."""
    body = await request.json()
    email_uid = body.get("email_uid", "")
    bijlage_index = body.get("bijlage_index", 0)
    bestandsnaam = body.get("bestandsnaam", "recept.pdf")

    email = _zoek_email_op_uid(email_uid)
    if not email:
        return JSONResponse(content={"fout": "E-mail niet gevonden"}, status_code=404)

    bijlagen = email.get("bijlagen", [])
    if bijlage_index >= len(bijlagen):
        return JSONResponse(content={"fout": "Bijlage niet gevonden"}, status_code=404)

    bijlage = bijlagen[bijlage_index]
    try:
        data = bijlage.get("data", "")
        if not data:
            return JSONResponse(content={"fout": "Geen bijlagedata"}, status_code=404)
        # Voeg padding toe als nodig
        padding = 4 - len(data) % 4
        if padding != 4:
            data += "=" * padding
        inhoud = base64.b64decode(data, validate=False)
        ct = bijlage.get("type", "application/pdf")
        return StreamingResponse(
            io.BytesIO(inhoud),
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{bestandsnaam}"'}
        )
    except Exception as e:
        return JSONResponse(content={"fout": str(e)}, status_code=500)


@app.delete("/api/emails-wissen")
async def wis_emails():
    """Leegmaken van de e-mailcache (SQLite)."""
    _init_email_db()
    conn = _sqlite3.connect(_DB_PAD)
    try:
        conn.execute("DELETE FROM emails")
        conn.commit()
        return JSONResponse(content={"ok": True, "bericht": "Alle e-mails gewist"})
    finally:
        conn.close()


@app.get("/api/emails")
async def haal_emails_op(verwerkt: str = "nee"):
    """Geeft opgeslagen e-mails terug vanuit SQLite. verwerkt=nee toont alleen onverwerkte."""
    emails = _haal_emails_op_db()
    if not emails:
        return JSONResponse(content={"emails": [], "info": "Nog geen e-mails ontvangen — start de lokale email_poller.py"})
    # Filter verwerkte e-mails tenzij expliciet gevraagd
    if verwerkt == "nee":
        emails = [e for e in emails if not e.get("verwerkt")]
    emails_zonder_data = []
    for e in emails:
        email_slim = {k: v for k, v in e.items() if k != "bijlagen"}
        email_slim["bijlagen"] = [{"naam": b["naam"], "type": b["type"]} for b in e.get("bijlagen", [])]
        emails_zonder_data.append(email_slim)
    return JSONResponse(content={"emails": emails_zonder_data})


@app.post("/api/email-verwerkt")
async def markeer_email_verwerkt(request: Request):
    """Markeert een e-mail als verwerkt (order aangemaakt) in de database."""
    body = await request.json()
    email_uid = body.get("email_uid", "")
    order_id = body.get("order_id")
    email = _zoek_email_op_uid(email_uid)
    if email:
        email["verwerkt"] = True
        email["order_id"] = order_id
        _sla_email_op(email)
    return JSONResponse(content={"ok": True})

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

    email = _zoek_email_op_uid(email_uid)
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

    email = _zoek_email_op_uid(email_uid)
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

        # BSN opschonen
        if recept_data.get("bsn"):
            import re as _re
            bsn_clean = _re.sub(r"[^0-9]", "", str(recept_data["bsn"]))
            recept_data["bsn"] = bsn_clean if len(bsn_clean) == 9 else ""
            recept_data["bsn_fout"] = len(bsn_clean) != 9 and len(bsn_clean) > 0

        # Verrijk met WooCommerce-data
        wc_url = os.getenv("WC_URL", "")
        wc_key = os.getenv("WC_KEY", "")
        wc_secret = os.getenv("WC_SECRET", "")
        if all([wc_url, wc_key, wc_secret]):
            recept_data = await _verrijk_met_woocommerce(recept_data, wc_url, wc_key, wc_secret)

        # Sla bijlagedata op in cache voor latere download
        email_cached = _zoek_email_op_uid(email_uid)
        if email_cached:
            bijlagen_cache = email_cached.get("bijlagen", [])
            if bijlage_index < len(bijlagen_cache):
                bijlagen_cache[bijlage_index]["data"] = b64
                email_cached["bijlagen"] = bijlagen_cache
                _sla_email_op(email_cached)

        return JSONResponse(content={"preview": preview, "recept": recept_data})
    except Exception as e:
        return JSONResponse(content={"preview": preview, "fout": str(e)})
