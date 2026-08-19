"""
Farmamed Email Poller — draait als worker op Railway
Haalt e-mails op via IMAP en stuurt ze door naar de FastAPI app.
Verplaatst verwerkte e-mails naar INBOX/Afgehandeld.
"""

import imaplib
import email as email_lib
from email.header import decode_header
import base64
import re
import time
import os
import threading
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

IMAP_SERVER    = os.getenv("IMAP_SERVER", "")
IMAP_PORT      = int(os.getenv("IMAP_PORT", "993"))
IMAP_USER      = os.getenv("IMAP_USER", "")
IMAP_PASS      = os.getenv("IMAP_PASS", "")
RAILWAY_URL    = os.getenv("RAILWAY_URL", "http://localhost:8080")
POLL_INTERVAL  = int(os.getenv("POLL_INTERVAL_SEC", "60"))
AFGEHANDELD_MAP = "INBOX/Afgehandeld"

al_verstuurd = set()
_lock = threading.Lock()


def decodeer(waarde, enc=None) -> str:
    if isinstance(waarde, bytes):
        return waarde.decode(enc or "utf-8", errors="replace")
    return str(waarde)


def html_naar_tekst(html: str) -> str:
    """Zet een HTML-body om naar leesbare platte tekst (fallback als text/plain ontbreekt)."""
    tekst = re.sub(r'(?i)<(br|/p|/div|/tr)\s*/?>', '\n', html)
    tekst = re.sub(r'(?is)<(script|style).*?</\1>', '', tekst)
    tekst = re.sub(r'(?s)<[^>]+>', '', tekst)
    tekst = tekst.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"').replace('&#39;', "'")
    tekst = re.sub(r'\n\s*\n+', '\n\n', tekst)
    return tekst.strip()


def verwerk_email(msg, uid: str) -> dict:
    onderwerp = "".join(decodeer(p, e) for p, e in decode_header(msg.get("Subject", "")))

    afzender_raw = "".join(decodeer(p, e) for p, e in decode_header(msg.get("From", "")))
    if "<" in afzender_raw:
        afzender_naam  = afzender_raw.split("<")[0].strip().strip('"')
        afzender_email = afzender_raw.split("<")[1].rstrip(">").strip()
    else:
        afzender_naam  = ""
        afzender_email = afzender_raw.strip()

    body = ""
    body_html = ""
    bijlagen = []
    for part in msg.walk():
        ct = part.get_content_type()
        cd = str(part.get("Content-Disposition", ""))
        if ct == "text/plain" and "attachment" not in cd:
            try:
                body = part.get_payload(decode=True).decode("utf-8", errors="replace")
            except Exception:
                pass
        elif ct == "text/html" and "attachment" not in cd:
            try:
                body_html = part.get_payload(decode=True).decode("utf-8", errors="replace")
            except Exception:
                pass
        elif "attachment" in cd or ct in ("application/pdf", "image/jpeg", "image/png", "image/jpg"):
            naam   = part.get_filename() or f"bijlage_{len(bijlagen)+1}"
            inhoud = part.get_payload(decode=True) or b""
            if inhoud:
                bijlagen.append({"naam": naam, "type": ct, "data": base64.b64encode(inhoud).decode()})

    # Fallback: geen text/plain gevonden, maar wel text/html -> omzetten naar platte tekst
    if not body.strip() and body_html.strip():
        body = html_naar_tekst(body_html)

    tekst = (onderwerp + " " + body).lower()
    if any(t in tekst for t in ["herhaalrecept", "herhaling", "iter", "verlenging"]):
        email_type = "herhaalrecept"
    elif any(t in tekst for t in ["recept", "voorschrift", "medicijn", "bijlage"]) or bijlagen:
        email_type = "nieuw_recept"
    else:
        email_type = "overig"

    return {
        "uid": uid, "onderwerp": onderwerp or "(geen onderwerp)",
        "afzender": afzender_email, "afzender_naam": afzender_naam,
        "datum": msg.get("Date", ""), "body": body[:2000],
        "bijlagen": bijlagen, "heeft_bijlage": bool(bijlagen),
        "ongelezen": True, "type": email_type,
    }


def maak_map_aan(conn, mapnaam: str):
    """Maak IMAP map aan als die nog niet bestaat."""
    try:
        status, _ = conn.select(mapnaam)
        if status != "OK":
            conn.create(mapnaam)
            print(f"  📁 Map aangemaakt: {mapnaam}")
        conn.select("INBOX")
    except Exception as e:
        print(f"  ⚠ Kon map niet aanmaken {mapnaam}: {e}")


def verplaats_naar_afgehandeld(uid_str: str):
    """Verplaats e-mail naar INBOX/Afgehandeld via IMAP."""
    try:
        conn = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        conn.login(IMAP_USER, IMAP_PASS)
        conn.select("INBOX")
        maak_map_aan(conn, AFGEHANDELD_MAP)

        # Zoek e-mail op UID
        uid_bytes = uid_str.encode() if isinstance(uid_str, str) else uid_str
        conn.uid("COPY", uid_bytes, AFGEHANDELD_MAP)
        conn.uid("STORE", uid_bytes, "+FLAGS", "\\Deleted")
        conn.expunge()
        conn.logout()
        print(f"  📨 E-mail {uid_str} verplaatst naar {AFGEHANDELD_MAP}")
        return True
    except Exception as e:
        print(f"  ✗ Kon e-mail niet verplaatsen: {e}")
        return False


def poll():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Mailbox controleren…")
    try:
        conn = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        conn.login(IMAP_USER, IMAP_PASS)
        conn.select("INBOX")
        maak_map_aan(conn, AFGEHANDELD_MAP)

        _, berichten = conn.search(None, "ALL")
        uids = berichten[0].split()

        with _lock:
            nieuwe = [u for u in uids if u.decode() not in al_verstuurd]

        if not nieuwe:
            print("  Geen nieuwe e-mails.")
            conn.logout()
            return

        print(f"  {len(nieuwe)} nieuwe e-mail(s).")
        for uid in nieuwe:
            uid_str = uid.decode()
            try:
                _, data = conn.fetch(uid, "(RFC822)")
                msg = email_lib.message_from_bytes(data[0][1])
                email_data = verwerk_email(msg, uid_str)
                print(f"  → {email_data['onderwerp'][:50]} ({email_data['type']})")

                resp = requests.post(
                    f"{RAILWAY_URL}/api/email-inkomend",
                    json=email_data,
                    timeout=300,
                )
                if resp.status_code == 200:
                    with _lock:
                        al_verstuurd.add(uid_str)
                    print(f"    ✓ Verstuurd")
                else:
                    print(f"    ✗ Mislukt (status {resp.status_code})")
            except Exception as e:
                print(f"  ✗ Fout UID {uid_str}: {e}")

        conn.logout()
    except Exception as e:
        print(f"  ✗ IMAP-fout: {e}")


def start_verplaats_server():
    """
    Kleine HTTP server die luistert op /verplaats-email
    zodat de FastAPI app kan vragen om een e-mail te verplaatsen.
    """
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import json

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # Stil

        def do_POST(self):
            if self.path == "/verplaats-email":
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                uid = body.get("uid", "")
                ok = verplaats_naar_afgehandeld(uid)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"ok": ok}).encode())
            else:
                self.send_response(404)
                self.end_headers()

    poort = int(os.getenv("POLLER_PORT", "8081"))
    server = HTTPServer(("0.0.0.0", poort), Handler)
    print(f"  🔌 Verplaats-server luistert op poort {poort}")
    server.serve_forever()


def main():
    print("=" * 50)
    print("Farmamed Email Poller (Railway)")
    print(f"Mailbox : {IMAP_USER}")
    print(f"Server  : {IMAP_SERVER}:{IMAP_PORT}")
    print(f"Interval: elke {POLL_INTERVAL}s")
    print("=" * 50)

    # Start verplaats-server in aparte thread
    t = threading.Thread(target=start_verplaats_server, daemon=True)
    t.start()

    # Poll loop
    while True:
        try:
            poll()
        except KeyboardInterrupt:
            print("\nGestopt.")
            break
        except Exception as e:
            print(f"Onverwachte fout: {e}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
