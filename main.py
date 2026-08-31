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
from fastapi.responses import JSONResponse, HTMLResponse, Response
from dotenv import load_dotenv
import requests as http_requests
import asyncio
import imaplib as _imaplib
import email as _email_lib
from email.header import decode_header as _decode_header

load_dotenv()

_DATABASE_URL = os.getenv("DATABASE_URL", "")

def _pg_conn():
    import psycopg2
    return psycopg2.connect(_DATABASE_URL)

def _farmamed_db():
    return _pg_conn()

app = FastAPI(title="Farmamed Recept Agent")

# === FARMAMED DATABASE ===
_FARMAMED_DB = os.getenv("FARMAMED_DB", "/app/data/farmamed.db")

# _init_farmamed_db en _farmamed_db zijn vervangen door PostgreSQL versies bovenaan
def _zoek_of_maak_patient(billing: dict, bsn: str = "", geboortedatum: str = "") -> int | None:
    """Zoekt bestaande patiënt op email of maakt nieuwe aan. Geeft patient_id terug.

    Bij een bestaande match worden ontbrekende velden (BSN, geboortedatum,
    telefoon, adres, postcode, plaats) aangevuld met wat nu bekend is —
    zonder bestaande waarden te overschrijven. Zo verrijkt elke nieuwe
    e-mail/order het patiëntdossier automatisch, in plaats van dat verse
    informatie (bijv. een BSN die net van een recept is uitgelezen) alleen
    voor deze ene order gebruikt wordt en daarna weer verloren gaat."""
    email = (billing.get("email") or "").lower().strip()
    if not email or email in ("info@farmamed.nl", "info@apotheekwoerden.nl"):
        return None
    if not _DATABASE_URL:
        return None
    conn = _pg_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM patienten WHERE LOWER(TRIM(email)) = %s", (email,))
        row = cur.fetchone()
        if row:
            pid = row[0]
            aanvullen = {
                "bsn": bsn,
                "geboortedatum": geboortedatum,
                "telefoon": billing.get("phone", ""),
                "adres": billing.get("address_1", ""),
                "postcode": billing.get("postcode", ""),
                "plaats": billing.get("city", ""),
            }
            for veld, waarde in aanvullen.items():
                if waarde:
                    cur.execute(
                        f"UPDATE patienten SET {veld}=%s, bijgewerkt_op=NOW() WHERE id=%s AND ({veld} IS NULL OR {veld}='')",
                        (waarde, pid)
                    )
            conn.commit()
            return pid
        cur.execute("""
            INSERT INTO patienten (email, bsn, voornaam, achternaam, geboortedatum,
                                   adres, postcode, plaats, telefoon)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (
            email, bsn or None,
            billing.get("first_name",""), billing.get("last_name",""),
            geboortedatum or None,
            billing.get("address_1",""), billing.get("postcode",""),
            billing.get("city",""), billing.get("phone",""),
        ))
        new_id = cur.fetchone()[0]
        conn.commit()
        return new_id
    finally:
        cur.close()
        conn.close()


def _zoek_of_maak_voorschrijver(naam: str, agb_code: str = "") -> int | None:
    """Zoekt of maakt voorschrijver in PostgreSQL."""
    if not naam and not agb_code:
        return None
    if not _DATABASE_URL:
        return None
    conn = _pg_conn()
    cur = conn.cursor()
    try:
        if agb_code:
            cur.execute("SELECT id FROM voorschrijvers WHERE agb_code = %s", (agb_code,))
            row = cur.fetchone()
            if row:
                return row[0]
        naam = naam or "Onbekende voorschrijver"
        cur.execute(
            "INSERT INTO voorschrijvers (agb_code, naam) VALUES (%s,%s) ON CONFLICT (agb_code) DO UPDATE SET naam=EXCLUDED.naam RETURNING id",
            (agb_code or None, naam)
        )
        row = cur.fetchone()
        if row:
            conn.commit()
            return row[0]
        cur.execute("SELECT id FROM voorschrijvers WHERE naam = %s", (naam,))
        row = cur.fetchone()
        conn.commit()
        return row[0] if row else None
    finally:
        cur.close()
        conn.close()

def _haal_coulance_korting(email: str) -> float:
    """
    Haalt het coulance-kortingsbedrag per tube op voor dit e-mailadres.
    0 (geen korting) als de patiënt niet bekend is of geen korting heeft.
    """
    if not email or not _DATABASE_URL:
        return 0.0
    conn = _pg_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT coulance_korting_bedrag FROM patienten WHERE LOWER(TRIM(email))=%s ORDER BY bijgewerkt_op DESC LIMIT 1",
            (email.strip().lower(),)
        )
        rij = cur.fetchone()
        return float(rij[0]) if rij and rij[0] else 0.0
    finally:
        cur.close()
        conn.close()


def _zoek_medicijn_id(productnaam: str) -> int | None:
    """
    Zoekt medicijn_id op basis van productnaam via fuzzy matching.

    Gebruikt dezelfde robuuste matchlogica als sync_permutaties.py: exacte
    match eerst, dan alleen kandidaten waarvan het EERSTE woord (de werkzame
    stof) overeenkomt, met DMSO/DIMETHYLSULFOXIDE als hard onderscheid en
    concentratiegetallen die moeten overeenkomen. Voorkomt bijv. dat 'plain
    10%' matcht met de '10% in DMSO'-variant van hetzelfde medicijn, of dat
    'Baclofen' matcht met 'Baclofen/Fenytoïne' puur op gedeelde woorden.
    """
    import re as _re_medid
    from rapidfuzz import fuzz
    if not productnaam or not _DATABASE_URL:
        return None
    conn = _pg_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, naam_smarthub, naam_woocommerce, naam_canoniek FROM medicijnen")
        rijen = cur.fetchall()

        def _woorden(s):
            genorm = _normaliseer_naam(s or "")
            return [w for w in genorm.split() if w and (len(w) >= 3 or _re_medid.match(r'^[\d.]+$', w))]

        naam_norm = _normaliseer_naam(productnaam)
        woorden_bron = _woorden(productnaam)
        if not woorden_bron:
            return None
        eerste_woord = woorden_bron[0]
        getallen_bron = {w for w in woorden_bron if _re_medid.match(r'^[\d.]+$', w)}

        # Exacte match eerst
        for mid, naam_sh, naam_wc, naam_can in rijen:
            for veld in [naam_can, naam_wc, naam_sh]:
                if veld and _normaliseer_naam(veld) == naam_norm:
                    return mid

        beste_score, beste_id = 0, None
        for mid, naam_sh, naam_wc, naam_can in rijen:
            for veld in [naam_can, naam_wc, naam_sh]:
                if not veld:
                    continue
                woorden_kandidaat = _woorden(veld)
                if not woorden_kandidaat:
                    continue
                eerste_woord_kandidaat = woorden_kandidaat[0]
                if eerste_woord != eerste_woord_kandidaat \
                        and not eerste_woord.startswith(eerste_woord_kandidaat) and not eerste_woord_kandidaat.startswith(eerste_woord):
                    continue
                if ('dmso' in woorden_bron) != ('dmso' in woorden_kandidaat):
                    continue
                getallen_kandidaat = {w for w in woorden_kandidaat if _re_medid.match(r'^[\d.]+$', w)}
                if getallen_bron and getallen_kandidaat and not (getallen_bron & getallen_kandidaat):
                    continue
                score = max(
                    fuzz.token_set_ratio(naam_norm, _normaliseer_naam(veld)),
                    fuzz.partial_ratio(naam_norm, _normaliseer_naam(veld)),
                )
                if score > beste_score:
                    beste_score, beste_id = score, mid
        return beste_id if beste_score >= 80 else None
    finally:
        cur.close()
        conn.close()


def _sla_order_op(wc_order_id: str, patient_id, voorschrijver_id, medicijn_id,
                   hoeveelheid=1, recept_datum="", iter_=0,
                   recept_origineel_naam="", recept_opgeslagen_naam="",
                   recept_geldig_tot="") -> int:
    """Slaat order op in PostgreSQL of werkt bij."""
    if not _DATABASE_URL:
        return 0
    conn = _pg_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM orders WHERE wc_order_id = %s", (wc_order_id,))
        row = cur.fetchone()
        if row:
            cur.execute("""UPDATE orders SET
                patient_id=COALESCE(%s,patient_id),
                voorschrijver_id=COALESCE(%s,voorschrijver_id),
                medicijn_id=COALESCE(%s,medicijn_id),
                bijgewerkt_op=NOW()
                WHERE wc_order_id=%s""",
                (patient_id, voorschrijver_id, medicijn_id, wc_order_id))
            conn.commit()
            return row[0]
        cur.execute("""INSERT INTO orders
            (wc_order_id, patient_id, voorschrijver_id, medicijn_id, hoeveelheid,
             recept_datum, iter, recept_origineel_naam, recept_opgeslagen_naam, recept_geldig_tot)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (wc_order_id, patient_id, voorschrijver_id, medicijn_id, hoeveelheid,
             recept_datum or None, iter_,
             recept_origineel_naam or None, recept_opgeslagen_naam or None,
             recept_geldig_tot or None))
        order_id = cur.fetchone()[0]
        conn.commit()
        return order_id
    finally:
        cur.close()
        conn.close()


def _wc_auth(wc_key: str, wc_secret: str) -> dict:
    """Auth headers voor WooCommerce REST API."""
    import base64 as _b64wc
    token = _b64wc.b64encode(f"{wc_key}:{wc_secret}".encode()).decode()
    return {"Accept": "application/json", "Authorization": f"Basic {token}", "User-Agent": "curl/7.68.0"}

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- HTTP Basic Auth ---
# Beveiligt de hele app achter een gebruikersnaam/wachtwoord, ingesteld via
# de Railway environment variables APP_USER en APP_PASS.
#
# BELANGRIJKE UITZONDERING: /api/wc-webhook moet publiek bereikbaar blijven.
# WooCommerce roept dit endpoint zelf automatisch aan bij elke order-wijziging
# en kan daarbij geen Basic Auth-header meesturen — met auth hierop zou de
# hele order-synchronisatie stilvallen.
#
# Is APP_USER of APP_PASS niet ingesteld (bijv. lokaal ontwikkelen), dan
# wordt er geen auth vereist — dat voorkomt dat je jezelf per ongeluk buitensluit
# tijdens lokaal testen.
@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    if request.url.path.startswith("/api/wc-webhook"):
        return await call_next(request)

    app_user = os.getenv("APP_USER", "")
    app_pass = os.getenv("APP_PASS", "")
    if not app_user or not app_pass:
        return await call_next(request)

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Basic "):
        toegestaan = False
        try:
            import base64 as _b64auth
            decoded = _b64auth.b64decode(auth_header[6:]).decode("utf-8")
            gebruiker, _, wachtwoord = decoded.partition(":")
            toegestaan = (gebruiker == app_user and wachtwoord == app_pass)
        except Exception:
            toegestaan = False
        if toegestaan:
            return await call_next(request)

    return Response(
        status_code=401,
        headers={"WWW-Authenticate": "Basic realm=\"Farmamed\""},
        content="Unauthorized",
    )

BASE_DIR = Path(__file__).parent
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-5"


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = BASE_DIR / "templates" / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.on_event("startup")
async def startup_event():
    if _DATABASE_URL:
        try:
            _conn = _pg_conn(); _cur = _conn.cursor()
            _cur.execute("ALTER TABLE voorschrijvers ADD COLUMN IF NOT EXISTS telefoon TEXT")
            _cur.execute("ALTER TABLE medicijnen ADD COLUMN IF NOT EXISTS article_code TEXT")
            # Permutatie-schema: canonieke naam (Farmasys, leidend), eenheid-type
            # (tube/tablet) en verpakkingsgrootte (gram per tube, of tabletten per
            # doosje) — samen de basis om D/ betrouwbaar naar een WooCommerce-
            # bestelhoeveelheid te vertalen zonder afronding.
            _cur.execute("ALTER TABLE medicijnen ADD COLUMN IF NOT EXISTS naam_canoniek TEXT")
            _cur.execute("ALTER TABLE medicijnen ADD COLUMN IF NOT EXISTS eenheid_type TEXT")
            _cur.execute("ALTER TABLE medicijnen ADD COLUMN IF NOT EXISTS verpakkingsgrootte INTEGER")
            _cur.execute("ALTER TABLE medicijnen ADD COLUMN IF NOT EXISTS wc_product_id INTEGER")
            # Coulance-korting: een vast bedrag per tube voor een klein aantal
            # klanten (historisch gegroeid, los van een specifiek medicijn).
            # Standaard 0 (geen korting) — alleen expliciet ingesteld voor de
            # klanten die dit krijgen.
            _cur.execute("ALTER TABLE patienten ADD COLUMN IF NOT EXISTS coulance_korting_bedrag NUMERIC DEFAULT 0")
            _cur.execute("""
                CREATE TABLE IF NOT EXISTS medicijn_hoeveelheden (
                    id SERIAL PRIMARY KEY,
                    medicijn_id INTEGER REFERENCES medicijnen(id) ON DELETE CASCADE,
                    hoeveelheid INTEGER NOT NULL,
                    UNIQUE(medicijn_id, hoeveelheid)
                )
            """)
            _cur.execute("""
                CREATE TABLE IF NOT EXISTS medicijn_gebruik_opties (
                    id SERIAL PRIMARY KEY,
                    medicijn_id INTEGER REFERENCES medicijnen(id) ON DELETE CASCADE,
                    code TEXT NOT NULL,
                    is_standaard BOOLEAN NOT NULL DEFAULT FALSE,
                    UNIQUE(medicijn_id, code)
                )
            """)
            _conn.commit()
            _cur.close(); _conn.close()
        except Exception as e:
            print(f"[startup] Schema-migratie fout: {e}")
        try:
            _backfill_smarthub_codes_naar_db()
        except Exception as e:
            print(f"[startup] Backfill SMARTHUB_MEDICIJNEN -> database fout: {e}")
    asyncio.create_task(_email_poller_loop())


def _backfill_smarthub_codes_naar_db():
    """
    Eenmalige (idempotente — vult alleen lege velden, dus veilig om bij elke
    opstart opnieuw te draaien) overzetting van de bestaande, hardgecodeerde
    SMARTHUB_MEDICIJNEN-codes naar medicijnen.article_code. Zo staat die data
    voortaan centraal in de database (en dus beheerbaar via
    sync_artikelcodes.py) in plaats van verstopt in Python-code.
    """
    from rapidfuzz import fuzz
    conn = _pg_conn(); cur = conn.cursor()
    try:
        cur.execute("SELECT id, naam_smarthub, naam_woocommerce, article_code FROM medicijnen")
        rijen = cur.fetchall()
        aangevuld = 0
        for mid, naam_sh, naam_wc, bestaande_code in rijen:
            if bestaande_code:
                continue  # al een code (bijv. al gesynchroniseerd vanuit Farmasys) -> nooit overschrijven
            naam_norm = _normaliseer_naam(naam_sh or naam_wc or "")
            if not naam_norm:
                continue
            beste_score, beste_code = 0, None
            for sleutel, artikel in SMARTHUB_MEDICIJNEN.items():
                score = max(
                    fuzz.token_set_ratio(naam_norm, _normaliseer_naam(sleutel)),
                    fuzz.partial_ratio(naam_norm, _normaliseer_naam(sleutel)),
                )
                if score > beste_score:
                    beste_score, beste_code = score, artikel["code"]
            if beste_score >= 70 and beste_code:
                cur.execute("UPDATE medicijnen SET article_code=%s WHERE id=%s AND (article_code IS NULL OR article_code='')", (beste_code, mid))
                aangevuld += 1
        conn.commit()
        if aangevuld:
            print(f"[startup] {aangevuld} article_code(s) overgezet vanuit SMARTHUB_MEDICIJNEN naar de database.")
    finally:
        cur.close()
        conn.close()


@app.post("/api/poll-emails")
async def poll_emails_nu():
    asyncio.create_task(_poll_eenmalig())
    return JSONResponse(content={"ok": True})


@app.post("/api/emails-volledig-herladen")
async def emails_volledig_herladen():
    """Wist de cache en haalt de volledige INBOX opnieuw op (synchroon)."""
    _init_email_db()
    conn = _sqlite3.connect(_DB_PAD)
    try:
        conn.execute("DELETE FROM emails")
        conn.commit()
    finally:
        conn.close()
    await _poll_eenmalig()
    aantal = len(_haal_emails_op_db(limit=1000))
    return JSONResponse(content={"ok": True, "aantal": aantal})


async def _poll_eenmalig():
    import base64 as _b64
    imap_server = os.getenv("IMAP_SERVER", "")
    imap_port   = int(os.getenv("IMAP_PORT", "993"))
    imap_user   = os.getenv("IMAP_USER", "")
    imap_pass   = os.getenv("IMAP_PASS", "")
    afgehandeld_map = "INBOX.Afgehandeld"
    if not all([imap_server, imap_user, imap_pass]):
        return
    try:
        bestaande = _haal_emails_op_db(limit=1000)
        verwerkt_status = {e["uid"]: e for e in bestaande if e.get("verwerkt")}

        conn = _imaplib.IMAP4_SSL(imap_server, imap_port)
        conn.login(imap_user, imap_pass)
        conn.select("INBOX")
        try:
            status, sel_data = conn.select(afgehandeld_map)
            print(f"[IMAP] Map '{afgehandeld_map}' select: {status} {sel_data}")
            if status != "OK":
                create_result = conn.create(afgehandeld_map)
                print(f"[IMAP] Map '{afgehandeld_map}' aanmaken: {create_result}")
            conn.select("INBOX")
        except Exception as e:
            print(f"[IMAP] Fout bij map check/aanmaken: {e}")

        status_uid, berichten = conn.uid("SEARCH", None, "ALL")
        uids = berichten[0].split()
        print(f"[Poll] Inbox bevat {len(uids)} e-mail(s), cache wordt gesynchroniseerd")

        huidige_uids = {u.decode() for u in uids}
        _verwijder_emails_niet_in(huidige_uids)

        for uid in uids:
            uid_str = uid.decode()
            if uid_str in verwerkt_status:
                continue
            try:
                _, data = conn.uid("FETCH", uid, "(RFC822)")
                msg = _email_lib.message_from_bytes(data[0][1])
                onderwerp = "".join(
                    part.decode(enc or "utf-8", errors="replace") if isinstance(part, bytes) else str(part)
                    for part, enc in _decode_header(msg.get("Subject", ""))
                )
                afz_raw = "".join(
                    part.decode(enc or "utf-8", errors="replace") if isinstance(part, bytes) else str(part)
                    for part, enc in _decode_header(msg.get("From", ""))
                )
                if "<" in afz_raw:
                    afz_naam = afz_raw.split("<")[0].strip().strip('"')
                    afz_email = afz_raw.split("<")[1].rstrip(">").strip()
                else:
                    afz_naam, afz_email = "", afz_raw.strip()
                body = ""
                bijlagen = []

                # Verzamel alle parts inclusief geneste doorgestuurde mails
                queue = [msg]
                alle_parts = []
                while queue:
                    huidig = queue.pop(0)
                    for part in huidig.walk():
                        alle_parts.append(part)
                        if part.get_content_type() == "message/rfc822":
                            geneste = part.get_payload()
                            if isinstance(geneste, list):
                                queue.extend(geneste)

                for part in alle_parts:
                    ct = part.get_content_type()
                    cd = " ".join(str(part.get("Content-Disposition", "")).split())
                    cid = part.get("Content-ID", "")
                    if ct == "message/rfc822":
                        continue
                    if ct == "text/plain" and "attachment" not in cd:
                        try:
                            tekst_deel = part.get_payload(decode=True)
                            if tekst_deel and not body:
                                body = tekst_deel.decode("utf-8", errors="replace")
                        except Exception:
                            pass
                    elif (ct == "application/pdf" or \
                          ct in ("image/jpeg", "image/png", "image/jpg", "image/gif")) and \
                         ("attachment" in cd or ct == "application/pdf"):
                        naam = part.get_filename() or f"bijlage_{len(bijlagen)+1}"
                        inhoud = part.get_payload(decode=True) or b""
                        if inhoud:
                            bijlagen.append({"naam": naam, "type": ct, "data": _b64.b64encode(inhoud).decode()})
                            print(f"[Poll] Bijlage gevonden: {naam} ({ct}, {len(inhoud)} bytes)")
                tekst = (onderwerp + " " + body).lower()
                if any(t in tekst for t in ["herhaalrecept", "herhaling", "iter"]):
                    email_type = "herhaalrecept"
                elif any(t in tekst for t in ["recept", "voorschrift", "medicijn", "bijlage"]) or bijlagen:
                    email_type = "nieuw_recept"
                else:
                    email_type = "overig"
                _sla_email_op({
                    "uid": uid_str, "onderwerp": onderwerp or "(geen onderwerp)",
                    "afzender": afz_email, "afzender_naam": afz_naam,
                    "datum": msg.get("Date", "")[:25], "body": body[:2000],
                    "bijlagen": bijlagen, "heeft_bijlage": bool(bijlagen),
                    "ongelezen": True, "type": email_type,
                })
                print(f"[Poll] OK: {onderwerp[:50]}")
            except Exception as e:
                print(f"[Poll] Fout {uid_str}: {e}")
        conn.logout()
    except Exception as e:
        print(f"[Poll] IMAP fout: {e}")


async def _email_poller_loop():
    interval = int(os.getenv("POLL_INTERVAL_SEC", "60"))
    print(f"[Poller] Gestart, interval: {interval}s")
    while True:
        await _poll_eenmalig()
        await asyncio.sleep(interval)


def _verplaats_email_imap(uid_str: str) -> bool:
    imap_server = os.getenv("IMAP_SERVER", "")
    imap_port   = int(os.getenv("IMAP_PORT", "993"))
    imap_user   = os.getenv("IMAP_USER", "")
    imap_pass   = os.getenv("IMAP_PASS", "")
    afgehandeld_map = "INBOX.Afgehandeld"
    if not all([imap_server, imap_user, imap_pass]):
        return False
    try:
        conn = _imaplib.IMAP4_SSL(imap_server, imap_port)
        conn.login(imap_user, imap_pass)
        conn.select("INBOX")
        try:
            status, sel_data = conn.select(afgehandeld_map)
            if status != "OK":
                conn.create(afgehandeld_map)
            conn.select("INBOX")
        except Exception:
            pass
        uid_bytes = uid_str.encode() if isinstance(uid_str, str) else uid_str
        result, data = conn.uid("COPY", uid_bytes, afgehandeld_map)
        print(f"[IMAP] COPY uid={uid_str} -> {afgehandeld_map}: {result} | server-respons: {data}")
        if result == "OK":
            conn.uid("STORE", uid_bytes, "+FLAGS", "(\\Deleted)")
            conn.expunge()
            conn.logout()
            print(f"[IMAP] Verplaatst: {uid_str}")
            return True
        _, check = conn.uid("SEARCH", None, f"UID {uid_str}")
        print(f"[IMAP] COPY mislukt. UID {uid_str} nog in INBOX: {check[0] if check else 'onbekend'}")
        conn.logout()
        return False
    except Exception as e:
        print(f"[IMAP] Fout: {e}")
        return False


def _verwijder_emails_niet_in(huidige_uids: set):
    """Verwijdert lokale e-mailrecords waarvan de UID niet meer in de huidige INBOX-set zit."""
    _init_email_db()
    conn = _sqlite3.connect(_DB_PAD)
    try:
        rows = conn.execute("SELECT uid FROM emails").fetchall()
        te_verwijderen = [r[0] for r in rows if r[0] not in huidige_uids]
        for uid in te_verwijderen:
            conn.execute("DELETE FROM emails WHERE uid = ?", (uid,))
        if te_verwijderen:
            conn.commit()
            print(f"[Poll] {len(te_verwijderen)} verouderde e-mail(s) uit cache verwijderd")
    finally:
        conn.close()


# === SMARTHUB INTEGRATIE ===
SMARTHUB_URL = os.getenv("SMARTHUB_URL", "https://smarthub-dev.smartmed.cloud/api/integrations/care/orders")
SMARTHUB_ORG = os.getenv("SMARTHUB_ORG", "SMARTMED")

# PRK-code mapping: WooCommerce productnaam → SmartHUB artikel
SMARTHUB_MEDICIJNEN = {
    "amitriptyline 5% in dimethylsulfoxide 50% saw": {"code": "90981758", "name": "AMITRIPTYLINE 5% IN DIMETHYLSULFOXIDE 50% SAW"},
    "baclofen 5% fenytoine 5% saw": {"code": "90062753", "name": "BACLOFEN 5%/ FENYTOINE 5% SAW CREME 30G"},
    "amitriptyline 10% in dimethylsulfoxide 50% saw": {"code": "90022256", "name": "AMITRIPTYLINE 10% IN DIMETHYLSULFOXIDE 50% SAW 30G"},
    "gabapentine 10% saw": {"code": "90900764", "name": "GABAPENTINE 10% SAW CREME 30G"},
    "clonidine 0,2% saw": {"code": "90860267", "name": "CLONIDINE 0,2% SAW CREME 30G"},
    "amitriptyline 10% saw": {"code": "90536291", "name": "AMITRIPTYLINE 10% SAW CREME 10%"},
    "ketamine 10% saw": {"code": "90576788", "name": "KETAMINE 10% SAW CREME 30G"},
    "tretinoine 0,05% saw": {"code": "90071960", "name": "TRETINOINE 0,05% SAW CREME"},
    "tretinoine 0,02% saw": {"code": "90031463", "name": "TRETINOINE 0,02% SAW CREME"},
    "proefbehandeling kopsky": {"code": "90143747", "name": "PROEFBEHANDELING PIJNSTILLENDE CREMES SAW KOPSKY"},
    "fenytoine 10% saw": {"code": "90698279", "name": "FENYTOINE 10% SAW CREME 30G"},
    "clonidine 0,1% saw": {"code": "90819770", "name": "CLONIDINE 0,1% SAW CREME 30G"},
    "amitriptyline 5% saw": {"code": "90495794", "name": "AMITRIPTYLINE 5% SAW CREME 30g"},
    "proefbehandeling v02": {"code": "90990965", "name": "PROEFBEHANDELING PIJNSTILLENDE CREMES SAW V02"},
    "fenytoine 5% saw": {"code": "90657782", "name": "FENYTOINE 5% SAW CREME 30G"},
    "baclofen 5% saw": {"code": "90617285", "name": "BACLOFEN 5% SAW CREME 30G"},
    "fenytoine 20% saw": {"code": "90738776", "name": "FENYTOINE 20% SAW CREME 30G"},
    "tretinoine 0,1% saw": {"code": "90112457", "name": "TRETINOINE 0,1% SAW CREME"},
    "ketamine 10% in saw": {"code": "90209096", "name": "KETAMINE 10% IN SAW CREME"},
    "amitriptilyne 5% in dsmo 50% saw": {"code": "90249593", "name": "AMITRIPTILYNE 5% IN DSMO 50% SAW"},
}

def _zoek_smarthub_artikel(productnaam: str) -> dict:
    """
    Matcht WooCommerce productnaam aan SmartHUB artikel (code + naam) voor
    het article-veld in de JSON.

    Volgorde: 1) database (medicijnen.article_code — leidend, hier komen
    zowel eigen SAW-crèmes als 'gewone' Z-index-artikelen zoals Tadalafil
    binnen via sync_artikelcodes.py), 2) de hardgecodeerde SMARTHUB_MEDICIJNEN-
    lijst (legacy, dekt alleen de eigen crèmes), 3) 'ONBEKEND' als niets matcht.
    """
    db_artikel = _zoek_article_code_en_naam(productnaam)
    if db_artikel:
        return db_artikel

    import unicodedata as _ud
    from rapidfuzz import fuzz

    def _norm(s):
        # Verwijder diakritische tekens (ï→i, é→e) en normaliseer
        s = _ud.normalize('NFD', s)
        s = ''.join(c for c in s if _ud.category(c) != 'Mn')
        return s.lower().strip()

    naam_norm = _norm(productnaam)

    # Exacte substring match eerst
    for sleutel, artikel in SMARTHUB_MEDICIJNEN.items():
        sleutel_norm = _norm(sleutel)
        if sleutel_norm in naam_norm or naam_norm in sleutel_norm:
            return artikel

    # Fuzzy match met genormaliseerde namen
    beste_score = 0
    beste_artikel = None
    for sleutel, artikel in SMARTHUB_MEDICIJNEN.items():
        sleutel_norm = _norm(sleutel)
        score = max(
            fuzz.token_set_ratio(naam_norm, sleutel_norm),
            fuzz.partial_ratio(naam_norm, sleutel_norm),
        )
        if score > beste_score:
            beste_score = score
            beste_artikel = artikel

    if beste_score >= 60:
        return beste_artikel

    return {"code": "ONBEKEND", "name": productnaam.upper()}


def _zoek_article_code_en_naam(productnaam: str) -> dict | None:
    """Zoekt {code, name} op in de database (medicijnen.article_code) via
    dezelfde fuzzy-matchlogica als _zoek_article_code. Geeft None terug als
    er geen code bekend is, zodat de aanroeper op de oude bronnen kan terugvallen."""
    from rapidfuzz import fuzz
    if not productnaam or not _DATABASE_URL:
        return None
    conn = _pg_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT article_code, naam_smarthub, naam_woocommerce FROM medicijnen WHERE article_code IS NOT NULL AND article_code <> ''")
        rijen = cur.fetchall()
        beste_score, beste = 0, None
        naam_norm = _normaliseer_naam(productnaam)
        for rij in rijen:
            for veld in [rij[1] or "", rij[2] or ""]:
                score = max(
                    fuzz.token_set_ratio(naam_norm, _normaliseer_naam(veld)),
                    fuzz.partial_ratio(naam_norm, _normaliseer_naam(veld)),
                )
                if score > beste_score:
                    beste_score = score
                    beste = {"code": rij[0], "name": (rij[1] or rij[2] or productnaam).upper()}
        return beste if beste_score >= 60 else None
    finally:
        cur.close()
        conn.close()


@app.post("/api/verstuur-smarthub")
async def verstuur_smarthub(request: Request):
    """Zet WooCommerce order om naar SmartHUB JSON en verstuurt naar SmartHUB."""
    body = await request.json()
    order_id = body.get("order_id")

    wc_url = os.getenv("WC_URL", "")
    wc_key = os.getenv("WC_KEY", "")
    wc_secret = os.getenv("WC_SECRET", "")

    if not order_id:
        return JSONResponse(content={"ok": False, "fout": "Geen order_id opgegeven"})

    # Haal order op uit WooCommerce
    resp = http_requests.get(
        f"{wc_url}/wp-json/wc/v3/orders/{order_id}",
        headers=_wc_auth(wc_key, wc_secret),
        timeout=10,
    )
    if resp.status_code != 200:
        return JSONResponse(content={"ok": False, "fout": f"Order niet gevonden: HTTP {resp.status_code}"})

    order = resp.json()
    billing = order.get("billing", {})
    meta = {m["key"]: m["value"] for m in order.get("meta_data", [])}
    items = order.get("line_items", [])
    shipping = order.get("shipping", {})

    # Gebruik shipping adres als beschikbaar, anders billing
    adres = shipping if shipping.get("address_1") else billing

    # Patiëntgegevens
    geboortedatum_raw = meta.get("billing_birth") or meta.get("_billing_birth") or ""
    # Converteer YYYY-MM-DD naar YYYY-MM-DD (al correct formaat voor SmartHUB)
    geboortedatum = geboortedatum_raw[:10] if geboortedatum_raw else ""

    bsn = (meta.get("billing_bsn") or meta.get("_billing_bsn") or meta.get("bsn") or "").strip()
    email = billing.get("email", "")
    telefoon = billing.get("phone", "")

    # Voorschrijver
    voorschrijver_naam = meta.get("voorschrijver") or "Onbekende voorschrijver"
    voorschrijver_agb = meta.get("agb_code") or ""

    # Medicijn → SmartHUB artikel
    productnaam = items[0]["name"] if items else ""
    artikel = _zoek_smarthub_artikel(productnaam)
    hoeveelheid = int(items[0].get("quantity", 1)) if items else 1

    # Recept datum (vandaag als niet gevuld)
    from datetime import datetime
    recept_datum = meta.get("recept_datum") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Bouw SmartHUB JSON
    smarthub_bericht = {
        "organizationCode": SMARTHUB_ORG,
        "code": str(order_id),
        "system": "Farmamed",
        "client": {
            "code": email or str(order_id),
            "initials": billing.get("first_name", "")[:1] + "." if billing.get("first_name") else "",
            "firstname": billing.get("first_name", ""),
            "surname": billing.get("last_name", ""),
            "name": f"{billing.get('first_name','')} {billing.get('last_name','')}".strip(),
            "dateOfBirth": geboortedatum,
            "socialSecurityNumber": bsn,
            "contacts": [c for c in [
                {"type": "Email", "value": email} if email else None,
                {"type": "Phone", "value": telefoon} if telefoon else None,
            ] if c],
        },
        "items": [
            {
                "article": {
                    "code": artikel["code"],
                    "name": artikel["name"],
                    "system": "CUSTOM.Article",
                },
                "quantity": {
                    "type": "Package",
                    "value": hoeveelheid,
                },
                "prescription": {
                    "code": str(order_id),
                    "date": recept_datum,
                    "prescriber": {
                        "code": voorschrijver_agb or "00000000",
                        "name": voorschrijver_naam,
                        "system": "NL_AGB",
                    },
                },
            }
        ],
        "delivery": {
            "type": "Shipper",
            "code": "SendCloud",
            "address": {
                "line": adres.get("address_1", ""),
                "postalCode": adres.get("postcode", "").replace(" ", ""),
                "city": adres.get("city", ""),
            },
        },
    }

    # Sla op in eigen Farmamed database
    try:
        patient_id = _zoek_of_maak_patient(billing, bsn, geboortedatum)
        voorschrijver_id = _zoek_of_maak_voorschrijver(voorschrijver_naam, voorschrijver_agb)
        medicijn_id = _zoek_medicijn_id(productnaam)
        _sla_order_op(
            wc_order_id=str(order_id),
            patient_id=patient_id,
            voorschrijver_id=voorschrijver_id,
            medicijn_id=medicijn_id,
            hoeveelheid=hoeveelheid,
            recept_datum=recept_datum,
        )
    except Exception as e:
        print(f"[SmartHUB] DB fout: {e}")

    # Verstuur naar SmartHUB
    try:
        smarthub_resp = http_requests.post(
            SMARTHUB_URL,
            json=smarthub_bericht,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=15,
        )
        ok = smarthub_resp.status_code == 200
        print(f"[SmartHUB] HTTP {smarthub_resp.status_code}: {smarthub_resp.text[:500]}")
        print(f"[SmartHUB] Bericht: {json.dumps(smarthub_bericht, ensure_ascii=False)[:500]}")
        # Markeer als verstuurd in DB
        if ok:
            try:
                conn = _pg_conn()
                cur = conn.cursor()
                cur.execute(
                    "UPDATE orders SET smarthub_verstuurd=1, smarthub_verstuurd_op=NOW(), smarthub_response=%s WHERE wc_order_id=%s",
                    (smarthub_resp.text[:500], str(order_id))
                )
                conn.commit()
                cur.close()
                conn.close()
            except Exception:
                pass
        return JSONResponse(content={
            "ok": ok,
            "status_code": smarthub_resp.status_code,
            "bericht": smarthub_bericht,
            "response": smarthub_resp.text[:500],
        })
    except Exception as e:
        return JSONResponse(content={"ok": False, "fout": str(e), "bericht": smarthub_bericht})


@app.get("/patient", response_class=HTMLResponse)
async def patient_pagina():
    html_path = BASE_DIR / "templates" / "patient.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/api/zoek-patient")
async def zoek_patient(q: str = ""):
    """Zoekt patiënten op email, naam, telefoon of BSN."""
    if not q or len(q) < 2:
        return JSONResponse(content={"patienten": []})
    if not _DATABASE_URL:
        return JSONResponse(content={"fout": "Geen database verbinding"})
    conn = _pg_conn()
    cur = conn.cursor()
    try:
        zoek = f"%{q.lower()}%"
        # Telefoonnummers kunnen in allerlei notaties staan (spaties, streepjes,
        # +31 vs 06). Vergelijk daarom OOK op alleen-cijfers, naast de normale
        # LIKE-vergelijking (die voor naam/email/BSN blijft werken zoals altijd).
        q_cijfers = _normaliseer_telefoon(q)
        cur.execute("""
            SELECT id, email, voornaam, achternaam, geboortedatum,
                   bsn, telefoon, adres, postcode, plaats
            FROM patienten
            WHERE LOWER(email) LIKE %s
               OR LOWER(achternaam) LIKE %s
               OR LOWER(voornaam || ' ' || achternaam) LIKE %s
               OR telefoon LIKE %s
               OR bsn LIKE %s
               OR (%s <> '' AND regexp_replace(COALESCE(telefoon,''), '[^0-9]', '', 'g') LIKE %s)
            ORDER BY achternaam, voornaam
            LIMIT 20
        """, (zoek, zoek, zoek, zoek, zoek, q_cijfers, f"%{q_cijfers}%"))
        cols = [d[0] for d in cur.description]
        rijen = [dict(zip(cols, r)) for r in cur.fetchall()]
        # Als geen resultaten in PostgreSQL, zoek ook in WooCommerce
        if not rijen and len(q) >= 3:
            wc_url = os.getenv("WC_URL",""); wc_key = os.getenv("WC_KEY",""); wc_secret = os.getenv("WC_SECRET","")
            try:
                resp = http_requests.get(
                    f"{wc_url}/wp-json/wc/v3/orders",
                    headers=_wc_auth(wc_key, wc_secret),
                    params={"search": q, "per_page": 10, "orderby": "date", "order": "desc"},
                    timeout=8,
                )
                if resp.status_code == 200:
                    gezien = set()
                    for o in (resp.json() if isinstance(resp.json(), list) else []):
                        billing = o.get("billing", {})
                        naam_wc = f"{billing.get('first_name','')} {billing.get('last_name','')}".strip()
                        sleutel = naam_wc.lower()
                        if sleutel in gezien or not naam_wc:
                            continue
                        gezien.add(sleutel)
                        rijen.append({
                            "id": None,
                            "email": billing.get("email", ""),
                            "voornaam": billing.get("first_name", ""),
                            "achternaam": billing.get("last_name", ""),
                            "geboortedatum": "",
                            "bsn": "",
                            "telefoon": billing.get("phone", ""),
                            "adres": billing.get("address_1", ""),
                            "postcode": billing.get("postcode", ""),
                            "plaats": billing.get("city", ""),
                            "bron": "woocommerce",
                        })
            except Exception as e:
                print(f"[zoek-patient] WC fout: {e}")

        return JSONResponse(content={"patienten": rijen})
    finally:
        cur.close()
        conn.close()


@app.get("/api/medicijn-permutaties")
async def get_medicijn_permutaties():
    """
    Geeft voor elk medicijn de toegestane D/-hoeveelheden en S/-gebruiksopties
    terug (uit sync_permutaties.py), zodat de vergelijkingstabel deze als
    dropdowns kan tonen i.p.v. vrije tekst. Klein datavolume (~20 medicijnen)
    — bedoeld om één keer opgehaald te worden, niet per medicijnwissel.
    """
    if not _DATABASE_URL:
        return JSONResponse(content={"medicijnen": []})
    conn = _pg_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT id, naam_smarthub, naam_woocommerce, naam_canoniek, eenheid_type, verpakkingsgrootte
            FROM medicijnen
        """)
        medicijnen_rijen = cur.fetchall()

        cur.execute("SELECT medicijn_id, hoeveelheid FROM medicijn_hoeveelheden ORDER BY hoeveelheid")
        hoeveelheden_per_medicijn = {}
        for mid, hoeveelheid in cur.fetchall():
            hoeveelheden_per_medicijn.setdefault(mid, []).append(hoeveelheid)

        cur.execute("SELECT medicijn_id, code, is_standaard FROM medicijn_gebruik_opties ORDER BY is_standaard DESC, code")
        gebruik_per_medicijn = {}
        for mid, code, is_standaard in cur.fetchall():
            gebruik_per_medicijn.setdefault(mid, []).append({"code": code, "is_standaard": is_standaard})

        resultaat = []
        for mid, naam_sh, naam_wc, naam_can, eenheid_type, verpakkingsgrootte in medicijnen_rijen:
            resultaat.append({
                "naam_smarthub": naam_sh, "naam_woocommerce": naam_wc, "naam_canoniek": naam_can,
                "eenheid_type": eenheid_type, "verpakkingsgrootte": verpakkingsgrootte,
                "hoeveelheden": hoeveelheden_per_medicijn.get(mid, []),
                "gebruik_opties": gebruik_per_medicijn.get(mid, []),
            })
        return JSONResponse(content={"medicijnen": resultaat})
    finally:
        cur.close()
        conn.close()


@app.get("/api/patient/{patient_id}")
async def get_patient(patient_id: int):
    """Haalt volledige patiëntdatasheet op inclusief alle orders."""
    if not _DATABASE_URL:
        return JSONResponse(content={"fout": "Geen database verbinding"})
    conn = _pg_conn()
    cur = conn.cursor()
    try:
        # Patiëntgegevens, inclusief de (laatst bekende) voorschrijver
        cur.execute("""
            SELECT p.id, p.email, p.voornaam, p.achternaam, p.geboortedatum,
                   p.bsn, p.telefoon, p.adres, p.postcode, p.plaats, p.wc_customer_id,
                   p.aangemaakt_op, p.bijgewerkt_op,
                   p.voorschrijver_id, p.voorschrijver_datum,
                   v.agb_code, v.naam, v.email, v.telefoon
            FROM patienten p
            LEFT JOIN voorschrijvers v ON v.id = p.voorschrijver_id
            WHERE p.id = %s
        """, (patient_id,))
        cols = ["id", "email", "voornaam", "achternaam", "geboortedatum",
                "bsn", "telefoon", "adres", "postcode", "plaats", "wc_customer_id",
                "aangemaakt_op", "bijgewerkt_op",
                "voorschrijver_id", "voorschrijver_datum",
                "voorschrijver_agb_code", "voorschrijver_naam", "voorschrijver_email", "voorschrijver_telefoon"]
        row = cur.fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"fout": "Patiënt niet gevonden"})
        patient = dict(zip(cols, row))
        # Datums naar string
        for k in ["aangemaakt_op", "bijgewerkt_op", "voorschrijver_datum"]:
            if patient[k]:
                patient[k] = str(patient[k])[:16]

        # Verrijk patiëntgegevens met meest recente WooCommerce order
        email = patient.get("email", "")
        if email:
            wc_url = os.getenv("WC_URL",""); wc_key = os.getenv("WC_KEY",""); wc_secret = os.getenv("WC_SECRET","")
            try:
                resp = http_requests.get(
                    f"{wc_url}/wp-json/wc/v3/orders",
                    headers=_wc_auth(wc_key, wc_secret),
                    params={"search": email, "per_page": 1, "orderby": "date", "order": "desc"},
                    timeout=8,
                )
                if resp.status_code == 200:
                    orders_wc = resp.json()
                    for o in (orders_wc if isinstance(orders_wc, list) else []):
                        billing = o.get("billing", {})
                        if billing.get("email", "").lower() != email.lower():
                            continue
                        meta = {m["key"]: m["value"] for m in o.get("meta_data", [])}
                        # Velden om aan te vullen vanuit WooCommerce
                        aanvullen = {
                            "voornaam":    billing.get("first_name", ""),
                            "achternaam":  billing.get("last_name", ""),
                            "telefoon":    billing.get("phone", ""),
                            "adres":       billing.get("address_1", ""),
                            "postcode":    billing.get("postcode", ""),
                            "plaats":      billing.get("city", ""),
                            "bsn":         (meta.get("billing_bsn") or meta.get("_billing_bsn") or meta.get("bsn") or "").strip(),
                            "geboortedatum": (meta.get("billing_birth") or meta.get("_billing_birth") or meta.get("_geboortedatum") or "").strip(),
                        }
                        updates = {}
                        for veld, wc_waarde in aanvullen.items():
                            if wc_waarde and not patient.get(veld):
                                patient[veld] = wc_waarde
                                updates[veld] = wc_waarde
                        # Sla aanvullingen op in PostgreSQL
                        if updates:
                            set_clause = ", ".join(f"{k}=%s" for k in updates)
                            vals = list(updates.values()) + [patient_id]
                            cur.execute(f"UPDATE patienten SET {set_clause}, bijgewerkt_op=NOW() WHERE id=%s", vals)
                            conn.commit()
                        break
            except Exception as e:
                print(f"[patient] WC verrijking fout: {e}")

        # Apotheek-velden uit PostgreSQL (smarthub, voorschrijver, recept)
        cur.execute("""
            SELECT o.wc_order_id, o.smarthub_verstuurd, o.smarthub_verstuurd_op,
                   o.recept_datum, o.recept_geldig_tot, o.recept_opgeslagen_naam,
                   m.naam_smarthub as medicijn_db,
                   v.naam as voorschrijver, v.agb_code
            FROM orders o
            LEFT JOIN medicijnen m ON m.id = o.medicijn_id
            LEFT JOIN voorschrijvers v ON v.id = o.voorschrijver_id
            WHERE o.patient_id = %s
        """, (patient_id,))
        cols_o = [d[0] for d in cur.description]
        db_orders = {}
        for r in cur.fetchall():
            d = dict(zip(cols_o, r))
            if d["smarthub_verstuurd_op"]:
                d["smarthub_verstuurd_op"] = str(d["smarthub_verstuurd_op"])[:16]
            db_orders[str(d["wc_order_id"])] = d

        # Orders live uit WooCommerce (status, bedrag, datum, medicijn)
        orders = []

        def _voeg_wc_orders_toe(ruwe_orders, verwacht_email):
            gezien_ids = {o["id"] for o in orders}
            for o in ruwe_orders:
                if o["id"] in gezien_ids:
                    continue
                billing = o.get("billing", {})
                if verwacht_email and billing.get("email", "").lower() != verwacht_email.lower():
                    continue
                meta = {m["key"]: m["value"] for m in o.get("meta_data", [])}
                items = o.get("line_items", [])
                wc_id = str(o["id"])
                db = db_orders.get(wc_id, {})
                # Voorkeur: de exact opgeslagen hoeveelheid (sinds we dit apart als
                # meta zijn gaan bewaren). Oudere orders hebben dit veld nog niet —
                # daarvoor blijft de terugrekening uit aantal tubes (1 tube = 30 gram)
                # als beste-schatting-terugval, al is die bij niet-ronde hoeveelheden
                # (bijv. 50 gram) niet exact.
                if meta.get("hoeveelheid"):
                    hoeveelheid_str = meta["hoeveelheid"]
                else:
                    aantal_tubes = float(items[0].get("quantity", 1)) if items else 1
                    gram_totaal = aantal_tubes * 30
                    hoeveelheid_str = f"{gram_totaal:g} gram"
                orders.append({
                    "id": o["id"],
                    "wc_order_id": wc_id,
                    "wc_status": o.get("status", ""),
                    "wc_datum": o.get("date_created", "")[:10],
                    "wc_totaal": o.get("total", ""),
                    "medicijn": items[0]["name"] if items else (db.get("medicijn_db") or ""),
                    "voorschrijver": db.get("voorschrijver") or meta.get("voorschrijver", ""),
                    "agb_code": db.get("agb_code") or meta.get("agb_code", ""),
                    "hoeveelheid": hoeveelheid_str,
                    "iter": meta.get("iter", ""),
                    "gebruiksaanwijzing": meta.get("gebruiksaanwijzing", ""),
                    "smarthub_verstuurd": db.get("smarthub_verstuurd", 0),
                    "smarthub_verstuurd_op": db.get("smarthub_verstuurd_op", ""),
                    "recept_datum": db.get("recept_datum", ""),
                    "recept_geldig_tot": db.get("recept_geldig_tot", ""),
                    "recept_opgeslagen_naam": db.get("recept_opgeslagen_naam", ""),
                })
                gezien_ids.add(o["id"])

        if email:
            try:
                resp_o = http_requests.get(
                    f"{wc_url}/wp-json/wc/v3/orders",
                    headers=_wc_auth(wc_key, wc_secret),
                    params={"search": email, "per_page": 20, "orderby": "date", "order": "desc"},
                    timeout=10,
                )
                resp_o.raise_for_status()
                _voeg_wc_orders_toe(resp_o.json() if isinstance(resp_o.json(), list) else [], email)
            except Exception as e:
                print(f"[patient] WC orders (email) fout: {e}")

        # Fallback: als het opgeslagen e-mailadres fout/verouderd is (bijv. per ongeluk
        # info@farmamed.nl, een tikfout, of een oud adres), vind orders alsnog via het
        # WooCommerce klant-ID — dat verandert niet, ook al klopt het e-mailadres niet meer.
        if not orders and patient.get("wc_customer_id"):
            try:
                resp_c = http_requests.get(
                    f"{wc_url}/wp-json/wc/v3/orders",
                    headers=_wc_auth(wc_key, wc_secret),
                    params={"customer": patient["wc_customer_id"], "per_page": 20, "orderby": "date", "order": "desc"},
                    timeout=10,
                )
                resp_c.raise_for_status()
                _voeg_wc_orders_toe(resp_c.json() if isinstance(resp_c.json(), list) else [], "")
            except Exception as e:
                print(f"[patient] WC orders (customer_id) fout: {e}")

        patient["orders"] = orders
        return JSONResponse(content=patient)
    finally:
        cur.close()
        conn.close()


@app.put("/api/patient/{patient_id}")
async def update_patient(patient_id: int, request: Request):
    """Werkt patiëntgegevens bij (BSN, voorschrijver email, etc.)."""
    if not _DATABASE_URL:
        return JSONResponse(content={"fout": "Geen database verbinding"})
    data = await request.json()
    conn = _pg_conn()
    cur = conn.cursor()
    try:
        toegestane_velden = ["bsn", "voornaam", "achternaam", "geboortedatum",
                             "telefoon", "email", "adres", "postcode", "plaats"]
        updates = {k: v for k, v in data.items() if k in toegestane_velden and v is not None}
        if not updates:
            return JSONResponse(content={"ok": False, "fout": "Geen geldige velden"})
        set_clause = ", ".join(f"{k}=%s" for k in updates)
        values = list(updates.values()) + [patient_id]
        cur.execute(f"UPDATE patienten SET {set_clause}, bijgewerkt_op=NOW() WHERE id=%s", values)
        conn.commit()
        return JSONResponse(content={"ok": True})
    except Exception as e:
        conn.rollback()
        import traceback
        print(f"[update_patient] Fout: {e}\n{traceback.format_exc()}")
        return JSONResponse(status_code=500, content={"ok": False, "fout": str(e)})
    finally:
        cur.close()
        conn.close()


@app.delete("/api/patient/{patient_id}")
async def delete_patient(patient_id: int, samenvoegen_met: int | None = None):
    """
    Verwijdert een patiëntrecord — bedoeld voor het opschonen van
    duplicaten (bijv. dezelfde persoon met twee verschillende
    @farmamed.nl-adressen).

    Standaard wordt de verwijdering GEWEIGERD zolang er nog orders aan deze
    patiënt gekoppeld zijn (om te voorkomen dat je per ongeluk orderhistorie
    "verweest" achterlaat). Geef `samenvoegen_met=<ander_patient_id>` mee om
    eerst alle gekoppelde orders naar dat andere (canonieke) patiëntrecord
    te verplaatsen, en dan pas te verwijderen.
    """
    if not _DATABASE_URL:
        return JSONResponse(content={"fout": "Geen database verbinding"})
    conn = _pg_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, email, voornaam, achternaam FROM patienten WHERE id=%s", (patient_id,))
        rij = cur.fetchone()
        if not rij:
            return JSONResponse(status_code=404, content={"ok": False, "fout": "Patiënt niet gevonden"})

        cur.execute("SELECT wc_order_id FROM orders WHERE patient_id=%s", (patient_id,))
        gekoppelde_orders = [r[0] for r in cur.fetchall()]

        if gekoppelde_orders and not samenvoegen_met:
            return JSONResponse(status_code=409, content={
                "ok": False,
                "fout": f"Kan niet verwijderen: {len(gekoppelde_orders)} order(s) zijn nog gekoppeld aan deze patiënt.",
                "gekoppelde_orders": gekoppelde_orders,
                "tip": "Geef samenvoegen_met=<ander_patient_id> mee om de orders eerst te verplaatsen.",
            })

        if gekoppelde_orders and samenvoegen_met:
            cur.execute("SELECT id FROM patienten WHERE id=%s", (samenvoegen_met,))
            if not cur.fetchone():
                return JSONResponse(status_code=400, content={"ok": False, "fout": f"samenvoegen_met patiënt #{samenvoegen_met} bestaat niet"})
            cur.execute("UPDATE orders SET patient_id=%s WHERE patient_id=%s", (samenvoegen_met, patient_id))

        cur.execute("DELETE FROM patienten WHERE id=%s", (patient_id,))
        conn.commit()
        return JSONResponse(content={
            "ok": True,
            "verwijderd": {"id": rij[0], "email": rij[1], "naam": f"{rij[2] or ''} {rij[3] or ''}".strip()},
            "orders_verplaatst": len(gekoppelde_orders) if samenvoegen_met else 0,
        })
    except Exception as e:
        conn.rollback()
        return JSONResponse(status_code=500, content={"ok": False, "fout": str(e)})
    finally:
        cur.close()
        conn.close()


@app.post("/api/wc-webhook")
async def wc_webhook(request: Request):
    """WooCommerce webhook — haalt volledige order op via API voor alle meta-velden."""
    try:
        order = await request.json()
    except Exception:
        return JSONResponse(content={"ok": False})
    order_id = order.get("id")
    if not order_id:
        return JSONResponse(content={"ok": False})
    wc_url = os.getenv("WC_URL",""); wc_key = os.getenv("WC_KEY",""); wc_secret = os.getenv("WC_SECRET","")
    try:
        resp = http_requests.get(f"{wc_url}/wp-json/wc/v3/orders/{order_id}", headers=_wc_auth(wc_key, wc_secret), timeout=8)
        if resp.status_code == 200:
            order = resp.json()
    except Exception:
        pass
    billing = order.get("billing", {})
    meta = {m["key"]: m["value"] for m in order.get("meta_data", [])}
    items = order.get("line_items", [])
    bsn = (meta.get("billing_bsn") or meta.get("_billing_bsn") or meta.get("bsn") or "").strip()
    geboortedatum = (meta.get("billing_birth") or meta.get("_billing_birth") or meta.get("_geboortedatum") or meta.get("geboortedatum") or "").strip()
    voorschrijver_naam = (meta.get("voorschrijver") or "").strip()
    agb_code = (meta.get("agb_code") or "").strip()
    productnaam = items[0]["name"] if items else ""
    print(f"[Webhook] #{order_id} email={billing.get('email','')} gbd={bool(geboortedatum)} bsn={bool(bsn)}")
    if not _DATABASE_URL:
        return JSONResponse(content={"ok": False})
    try:
        patient_id = _zoek_of_maak_patient(billing, bsn, geboortedatum)
        voorschrijver_id = _zoek_of_maak_voorschrijver(voorschrijver_naam, agb_code) if (voorschrijver_naam or agb_code) else None
        medicijn_id = _zoek_medicijn_id(productnaam) if productnaam else None
        _sla_order_op(wc_order_id=str(order_id), patient_id=patient_id, voorschrijver_id=voorschrijver_id, medicijn_id=medicijn_id, hoeveelheid=int(items[0].get("quantity",1)) if items else 1, recept_datum=meta.get("recept_datum",""))
        return JSONResponse(content={"ok": True, "patient_id": patient_id, "bsn": bool(bsn), "gbd": bool(geboortedatum)})
    except Exception as e:
        print(f"[Webhook] Fout: {e}")
        return JSONResponse(content={"ok": False, "fout": str(e)})


@app.get("/api/download-db")
async def download_db(request: Request):
    """Download de Farmamed database als SQLite bestand."""
    from fastapi.responses import FileResponse
    import hashlib, hmac
    # Simpele token check via query parameter
    token = request.query_params.get("token", "")
    verwacht = os.getenv("DB_DOWNLOAD_TOKEN", "farmamed2026")
    if not hmac.compare_digest(token, verwacht):
        return JSONResponse(status_code=403, content={"fout": "Geen toegang"})
    if not os.path.exists(_FARMAMED_DB):
        return JSONResponse(status_code=404, content={"fout": "Database niet gevonden"})
    return FileResponse(
        path=_FARMAMED_DB,
        filename="farmamed.db",
        media_type="application/octet-stream"
    )


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


@app.post("/api/order/{order_id}/kloon")
async def kloon_order_exact(order_id: int):
    """
    Kloont een bestaande WooCommerce-order 1-op-1: kopieert de EXACTE
    line_items (dus ook het exacte aantal tubes, rechtstreeks uit de
    bestelling zelf — geen enkele herberekening of afronding via een
    afgelezen 'hoeveelheid'-tekstveld) en billing/meta-gegevens naar een
    nieuwe order. Dit is de meest betrouwbare manier om te klonen: we
    vertrouwen simpelweg wat er al exact in WooCommerce staat.

    Verstuurt de order NIET automatisch naar de klant — dat blijft een
    aparte, bewuste stap (zie /api/order/{order_id}/verstuur-naar-klant).
    """
    wc_url = os.getenv("WC_URL", ""); wc_key = os.getenv("WC_KEY", ""); wc_secret = os.getenv("WC_SECRET", "")
    if not (wc_url and wc_key and wc_secret):
        return JSONResponse(status_code=500, content={"fout": "WooCommerce niet geconfigureerd"})
    try:
        resp = http_requests.get(
            f"{wc_url}/wp-json/wc/v3/orders/{order_id}",
            headers=_wc_auth(wc_key, wc_secret), timeout=15,
        )
        resp.raise_for_status()
        bron_order = resp.json()

        # Line items EXACT kopiëren — product_id + quantity, geen enkele herberekening
        nieuwe_line_items = [
            {"product_id": item["product_id"], "quantity": item["quantity"]}
            for item in bron_order.get("line_items", [])
        ]
        if not nieuwe_line_items:
            return JSONResponse(status_code=400, content={"fout": "Bronorder heeft geen line_items om te klonen"})

        # Meta_data kopiëren (voorschrijver, BSN, geboortedatum, iter, etc.), op een
        # paar technische systeemvelden na die per order opnieuw gezet moeten worden
        _uit_te_sluiten_meta = {"_created_via_farmamed", "oorsprong", "Oorsprong bestelling", "gekloond_van_order"}
        nieuwe_meta = [
            {"key": m["key"], "value": m["value"]}
            for m in bron_order.get("meta_data", [])
            if m["key"] not in _uit_te_sluiten_meta
        ]
        nieuwe_meta.append({"key": "oorsprong", "value": "kloon"})
        nieuwe_meta.append({"key": "Oorsprong bestelling", "value": "kloon"})
        nieuwe_meta.append({"key": "_created_via_farmamed", "value": "Farmamed_apotheek"})
        nieuwe_meta.append({"key": "gekloond_van_order", "value": str(order_id)})

        order_payload = {
            "status": "pending",
            "billing": bron_order.get("billing", {}),
            "shipping": bron_order.get("shipping") or bron_order.get("billing", {}),
            "line_items": nieuwe_line_items,
            "meta_data": nieuwe_meta,
            "customer_note": bron_order.get("customer_note", ""),
        }

        maak_resp = http_requests.post(
            f"{wc_url}/wp-json/wc/v3/orders",
            headers=_wc_auth(wc_key, wc_secret),
            json=order_payload, timeout=20,
        )
        maak_resp.raise_for_status()
        nieuwe_order = maak_resp.json()

        billing = nieuwe_order.get("billing", {})
        meta = {m["key"]: m["value"] for m in nieuwe_order.get("meta_data", [])}
        items = nieuwe_order.get("line_items", [])
        productnaam = items[0]["name"] if items else ""
        aantal_tubes = int(items[0].get("quantity", 1)) if items else 1

        # Lokaal ook vastleggen, net als bij een normale order-aanmaak
        patient_id = None
        if _DATABASE_URL:
            try:
                bsn = meta.get("bsn", "")
                geboortedatum = meta.get("geboortedatum", "")
                voorschrijver_naam = meta.get("voorschrijver", "")
                agb_code = meta.get("agb_code", "")

                patient_id = _zoek_of_maak_patient(billing, bsn, geboortedatum)
                voorschrijver_id = _zoek_of_maak_voorschrijver(voorschrijver_naam, agb_code) if (voorschrijver_naam or agb_code) else None
                medicijn_id = _zoek_medicijn_id(productnaam) if productnaam else None
                _sla_order_op(
                    wc_order_id=str(nieuwe_order["id"]), patient_id=patient_id,
                    voorschrijver_id=voorschrijver_id, medicijn_id=medicijn_id,
                    hoeveelheid=aantal_tubes, recept_datum=meta.get("recept_datum", ""),
                )
            except Exception as e:
                print(f"[kloon-order] DB fout: {e}")

        # EDIFACT verstrekkingsverzoek, met het EXACTE gekopieerde aantal tubes
        edifact = _genereer_edifact({
            "id": nieuwe_order["id"],
            "medicijn": productnaam,
            "hoeveelheid": f"{aantal_tubes * 30} gram",
            "patient_naam": f"{billing.get('first_name','')} {billing.get('last_name','')}".strip(),
        }, {"voorschrijver": meta.get("voorschrijver", ""), "recept_datum": meta.get("recept_datum", "")})

        return JSONResponse(content={
            "order_id": nieuwe_order["id"],
            "status": nieuwe_order["status"],
            "edifact": edifact,
            "gekloond_van": order_id,
        })
    except Exception as e:
        return JSONResponse(status_code=500, content={"fout": str(e)})


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

    voornaam, achternaam = _splits_voornaam_achternaam(data.get("patient_naam") or "")

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

    # Aantal eenheden bepalen uit hoeveelheid (bijv. "60 gram" bij een 30-grams
    # tube = 2, "56 tabletten" bij 28 per doosje = 2). Gebruikt de daadwerkelijke
    # verpakkingsgrootte van dit specifieke medicijn — een vaste aanname van
    # 30 zou bij tabletten of afwijkende verpakkingen (bijv. Proefbehandeling,
    # 25 gram) een verkeerd aantal opleveren.
    import re as _re2
    hoeveelheid_str = str(data.get("hoeveelheid") or "30")
    getal_match = _re2.search(r"[\d.]+", hoeveelheid_str.replace(",", "."))
    totaal = float(getal_match.group(0)) if getal_match else 30.0
    verpakkingsgrootte = _zoek_verpakkingsgrootte(medicijn) or 30.0
    aantal_tubes = max(1, round(totaal / verpakkingsgrootte))

    # Line items: medicijn + WMG tarief + eventuele coulance-korting
    line_items = []
    if product_id:
        line_items.append({"product_id": product_id, "quantity": aantal_tubes})
    line_items.append({"product_id": 1139, "quantity": 1})  # WMG tarief

    coulance_bedrag = _haal_coulance_korting(data.get("email") or "")
    if coulance_bedrag > 0:
        korting_totaal = round(coulance_bedrag * aantal_tubes, 2)
        line_items.append({
            "product_id": 1647,  # Coulanceregeling per tube
            "quantity": aantal_tubes,
            "subtotal": f"-{korting_totaal}",
            "total": f"-{korting_totaal}",
        })

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
            {"key": "hoeveelheid",      "value": data.get("hoeveelheid") or ""},
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
            headers=_wc_auth(wc_key, wc_secret),
            json=order_payload,
            timeout=20,
        )
        print(f"[maak-order] HTTP {response.status_code}: {response.text[:300]}")
        response.raise_for_status()
        order = response.json()

        # Sla patiënt + order op in PostgreSQL
        if _DATABASE_URL:
            try:
                _voornaam_lokaal, _achternaam_lokaal = _splits_voornaam_achternaam(data.get("patient_naam") or "")
                _bil = {"email":data.get("email",""),"first_name":_voornaam_lokaal,"last_name":_achternaam_lokaal,"phone":data.get("telefoon",""),"address_1":data.get("straat",""),"postcode":data.get("postcode",""),"city":data.get("woonplaats","")}
                _gbd = _nl_naar_amerikaans(data.get("geboortedatum",""))
                _pid = _zoek_of_maak_patient(_bil, data.get("bsn",""), _gbd)
                if _pid:
                    _vid = _zoek_of_maak_voorschrijver(data.get("voorschrijver",""), data.get("agb_code","")) if (data.get("voorschrijver") or data.get("agb_code")) else None
                    _sla_order_op(wc_order_id=str(order["id"]),patient_id=_pid,voorschrijver_id=_vid,medicijn_id=_zoek_medicijn_id(data.get("medicijn","")),hoeveelheid=1,recept_datum=data.get("recept_datum",""))
            except Exception as _e:
                print(f"[maak-order] DB fout: {_e}")

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


@app.post("/api/order/{order_id}/verstuur-naar-klant")
async def verstuur_order_naar_klant(order_id: int):
    """
    Zet de order op status 'processing' — dit is in een standaard
    WooCommerce-installatie de statusovergang die de ingebouwde
    'Order processing'-e-mail naar de klant activeert.

    LET OP: of dit daadwerkelijk een e-mail naar de klant stuurt, hangt af
    van de WooCommerce e-mailinstellingen (WooCommerce → Instellingen →
    E-mails → 'Bestelling in verwerking'). Dit endpoint kan dat vanuit hier
    niet garanderen of controleren — test dit zelf één keer om te
    bevestigen dat het écht een mail verstuurt in jullie configuratie.
    """
    wc_url = os.getenv("WC_URL", ""); wc_key = os.getenv("WC_KEY", ""); wc_secret = os.getenv("WC_SECRET", "")
    if not (wc_url and wc_key and wc_secret):
        return JSONResponse(status_code=500, content={"ok": False, "fout": "WooCommerce niet geconfigureerd"})
    try:
        resp = http_requests.put(
            f"{wc_url}/wp-json/wc/v3/orders/{order_id}",
            headers=_wc_auth(wc_key, wc_secret),
            json={"status": "processing"},
            timeout=15,
        )
        resp.raise_for_status()
        return JSONResponse(content={"ok": True, "status": resp.json().get("status")})
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "fout": str(e)})


# Volledige productcatalogus Farmamed
FARMAMED_PRODUCTEN = [
    (3430, "Tretinoïne crème 0.1% FNA - tegen huidveroudering (30 gram)"),
    (2760, "Tadalafil 5mg tabletten op recept (Sandoz)"),
    (2734, "Oxybutynine"),
    # 90-grams-varianten bewust verwijderd (26-8-2026): Tretinoïne 0,02% en
    # 0,05% bestaan alleen nog als 30-grams-product in WooCommerce. 90 gram
    # wordt voortaan gewoon quantity=3 op datzelfde 30-grams-product — geen
    # apart WC-product meer nodig, en voorkomt de dubbelzinnigheid die eerder
    # ontstond bij het koppelen van wc_product_id per medicijn.
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


def _zoek_wc_product_id_via_database(medicijn_naam: str) -> int | None:
    """
    Zoekt het WooCommerce product-ID rechtstreeks op via medicijnen.wc_product_id
    (gezet door sync_permutaties.py) — deterministisch, geen AI nodig. Geeft
    None terug als dit medicijn nog niet gekoppeld is, zodat de aanroeper op
    de AI-matching kan terugvallen.
    """
    from rapidfuzz import fuzz
    if not medicijn_naam or not _DATABASE_URL:
        return None
    conn = _pg_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT wc_product_id, naam_smarthub, naam_woocommerce, naam_canoniek FROM medicijnen WHERE wc_product_id IS NOT NULL")
        rijen = cur.fetchall()
        naam_norm = _normaliseer_naam(medicijn_naam)

        # Exacte match eerst (het normale geval: de dropdown levert de
        # canonieke naam al letterlijk) — geen fuzzy-gok nodig.
        for wc_id, naam_sh, naam_wc, naam_can in rijen:
            for veld in [naam_can, naam_wc, naam_sh]:
                if veld and _normaliseer_naam(veld) == naam_norm:
                    return wc_id

        beste_score, beste_id = 0, None
        for wc_id, naam_sh, naam_wc, naam_can in rijen:
            for veld in [naam_can, naam_wc, naam_sh]:
                if not veld:
                    continue
                score = max(
                    fuzz.token_set_ratio(naam_norm, _normaliseer_naam(veld)),
                    fuzz.partial_ratio(naam_norm, _normaliseer_naam(veld)),
                )
                if score > beste_score:
                    beste_score, beste_id = score, wc_id
        return beste_id if beste_score >= 80 else None  # hogere drempel: dit vervangt de AI, dus extra zeker zijn
    finally:
        cur.close()
        conn.close()


def _zoek_verpakkingsgrootte(medicijn_naam: str) -> int | None:
    """
    Zoekt de daadwerkelijke verpakkingsgrootte van een medicijn op (gram per
    tube, of tabletten per doosje — via sync_permutaties.py). Gebruikt om de
    WooCommerce-bestelhoeveelheid correct te berekenen, i.p.v. altijd blind
    'gram / 30' aan te nemen — dat klopt niet voor tabletten (bijv. 28 per
    doosje) of afwijkende verpakkingen (bijv. Proefbehandeling, 25 gram).
    Geeft None terug als dit medicijn nog niet gekoppeld is, zodat de
    aanroeper op de oude /30-aanname kan terugvallen.
    """
    from rapidfuzz import fuzz
    if not medicijn_naam or not _DATABASE_URL:
        return None
    conn = _pg_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT verpakkingsgrootte, naam_smarthub, naam_woocommerce, naam_canoniek FROM medicijnen WHERE verpakkingsgrootte IS NOT NULL")
        rijen = cur.fetchall()
        naam_norm = _normaliseer_naam(medicijn_naam)

        for grootte, naam_sh, naam_wc, naam_can in rijen:
            for veld in [naam_can, naam_wc, naam_sh]:
                if veld and _normaliseer_naam(veld) == naam_norm:
                    return grootte

        beste_score, beste_grootte = 0, None
        for grootte, naam_sh, naam_wc, naam_can in rijen:
            for veld in [naam_can, naam_wc, naam_sh]:
                if not veld:
                    continue
                score = max(
                    fuzz.token_set_ratio(naam_norm, _normaliseer_naam(veld)),
                    fuzz.partial_ratio(naam_norm, _normaliseer_naam(veld)),
                )
                if score > beste_score:
                    beste_score, beste_grootte = score, grootte
        return beste_grootte if beste_score >= 80 else None
    finally:
        cur.close()
        conn.close()


async def _zoek_product_id(medicijn_naam: str, wc_url: str, wc_key: str, wc_secret: str) -> int | None:
    """
    Zoek het beste WooCommerce product-ID op basis van medicijnnaam.

    Volgorde: 1) exacte database-koppeling (medicijnen.wc_product_id — gezet
    via sync_permutaties.py, deterministisch, geen gok nodig), 2) AI-matching
    op de FARMAMED_PRODUCTEN-lijst als terugval voor medicijnen die nog niet
    gekoppeld zijn. Dit maakt 'order aanmaken' voor gekoppelde medicijnen
    net zo betrouwbaar als klonen — geen AI-onzekerheid meer in de weg.
    """
    if not medicijn_naam:
        return None

    db_product_id = _zoek_wc_product_id_via_database(medicijn_naam)
    if db_product_id:
        return db_product_id

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
                headers=_wc_auth(wc_key, wc_secret),
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

        # Betaalstatus: Pay, Bank, Anders of Niet betaald
        betaal_methode = o.get("payment_method", "")
        datum_betaald = o.get("date_paid")
        bank_betaald = meta.get("_farmamed_bank_betaald", "") == "1"
        order_status_val = o.get("status", "")

        if order_status_val == "pending":
            betaal_status = "Niet betaald"
            betaal_type = "onbetaald"
        elif datum_betaald and o.get("transaction_id") and ("pay" in betaal_methode.lower() or "paynl" in betaal_methode.lower()):
            betaal_status = "Pay"
            betaal_type = "pay"
        elif bank_betaald:
            betaal_status = "Bank"
            betaal_type = "bank"
        else:
            betaal_status = "Anders"
            betaal_type = "anders"

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
    """MT940 parser: gaat door betalingen (0-350 euro) en zoekt per betaling een order.
    Stap 1: ordernummer in omschrijving (4 cijfers) → directe match
    Stap 2: achternaam fuzzy match + zelfde bedrag → naam match
    """
    import re as _re
    from rapidfuzz import fuzz

    inhoud = await bestand.read()
    tekst = inhoud.decode("utf-8", errors="replace")
    FARMAMED_AGB = "02009907"

    # --- Stap 1: parse MT940 betalingen ---
    betalingen = []
    for blok in _re.split(r":61:", tekst)[1:]:
        try:
            header = _re.match(r"(\d{6})(\d{4})?([CD])(\d+),(\d*)", blok)
            if not header or header.group(3) == "D":
                continue
            d = header.group(1)
            datum = f"20{d[:2]}-{d[2:4]}-{d[4:6]}"
            bedrag = float(f"{header.group(4)}.{header.group(5) or '00'}")
            if bedrag <= 0 or bedrag > 350:
                continue
            oms_match = _re.search(r":86:(.*?)(?=:6[12]:|:62|$)", blok, _re.DOTALL)
            if not oms_match:
                continue
            oms_raw = oms_match.group(1)
            if any(w in oms_raw for w in ["Pay.nl", "CLEARING", "Stichting Pay"]):
                continue

            naam = ""
            oms_naam = oms_raw.replace("/NA\r\nME/", "/NAME/").replace("/NA\nME/", "/NAME/")
            nm = _re.search(r"/NAME/([^/]+)", oms_naam)
            if nm:
                naam = _re.sub(r"\s+", " ", nm.group(1)).strip()
                naam = _re.sub(r"^(De heer|Mevr?\.?|Dhr\.?|Mw\.?)\s+", "", naam, flags=_re.IGNORECASE).strip()
                naam = _re.sub(r"\s+(cj|eo|e/o)\s*$", "", naam, flags=_re.IGNORECASE).strip()

            remi = ""
            rm = _re.search(r"/REMI/(.+?)(?=/EREF/|/CSID/|$)", oms_raw, _re.DOTALL)
            if rm:
                remi = _re.sub(r"\s+", " ", rm.group(1)).strip()

            # Zoek 4-cijferig ordernummer in omschrijving
            order_nr = ""
            if remi:
                rc = remi.replace(FARMAMED_AGB, "")
                rc_nospace = _re.sub(r"\s+", "", rc)
                nm2 = _re.search(
                    r"(?:ordernummer|ordernr|order|fact(?:uur)?|bestelling)[nr\.#]*(\d{3,5})",
                    rc_nospace, _re.IGNORECASE
                )
                if nm2:
                    order_nr = nm2.group(1)
                    if not (3 <= len(order_nr) <= 5):
                        order_nr = ""
                if not order_nr:
                    # Zoek los 4-cijferig getal dat een ordernummer kan zijn
                    for m in _re.finditer(r"\b(\d{4})\b", rc):
                        c = m.group(1)
                        if c != FARMAMED_AGB[:4] and not c.startswith("020"):
                            order_nr = c
                            break

            betalingen.append({
                "datum": datum, "bedrag": bedrag, "naam": naam,
                "remi": remi[:80], "order_nr": order_nr
            })
        except Exception:
            continue

    if not betalingen:
        return JSONResponse(content={"fout": "Geen betalingen gevonden (0-350 euro) in MT940 bestand"})

    wc_url = os.getenv("WC_URL", "")
    wc_key = os.getenv("WC_KEY", "")
    wc_secret = os.getenv("WC_SECRET", "")

    # --- Stap 2: haal pending orders op ---
    orders = []
    try:
        pagina = 1
        while True:
            resp = http_requests.get(
                f"{wc_url}/wp-json/wc/v3/orders",
                headers=_wc_auth(wc_key, wc_secret),
                params={"status": "pending", "per_page": 100, "page": pagina},
                timeout=20,
            )
            if resp.status_code != 200:
                break
            batch = resp.json()
            if not batch:
                break
            orders.extend(batch)
            if len(batch) < 100:
                break
            pagina += 1
    except Exception:
        pass

    # Bouw order-index: id → order info
    order_index = {}
    for o in orders:
        billing = o.get("billing", {})
        order_index[str(o["id"])] = {
            "id": o["id"],
            "klant_naam": f"{billing.get('first_name','')} {billing.get('last_name','')}".strip(),
            "achternaam": billing.get("last_name", ""),
            "totaal": o.get("total", "0"),
            "datum": o.get("date_created", "")[:10],
            "status": o.get("status", ""),
        }

    # --- Stap 3: match betalingen aan orders ---
    resultaten = []
    gebruikt_order_ids = set()

    for b in betalingen:
        match = None
        methode = "geen"

        # Stap 3a: ordernummer match
        if b.get("order_nr") and b["order_nr"] in order_index:
            o = order_index[b["order_nr"]]
            bedrag_klopt = abs(b["bedrag"] - float(o["totaal"])) < 0.02
            match = {"order": o, "methode": "ordernummer", "bedrag_klopt": bedrag_klopt, "score": 100}
            gebruikt_order_ids.add(b["order_nr"])

        # Stap 3b: naam fuzzy + bedrag match
        if not match and b.get("naam"):
            b_naam = _normaliseer_naam(b["naam"])
            beste_score = 0
            beste_order = None
            for oid, o in order_index.items():
                if oid in gebruikt_order_ids:
                    continue
                wc_naam = _normaliseer_naam(o["achternaam"])
                if not wc_naam:
                    continue
                score = fuzz.token_sort_ratio(b_naam, wc_naam)
                bedrag_klopt = abs(b["bedrag"] - float(o["totaal"])) < 0.02
                if score >= 70 and bedrag_klopt and score > beste_score:
                    beste_score = score
                    beste_order = o
            if beste_order:
                match = {"order": beste_order, "methode": "naam+bedrag", "bedrag_klopt": True, "score": beste_score}
                gebruikt_order_ids.add(str(beste_order["id"]))

        resultaten.append({
            "betaling": b,
            "match": match,
        })

    gematcht = sum(1 for r in resultaten if r["match"])

    return JSONResponse(content={
        "betalingen": len(betalingen),
        "orders_totaal": len(orders),
        "gematcht": gematcht,
        "resultaten": resultaten,
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
                headers=_wc_auth(wc_key, wc_secret),
                json={
                    "status": "processing",
                    "meta_data": [{"key": "_farmamed_bank_betaald", "value": "1"}]
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
                headers=_wc_auth(wc_key, wc_secret),
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
                headers=_wc_auth(wc_key, wc_secret),
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
- BELANGRIJK: e-mails naar recept@farmamed.nl bevatten vaak een e-mailhandtekening van de apotheekmedewerker zelf onderaan de mail (naam, telefoonnummer, e-mailadres, functietitel). Dit is NOOIT de patiënt — negeer deze handtekeninginformatie volledig, ook als het de meest recente naam/contactgegevens in de mail lijkt.
- LET OP: sommige Nederlandse achternamen zijn ook gewone woorden of lijken op medische termen (bijv. "Stuitje", "Bakker", "Visser"). Neem bij een kort/dubbelzinnig bericht toch aan dat het een achternaam is, ook bij een mogelijke tikfout — behandel het niet als lichaamsdeel/medische term. Een verkeerde gok wordt later gecorrigeerd via een kandidatenlijst; een gemiste naam (null) blokkeert het verzoek helemaal.

{{
  "patient_naam": "naam van de originele patient/arts",
  "patient_email": "e-mailadres van de originele afzender",
  "geboortedatum": "geboortedatum van de patiënt in DD-MM-YYYY formaat, of null als niet genoemd",
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

    # Verzamel alle e-mailadressen: Claude-analyse, afzender, en uit de body
    _eigen_domeinen = ["farmamed.nl", "apotheekwoerden.nl"]
    def _is_eigen_mail(e):
        if not e: return True
        return any(str(e).lower().strip().endswith("@" + d) for d in _eigen_domeinen)

    import re as _re_mail
    email_kandidaten = []
    for k in [email_data.get("patient_email"), afzender_email]:
        if k and "@" in str(k) and not _is_eigen_mail(k):
            e = str(k).strip().lower()
            if e not in email_kandidaten:
                email_kandidaten.append(e)
    # Zoek ook in de e-mailbody
    if email_body:
        for gevonden in _re_mail.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", email_body):
            if not _is_eigen_mail(gevonden) and gevonden.lower() not in email_kandidaten:
                email_kandidaten.append(gevonden.lower())

    zoek_email = email_kandidaten[0] if email_kandidaten else None
    zoek_naam = email_data.get("patient_naam") or afzender_naam

    # Zoek eerdere orders op e-mailadres
    beste_order = None
    beste_score = 0
    huidige_order_id = str(body.get("huidige_order_id", ""))

    try:
        # Zoek op e-mailadres — probeer alle gevonden adressen (afzender + body)
        for _email_kandidaat in email_kandidaten[:5]:  # max 5 pogingen
            if beste_order:
                break
            resp = http_requests.get(
                f"{wc_url}/wp-json/wc/v3/orders",
                headers=_wc_auth(wc_key, wc_secret),
                params={"search": _email_kandidaat, "per_page": 5, "orderby": "date", "order": "desc"},
                timeout=10,
            )
            orders = resp.json() if resp.status_code == 200 else []
            for order in (orders if isinstance(orders, list) else []):
                if str(order.get("id")) == huidige_order_id:
                    continue
                billing = order.get("billing", {})
                if billing.get("email", "").lower() == _email_kandidaat:
                    beste_order = order
                    beste_score = 100
                    zoek_email = _email_kandidaat
                    break

        # Fallback: lokale fuzzy matching op alle recente orders
        # Werkt ook bij spellingsfouten (Wilbrink vs Wilberink) en DDMMJJJJ geboortedatum
        if not beste_order:
            naam = zoek_naam
            import re as _re_gbd
            from rapidfuzz import fuzz

            # Normaliseer geboortedatum
            zoek_geboortedatum = email_data.get("geboortedatum") or ""
            if zoek_geboortedatum and _re_gbd.match(r"^\d{8}$", str(zoek_geboortedatum).strip()):
                g = str(zoek_geboortedatum).strip()
                zoek_geboortedatum = f"{g[0:2]}-{g[2:4]}-{g[4:8]}"
            # Zoek ook in e-mailbody
            if not zoek_geboortedatum and email_body:
                for pat in [r"(\d{2})[.\-/](\d{2})[.\-/](\d{4})", r"(\d{2})(\d{2})(\d{4})"]:
                    m_gbd = _re_gbd.search(pat, email_body)
                    if m_gbd:
                        d, mn, y = m_gbd.groups()
                        if 1 <= int(mn) <= 12 and 1 <= int(d) <= 31 and 1900 <= int(y) <= 2020:
                            zoek_geboortedatum = f"{d}-{mn}-{y}"
                            break

            zoek_achternaam = _normaliseer_naam(naam)
            zoek_woonplaats = (email_data.get("woonplaats") or "").lower().strip()

            if zoek_achternaam and len(zoek_achternaam) >= 3:
                # Haal laatste 500 orders lokaal op voor fuzzy matching
                alle_orders = []
                for _pag in range(1, 6):
                    _resp = http_requests.get(
                        f"{wc_url}/wp-json/wc/v3/orders",
                        headers=_wc_auth(wc_key, wc_secret),
                        params={"per_page": 100, "page": _pag, "orderby": "date", "order": "desc"},
                        timeout=15,
                    )
                    if _resp.status_code != 200:
                        break
                    _batch = _resp.json()
                    if not _batch:
                        break
                    alle_orders.extend(_batch)
                    if len(_batch) < 100:
                        break

                for order in alle_orders:
                    if str(order.get("id")) == huidige_order_id:
                        continue
                    billing = order.get("billing", {})
                    meta = {m["key"]: m["value"] for m in order.get("meta_data", [])}
                    wc_achternaam = _normaliseer_naam(billing.get("last_name", ""))
                    if not wc_achternaam:
                        continue

                    # Gebruik beste van token_sort en token_set — de laatste werkt
                    # beter voor samengestelde namen (Beemster vs Beemster-Venema)
                    naam_score = max(
                        fuzz.token_sort_ratio(zoek_achternaam, wc_achternaam),
                        fuzz.token_set_ratio(zoek_achternaam, wc_achternaam),
                    )
                    if naam_score < 55:
                        continue

                    wc_geboortedatum = _amerikaans_naar_nederlands(
                        meta.get("billing_birth") or meta.get("_billing_birth") or ""
                    )
                    geboortedatum_klopt = bool(
                        zoek_geboortedatum and wc_geboortedatum and
                        zoek_geboortedatum == wc_geboortedatum
                    )
                    wc_woonplaats = (billing.get("city") or "").lower().strip()
                    woonplaats_klopt = bool(
                        zoek_woonplaats and wc_woonplaats and
                        (zoek_woonplaats in wc_woonplaats or wc_woonplaats in zoek_woonplaats)
                    )

                    if geboortedatum_klopt and naam_score >= 70:
                        score = max(naam_score, 95)
                    elif geboortedatum_klopt and naam_score >= 55:
                        score = max(naam_score, 80)
                    elif woonplaats_klopt and naam_score >= 75:
                        score = max(naam_score, 82)
                    else:
                        score = naam_score

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

    _voornaam_edifact, _achternaam_edifact = _splits_voornaam_achternaam(gegevens.get("patient_naam") or "")
    order_payload = {
        "status": "processing",
        "billing": {
            "first_name": _voornaam_edifact,
            "last_name": _achternaam_edifact,
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
            headers=_wc_auth(wc_key, wc_secret),
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
        statussen = ["pending", "processing"] if toon_alle else ["processing"]
        for status in statussen:
            pagina = 1
            while True:
                response = http_requests.get(
                    f"{wc_url}/wp-json/wc/v3/orders",
                    headers=_wc_auth(wc_key, wc_secret),
                    params={"status": status, "per_page": 100, "orderby": "date", "order": "desc", "page": pagina},
                    timeout=20,
                )
                if response.status_code != 200:
                    print(f"[ORDERS] {status} HTTP {response.status_code}: {response.text[:200]}")
                    break
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

        orders.sort(key=lambda o: o["id"], reverse=True)
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
        response = http_requests.get(url, timeout=20, headers={"User-Agent": "curl/7.68.0"})
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
        resp = http_requests.get(recept_url, timeout=20, headers={"User-Agent": "curl/7.68.0"})
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
            headers=_wc_auth(wc_key, wc_secret),
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
            headers=_wc_auth(wc_key, wc_secret),
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


def _splits_voornaam_achternaam(volledige_naam: str) -> tuple[str, str]:
    """
    Splitst een volledige naam in voornaam/achternaam volgens de gangbare
    Nederlandse conventie: het LAATSTE woord (met eventuele aaneengesloten
    tussenvoegsels ervoor, zoals 'van der') is de achternaam, de rest is
    voornaam/tussennamen.

    Voorkomt dat bijv. 'Hermana Eliza Theodora Woensel' verkeerd wordt
    opgesplitst in voornaam='Hermana', achternaam='Eliza Theodora Woensel'
    (zoals de oude '.split(" ", 1)'-aanpak deed) — wat de patiëntherkenning
    bij een volgend recept ernstig verzwakt, omdat de opgeslagen achternaam
    dan drie extra woorden bevat die niets met de werkelijke achternaam te maken hebben.

    Herkent ook de 'Achternaam-eerst'-notatie met een komma (bijv.
    'H. Woensel, Hermana Eliza Theodora' — gangbaar op recepten/in het AIS):
    het deel VÓÓR de komma wordt dan als achternaam behandeld, de rest als voornaam.
    """
    naam = (volledige_naam or "").strip()
    if not naam:
        return "", ""

    if "," in naam:
        voor_komma, _, na_komma = naam.partition(",")
        achternaam = voor_komma.strip()
        voornaam = na_komma.strip()
        # Losse voorletters (bijv. 'H.') vóór de komma horen niet bij de
        # achternaam zelf — die horen bij de voornaam, als die daar nog niet staat.
        achternaam_woorden = achternaam.split()
        voorletter_woorden = [w for w in achternaam_woorden if len(w.rstrip('.')) <= 2]
        if voorletter_woorden and len(voorletter_woorden) < len(achternaam_woorden):
            achternaam = " ".join(w for w in achternaam_woorden if w not in voorletter_woorden)
            if not voornaam:
                voornaam = " ".join(voorletter_woorden)
        return voornaam, achternaam

    woorden = naam.split()
    if len(woorden) == 1:
        return woorden[0], ""
    _tussenvoegsels = {"van", "de", "den", "der", "het", "'t", "ten", "ter", "in", "op", "aan", "onder", "over", "bij"}
    achternaam_start = len(woorden) - 1
    while achternaam_start > 0 and woorden[achternaam_start - 1].lower() in _tussenvoegsels:
        achternaam_start -= 1
    voornaam = " ".join(woorden[:achternaam_start])
    achternaam = " ".join(woorden[achternaam_start:])
    return voornaam, achternaam


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

    # Afgekorte tussenvoegsels zoals "v.d" of "v/d" (= "van de") vóór de
    # voorletters-regex omzetten, anders wordt alleen de "v." herkend en
    # blijft er een losse "d" achter.
    naam = re.sub(r"\bv[./]d\b\.?", 'van de', naam)
    naam = re.sub(r"\bv[./]d[./]", 'van der ', naam)

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



async def _identificeer_patient(email="", naam="", telefoon="", bsn="", geboortedatum="", woonplaats=""):
    from rapidfuzz import fuzz
    _eigen = ["farmamed.nl", "apotheekwoerden.nl"]

    # Filter eigen handtekening (apotheek-medewerker) eruit vóórdat er iets mee gezocht wordt
    naam, email, telefoon = _filter_eigen_identiteit(naam, email, telefoon)

    email = (email or "").lower().strip()
    if any(email.endswith("@"+d) for d in _eigen): email = ""
    naam = (naam or "").strip()
    bsn = (bsn or "").strip()
    geboortedatum = (geboortedatum or "").strip()
    woonplaats = (woonplaats or "").strip()
    patient = None; match_methode = "nieuw"; match_score = 0; wc_orders = []
    _debug = {"email_input": email, "naam_input": naam, "bsn_input": bsn}
    wc_url = os.getenv("WC_URL",""); wc_key = os.getenv("WC_KEY",""); wc_secret = os.getenv("WC_SECRET","")

    if _DATABASE_URL:
        try:
            conn = _pg_conn(); cur = conn.cursor()
            if bsn and len(bsn)==9:
                cur.execute("SELECT id,email,voornaam,achternaam,geboortedatum,bsn,telefoon,adres,postcode,plaats,wc_customer_id FROM patienten WHERE bsn=%s",(bsn,))
                row=cur.fetchone()
                if row: cols=[d[0] for d in cur.description]; patient=dict(zip(cols,row)); match_methode="bsn"; match_score=100
            if not patient and email:
                cur.execute("SELECT id,email,voornaam,achternaam,geboortedatum,bsn,telefoon,adres,postcode,plaats,wc_customer_id FROM patienten WHERE LOWER(TRIM(email))=%s",(email,))
                row=cur.fetchone()
                if row: cols=[d[0] for d in cur.description]; patient=dict(zip(cols,row)); match_methode="email"; match_score=100
            if not patient and naam:
                zoek_ach=_normaliseer_naam(naam.split()[-1] if naam.split() else naam)
                fragment = zoek_ach[:4] if len(zoek_ach) >= 4 else zoek_ach
                cur.execute(
                    "SELECT id,email,voornaam,achternaam,geboortedatum,bsn,telefoon,adres,postcode,plaats,wc_customer_id "
                    "FROM patienten WHERE LENGTH(COALESCE(achternaam,''))>=3 AND achternaam ILIKE %s "
                    "ORDER BY achternaam, voornaam LIMIT 500",
                    (f"%{fragment}%",)
                )
                cols=[d[0] for d in cur.description]; rijen=[dict(zip(cols,r)) for r in cur.fetchall()]
                beste_score=0; beste_rij=None
                for rij in rijen:
                    wc_ach=_normaliseer_naam(rij.get("achternaam",""))
                    ns=max(fuzz.token_sort_ratio(zoek_ach,wc_ach),fuzz.token_set_ratio(zoek_ach,wc_ach))
                    if zoek_ach and zoek_ach in wc_ach: ns=max(ns,90)
                    if geboortedatum and rij.get("geboortedatum")==geboortedatum and ns>=60: ns=max(ns,95)
                    elif woonplaats and rij.get("plaats","").lower()==woonplaats.lower() and ns>=70: ns=max(ns,85)
                    if ns>beste_score: beste_score=ns; beste_rij=rij
                if beste_rij and beste_score>=75: patient=beste_rij; match_methode="naam"; match_score=beste_score
            cur.close(); conn.close()
        except Exception as e: print(f"[Identificeer] DB fout: {e}")

    if not patient and (email or naam):
        try:
            zoek_term = email or (max(naam.split(), key=len) if naam.split() else naam)
            resp=http_requests.get(f"{wc_url}/wp-json/wc/v3/orders",headers=_wc_auth(wc_key,wc_secret),params={"search":zoek_term,"per_page":20,"orderby":"date","order":"desc"},timeout=10)
            resp.raise_for_status()
            for o in (resp.json() if isinstance(resp.json(),list) else []):
                    billing=o.get("billing",{}); wc_email=billing.get("email","").lower()
                    meta={m["key"]:m["value"] for m in o.get("meta_data",[])}
                    if email and wc_email==email: match_methode="email_wc"; match_score=100
                    elif naam:
                        wc_naam=f"{billing.get('first_name','')} {billing.get('last_name','')}".strip()
                        score=fuzz.token_sort_ratio(_normaliseer_naam(naam),_normaliseer_naam(wc_naam))
                        if score<65: continue
                        match_methode="naam_wc"; match_score=score
                    else: continue
                    patient={"id":None,"email":wc_email,"voornaam":billing.get("first_name",""),"achternaam":billing.get("last_name",""),"geboortedatum":meta.get("billing_birth") or meta.get("_billing_birth") or "","bsn":meta.get("billing_bsn") or meta.get("_billing_bsn") or meta.get("bsn") or "","telefoon":billing.get("phone",""),"adres":billing.get("address_1",""),"postcode":billing.get("postcode",""),"plaats":billing.get("city",""),"wc_customer_id":str(o.get("customer_id",""))}
                    if _DATABASE_URL and wc_email:
                        try:
                            conn_db=_pg_conn(); cur_db=conn_db.cursor()
                            cur_db.execute("SELECT id,bsn,geboortedatum,telefoon,adres,postcode,plaats FROM patienten WHERE LOWER(TRIM(email))=%s",(wc_email,))
                            db_row=cur_db.fetchone()
                            if db_row:
                                db_cols=[d[0] for d in cur_db.description]; db_data=dict(zip(db_cols,db_row))
                                patient["id"]=db_data["id"]
                                for v in ["bsn","geboortedatum","telefoon","adres","postcode","plaats"]:
                                    if db_data.get(v): patient[v]=db_data[v]
                            cur_db.close(); conn_db.close()
                        except Exception as e: print(f"[Identificeer] DB verrijking fout: {e}")
                    break
        except Exception as e:
            import traceback; print(f"[Identificeer] WC fout: {e}\n{traceback.format_exc()}")

    if patient and patient.get("email"):
        _debug["order_zoek_email"] = patient["email"]
        _debug["patient_id"] = patient.get("id")
        _debug["wc_customer_id"] = patient.get("wc_customer_id")
        try:
            pid = patient.get("id")
            db_orders = {}
            if pid and _DATABASE_URL:
                try:
                    conn_o = _pg_conn(); cur_o = conn_o.cursor()
                    cur_o.execute("""
                        SELECT o.wc_order_id, o.smarthub_verstuurd, o.smarthub_verstuurd_op,
                               o.recept_datum, o.recept_geldig_tot, o.recept_opgeslagen_naam,
                               m.naam_smarthub as medicijn_db,
                               v.naam as voorschrijver, v.agb_code
                        FROM orders o
                        LEFT JOIN medicijnen m ON m.id = o.medicijn_id
                        LEFT JOIN voorschrijvers v ON v.id = o.voorschrijver_id
                        WHERE o.patient_id = %s
                    """, (pid,))
                    cols_o = [d[0] for d in cur_o.description]
                    for r in cur_o.fetchall():
                        d = dict(zip(cols_o, r))
                        db_orders[str(d["wc_order_id"])] = d
                    cur_o.close(); conn_o.close()
                    _debug["lokale_orders_gevonden"] = len(db_orders)
                except Exception as e:
                    print(f"[Identificeer] DB orders fout: {e}")
                    _debug["lokale_orders_fout"] = str(e)

            def _voeg_orders_toe(resultaten, label):
                _debug[f"wc_resultaten_{label}"] = len(resultaten)
                _debug[f"wc_emails_{label}"] = [ (o.get("billing",{}).get("email","") or "") for o in resultaten[:5] ]
                gezien_ids = {o["id"] for o in wc_orders}
                toegevoegd = 0
                for o in resultaten:
                    if o["id"] in gezien_ids:
                        continue
                    billing = o.get("billing", {})
                    if billing.get("email", "").lower() != patient["email"].lower():
                        continue
                    meta = {m["key"]: m["value"] for m in o.get("meta_data", [])}
                    items = o.get("line_items", [])
                    wc_id = str(o["id"])
                    db = db_orders.get(wc_id, {})
                    wc_orders.append({
                        "id": o["id"],
                        "datum": o.get("date_created", "")[:10],
                        "status": o.get("status", ""),
                        "medicijn": items[0]["name"] if items else (db.get("medicijn_db") or ""),
                        "totaal": o.get("total", ""),
                        "voorschrijver": db.get("voorschrijver") or meta.get("voorschrijver", ""),
                        "agb_code": db.get("agb_code") or meta.get("agb_code", ""),
                        "bsn": meta.get("billing_bsn") or meta.get("bsn") or "",
                    })
                    gezien_ids.add(o["id"])
                    toegevoegd += 1
                _debug[f"wc_toegevoegd_{label}"] = toegevoegd

            resp = http_requests.get(
                f"{wc_url}/wp-json/wc/v3/orders", headers=_wc_auth(wc_key, wc_secret),
                params={"search": patient["email"], "per_page": 20, "orderby": "date", "order": "desc"},
                timeout=10,
            )
            _debug["wc_search_status"] = resp.status_code
            resp.raise_for_status()
            _voeg_orders_toe(resp.json() if isinstance(resp.json(), list) else [], "search")

            # Fallback: zoeken via wc_customer_id (exacte match) als search-op-email niets
            # opleverde. Tekst-search op WooCommerce-orders is minder betrouwbaar dan
            # filteren op klant-ID.
            if not wc_orders and patient.get("wc_customer_id"):
                try:
                    resp2 = http_requests.get(
                        f"{wc_url}/wp-json/wc/v3/orders", headers=_wc_auth(wc_key, wc_secret),
                        params={"customer": patient["wc_customer_id"], "per_page": 20, "orderby": "date", "order": "desc"},
                        timeout=10,
                    )
                    _debug["wc_customer_status"] = resp2.status_code
                    resp2.raise_for_status()
                    _voeg_orders_toe(resp2.json() if isinstance(resp2.json(), list) else [], "customer")
                except Exception as e:
                    import traceback; print(f"[Identificeer] WC orders (customer_id) fout: {e}\n{traceback.format_exc()}")
                    _debug["wc_customer_fout"] = str(e)
        except Exception as e:
            import traceback; print(f"[Identificeer] WC orders fout: {e}\n{traceback.format_exc()}")
            _debug["wc_search_fout"] = str(e)

    verplichte=["voornaam","achternaam","email","geboortedatum","bsn","adres","postcode","plaats"]
    ontbrekende=[v for v in verplichte if not (patient or {}).get(v)] if patient else verplichte
    laatste_voors={}
    for o in wc_orders:
        if o.get("voorschrijver"): laatste_voors={"naam":o["voorschrijver"],"agb_code":o.get("agb_code","")}; break
    return {"gevonden":bool(patient),"patient":patient or {},"wc_orders":wc_orders,"ontbrekende_velden":ontbrekende,"match_methode":match_methode,"match_score":match_score,"suggestie":"kloon" if patient and wc_orders else "nieuw","laatste_voorschrijver":laatste_voors,"_debug":_debug}


@app.post("/api/identificeer-patient")
async def identificeer_patient_endpoint(request: Request):
    data = await request.json()
    resultaat = await _identificeer_patient(email=data.get("email",""),naam=data.get("naam",""),telefoon=data.get("telefoon",""),bsn=data.get("bsn",""),geboortedatum=data.get("geboortedatum",""),woonplaats=data.get("woonplaats",""))
    return JSONResponse(content=resultaat)


_MAAND_NAMEN = {
    "januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5, "juni": 6,
    "juli": 7, "augustus": 8, "september": 9, "oktober": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mrt": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "okt": 10, "nov": 11, "dec": 12,
}

def _normaliseer_geboortedatum(ruw: str) -> str | None:
    """
    Zet een geboortedatum in willekeurig herkenbaar formaat om naar een
    kanonieke 'DD-MM-YYYY'-string, zodat verschillende schrijfwijzen
    (04091937 / 4 september 1937 / 04-09-1937 / 4-9-1937 / 04/09/1937 /
    1937-09-04) onderling vergelijkbaar worden. Geeft None terug als het
    niet te interpreteren is.
    """
    import re as _re
    if not ruw:
        return None
    tekst = ruw.strip().lower()

    def _geldig(d, m_, j):
        return 1 <= d <= 31 and 1 <= m_ <= 12 and 1900 <= j <= 2099

    # "4 september 1937" / "4 sept 1937"
    m = _re.match(r"^(\d{1,2})\s+([a-zé]+)\s+(\d{4})$", tekst)
    if m:
        dag, maand_naam, jaar = m.groups()
        maand = _MAAND_NAMEN.get(maand_naam)
        if maand:
            try:
                d, j = int(dag), int(jaar)
                if _geldig(d, maand, j):
                    return f"{d:02d}-{maand:02d}-{j:04d}"
            except ValueError:
                pass
        return None

    # "4-9-1937" / "04-09-1937" / "4/9/1937" / "04.09.1937" (DD-MM-YYYY, flexibele scheiding en voorloopnullen)
    m = _re.match(r"^(\d{1,2})[\-/.](\d{1,2})[\-/.](\d{4})$", tekst)
    if m:
        dag, maand, jaar = m.groups()
        try:
            d, m_, j = int(dag), int(maand), int(jaar)
            if _geldig(d, m_, j):
                return f"{d:02d}-{m_:02d}-{j:04d}"
        except ValueError:
            pass
        return None

    # "1937-09-04" (ISO-achtig YYYY-MM-DD)
    m = _re.match(r"^(\d{4})[\-/.](\d{1,2})[\-/.](\d{1,2})$", tekst)
    if m:
        jaar, maand, dag = m.groups()
        try:
            d, m_, j = int(dag), int(maand), int(jaar)
            if _geldig(d, m_, j):
                return f"{d:02d}-{m_:02d}-{j:04d}"
        except ValueError:
            pass
        return None

    # Puur cijfers zonder scheidingstekens: "04091937" (DDMMYYYY, met YYYYMMDD als fallback)
    cijfers = _re.sub(r"[^0-9]", "", tekst)
    if len(cijfers) == 8:
        dag, maand, jaar = cijfers[0:2], cijfers[2:4], cijfers[4:8]
        try:
            d, m_, j = int(dag), int(maand), int(jaar)
            if _geldig(d, m_, j):
                return f"{d:02d}-{m_:02d}-{j:04d}"
        except ValueError:
            pass
        jaar2, maand2, dag2 = cijfers[0:4], cijfers[4:6], cijfers[6:8]
        try:
            d, m_, j = int(dag2), int(maand2), int(jaar2)
            if _geldig(d, m_, j):
                return f"{d:02d}-{m_:02d}-{j:04d}"
        except ValueError:
            pass
    return None

def _normaliseer_telefoon(ruw: str) -> str:
    """
    Normaliseert een telefoonnummer naar alleen cijfers, met +31/0031
    omgezet naar een leidende 0 (Nederlandse notatie), zodat '06 12345678',
    '0612345678' en '+31612345678' allemaal hetzelfde normaliseren.
    """
    import re as _re
    if not ruw:
        return ""
    cijfers = _re.sub(r"[^0-9]", "", ruw)
    if cijfers.startswith("0031"):
        cijfers = "0" + cijfers[4:]
    elif cijfers.startswith("31") and len(cijfers) in (10, 11):
        cijfers = "0" + cijfers[2:]
    return cijfers


# Eigen (apotheek-medewerker) handtekeninginfo — deze mag NOOIT als patiëntgegeven
# worden gebruikt bij het zoeken naar patiënten, ook al staat het letterlijk in de
# e-mail (bijv. onderaan een handtekening bij mails naar recept@farmamed.nl).
# Configureerbaar via env vars (komma-gescheiden voor meerdere medewerkers), met
# de bekende waarden als standaard zodat het meteen werkt.
_EIGEN_NAMEN = [n.strip() for n in os.getenv("EIGEN_SIGNATURE_NAMEN", "Gijs de Rooij").split(",") if n.strip()]
_EIGEN_EMAILS = [e.strip().lower() for e in os.getenv("EIGEN_SIGNATURE_EMAILS", "gijs@gijsderooij.com").split(",") if e.strip()]
_EIGEN_TELEFOONS = [_normaliseer_telefoon(t) for t in os.getenv("EIGEN_SIGNATURE_TELEFOONS", "0622791979").split(",") if t.strip()]

def _filter_eigen_identiteit(naam="", email="", telefoon=""):
    """
    Verwijdert de eigen (apotheek-)handtekening uit geëxtraheerde 'patiënt'-gegevens,
    zodat een e-mailhandtekening (naam, e-mail, telefoon van de apotheker/medewerker
    zelf) nooit wordt gebruikt om naar patiënten te zoeken.
    """
    from rapidfuzz import fuzz
    naam_out, email_out, tel_out = naam or "", email or "", telefoon or ""

    if email_out and email_out.lower().strip() in _EIGEN_EMAILS:
        email_out = ""

    if tel_out and _normaliseer_telefoon(tel_out) in _EIGEN_TELEFOONS:
        tel_out = ""

    if naam_out:
        naam_norm = _normaliseer_naam(naam_out)
        for eigen in _EIGEN_NAMEN:
            if fuzz.token_sort_ratio(naam_norm, _normaliseer_naam(eigen)) >= 90:
                naam_out = ""
                break

    return naam_out, email_out, tel_out


async def _zoek_patient_kandidaten(email="", naam="", telefoon="", bsn="", geboortedatum="", woonplaats="", medicijn="", max_kandidaten=8, _debug=None):
    """
    Zoekt ALLE plausibele patiënten op basis van de beschikbare informatie
    (net als de zoekbalk op de patiëntenpagina), in plaats van er automatisch
    één te kiezen. Bij veelvoorkomende namen of dubbelzinnige plaatsnamen
    (bijv. 'Jansen', of 'Elst' dat ook in 'Helst' voorkomt) kunnen er dus
    meerdere kandidaten teruskomen — de gebruiker kiest vervolgens de juiste.

    Matching-signalen en gewicht:
      - email (uniek)        -> score 100
      - bsn (9 cijfers)       -> score 100
      - telefoon (genorm.)    -> score 95
      - naam (fuzzy)          -> score 60-90, geboosted door geboortedatum/woonplaats
      - woonplaats            -> extra boost bovenop naam-match
      - medicijn              -> extra boost als een eerdere order dit medicijn bevat
    """
    import re as _re
    from rapidfuzz import fuzz
    _eigen = ["farmamed.nl", "apotheekwoerden.nl"]

    naam_ruw, email_ruw, telefoon_ruw = naam, email, telefoon
    # Filter eigen handtekening (apotheek-medewerker) eruit vóórdat er iets mee gezocht wordt
    naam, email, telefoon = _filter_eigen_identiteit(naam, email, telefoon)

    email = (email or "").lower().strip()
    if any(email.endswith("@"+d) for d in _eigen): email = ""
    naam = (naam or "").strip()
    bsn = _re.sub(r"[^0-9]", "", (bsn or ""))
    telefoon_clean = _normaliseer_telefoon(telefoon or "")
    geboortedatum_norm = _normaliseer_geboortedatum(geboortedatum or "")
    woonplaats = (woonplaats or "").strip()
    medicijn = (medicijn or "").strip()

    if _debug is not None:
        _debug["input_ruw"] = {"naam": naam_ruw, "email": email_ruw, "telefoon": telefoon_ruw, "bsn": bsn, "geboortedatum": geboortedatum, "woonplaats": woonplaats, "medicijn": medicijn}
        _debug["na_handtekening_filter"] = {"naam": naam, "email": email, "telefoon": telefoon}
        _debug["na_normalisatie"] = {"email": email, "naam": naam, "bsn": bsn, "telefoon_clean": telefoon_clean, "geboortedatum_norm": geboortedatum_norm, "woonplaats": woonplaats}

    if not _DATABASE_URL:
        if _debug is not None: _debug["fout"] = "geen _DATABASE_URL"
        return []

    kolommen = "id,email,voornaam,achternaam,geboortedatum,bsn,telefoon,adres,postcode,plaats,wc_customer_id"
    kandidaten = {}  # patient_id -> dict met match_methode/match_score

    def _kandidaat_toevoegen(rij, methode, score):
        bestaand = kandidaten.get(rij["id"])
        if not bestaand or score > bestaand["match_score"]:
            kandidaten[rij["id"]] = {**rij, "match_methode": methode, "match_score": score}

    conn = _pg_conn(); cur = conn.cursor()
    try:
        # 1. Exacte BSN-match
        if bsn and len(bsn) == 9:
            cur.execute(f"SELECT {kolommen} FROM patienten WHERE bsn=%s", (bsn,))
            cols = [d[0] for d in cur.description]
            for r in cur.fetchall():
                _kandidaat_toevoegen(dict(zip(cols, r)), "bsn", 100)

        # 2. Exacte e-mailmatch
        if email:
            cur.execute(f"SELECT {kolommen} FROM patienten WHERE LOWER(TRIM(email))=%s", (email,))
            cols = [d[0] for d in cur.description]
            for r in cur.fetchall():
                _kandidaat_toevoegen(dict(zip(cols, r)), "email", 100)

        # 3. Exacte telefoonmatch (genormaliseerd, alleen cijfers)
        if telefoon_clean and len(telefoon_clean) >= 8:
            cur.execute(
                f"SELECT {kolommen} FROM patienten WHERE regexp_replace(COALESCE(telefoon,''), '[^0-9]', '', 'g') = %s",
                (telefoon_clean,)
            )
            cols = [d[0] for d in cur.description]
            for r in cur.fetchall():
                _kandidaat_toevoegen(dict(zip(cols, r)), "telefoon", 95)

        # 4. Fuzzy naam-match — hier bewust MEERDERE resultaten toegestaan
        if naam:
            # Splits de naam in losse woorden i.p.v. blind op het LAATSTE woord te
            # vertrouwen als achternaam. Bij een compacte/aaneengesloten brontekst
            # zonder duidelijke scheiding (bijv. "JG van de Kraats 20011949
            # Amitriptyline 10% 5 tubes Soest 0642171184") kan de AI-extractie
            # net iets te veel of te weinig meepakken in het naam-veld, waardoor
            # het laatste woord niet meer de werkelijke achternaam is.
            #
            # LET OP: een komma betekent 'Achternaam-eerst'-notatie (bijv.
            # "H. Woensel, Hermana Eliza Theodora" — gangbaar op recepten/in
            # het AIS). In dat geval is het deel VÓÓR de komma de achternaam
            # met volledig vertrouwen, niet het laatste woord van de hele string
            # (dat zou dan juist een voornaam zijn, met averechts effect).
            _tussenvoegsels = {"van", "de", "den", "der", "het", "'t", "ten", "ter", "in"}

            def _filter_naamwoorden(tekst):
                resultaat = []
                for w in _re.split(r"\s+", (tekst or "").strip()):
                    if not w:
                        continue
                    w_schoon = _re.sub(r"[^a-zA-ZÀ-ÿ\-]", "", w)
                    if len(w_schoon) < 3:
                        continue  # voorletters/te kort (bijv. 'JG')
                    if w_schoon.lower() in _tussenvoegsels:
                        continue
                    if w_schoon.isupper() and len(w_schoon) <= 3:
                        continue  # vermoedelijk initialen
                    genorm = _normaliseer_naam(w_schoon)
                    if genorm:
                        resultaat.append(genorm)
                return resultaat

            if "," in naam:
                _voor_komma, _, _na_komma = naam.partition(",")
                vertrouwde_woorden = _filter_naamwoorden(_voor_komma)
                overige_woorden = _filter_naamwoorden(_na_komma)
            else:
                _alle_woorden = _filter_naamwoorden(naam)
                # Zonder komma: gangbare 'Voornaam(en) Achternaam'-volgorde —
                # alleen het LAATSTE woord krijgt vol vertrouwen.
                vertrouwde_woorden = _alle_woorden[-1:] if _alle_woorden else []
                overige_woorden = _alle_woorden[:-1]

            kandidaat_woorden = list(dict.fromkeys(vertrouwde_woorden + overige_woorden))  # dedupe, vertrouwde eerst
            if not kandidaat_woorden and naam.split():
                kandidaat_woorden = [_normaliseer_naam(naam.split()[-1])]
                vertrouwde_woorden = list(kandidaat_woorden)

            fragmenten = list({w[:4] if len(w) >= 4 else w for w in kandidaat_woorden if w})
            if _debug is not None:
                _debug["naam_kandidaat_woorden"] = kandidaat_woorden
                _debug["naam_vertrouwde_woorden"] = vertrouwde_woorden
                _debug["naam_sql_fragmenten"] = fragmenten
            if fragmenten:
                voorwaarden = " OR ".join(["achternaam ILIKE %s"] * len(fragmenten))
                params = tuple(f"%{f}%" for f in fragmenten)
                cur.execute(
                    f"SELECT {kolommen} FROM patienten WHERE LENGTH(COALESCE(achternaam,''))>=2 "
                    f"AND ({voorwaarden}) ORDER BY achternaam, voornaam LIMIT 500",
                    params
                )
                cols = [d[0] for d in cur.description]
                sql_rijen = cur.fetchall()
                if _debug is not None:
                    _debug["naam_sql_rijen_gevonden"] = len(sql_rijen)
                for r in sql_rijen:
                    rij = dict(zip(cols, r))
                    wc_ach = _normaliseer_naam(rij.get("achternaam", ""))
                    score = 0
                    for zoek_ach in kandidaat_woorden:
                        s = max(
                            fuzz.token_sort_ratio(zoek_ach, wc_ach),
                            fuzz.token_set_ratio(zoek_ach, wc_ach),
                        )
                        if zoek_ach and zoek_ach in wc_ach:
                            s = max(s, 88)
                        # Positie-gewicht: bij 'Voornaam(en) Achternaam' (de
                        # gangbare volgorde) is het LAATSTE woord verreweg het
                        # meest waarschijnlijk de echte achternaam. Eerdere
                        # woorden (voornaam/tussennamen) krijgen een korting,
                        # zodat een toevallige klank-overeenkomst met een
                        # voornaam (bijv. 'Hermana' ~ 'Hermans') niet de
                        # echte achternaam (bijv. 'Woensel') kan overstemmen.
                        # Positie-/notatie-gewicht: bij 'Voornaam(en) Achternaam'
                        # is het LAATSTE woord het meest waarschijnlijk de
                        # achternaam; bij een komma ('Achternaam, Voornamen')
                        # is dat juist het deel VÓÓR de komma. Woorden buiten
                        # deze 'vertrouwde' groep krijgen een korting, zodat
                        # een toevallige klank-overeenkomst met een voornaam
                        # (bijv. 'Hermana' ~ 'Hermans') niet de echte
                        # achternaam (bijv. 'Woensel') kan overstemmen.
                        if zoek_ach not in vertrouwde_woorden:
                            s = max(0, s - 20)
                        score = max(score, s)
                    if geboortedatum_norm and _normaliseer_geboortedatum(rij.get("geboortedatum") or "") == geboortedatum_norm:
                        score = max(score, 93)
                    if woonplaats and (rij.get("plaats") or "").lower() == woonplaats.lower():
                        score = max(score, 80)
                    if score >= 60:
                        _kandidaat_toevoegen(rij, "naam", score)
    except Exception as e:
        import traceback
        print(f"[KandidatenZoek] SQL fout: {e}\n{traceback.format_exc()}")
        if _debug is not None:
            _debug["sql_fout"] = str(e)
    finally:
        cur.close(); conn.close()

    if _debug is not None:
        _debug["kandidaten_na_db_stap"] = len(kandidaten)

    # 4b. Medicijn als extra score-signaal: als een kandidaat een eerdere order
    # heeft met een medicijn dat overeenkomt met wat er in de e-mail/het recept
    # staat, is dat extra bevestiging dat het de juiste patiënt is.
    if medicijn and kandidaten:
        lokale_ids = [pid for pid in kandidaten if pid is not None]
        if lokale_ids:
            try:
                conn2 = _pg_conn(); cur2 = conn2.cursor()
                try:
                    cur2.execute(
                        """SELECT o.patient_id, m.naam_smarthub, m.naam_woocommerce
                           FROM orders o JOIN medicijnen m ON m.id = o.medicijn_id
                           WHERE o.patient_id = ANY(%s)""",
                        (lokale_ids,)
                    )
                    medicijn_norm = _normaliseer_medicijn(medicijn)
                    for pid, naam_sh, naam_wc in cur2.fetchall():
                        beste = 0
                        for veld in (naam_sh, naam_wc):
                            if veld:
                                beste = max(beste, fuzz.token_set_ratio(medicijn_norm, _normaliseer_medicijn(veld)))
                        if beste >= 70 and pid in kandidaten:
                            kandidaten[pid]["match_score"] = min(99, kandidaten[pid]["match_score"] + 8)
                            kandidaten[pid]["medicijn_match"] = True
                finally:
                    cur2.close(); conn2.close()
            except Exception as e:
                print(f"[KandidatenZoek] medicijn-boost fout: {e}")

    resultaten = sorted(kandidaten.values(), key=lambda p: -p["match_score"])

    # 5. Als de lokale database niets oplevert: zoek als laatste redmiddel ook
    # in WooCommerce zelf (bijv. gloednieuwe klant die nog niet lokaal is opgeslagen).
    if not resultaten and (email or naam):
        wc_url = os.getenv("WC_URL",""); wc_key = os.getenv("WC_KEY",""); wc_secret = os.getenv("WC_SECRET","")
        try:
            zoek_term = email or (max(naam.split(), key=len) if naam.split() else naam)
            resp = http_requests.get(
                f"{wc_url}/wp-json/wc/v3/orders", headers=_wc_auth(wc_key, wc_secret),
                params={"search": zoek_term, "per_page": 20, "orderby": "date", "order": "desc"},
                timeout=10,
            )
            resp.raise_for_status()
            gezien = set()
            for o in (resp.json() if isinstance(resp.json(), list) else []):
                billing = o.get("billing", {})
                wc_email = billing.get("email", "").lower()
                wc_naam = f"{billing.get('first_name','')} {billing.get('last_name','')}".strip()
                sleutel = wc_email or wc_naam.lower()
                if not sleutel or sleutel in gezien:
                    continue
                score = 100 if email and wc_email == email else (
                    fuzz.token_sort_ratio(_normaliseer_naam(naam), _normaliseer_naam(wc_naam)) if naam else 0
                )
                if score < 60:
                    continue
                gezien.add(sleutel)
                meta = {m["key"]: m["value"] for m in o.get("meta_data", [])}
                resultaten.append({
                    "id": None, "email": wc_email,
                    "voornaam": billing.get("first_name", ""), "achternaam": billing.get("last_name", ""),
                    "geboortedatum": meta.get("billing_birth") or meta.get("_billing_birth") or "",
                    "bsn": meta.get("billing_bsn") or meta.get("_billing_bsn") or meta.get("bsn") or "",
                    "telefoon": billing.get("phone", ""), "adres": billing.get("address_1", ""),
                    "postcode": billing.get("postcode", ""), "plaats": billing.get("city", ""),
                    "wc_customer_id": str(o.get("customer_id", "")),
                    "match_methode": "woocommerce", "match_score": score,
                })
            resultaten.sort(key=lambda p: -p["match_score"])
        except Exception as e:
            print(f"[KandidatenZoek] WC fout: {e}")

    return resultaten[:max_kandidaten]


@app.post("/api/identificeer-patient-kandidaten")
async def identificeer_patient_kandidaten_endpoint(request: Request):
    """Geeft ALLE plausibele patiëntmatches terug, zodat de gebruiker kan kiezen
    (i.p.v. dat de applicatie er automatisch één selecteert)."""
    data = await request.json()
    _debug = {}
    kandidaten = await _zoek_patient_kandidaten(
        email=data.get("email", ""), naam=data.get("naam", ""),
        telefoon=data.get("telefoon", ""), bsn=data.get("bsn", ""),
        geboortedatum=data.get("geboortedatum", ""), woonplaats=data.get("woonplaats", ""),
        medicijn=data.get("medicijn", ""), _debug=_debug,
    )
    _debug["aantal_kandidaten_totaal"] = len(kandidaten)
    return JSONResponse(content={"kandidaten": kandidaten, "_debug": _debug})


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


@app.post("/api/order-verstrekking")
async def order_verstrekking(request: Request):
    """Slaat verstrekkingsverzoek op als WooCommerce meta veld."""
    body = await request.json()
    order_id = body.get("order_id")
    wc_url = os.getenv("WC_URL", "")
    wc_key = os.getenv("WC_KEY", "")
    wc_secret = os.getenv("WC_SECRET", "")
    if not order_id or not wc_url:
        return JSONResponse(content={"ok": False, "fout": "Onvoldoende gegevens"})
    try:
        resp = http_requests.put(
            f"{wc_url}/wp-json/wc/v3/orders/{order_id}",
            headers=_wc_auth(wc_key, wc_secret),
            json={"meta_data": [{"key": "_farmamed_verstrekking", "value": "1"}]},
            timeout=10,
        )
        return JSONResponse(content={"ok": resp.status_code == 200})
    except Exception as e:
        return JSONResponse(content={"ok": False, "fout": str(e)})


@app.get("/bank", response_class=HTMLResponse)
async def bank_pagina():
    html_path = BASE_DIR / "templates" / "bank.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


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
            headers=_wc_auth(wc_key, wc_secret),
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
from email.utils import parsedate_to_datetime as _parsedate_to_datetime

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
            ontvangen TEXT DEFAULT (datetime('now')),
            email_datum TEXT
        )
    """)
    # Migratie voor bestaande databases zonder email_datum kolom
    try:
        conn.execute("ALTER TABLE emails ADD COLUMN email_datum TEXT")
    except _sqlite3.OperationalError:
        pass  # kolom bestaat al
    conn.commit()
    conn.close()

def _parse_email_datum(email_data: dict) -> str | None:
    """Zet de Date-header van de e-mail om naar een sorteerbare ISO-datum (UTC)."""
    ruw = (email_data.get("datum") or "").strip()
    if not ruw:
        return None
    try:
        dt = _parsedate_to_datetime(ruw)
        if dt is None:
            return None
        if dt.tzinfo is None:
            # Geen tijdzone in header: als naive tijd behandelen, geen conversie
            return dt.isoformat()
        import datetime as _dt
        return dt.astimezone(_dt.timezone.utc).isoformat()
    except Exception:
        return None

def _sla_email_op(email_data: dict):
    """Sla e-mail op in SQLite."""
    _init_email_db()
    conn = _sqlite3.connect(_DB_PAD)
    try:
        # email_datum wordt alleen bij eerste keer opslaan bepaald en blijft daarna
        # ongewijzigd, zodat de volgorde stabiel blijft ook als de e-mail later
        # (bv. bij verwerken of bijlage uitlezen) opnieuw wordt opgeslagen.
        bestaande = conn.execute(
            "SELECT email_datum FROM emails WHERE uid = ?", (email_data["uid"],)
        ).fetchone()
        if bestaande and bestaande[0]:
            email_datum = bestaande[0]
        else:
            email_datum = _parse_email_datum(email_data)
        conn.execute(
            "INSERT OR REPLACE INTO emails (uid, data, email_datum) VALUES (?, ?, ?)",
            (email_data["uid"], json.dumps(email_data), email_datum)
        )
        conn.commit()
    finally:
        conn.close()

def _haal_emails_op_db(limit: int = 50) -> list:
    """Haal e-mails op uit SQLite, nieuwste eerst (op echte e-maildatum)."""
    _init_email_db()
    conn = _sqlite3.connect(_DB_PAD)
    try:
        rows = conn.execute(
            "SELECT data FROM emails ORDER BY COALESCE(email_datum, ontvangen) DESC LIMIT ?", (limit,)
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
            headers=_wc_auth(wc_key, wc_secret),
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
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "fout": str(e)})
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
        email_slim["bijlagen"] = [{"naam": b["naam"], "type": b["type"], "heeft_data": bool(b.get("data"))} for b in e.get("bijlagen", [])]
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
    verplaatst = _verplaats_email_imap(email_uid)
    return JSONResponse(content={"ok": True, "verplaatst": verplaatst})


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
        "bijlagen": [{"naam": b["naam"], "type": b["type"], "heeft_data": bool(b.get("data"))} for b in email.get("bijlagen", [])],
        "type": email.get("type", "overig"),
    })


@app.post("/api/analyseer-email-body")
async def analyseer_email_body(request: Request):
    """Analyseert e-mail body met Claude om patiënt/medicijn info te extraheren."""
    body = await request.json()
    uid = body.get("uid", "")
    email_body = body.get("body", "")
    onderwerp = body.get("onderwerp", "")
    afzender = body.get("afzender", "")

    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not anthropic_key:
        return JSONResponse(content={"fout": "Anthropic API niet geconfigureerd"})

    prompt = f"""Analyseer deze e-mail en extraheer de volgende informatie in JSON formaat.

Afzender: {afzender}
Onderwerp: {onderwerp}
Body:
{email_body[:2000]}

BELANGRIJK: e-mails naar recept@farmamed.nl bevatten soms onderaan een
e-mailhandtekening van de apotheekmedewerker zelf (na een afsluiting zoals
"Met vriendelijke groet" / "Groeten" / "Kind regards", meestal met functietitel
of bedrijfsnaam erbij). Als je zo'n handtekeningblok herkent, is dat NOOIT de
patiënt — negeer die specifieke naam/telefoon/e-mail dan.
Extraheer verder gewoon vol vertrouwen wat je ziet: een aparte technische
filter verwijdert automatisch alsnog de bekende namen/e-mailadressen/
telefoonnummers van apotheekmedewerkers uit je antwoord, dus jij hoeft niet
zelf voorzichtig te zijn of iets weg te laten enkel omdat het de enige naam
in de mail is — een korte mail met alleen een achternaam (en evt. woonplaats)
is heel normaal voor een herhaalverzoek en moet gewoon als patiëntnaam
worden geëxtraheerd.

LET OP bij compacte/aaneengesloten regels zonder duidelijke leestekens
(bijv. "JG van de Kraats 20011949 Amitriptyline 10% 5 tubes Soest
0642171184"): segmenteer dit zorgvuldig per informatietype. In dit
voorbeeld is "JG van de Kraats" de naam, "20011949" de geboortedatum
(20-01-1949), "Amitriptyline 10%" het medicijn, "5 tubes" de hoeveelheid,
"Soest" de woonplaats en "0642171184" het telefoonnummer. Zet in
"patient_naam" ALLEEN de naam zelf — geen geboortedatum, medicijn,
plaats of telefoonnummer erin meenemen.

LET OP: sommige Nederlandse achternamen zijn ook gewone woorden (bijv.
"Stuitje", "Bakker", "Visser", "de Boer"). Als een e-mail kort en
dubbelzinnig is (bijv. alleen een los woord, mogelijk met een tikfout),
neem dan toch aan dat het een achternaam is en vul die in bij
"patient_naam" — behandel het niet als een medische term of lichaamsdeel
enkel omdat het woord daar toevallig op lijkt. Twijfel je, kies dan voor
"het is een naam": een verkeerde gok wordt later gecorrigeerd doordat de
apotheker zelf uit een kandidatenlijst kiest, terwijl een gemiste naam
(null) het verzoek helemaal blokkeert.

Geef ALLEEN een JSON object terug, geen uitleg:
{{
  "patient_naam": "volledige naam van de patiënt (niet de apotheek/arts/medewerker), of null",
  "patient_email": "e-mailadres van de patiënt (niet eindigend op @farmamed.nl of @apotheekwoerden.nl, en niet de afzenderhandtekening), of null",
  "geboortedatum": "geboortedatum in DD-MM-YYYY formaat, of null",
  "telefoon": "telefoonnummer van de patiënt indien vermeld (niet het telefoonnummer uit de handtekening), of null",
  "bsn": "BSN-nummer van de patiënt indien vermeld, of null",
  "woonplaats": "woonplaats van de patiënt indien vermeld, of null",
  "medicijn": "gevraagd medicijn of preparaat, of null",
  "hoeveelheid": "hoeveelheid of verpakkingsgrootte, of null",
  "is_herhaalverzoek": true of false,
  "notitie": "korte samenvatting van het verzoek in max 15 woorden"
}}"""

    try:
        import requests as _req
        resp = _req.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": anthropic_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-sonnet-4-6", "max_tokens": 500, "messages": [{"role": "user", "content": prompt}]},
            timeout=15,
        )
        resp.raise_for_status()

        tekst = resp.json()["content"][0]["text"].strip()
        tekst = tekst.replace("```json", "").replace("```", "").strip()
        import json as _json
        return JSONResponse(content=_json.loads(tekst))
    except Exception as e:
        return JSONResponse(content={"fout": str(e)})


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

    # Alleen PDF en gangbare afbeeldingsformaten zijn een leesbaar recept voor
    # Claude's vision. Andere bijlagetypes (bijv. audio/* van een voicemail-
    # bericht) kunnen geen recept bevatten — sla de (dure en gegarandeerd
    # mislukkende) OCR-aanroep dan over en geef dat expliciet door, zodat de
    # front-end kan terugvallen op het analyseren van de e-mailtekst zelf.
    _ONDERSTEUNDE_TYPES = ("application/pdf", "image/jpeg", "image/jpg", "image/png", "image/gif", "image/webp")
    is_ondersteund = ct in _ONDERSTEUNDE_TYPES or naam.lower().endswith((".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp"))
    if not is_ondersteund:
        return JSONResponse(content={
            "fout": f"Bijlage '{naam}' ({ct}) is geen leesbaar recept — waarschijnlijk een voicemail of ander bestandstype.",
            "niet_ondersteund": True,
        })

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
