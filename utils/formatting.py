"""Construction des messages de notification — court, propre, lisible."""
import html

TITLE_MAX_LEN = 90


def _clip(text: str, max_len: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= max_len else text[: max_len - 1].rstrip() + "…"


def telegram_caption(ad: dict, new_find: bool = True) -> str:
    """Message HTML court pour Telegram (sendPhoto caption ou sendMessage text).

    Format (voir spec) :
        🆕 NOUVELLE ANNONCE
        👕 <titre>
        💰 <prix>
        🔗 <a href="...">Voir l'annonce</a>
    """
    title = html.escape(_clip(ad.get("title", "Sans titre"), TITLE_MAX_LEN))
    price = html.escape(str(ad.get("price", "N/A")))
    url = ad.get("url", "")
    header = "🆕 NOUVELLE ANNONCE" if new_find else "🔥 NOUVELLE TROUVAILLE"

    lines = [header, f"👕 {title}", f"💰 {price}"]
    if url:
        lines.append(f'🔗 <a href="{html.escape(url, quote=True)}">Voir l\'annonce</a>')
    return "\n".join(lines)


TEST_MESSAGE = "✅ TEST RÉUSSI\nVinted Monitor est correctement connecté à Telegram."
