"""
Benchmark avant/après — mesures réelles, exécutées localement, aucune valeur
inventée.

Ce script ne peut PAS mesurer la latence réseau réelle contre Vinted ou
Telegram : cet environnement de développement n'a pas de sortie réseau vers
ces services (vérifié). Ce qu'il mesure à la place, ce sont les parties du
"avant/après" qui sont reproductibles localement, sans réseau :

  A. Le bug de double-tâche par mot-clé (core/scheduler.py → core/scanner.py) :
     combien de tâches de scan restent "fantômes" après plusieurs cycles
     démarrer/arrêter, et combien d'itérations de boucle ça représente.
  B. Écriture SQLite : avec/sans WAL (l'ancien dedup.py n'activait aucun
     PRAGMA — voir audit).
  C. Rendu GUI : widgets Tkinter empilés sans limite (ancien) vs un seul
     ttk.Treeview borné (nouveau) — temps de rendu ET nombre de widgets créés.
  D. Débit du cache de dédup mémoire (inchangé, déjà O(1) avant/après —
     mesuré ici comme référence, pas comme "amélioration").

Pour la latence de scan réelle contre Vinted et la latence de livraison
Telegram, utilise le dashboard intégré à l'application (section PERFORMANCE) :
il mesure ces temps en direct pendant que le bot tourne réellement, avec ta
propre connexion. C'est la seule source honnête pour ces chiffres.

Usage :
    python benchmark.py
"""
import asyncio
import resource
import time
from pathlib import Path
from typing import Optional


def _rss_kb() -> int:
    """Pic de mémoire résidente du process (Ko) — Linux/macOS uniquement."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def _cpu_seconds() -> float:
    return time.process_time()


# ── A. Bug du double-scan (voir core/scanner.py, docstring) ─────────────────

class _OldBuggyScheduler:
    """Reproduction fidèle de l'ancien core/scheduler.py::start()/stop()
    (avant correction), gardée uniquement pour mesurer l'impact réel du bug.
    Le code de production ne contient plus cette version."""

    def __init__(self, loop_body):
        self._tasks: dict[str, dict] = {}
        self._loop_body = loop_body

    def start(self, loop, keywords):
        for kw in keywords:
            if kw in self._tasks:
                continue
            kt = {"task": None}
            # Ligne bugguée d'origine : planifie _keyword_loop UNE FOIS, jamais trackée.
            asyncio.run_coroutine_threadsafe(self._loop_body(kw), loop)
            # ... puis une SECONDE fois, celle-ci trackée dans kt["task"].
            asyncio.run_coroutine_threadsafe(self._create_task(kt, kw), loop)
            self._tasks[kw] = kt

    async def _create_task(self, kt, kw):
        kt["task"] = asyncio.create_task(self._loop_body(kw), name=f"old-scan-{kw}")

    def stop(self, loop):
        for kt in self._tasks.values():
            if kt["task"] and not kt["task"].done():
                loop.call_soon_threadsafe(kt["task"].cancel)
        self._tasks.clear()


async def benchmark_task_leak(cycles: int = 5) -> dict:
    import core.scanner as scanner_mod
    from core.scanner import Scanner

    loop = asyncio.get_running_loop()

    # ── Ancien comportement (bug) ────────────────────────────────────────
    old_iterations = {"n": 0}

    async def old_loop_body(name):
        try:
            while True:
                old_iterations["n"] += 1
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            raise

    old = _OldBuggyScheduler(old_loop_body)
    for _ in range(cycles):
        old.start(loop, ["nike"])
        await asyncio.sleep(0.05)
        old.stop(loop)
        await asyncio.sleep(0.02)

    old_orphans = [t for t in asyncio.all_tasks() if not t.done() and t is not asyncio.current_task()]
    old_leaked = len(old_orphans)
    for t in old_orphans:
        t.cancel()
    await asyncio.sleep(0.05)

    # ── Nouveau comportement (corrigé) ───────────────────────────────────
    new_iterations = {"n": 0}

    async def new_scrape_fn(active_keys, params, keywords=None, keyword_key="", per_page=10):
        new_iterations["n"] += 1
        return [], {"api_ms": 0.0, "parse_ms": 0.0, "retries": 0, "error": False}

    scanner = Scanner(
        config_provider=lambda: {
            "keywords_filter": ["nike"], "markets": {"fr": {"enabled": True}},
            "interval_seconds": 0.02, "warmup_first_run": True, "custom_url": "",
            "catalog_ids": [], "brand_ids": [], "min_price": None, "max_price": None,
            "telegram": {"enabled": False}, "discord": {"enabled": False},
        },
        on_log=lambda m: None, on_new_ad=lambda ad: None, scrape_fn=new_scrape_fn,
    )

    old_floor = scanner_mod.MIN_INTERVAL_SECONDS
    scanner_mod.MIN_INTERVAL_SECONDS = 0.02  # même plancher que l'ancien, pour comparer à interval égal
    try:
        for _ in range(cycles):
            scanner.start(loop)
            await asyncio.sleep(0.05)
            scanner.stop()
            await asyncio.sleep(0.02)
    finally:
        scanner_mod.MIN_INTERVAL_SECONDS = old_floor

    new_leaked = len([
        t for t in asyncio.all_tasks()
        if not t.done() and (t.get_name() or "").startswith("scan-")
    ])

    return {
        "cycles": cycles,
        "old_leaked_tasks_after_cycles": old_leaked,
        "old_total_loop_iterations": old_iterations["n"],
        "new_leaked_tasks_after_cycles": new_leaked,
        "new_total_loop_iterations": new_iterations["n"],
    }


# ── B. SQLite : avec/sans WAL ─────────────────────────────────────────────

async def benchmark_sqlite_wal(n_rows: int = 500) -> dict:
    import tempfile

    import aiosqlite

    async def _insert_n(path: Path, wal: bool, n: int) -> float:
        conn = await aiosqlite.connect(str(path))
        if wal:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("CREATE TABLE t (id TEXT PRIMARY KEY, v INTEGER)")
        await conn.commit()
        t0 = time.perf_counter()
        for i in range(n):
            await conn.execute("INSERT INTO t VALUES (?, ?)", (f"id_{i}", i))
            await conn.commit()
        elapsed = time.perf_counter() - t0
        await conn.close()
        return elapsed

    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        t_no_wal = await _insert_n(base / "no_wal.sqlite3", wal=False, n=n_rows)
        t_wal = await _insert_n(base / "wal.sqlite3", wal=True, n=n_rows)

    return {
        "rows": n_rows,
        "no_wal_seconds": t_no_wal,
        "wal_seconds": t_wal,
        "speedup_x": (t_no_wal / t_wal) if t_wal > 0 else float("inf"),
    }


# ── C. Rendu GUI : widgets empilés vs Treeview ────────────────────────────

def benchmark_gui_rendering(n: int = 500) -> Optional[dict]:
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception:
        return None

    try:
        root = tk.Tk()
        root.withdraw()
    except Exception:
        return None  # pas de display disponible (normal en CI headless)

    try:
        t0 = time.perf_counter()
        old_frame = tk.Frame(root)
        for i in range(n):
            card = tk.Frame(old_frame)
            tk.Label(card, text=f"Annonce {i}").pack()
            tk.Label(card, text="12 €").pack()
            card.pack()
        root.update()
        t_old = time.perf_counter() - t0
        old_widget_count = 1 + sum(1 + len(c.winfo_children()) for c in old_frame.winfo_children())
        old_frame.destroy()

        t0 = time.perf_counter()
        tree = ttk.Treeview(root, columns=("price",))
        for i in range(n):
            tree.insert("", 0, iid=str(i), values=(f"{i} €",))
        root.update()
        t_new = time.perf_counter() - t0
        tree.destroy()

        return {
            "n_ads": n,
            "old_render_seconds": t_old,
            "old_widget_count": old_widget_count,
            "new_render_seconds": t_new,
            "new_widget_count": 1,
        }
    finally:
        root.destroy()


# ── D. Débit du cache de dédup mémoire (référence, inchangé) ─────────────

def benchmark_dedup_throughput(n: int = 50_000) -> dict:
    import database.database as db

    db._cache.clear()
    ads = [{"id": f"bench_{i}"} for i in range(n)]

    t0 = time.perf_counter()
    first_pass = db.filter_new(ads)
    t_first = time.perf_counter() - t0

    t0 = time.perf_counter()
    second_pass = db.filter_new(ads)
    t_second = time.perf_counter() - t0

    db._cache.clear()
    return {
        "n": n,
        "first_pass_seconds": t_first,
        "first_pass_ops_per_sec": n / t_first if t_first > 0 else float("inf"),
        "dedup_pass_seconds": t_second,
        "dedup_pass_ops_per_sec": n / t_second if t_second > 0 else float("inf"),
        "new_found_first_pass": len(first_pass),
        "new_found_second_pass": len(second_pass),
    }


# ── Rapport ────────────────────────────────────────────────────────────────

async def main() -> None:
    print("=" * 72)
    print("BENCHMARK — mesures réelles, exécutées localement (aucun réseau)")
    print("=" * 72)

    cpu0, rss0 = _cpu_seconds(), _rss_kb()

    print("\n[A] Bug double-tâche par mot-clé (core/scheduler.py → core/scanner.py)")
    a = await benchmark_task_leak(cycles=5)
    print(f"    Cycles démarrer/arrêter simulés     : {a['cycles']}")
    print(f"    AVANT — tâches fantômes après coup  : {a['old_leaked_tasks_after_cycles']}"
          f"  (itérations de boucle cumulées: {a['old_total_loop_iterations']})")
    print(f"    APRÈS — tâches fantômes après coup  : {a['new_leaked_tasks_after_cycles']}"
          f"  (itérations de boucle cumulées: {a['new_total_loop_iterations']})")

    print("\n[B] Écriture SQLite — avec/sans WAL (500 lignes, commit par ligne)")
    b = await benchmark_sqlite_wal(n_rows=500)
    print(f"    AVANT (sans WAL) : {b['no_wal_seconds']:.3f}s")
    print(f"    APRÈS (WAL)      : {b['wal_seconds']:.3f}s")
    print(f"    Accélération     : {b['speedup_x']:.1f}x")

    print("\n[C] Rendu GUI — 500 annonces (widgets empilés vs Treeview)")
    c = benchmark_gui_rendering(n=500)
    if c is None:
        print("    (ignoré — pas d'environnement graphique disponible ici ; "
              "s'exécute normalement sur ta machine)")
    else:
        print(f"    AVANT — temps: {c['old_render_seconds']:.3f}s, widgets créés: {c['old_widget_count']}")
        print(f"    APRÈS — temps: {c['new_render_seconds']:.3f}s, widgets créés: {c['new_widget_count']}")

    print("\n[D] Cache de dédup mémoire (référence — inchangé, déjà O(1) avant/après)")
    d = benchmark_dedup_throughput(n=50_000)
    print(f"    {d['n']} IDs — 1ère passe (tous nouveaux)   : {d['first_pass_ops_per_sec']:,.0f} ops/s")
    print(f"    {d['n']} IDs — 2e passe (tous doublons)     : {d['dedup_pass_ops_per_sec']:,.0f} ops/s")

    cpu1, rss1 = _cpu_seconds(), _rss_kb()

    print("\n" + "=" * 72)
    print("RÉCAPITULATIF")
    print("=" * 72)
    print(f"CPU process consommé pendant le benchmark : {cpu1 - cpu0:.2f}s")
    print(f"RSS avant / pic après le benchmark          : {rss0 / 1024:.1f} MB → {rss1 / 1024:.1f} MB")
    print("""
Non mesurable depuis cet environnement (pas de sortie réseau vers Vinted /
Telegram) : temps de scan réel, latence réelle, nombre de requêtes/min en
conditions réelles. Une fois le bot lancé sur ta machine avec ta propre
connexion, consulte l'onglet 📊 Dashboard : les mesures API / Parsing /
Processing / TOTAL et la latence min/moyenne/max y sont calculées en direct,
sur du trafic réel — c'est la source à citer pour ces chiffres.
""")


if __name__ == "__main__":
    asyncio.run(main())
