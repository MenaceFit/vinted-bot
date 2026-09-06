"""
Diagnostic async — python debug_vinted.py [fr|pl|uk] [mot-clé]
Teste la connexion sur le marché choisi et affiche le JSON brut renvoyé par
Vinted à côté du résultat parsé, pour repérer un champ manquant/renommé.
"""
import asyncio
import json
import sys
import time

MARKETS = {
    "fr": {"label": "🇫🇷 France",     "base_url": "https://www.vinted.fr",    "lang": "fr-FR,fr;q=0.9", "region": "france"},
    "pl": {"label": "🇵🇱 Pologne",    "base_url": "https://www.vinted.pl",    "lang": "pl-PL,pl;q=0.9", "region": "pologne"},
    "uk": {"label": "🇬🇧 Angleterre", "base_url": "https://www.vinted.co.uk", "lang": "en-GB,en;q=0.9", "region": "angleterre"},
}

market_key = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in MARKETS else "fr"
keyword = sys.argv[2] if len(sys.argv) > 2 else ""


async def main():
    from services.vinted import MarketScraper, MARKETS as M
    minfo = M[market_key]
    print(f"Marché: {minfo['label']} ({minfo['base_url']})")
    print("=" * 60)

    scraper = MarketScraper(market_key, minfo)

    print("Étape 1 — Init session...")
    t0 = time.monotonic()
    await scraper._init_session()
    print(f"  Init: {(time.monotonic()-t0)*1000:.0f}ms — initialized={scraper._initialized}")

    print("\nÉtape 2 — Appel API...")
    t1 = time.monotonic()
    params = {"catalog_ids[]": "2050", "brand_ids[]": "53", "order": "newest_first"}
    if keyword:
        params["search_text"] = keyword
    items, retries = await scraper.fetch_raw(params, per_page=10)
    elapsed = (time.monotonic() - t1) * 1000
    print(f"  API: {elapsed:.0f}ms — {len(items)} items — {retries} retry(s)")

    if items:
        raw = items[0]
        parsed = scraper._parse_item(raw, keyword)

        print("\n--- JSON BRUT (1er item, tel que renvoyé par Vinted) ---")
        print(json.dumps(raw, indent=2, ensure_ascii=False)[:3000])

        print("\n--- RÉSULTAT PARSÉ (ce que le bot en tire) ---")
        for key in ("title", "price", "image", "url", "size", "seller"):
            print(f"  {key}: {parsed.get(key)!r}")

        missing = [k for k in ("title", "price_numeric", "total_item_price", "photos") if k not in raw]
        if missing:
            print(f"\n⚠️  Champs absents du JSON brut : {missing}")
            print("    → si 'title' ou 'price'/'price_numeric' manque ici, c'est la cause exacte")
            print("      des annonces 'Sans titre' / 'N/A' envoyées sur Telegram.")

        print("\n✅ SUCCÈS — API jointe, voir le détail ci-dessus")
    else:
        print("\n❌ 0 items — vérifier la session ou VPN")

    await scraper.close()
    print("=" * 60)


asyncio.run(main())
