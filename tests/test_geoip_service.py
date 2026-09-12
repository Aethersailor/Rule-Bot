import unittest

from src.services.geoip_service import GeoIPService
from src.utils.cache import TTLCache


class _Country:
    def __init__(self, code="US"):
        self.iso_code = code
        self.names = {"zh-CN": "中国" if code == "CN" else "美国"}
        self.name = "China" if code == "CN" else "United States"


class _Response:
    def __init__(self, code="US"):
        self.country = _Country(code)
        self.registered_country = _Country(code)
        self.represented_country = _Country(code)


class _Reader:
    def __init__(self, code="US"):
        self.calls = 0
        self.code = code

    def country(self, _ip):
        self.calls += 1
        return _Response(self.code)


class TestGeoIPService(unittest.TestCase):
    def test_location_lookup_populates_and_reuses_empty_cache(self):
        reader = _Reader()
        service = GeoIPService.__new__(GeoIPService)
        service.reader = reader
        service.baseline_reader = None
        service.baseline_geoip_file = None
        service._cn_ipv4_ranges = []
        service._cn_ipv4_range_starts = []
        service._location_cache = TTLCache(16, 60)

        first = service.get_location_info("8.8.8.8")
        second = service.get_location_info("8.8.8.8")

        self.assertEqual(first, second)
        self.assertEqual(first["country_code"], "US")
        self.assertEqual(first["country_name"], "美国")
        self.assertEqual(reader.calls, 1)
        self.assertEqual(len(service._location_cache), 1)

    def test_strict_china_requires_primary_and_baseline_agreement(self):
        service = GeoIPService.__new__(GeoIPService)
        service.reader = _Reader("CN")
        service.baseline_reader = _Reader("US")
        service.baseline_geoip_file = None
        service._cn_ipv4_ranges = []
        service._cn_ipv4_range_starts = []
        service._location_cache = TTLCache(16, 60)

        location = service.get_location_info("202.165.97.53")

        self.assertTrue(location["is_china"])
        self.assertEqual(location["baseline_country_code"], "US")
        self.assertFalse(location["strict_is_china"])
        self.assertFalse(service.is_strict_china_ip("202.165.97.53"))

    def test_strict_china_accepts_matching_primary_and_baseline(self):
        service = GeoIPService.__new__(GeoIPService)
        service.reader = _Reader("CN")
        service.baseline_reader = _Reader("CN")
        service.baseline_geoip_file = None
        service._cn_ipv4_ranges = []
        service._cn_ipv4_range_starts = []
        service._location_cache = TTLCache(16, 60)

        self.assertTrue(service.is_strict_china_ip("1.2.3.4"))


if __name__ == "__main__":
    unittest.main()
