"""Provider additions are discovered and validated without a hardcoded test list."""

import importlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import providers
from app import provider_registry as registry


class RegistryTests(unittest.TestCase):
    def tearDown(self):
        registry.discover.cache_clear()
        sys.modules.pop("providers.fixture_site", None)
        sys.modules.pop("providers.broken_site", None)
        importlib.invalidate_caches()

    def test_every_real_provider_definition_is_valid_and_documented(self):
        self.assertEqual(registry.definition_errors(), [])
        for key, module in registry.definitions().items():
            with self.subTest(provider=key):
                self.assertTrue(module.__doc__)
                self.assertTrue(module.TEST_URLS)
                self.assertEqual(registry.validate(module, key), key)

    def test_drop_in_file_is_discovered_in_inventory_and_network_suite(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "fixture_site.py").write_text(
                '"""Plain yt-dlp output needs no hooks."""\nNAME="Fixture Site"\nTEST_URLS=[{"kind":"video","url":"https://videos.example/watch/123"}]\n'
            )
            with patch.object(providers, "__path__", [directory]):
                registry.discover.cache_clear()
                importlib.invalidate_caches()
                self.assertEqual(registry.definition_errors(), [])
                self.assertEqual(registry.test_cases()[0]["provider"], "fixture_site")
                self.assertEqual(registry.catalog()[0]["handlers"], {})
                self.assertEqual(registry.identify("Fixture_SiteVideo"), "fixture_site")

    def test_malformed_site_is_reported_without_hiding_valid_sites(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "broken_site.py").write_text('NAME="Broken"\n')
            with patch.object(
                providers, "__path__", list(providers.__path__) + [directory]
            ):
                registry.discover.cache_clear()
                importlib.invalidate_caches()
                self.assertIn("youtube", registry.definitions())
                errors = registry.definition_errors()
                self.assertEqual(len(errors), 1)
                self.assertEqual(errors[0]["provider"], "broken_site")
                self.assertIn("TEST_URLS", errors[0]["message"])

    def test_unknown_provider_fails_instead_of_running_zero_tests(self):
        with self.assertRaisesRegex(ValueError, "Unknown provider"):
            registry.test_cases(["not-a-real-provider"])

    def test_volatile_cases_are_explicitly_optional(self):
        self.assertFalse(any(c["kind"] == "live" for c in registry.test_cases()))
        self.assertTrue(
            any(c["kind"] == "live" for c in registry.test_cases(include_live=True))
        )
        self.assertFalse(any(c.get("experimental") for c in registry.test_cases()))
        self.assertTrue(
            any(
                c.get("experimental")
                for c in registry.test_cases(include_experimental=True)
            )
        )
