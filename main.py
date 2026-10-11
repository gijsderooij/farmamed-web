"""
Synct het ingevulde permutaties-sjabloon naar:
  1. De lokale database — medicijnen (naam_canoniek, article_code, eenheid_type,
     verpakkingsgrootte) + medicijn_hoeveelheden + medicijn_gebruik_opties
  2. WooCommerce — SKU (Article Code) van het bijbehorende product

Matcht rijen op naam tegen de bestaande medicijnen-tabel (naam_smarthub/
naam_woocommerce) EN tegen FARMAMED_PRODUCTEN (voor de WC-productkoppeling),
net als bij sync_artikelcodes.py. Bestaat een medicijn nog niet lokaal, dan
wordt het aangemaakt (met naam_canoniek als naam_woocommerce/naam_smarthub).

Net als de eerdere scripts: standaard dry-run, nooit stilzwijgend overschrijven
bij een conflict.

Gebruik:
    python sync_permutaties.py permutaties_sjabloon.xlsx
    python sync_permutaties.py permutaties_sjabloon.xlsx --apply

Verwacht kolomkoppen (tabblad 'Permutaties'):
  "Naam (Farmasys, canoniek)", "Article Code", "Eenheid-type",
  "WooCommerce verpakkingsgrootte", "Toegestane D/ hoeveelheden (totaal)",
  "S/ opties (alle toegestane)", "S/ standaardwaarde"

Vereist DATABASE_URL, WC_URL, WC_KEY, WC_SECRET — via env vars OF via een
.env-bestand in dezelfde map. Moet in dezelfde map als main.py draaien (voor
de FARMAMED_PRODUCTEN-lijst) of geef --main-pad mee.
"""
import os
import sys
import base64
import argparse
import importlib.util

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import pandas as pd
except ImportError:
    print("pandas is niet geïnstalleerd: pip install pandas openpyxl")
    sys.exit(1)

try:
    import psycopg2
except ImportError:
    print("psycopg2 is niet geïnstalleerd: pip install psycopg2-binary")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("Het 'requests' package is niet geïnstalleerd: pip install requests")
    sys.exit(1)

try:
    from rapidfuzz import fuzz
except ImportError:
    print("rapidfuzz is niet geïnstalleerd: pip install rapidfuzz")
    sys.exit(1)

VERWACHTE_KOLOMMEN = [
    "Naam (Farmasys, canoniek)", "Article Code", "PRK Code", "Eenheid-type",
    "WooCommerce verpakkingsgrootte", "Toegestane D/ hoeveelheden (totaal)",
    "S/ opties (alle toegestane)", "S/ standaardwaarde",
]


def _normaliseer_naam(naam: str) -> str:
    import re
    import unicodedata
    if not naam:
        return ""
    naam = naam.lower().strip()
    naam = unicodedata.normalize('NFD', naam)
    naam = ''.join(c for c in naam if unicodedata.category(c) != 'Mn')  # diakritische tekens strippen
    naam = naam.replace(',', '.')  # '0,02%' en '0.02%' gelijktrekken
    naam = re.sub(r'[(),%]', ' ', naam)
    naam = re.sub(r'\s+', ' ', naam).strip()
    return naam


def _wc_auth(wc_key: str, wc_secret: str) -> dict:
    token = base64.b64encode(f"{wc_key}:{wc_secret}".encode()).decode()
    return {"Accept": "application/json", "Authorization": f"Basic {token}", "User-Agent": "curl/7.68.0"}


def _laad_main_module(main_pad: str):
    spec = importlib.util.spec_from_file_location("farmamed_main", main_pad)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _woorden(naam_norm: str) -> set:
    import re
    return set(w for w in re.split(r'\s+', naam_norm) if w and (len(w) >= 3 or re.match(r'^[\d.]+$', w)))


def _beste_match(naam_norm, kandidaten, key_fn):
    # Eerst een EXACTE (genormaliseerde) match proberen.
    for kandidaat in kandidaten:
        if _normaliseer_naam(key_fn(kandidaat)) == naam_norm:
            return kandidaat, 100

    woorden_bron = _woorden(naam_norm)
    if not woorden_bron:
        return None, 0
    eerste_woord = naam_norm.split(' ', 1)[0]

    beste_score, beste = 0, None
    for kandidaat in kandidaten:
        naam_kandidaat = _normaliseer_naam(key_fn(kandidaat))
        if not naam_kandidaat:
            continue
        eerste_woord_kandidaat = naam_kandidaat.split(' ', 1)[0]
        # Het eerste woord (bijna altijd de werkzame stof) moet overeenkomen.
        if eerste_woord != eerste_woord_kandidaat \
                and not eerste_woord.startswith(eerste_woord_kandidaat) and not eerste_woord_kandidaat.startswith(eerste_woord):
            continue
        woorden_kandidaat = _woorden(naam_kandidaat)
        if not woorden_kandidaat:
            continue
        # Concentratiegetallen (bijv. '0.02', '5', '10') moeten overeenkomen
        # als beide namen die bevatten — anders zouden 0,02% en 0,05% van
        # hetzelfde medicijn als 'match' gelden puur op basis van de
        # algemene woord-overlap ('saw', 'creme'), wat klinisch fout is.
        import re as _re_lokaal
        getallen_bron = {w for w in woorden_bron if _re_lokaal.match(r'^[\d.]+$', w)}
        getallen_kandidaat = {w for w in woorden_kandidaat if _re_lokaal.match(r'^[\d.]+$', w)}
        if getallen_bron and getallen_kandidaat and not (getallen_bron & getallen_kandidaat):
            continue
        # Draagstof-aanduidingen zoals 'DMSO' zijn een hard onderscheid, niet
        # zomaar een woordvariant — anders matcht 'gewoon 10%' met '10% in
        # DMSO 50%' puur omdat beide toevallig het cijfer 10 delen.
        if ('dmso' in woorden_bron) != ('dmso' in woorden_kandidaat):
            continue
        overlap = len(woorden_bron & woorden_kandidaat)
        kleinste = min(len(woorden_bron), len(woorden_kandidaat))
        score = (overlap / kleinste * 100) if kleinste else 0
        if score > beste_score:
            beste_score, beste = score, kandidaat
    return (beste, beste_score) if beste_score >= 70 else (None, beste_score)


def _parse_lijst(waarde):
    """Verwerkt zowel 'a, b, c' als een kaal enkel getal/tekst tot een lijst strings."""
    if waarde is None:
        return []
    return [str(waarde).strip()] if not isinstance(waarde, str) else [x.strip() for x in waarde.split(',') if x.strip()]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bron_xlsx", help="Ingevuld permutaties-sjabloon")
    parser.add_argument("--sheet", default="Permutaties",
                         help="Naam van het tabblad om in te lezen (standaard: 'Permutaties'). "
                              "Gebruik bijv. --sheet Veranderen als de nieuwe/gewijzigde rijen in "
                              "een los tabblad staan.")
    parser.add_argument("--main-pad", default="main.py")
    parser.add_argument("--apply", action="store_true", help="Voer de sync echt door (zonder deze vlag: alleen dry-run)")
    parser.add_argument("--force-conflicten", action="store_true",
                         help="Overschrijf ook bestaande codes die afwijken van de Excel (Farmasys leidend) — alleen samen met --apply")
    args = parser.parse_args()

    database_url = os.getenv("DATABASE_URL", "")
    wc_url = os.getenv("WC_URL", ""); wc_key = os.getenv("WC_KEY", ""); wc_secret = os.getenv("WC_SECRET", "")
    if not database_url:
        print("FOUT: DATABASE_URL ontbreekt."); sys.exit(1)
    if not (wc_url and wc_key and wc_secret):
        print("FOUT: WC_URL, WC_KEY en/of WC_SECRET ontbreken."); sys.exit(1)
    if not os.path.exists(args.main_pad):
        print(f"FOUT: {args.main_pad} niet gevonden — nodig voor de FARMAMED_PRODUCTEN-lijst."); sys.exit(1)

    print(f"Bronbestand inlezen: {args.bron_xlsx} (tabblad '{args.sheet}') ...")
    bron = pd.read_excel(args.bron_xlsx, sheet_name=args.sheet)
    ontbrekend = [k for k in VERWACHTE_KOLOMMEN if k not in bron.columns]
    if ontbrekend:
        print(f"FOUT: ontbrekende kolommen: {ontbrekend}")
        sys.exit(1)
    bron = bron[bron["Naam (Farmasys, canoniek)"].notna()]
    print(f"  {len(bron)} rij(en) gevonden.\n")

    print("main.py laden (voor FARMAMED_PRODUCTEN)...")
    main_mod = _laad_main_module(args.main_pad)
    wc_producten = list(main_mod.FARMAMED_PRODUCTEN)

    print("Lokale medicijnen-tabel ophalen...")
    conn = psycopg2.connect(database_url)
    cur = conn.cursor()

    # Vooraf-controle: zijn er kolommen in 'medicijnen' die dit script niet
    # kent, maar wel verplicht (NOT NULL, geen default) zijn? Dan zou een
    # INSERT halverwege crashen — dat willen we VOORAF weten, niet nadat er
    # al een deel van de rijen is verwerkt.
    bekende_kolommen = {"id", "naam_smarthub", "naam_woocommerce", "naam_canoniek",
                         "article_code", "prk_code", "eenheid_type", "verpakkingsgrootte"}
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'medicijnen' AND is_nullable = 'NO' AND column_default IS NULL
    """)
    onbekende_verplichte_kolommen = [r[0] for r in cur.fetchall() if r[0] not in bekende_kolommen]
    if onbekende_verplichte_kolommen:
        print(f"\nFOUT: de tabel 'medicijnen' heeft verplichte kolom(men) die dit script niet kent en dus niet kan invullen: {onbekende_verplichte_kolommen}")
        print("Er is NIETS aangepast. Vertel me wat deze kolom(men) betekenen, dan werk ik het script bij.")
        cur.close(); conn.close()
        sys.exit(1)

    cur.execute("SELECT id, naam_smarthub, naam_woocommerce, article_code, prk_code FROM medicijnen")
    lokale_medicijnen = [{"id": r[0], "naam_smarthub": r[1], "naam_woocommerce": r[2], "article_code": r[3], "prk_code": r[4]} for r in cur.fetchall()]
    print(f"  {len(lokale_medicijnen)} medicijn(en) lokaal bekend.\n")

    # Vooraf-controle: dubbele PRK-codes BINNEN de Excel zelf — die zouden
    # anders pas bij het wegschrijven op de UNIQUE-constraint stuklopen.
    prk_in_bron = {}
    for _, rij in bron.iterrows():
        p = str(rij["PRK Code"]).strip()
        prk_in_bron.setdefault(p, []).append(str(rij["Naam (Farmasys, canoniek)"]).strip())
    dubbele_prk_in_bron = {p: namen for p, namen in prk_in_bron.items() if len(namen) > 1}
    if dubbele_prk_in_bron:
        print("FOUT: dezelfde PRK Code komt meerdere keren voor in de Excel zelf — dat kan niet, PRK Code moet uniek zijn:")
        for p, namen in dubbele_prk_in_bron.items():
            print(f"  '{p}' staat bij: {namen}")
        print("\nEr is NIETS aangepast. Corrigeer de Excel en probeer opnieuw.")
        cur.close(); conn.close()
        sys.exit(1)

    prk_naar_lokaal = {m["prk_code"]: m for m in lokale_medicijnen if m["prk_code"]}

    print("Verwerken...")
    nieuwe_medicijnen, medicijn_updates, wc_sku_updates, conflicten, niet_gevonden_wc = [], [], [], [], []
    plan = []  # (medicijn_id_of_None, rij-data) voor de tweede fase (D/- en S/-opties)
    geclaimde_medicijn_ids = {}  # medicijn_id -> Excel-naam die het als eerste claimde (botsingsdetectie)

    for _, rij in bron.iterrows():
        naam = str(rij["Naam (Farmasys, canoniek)"]).strip()
        code = str(rij["Article Code"]).strip()
        prk_code = str(rij["PRK Code"]).strip()
        eenheid_type = str(rij["Eenheid-type"]).strip().lower()
        verpakkingsgrootte = int(rij["WooCommerce verpakkingsgrootte"])
        hoeveelheden = [int(x) for x in _parse_lijst(rij["Toegestane D/ hoeveelheden (totaal)"])]
        s_opties = _parse_lijst(rij["S/ opties (alle toegestane)"])
        s_standaard = str(rij["S/ standaardwaarde"]).strip()

        if eenheid_type not in ("tube", "tablet"):
            print(f"⚠ Overslaan — onbekend eenheid-type '{eenheid_type}' bij {naam}")
            continue
        if s_standaard not in s_opties:
            print(f"⚠ Overslaan — S/ standaardwaarde '{s_standaard}' zit niet in de S/-opties bij {naam}")
            continue

        naam_norm = _normaliseer_naam(naam)
        lokaal, score_lokaal = _beste_match(naam_norm, lokale_medicijnen, lambda m: m["naam_woocommerce"] or m["naam_smarthub"] or "")

        # De PRK-code hoort al bij een ANDER bestaand medicijn dan waarmee
        # de naam matcht? Dat is een echte inconsistentie tussen twee
        # bestaande database-records (niet iets dit script kan/mag oplossen
        # door te gokken) — altijd overslaan, ook met --force-conflicten.
        prk_eigenaar = prk_naar_lokaal.get(prk_code)
        if prk_eigenaar and lokaal and prk_eigenaar["id"] != lokaal["id"]:
            print(f"⚠ Overslaan — {naam}: matcht op naam met medicijn #{lokaal['id']} "
                  f"('{lokaal['naam_woocommerce'] or lokaal['naam_smarthub']}'), maar PRK Code '{prk_code}' hoort al bij "
                  f"ANDER medicijn #{prk_eigenaar['id']} ('{prk_eigenaar['naam_woocommerce'] or prk_eigenaar['naam_smarthub']}'). "
                  f"Dit zijn kennelijk twee losse records in de database die eigenlijk hetzelfde medicijn zijn — "
                  f"controleer en corrigeer dit handmatig in de database vóór je opnieuw synct.")
            continue

        # Geen naam-match, maar de PRK-code bestaat al bij een ANDER medicijn?
        # Dan gaat het waarschijnlijk om hetzelfde medicijn met een naam die
        # te veel afwijkt om automatisch te matchen. Met --force-conflicten
        # behandelen we dit als een bevestigde match (Farmasys/PRK-code is
        # dan leidend, ook over de naam) — anders blijft dit gemeld en overgeslagen.
        if not lokaal and prk_code in prk_naar_lokaal:
            bestaand_via_prk = prk_naar_lokaal[prk_code]
            if args.force_conflicten:
                print(f"✓ PRK Code '{prk_code}' ({naam}) gekoppeld aan medicijn #{bestaand_via_prk['id']} "
                      f"(had een niet-matchende naam: '{bestaand_via_prk['naam_woocommerce'] or bestaand_via_prk['naam_smarthub']}') — naam wordt bijgewerkt.")
                lokaal = bestaand_via_prk
            else:
                print(f"⚠ Overslaan — PRK Code '{prk_code}' ({naam}) hoort al bij medicijn #{bestaand_via_prk['id']} "
                      f"('{bestaand_via_prk['naam_woocommerce'] or bestaand_via_prk['naam_smarthub']}') met een naam die niet automatisch matchte. "
                      f"Controleer handmatig of dit hetzelfde medicijn is, of draai met --force-conflicten om te bevestigen.")
                continue

        # Vangnet: heeft een ANDERE Excel-rij dit medicijn_id al geclaimd?
        # Dan zouden we een bestaand record met twee verschillende Excel-rijen
        # overschrijven — de laatste zou de eerste stilzwijgend wissen. Dit
        # nooit laten doorgaan, ongeacht de precieze oorzaak (matchfout of
        # een echte dubbeling in de brondata).
        if lokaal and lokaal["id"] in geclaimde_medicijn_ids and geclaimde_medicijn_ids[lokaal["id"]] != naam:
            print(f"\nFOUT: zowel '{geclaimde_medicijn_ids[lokaal['id']]}' als '{naam}' matchen met hetzelfde bestaande "
                  f"medicijn #{lokaal['id']}. Dat zou de eerste update stilzwijgend overschrijven — dat doe ik niet.")
            print("Er is NIETS aangepast. Controleer deze twee namen handmatig — waarschijnlijk matcht er iets te los.")
            cur.close(); conn.close()
            sys.exit(1)
        if lokaal:
            geclaimde_medicijn_ids[lokaal["id"]] = naam

        is_conflict = False
        if lokaal:
            bestaande_code = lokaal["article_code"]
            bestaande_prk = lokaal["prk_code"]
            afwijkingen = []
            if bestaande_code and bestaande_code != code:
                afwijkingen.append(("Article Code", bestaande_code, code))
            if bestaande_prk and bestaande_prk != prk_code:
                afwijkingen.append(("PRK Code", bestaande_prk, prk_code))
            if afwijkingen:
                for veld, oud, nieuw in afwijkingen:
                    conflicten.append({"naam": naam, "veld": veld, "nieuwe_code": nieuw, "bestaande_code": oud,
                                        "medicijn_id": lokaal["id"], "overschreven": args.force_conflicten})
                if not args.force_conflicten:
                    is_conflict = True
            if not is_conflict:
                wc_match, score_wc = _beste_match(naam_norm, wc_producten, lambda p: p[1])
                medicijn_updates.append({"medicijn_id": lokaal["id"], "naam": naam, "code": code, "prk_code": prk_code,
                                          "eenheid_type": eenheid_type, "verpakkingsgrootte": verpakkingsgrootte,
                                          "wc_product_id": wc_match[0] if wc_match else None,
                                          "force": args.force_conflicten})
                plan.append({"medicijn_id": lokaal["id"], "naam": naam, "hoeveelheden": hoeveelheden,
                              "s_opties": s_opties, "s_standaard": s_standaard})
                if wc_match:
                    wc_sku_updates.append({"product_id": wc_match[0], "naam": naam, "wc_naam": wc_match[1], "code": code, "score": score_wc})
                else:
                    niet_gevonden_wc.append(naam)
        else:
            wc_match, score_wc = _beste_match(naam_norm, wc_producten, lambda p: p[1])
            nieuwe_medicijnen.append({"naam": naam, "code": code, "prk_code": prk_code, "eenheid_type": eenheid_type,
                                       "verpakkingsgrootte": verpakkingsgrootte,
                                       "wc_product_id": wc_match[0] if wc_match else None})
            plan.append({"medicijn_id": None, "naam": naam, "hoeveelheden": hoeveelheden,
                         "s_opties": s_opties, "s_standaard": s_standaard})
            if wc_match:
                wc_sku_updates.append({"product_id": wc_match[0], "naam": naam, "wc_naam": wc_match[1], "code": code, "score": score_wc})
            else:
                niet_gevonden_wc.append(naam)

    print(f"\n{len(nieuwe_medicijnen)} nieuw medicijn te registreren.")
    print(f"{len(medicijn_updates)} bestaand medicijn bij te werken (canonieke naam/code/eenheid).")
    print(f"{len(wc_sku_updates)} WooCommerce SKU-update(s).")
    echte_conflicten = [c for c in conflicten if not c.get("overschreven")]
    overschreven_conflicten = [c for c in conflicten if c.get("overschreven")]
    print(f"{len(echte_conflicten)} conflict(en) — bestaande waarde wijkt af, wordt overgeslagen (gebruik --force-conflicten om te overschrijven).")
    if overschreven_conflicten:
        print(f"{len(overschreven_conflicten)} conflict(en) worden overschreven (--force-conflicten actief):")
        for c in overschreven_conflicten:
            print(f"    {c['naam']} ({c['veld']}): '{c['bestaande_code']}' -> '{c['nieuwe_code']}'")
    print(f"{len(niet_gevonden_wc)} rij(en) niet gematcht in WooCommerce (Article Code + D/S/ gaan nog wel naar de database): {niet_gevonden_wc}")

    if echte_conflicten:
        print("\n⚠ CONFLICTEN (handmatig beoordelen, NIET aangepast — gebruik --force-conflicten om ze alsnog door te voeren):")
        for c in echte_conflicten:
            print(f"  {c['naam']} ({c['veld']}): bron zegt '{c['nieuwe_code']}', lokaal staat al '{c['bestaande_code']}' (medicijn #{c['medicijn_id']})")

    if not args.apply:
        print(f"\n--- DRY-RUN --- Er is niets aangepast.")
        print(f"Run met --apply om dit door te voeren.")
        cur.close(); conn.close()
        return

    print("\nBezig met bijwerken...")

    try:
        # Fase 1: nieuwe medicijnen aanmaken, bestaande bijwerken
        for u in medicijn_updates:
            if u.get("force"):
                cur.execute(
                    "UPDATE medicijnen SET naam_canoniek=%s, naam_smarthub=%s, naam_woocommerce=%s, "
                    "article_code=%s, prk_code=%s, eenheid_type=%s, verpakkingsgrootte=%s, "
                    "wc_product_id=COALESCE(%s, wc_product_id) WHERE id=%s",
                    (u["naam"], u["naam"], u["naam"], u["code"], u["prk_code"], u["eenheid_type"],
                     u["verpakkingsgrootte"], u["wc_product_id"], u["medicijn_id"])
                )
            else:
                cur.execute(
                    "UPDATE medicijnen SET naam_canoniek=%s, article_code=COALESCE(NULLIF(article_code,''), %s), "
                    "prk_code=COALESCE(NULLIF(prk_code,''), %s), eenheid_type=%s, verpakkingsgrootte=%s, "
                    "wc_product_id=COALESCE(wc_product_id, %s) WHERE id=%s",
                    (u["naam"], u["code"], u["prk_code"], u["eenheid_type"], u["verpakkingsgrootte"],
                     u["wc_product_id"], u["medicijn_id"])
                )
        for n in nieuwe_medicijnen:
            cur.execute(
                "INSERT INTO medicijnen (naam_smarthub, naam_woocommerce, naam_canoniek, article_code, prk_code, eenheid_type, verpakkingsgrootte, wc_product_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (n["naam"], n["naam"], n["naam"], n["code"], n["prk_code"], n["eenheid_type"], n["verpakkingsgrootte"], n["wc_product_id"])
            )
            nieuw_id = cur.fetchone()[0]
            for p in plan:
                if p["medicijn_id"] is None and p["naam"] == n["naam"]:
                    p["medicijn_id"] = nieuw_id
        conn.commit()
        print(f"✓ {len(medicijn_updates)} medicijn(en) bijgewerkt, {len(nieuwe_medicijnen)} nieuw aangemaakt.")

        # Fase 2: D/- en S/-opties wegschrijven (idempotent: eerst verwijderen, dan opnieuw invoegen)
        for p in plan:
            mid = p["medicijn_id"]
            if mid is None:
                continue
            cur.execute("DELETE FROM medicijn_hoeveelheden WHERE medicijn_id=%s", (mid,))
            for h in p["hoeveelheden"]:
                cur.execute("INSERT INTO medicijn_hoeveelheden (medicijn_id, hoeveelheid) VALUES (%s, %s)", (mid, h))
            cur.execute("DELETE FROM medicijn_gebruik_opties WHERE medicijn_id=%s", (mid,))
            for s in p["s_opties"]:
                cur.execute(
                    "INSERT INTO medicijn_gebruik_opties (medicijn_id, code, is_standaard) VALUES (%s, %s, %s)",
                    (mid, s, s == p["s_standaard"])
                )
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"\n✗ FOUT tijdens wegschrijven — alles teruggedraaid, er is NIETS aangepast: {e}")
        cur.close(); conn.close()
        sys.exit(1)
    conn.commit()
    print(f"✓ D/- en S/-opties weggeschreven voor {len([p for p in plan if p['medicijn_id']])} medicijn(en).")
    cur.close(); conn.close()

    # Fase 3: WooCommerce SKU's bijwerken
    wc_gelukt, wc_mislukt = 0, []
    for u in wc_sku_updates:
        try:
            resp = requests.put(
                f"{wc_url}/wp-json/wc/v3/products/{u['product_id']}",
                headers=_wc_auth(wc_key, wc_secret),
                json={"sku": u["code"]}, timeout=15,
            )
            if resp.status_code == 400 and "sku" in resp.text.lower():
                wc_mislukt.append((u, f"SKU al in gebruik bij een ander product (HTTP 400: {resp.text[:150]})"))
                continue
            resp.raise_for_status()
            wc_gelukt += 1
        except Exception as e:
            wc_mislukt.append((u, str(e)))
    print(f"✓ {wc_gelukt} WooCommerce-product(en) bijgewerkt (SKU).")
    if wc_mislukt:
        print(f"✗ {len(wc_mislukt)} WooCommerce-update(s) MISLUKT:")
        for u, fout in wc_mislukt:
            print(f"    product #{u['product_id']} ({u['wc_naam']}): {fout}")


if __name__ == "__main__":
    main()
