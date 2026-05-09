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

{
  "recept_datum": "DD-MM-YYYY of null",
  "medicijn": "volledige naam inclusief concentratie",
  "concentratie": "bijv. 0.02%",
  "hoeveelheid": "bijv. 30 gram",
  "iter": "aantal herhalingen of null",
  "gebruiksaanwijzing": "volledige instructie",
  "patient_naam": "voor- en achternaam",
  "geboortedatum": "DD-MM-YYYY of null",
  "bsn": "9-cijferig nummer of null",
  "email": "emailadres of null",
  "telefoon": "telefoonnummer of null",
  "voorschrijver": "naam arts",
  "agb_code": "AGB-code of null",
  "big_nummer": "BIG-nummer of null",
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
            timeout=30,
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
