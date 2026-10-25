"""Fuentes públicas y coordinación aislada; ninguna prueba usa red real."""

import json
import tempfile
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace

from monitor.app import run_retailers
from monitor.http import RemoteError
from monitor.models import Product, SearchResult, Stock
from monitor.ripley_background import RipleyDiscoveryManager
from monitor.ripley_worker import public_settings
from monitor.settings import Settings
from monitor.store import Store
from tiendas.ripley import Ripley
from tiendas.ripley_discovery import DiscoveryClient, BudgetExceeded, expanded_discovery
from test_ripley import URL, schema, detail_html

BASE = "https://simple.ripley.com.pe"
ROOT = BASE + "/sitemap.xml"
LEAF = BASE + "/sitemap_productos_1P.xml"
ROBOTS = "User-agent: *\nAllow: /\nSitemap: " + ROOT


def xml(kind, urls):
    tag = "sitemap" if kind == "sitemapindex" else "url"
    return (
        "<"
        + kind
        + ' xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(f"<{tag}><loc>{url}</loc></{tag}>" for url in urls)
        + "</"
        + kind
        + ">"
    )


class MappingClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        value = self.pages[url]
        if isinstance(value, Exception):
            raise value
        return value


def adapter(pages):
    a = Ripley()
    a.client = MappingClient(pages)
    a.search = Mock(return_value=SearchResult())
    return a


class SourceTests(unittest.TestCase):
    def test_sitemap_finds_target_missing_from_search_even_out_of_stock(self):
        a = adapter(
            {
                BASE + "/robots.txt": ROBOTS,
                ROOT: xml("sitemapindex", [LEAF]),
                LEAF: xml("urlset", [URL]),
                URL: detail_html(schema("https://schema.org/OutOfStock")),
            }
        )
        result = expanded_discovery(a)
        self.assertTrue(result.complete)
        self.assertEqual(len(result.products), 1)
        self.assertEqual(result.products[0].classification, "CANDIDATO")
        self.assertEqual(a.stock(result.products[0]).state, "AGOTADO")

    def test_marketplace_map_not_requested(self):
        third = BASE + "/sitemap_productos_3P.xml"
        a = adapter(
            {
                BASE + "/robots.txt": ROBOTS,
                ROOT: xml("sitemapindex", [LEAF, third]),
                LEAF: xml("urlset", []),
            }
        )
        self.assertTrue(expanded_discovery(a).complete)
        self.assertNotIn(third, a.client.calls)

    def test_category_links_find_targets(self):
        categories = BASE + "/sitemap_categorias.xml"
        category = BASE + "/jugueteria/kidults/cartas-pokemon"
        a = adapter(
            {
                BASE + "/robots.txt": ROBOTS,
                ROOT: xml("sitemapindex", [categories]),
                categories: xml("urlset", [category]),
                category: f'<a href="{URL}">ETB</a>',
                URL: detail_html(),
            }
        )
        result = expanded_discovery(a)
        self.assertTrue(result.complete)
        self.assertEqual(result.products[0].url, URL)
        self.assertEqual(result.stats["source_pages"], 1)

    def test_known_candidate_not_refetched_by_expansion(self):
        a = adapter(
            {
                BASE + "/robots.txt": ROBOTS,
                ROOT: xml("sitemapindex", [LEAF]),
                LEAF: xml("urlset", [URL]),
            }
        )
        result = expanded_discovery(a, known_urls=[URL])
        self.assertTrue(result.complete)
        self.assertNotIn(URL, a.client.calls)

    def test_navigation_and_sitemap_duplicates_fetched_once(self):
        a = adapter(
            {
                BASE + "/robots.txt": ROBOTS,
                ROOT: xml("sitemapindex", [LEAF]),
                LEAF: xml("urlset", [URL, URL]),
                URL: detail_html(),
            }
        )
        self.assertEqual(len(expanded_discovery(a).products), 1)
        self.assertEqual(a.client.calls.count(URL), 1)

    def test_unknown_stock_keeps_candidate(self):
        raw = schema()
        raw["offers"].pop("availability")
        a = adapter(
            {
                BASE + "/robots.txt": ROBOTS,
                ROOT: xml("sitemapindex", [LEAF]),
                LEAF: xml("urlset", [URL]),
                URL: detail_html(raw),
            }
        )
        p = expanded_discovery(a).products[0]
        self.assertEqual(p.classification, "CANDIDATO")
        self.assertEqual(a.stock(p).state, "DESCONOCIDO")

    def test_marketplace_link_never_candidate(self):
        raw = schema()
        raw["offers"]["seller"]["name"] = "Otro comercio"
        a = adapter(
            {
                BASE + "/robots.txt": ROBOTS,
                ROOT: xml("sitemapindex", [LEAF]),
                LEAF: xml("urlset", [URL]),
                URL: detail_html(raw),
            }
        )
        self.assertEqual(expanded_discovery(a).products, [])

    def test_robots_disallowed_product_not_requested(self):
        rules = "User-agent: *\nDisallow: /pokemon-tcg\nSitemap: " + ROOT
        a = adapter(
            {
                BASE + "/robots.txt": rules,
                ROOT: xml("sitemapindex", [LEAF]),
                LEAF: xml("urlset", [URL]),
            }
        )
        expanded_discovery(a)
        self.assertNotIn(URL, a.client.calls)

    def test_other_domains_not_requested(self):
        external = "https://evil.example/sitemap_1P.xml"
        a = adapter(
            {BASE + "/robots.txt": ROBOTS, ROOT: xml("sitemapindex", [external])}
        )
        expanded_discovery(a)
        self.assertNotIn(external, a.client.calls)

    def test_invalid_xml_preserves_partial_search(self):
        a = adapter({BASE + "/robots.txt": ROBOTS, ROOT: "not XML"})
        p = Ripley().normalize(dict(schema(), url=URL))
        a.search.return_value = SearchResult(products=[p])
        result = expanded_discovery(a)
        self.assertFalse(result.complete)
        self.assertEqual(result.products, [p])

    def test_budget_expiry_is_partial_not_false_empty_success(self):
        a = adapter({BASE + "/robots.txt": BudgetExceeded("test")})
        result = expanded_discovery(a)
        self.assertFalse(result.complete)
        self.assertIn("test", result.errors)


class BudgetTests(unittest.TestCase):
    def response(self, status=200, chunks=(b"<html/>",), headers=None):
        r = Mock(
            status_code=status,
            ok=status == 200,
            headers=headers or {},
            encoding="utf-8",
        )
        r.__enter__ = Mock(return_value=r)
        r.__exit__ = Mock(return_value=False)
        r.iter_content.return_value = iter(chunks)
        return r

    def test_limit_prevents_request(self):
        client = DiscoveryClient(Settings(), requests_limit=0)
        client.session = Mock()
        with self.assertRaises(BudgetExceeded):
            client.get(BASE)
        client.session.get.assert_not_called()

    def test_streamed_size_limit(self):
        class Huge:
            def __len__(self):
                return 33 * 1024 * 1024

        client = DiscoveryClient(Settings())
        client.session = Mock()
        client.session.get.return_value = self.response(chunks=(Huge(),))
        with self.assertRaises(BudgetExceeded):
            client.get(BASE)

    def test_external_redirect_rejected(self):
        client = DiscoveryClient(Settings())
        client.session = Mock()
        client.session.get.return_value = self.response(
            302, headers={"Location": "https://evil.example/"}
        )
        with self.assertRaises(ValueError):
            client.get(BASE)
        self.assertEqual(client.session.get.call_count, 1)

    def test_429_blocks_rest_of_job_and_preserves_delay(self):
        client = DiscoveryClient(Settings())
        client.session = Mock()
        client.session.get.return_value = self.response(
            429, headers={"Retry-After": "1200"}
        )
        for _ in range(2):
            with self.assertRaises(RemoteError):
                client.get(BASE)
        self.assertEqual(client.session.get.call_count, 1)
        self.assertEqual(client.retry_after, 1200)

    def test_no_tls_bypass_or_implicit_redirects(self):
        client = DiscoveryClient(Settings())
        client.session = Mock()
        client.session.get.return_value = self.response()
        self.assertEqual(client.get(BASE, as_text=True), "<html/>")
        options = client.session.get.call_args.kwargs
        self.assertFalse(options["allow_redirects"])
        self.assertTrue(options["stream"])
        self.assertNotEqual(options.get("verify"), False)


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(data_dir=Path(self.temp.name))
        self.manager = RipleyDiscoveryManager(self.settings)
        self.store = Store(":memory:")
        self.log = Mock()

    def tearDown(self):
        self.manager.close()
        self.store.close()
        self.temp.cleanup()

    def result(self, product=None, **overrides):
        if product is None:
            product = Ripley().normalize(dict(schema(), url=URL))
        payload = {
            "version": 1,
            "completed_at": time.time(),
            "complete": True,
            "products": [asdict(product)],
            "errors": [],
            "stats": {},
            "retry_after": 0,
        }
        payload.update(overrides)
        path = self.manager.folder / ("a" * 32 + ".result.json")
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_import_never_creates_alert_from_worker_snapshot(self):
        self.result()
        self.manager.consume(self.store, self.log)
        self.assertEqual(len(self.store.products()), 1)
        self.assertEqual(
            self.store.db.execute("SELECT state FROM products").fetchone()[0],
            "DESCONOCIDO",
        )
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM events").fetchone()[0], 0
        )

    def test_import_preserves_current_known_stock(self):
        p = Ripley().normalize(dict(schema(), url=URL))
        self.store.observe(p, Stock("AGOTADO"))
        self.result(p)
        self.manager.consume(self.store, self.log)
        self.assertEqual(
            self.store.db.execute("SELECT last_known FROM products").fetchone()[0],
            "AGOTADO",
        )

    def test_stale_result_not_imported(self):
        self.result(completed_at=time.time() - 20000)
        self.manager.consume(self.store, self.log)
        self.assertEqual(self.store.products(), [])
        self.assertFalse(self.store.has_discovery_success("Ripley"))

    def test_marketplace_result_rejected(self):
        raw = schema()
        raw["offers"]["seller"]["name"] = "Otro"
        p = Ripley().normalize(dict(raw, url=URL))
        self.result(p)
        self.manager.consume(self.store, self.log)
        self.assertEqual(self.store.products(), [])

    def test_config_has_no_credentials_or_chat(self):
        config = public_settings(Settings(token="real-secret", chat_id="private-chat"))
        text = json.dumps(config)
        self.assertNotIn("real-secret", text)
        self.assertNotIn("private-chat", text)

    @patch("monitor.ripley_background.subprocess.Popen")
    def test_running_child_is_never_joined_by_tick(self, spawn):
        child = Mock()
        child.poll.return_value = None
        spawn.return_value = child
        self.manager.tick(self.store, self.log)
        self.manager.tick(self.store, self.log)
        child.wait.assert_not_called()
        child.terminate.assert_not_called()
        self.assertEqual(spawn.call_count, 1)

    @patch("monitor.ripley_background.subprocess.Popen")
    def test_launch_and_import_leave_plaza_and_oechsle_clocks_unchanged(self, spawn):
        child = Mock()
        child.poll.return_value = None
        spawn.return_value = child
        self.store.mark_discovery(successful=True, retailer="Plaza Vea")
        self.store.mark_discovery(successful=True, retailer="Oechsle")
        before = dict(
            self.store.db.execute(
                "SELECT key,value FROM meta WHERE key IN ('discovery_success','oechsle:discovery_success')"
            )
        )
        self.manager.tick(self.store, self.log)
        self.result()
        self.manager.consume(self.store, self.log)
        after = dict(
            self.store.db.execute(
                "SELECT key,value FROM meta WHERE key IN ('discovery_success','oechsle:discovery_success')"
            )
        )
        self.assertEqual(before, after)

    @patch("monitor.app.scheduled_cycle", return_value=True)
    @patch("monitor.app.cycle", return_value=True)
    def test_plaza_pipeline_unchanged_and_ripley_not_searched_synchronously(
        self, fast, scheduled
    ):
        manager = Mock()
        manager.blocked.return_value = False
        pv = Mock()
        rp = Mock()
        telegram = Mock()
        self.assertTrue(
            run_retailers(
                self.settings,
                self.store,
                {"Plaza Vea": (self.settings, pv), "Ripley": (self.settings, rp)},
                telegram,
                self.log,
                ripley_discovery=manager,
            )
        )
        self.assertEqual(scheduled.call_count, 1)
        self.assertIs(scheduled.call_args.args[2], pv)
        self.assertEqual(scheduled.call_args.kwargs["retailer"], "Plaza Vea")
        self.assertFalse(fast.call_args.kwargs["discover"])
        rp.search.assert_not_called()

    @patch("monitor.ripley_background.subprocess.Popen")
    def test_single_run_detaches_worker_without_killing_it(self, spawn):
        child = Mock()
        child.poll.return_value = None
        spawn.return_value = child
        self.manager.tick(self.store, self.log)
        self.manager.close(detach=True)
        child.terminate.assert_not_called()
        self.manager.process = None

    def test_worker_does_not_open_database_or_telegram(self):
        from monitor.ripley_worker import execute

        p = Ripley().normalize(dict(schema(), url=URL))
        result = SearchResult(products=[p])
        with patch("monitor.ripley_worker.expanded_discovery", return_value=result):
            output = execute(
                {
                    "settings": public_settings(self.settings),
                    "seconds": 30,
                    "requests": 10,
                    "products": 5,
                }
            )
        self.assertEqual(output["products"][0]["url"], URL)
        self.assertEqual(self.store.products(), [])


class CadenceTests(unittest.TestCase):
    def test_intentional_budget_limit_does_not_request_fast_retry(self):
        a = adapter({BASE + "/robots.txt": BudgetExceeded("test")})
        result = expanded_discovery(a)
        self.assertFalse(result.complete)
        self.assertFalse(result.retryable)

    def test_real_network_error_requests_retry(self):
        a = adapter({BASE + "/robots.txt": RemoteError(503)})
        result = expanded_discovery(a)
        self.assertFalse(result.complete)
        self.assertTrue(result.retryable)

    def test_limited_completed_result_preserves_four_hour_cadence(self):
        with tempfile.TemporaryDirectory() as d:
            settings = Settings(data_dir=Path(d))
            manager = RipleyDiscoveryManager(settings)
            store = Store(":memory:")
            result = {
                "version": 1,
                "completed_at": time.time(),
                "complete": False,
                "retryable": False,
                "products": [],
                "errors": ["Límite de páginas"],
                "stats": {},
            }
            (manager.folder / ("c" * 32 + ".result.json")).write_text(
                json.dumps(result)
            )
            manager.consume(store, Mock())
            self.assertFalse(store.discovery_due(14400, 900, retailer="Ripley"))
            store.close()


class QueryIsolationTests(unittest.TestCase):
    def test_expanding_ripley_does_not_modify_original_terms(self):
        from tiendas.ripley_discovery import broadened_settings

        settings = Settings(terms=["pokemon tcg ingles"])
        original = list(settings.terms)
        expanded = broadened_settings(settings.for_retailer("ripley"))
        self.assertIn("pokemon", expanded.terms)
        self.assertEqual(settings.terms, original)
        self.assertEqual(settings.for_retailer("plazavea").terms, original)


class WatchdogTests(unittest.TestCase):
    @patch("monitor.ripley_worker.threading.Timer")
    @patch("monitor.ripley_worker.execute")
    def test_worker_has_process_local_timeout(self, execute, timer):
        from monitor.ripley_worker import main

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            request = root / "job.request.json"
            output = root / "job.result.json"
            request.write_text(json.dumps({"seconds": 30}))
            execute.return_value = {
                "version": 1,
                "complete": True,
                "products": [],
                "stats": {},
            }
            with patch(
                "argparse.ArgumentParser.parse_args",
                return_value=SimpleNamespace(request=request, output=output),
            ), patch("builtins.print"):
                self.assertEqual(main(), 0)
            self.assertEqual(timer.call_args.args[0], 40)
            timer.return_value.start.assert_called_once()
            timer.return_value.cancel.assert_called_once()
            self.assertTrue(output.exists())


if __name__ == "__main__":
    unittest.main()
