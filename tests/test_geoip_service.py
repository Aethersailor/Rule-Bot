import unittest

from src.services.geoip_service import GeoIPService
from src.utils.cache import TTLCache


class _Country:
    iso_code = "US"
    names = {"zh-CN": "美国"}
    name = "United States"


class _Response:
    country = _Country()
    registered_country = _Country()
    represented_country = _Country()


class _Reader:
    def __init__(self):
        self.calls = 0

    def country(self, _ip):
        self.calls += 1
        return _Response()


class TestGeoIPService(unittest.TestCase):
    def test_location_lookup_populates_and_reuses_empty_cache(self):
        reader = _Reader()
        service = GeoIPService.__new__(GeoIPService)
        service.reader = reader
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


if __name__ == "__main__":
    unittest.main()
