"""
Diagnostic async — python debug_vinted.py [fr|pl|uk]
Teste la connexion sur le marché choisi.
"""
import asyncio
import sys
import time

MARKETS = {
    "fr": {"label": "🇫🇷 France",     "base_url": "https://www.vinted.fr",    "lang": "fr-FR,fr;q=0.9", "region": "france"},
    "pl": {"label": "🇵🇱 Pologne",    "base_url": "https://www.vinted.pl",    "lang": "pl-PL,pl;q=0.9", "region": "pologne"},
    "uk": {"label": "🇬🇧 Angleterre", "base_url": "https://www.vinted.co.uk", "lang": "en-GB,en;q=0.9", "region": "angleterre"},
}

market_key = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in MARKETS else "fr"


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
    items, _retries = await scraper.fetch_raw(params, per_page=10)
    elapsed = (time.monotonic() - t1) * 1000
    print(f"  API: {elapsed:.0f}ms — {len(items)} items")

    if items:
        i = items[0]
        print(f"  Exemple: [{i.get('id')}] {i.get('title')} — {i.get('price')}")
        print("\n✅ SUCCÈS")
    else:
        print("\n❌ 0 items — vérifier la session ou VPN")

    await scraper.close()
    print("=" * 60)


asyncio.run(main())
