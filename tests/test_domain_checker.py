import asyncio
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, patch

import dns.rcode

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from src.services.domain_checker import DomainChecker
from src.services.dns_service import DNSService


class DummyDNSService:
    async def query_a_record(self, domain: str, use_edns_china: bool = True):
        if domain == "ns1.example.com":
            return ["2.2.2.2"]
        return ["1.1.1.1"]

    async def query_ns_records(self, domain: str):
        return ["ns1.example.com"]


class DummyGeoIPService:
    def get_location_info(self, ip: str):
        if ip == "1.1.1.1":
            return {
                "ip": ip,
                "country_code": "CN",
                "country_name": "China",
                "is_china": True,
            }
        return {
            "ip": ip,
            "country_code": "US",
            "country_name": "United States",
            "is_china": False,
        }


class DummyDNSServiceNoChina:
    async def query_a_record(self, domain: str, use_edns_china: bool = True):
        return ["8.8.8.8"]

    async def query_ns_records(self, domain: str):
        return ["ns1.example.net"]


class DummyGeoIPServiceNoChina:
    def get_location_info(self, ip: str):
        return {
            "ip": ip,
            "country_code": "US",
            "country_name": "United States",
            "is_china": False,
        }


class DummyDNSServiceConcurrentNS:
    async def query_a_record(self, domain: str, use_edns_china: bool = True):
        if domain == "example.org":
            return ["1.1.1.1"]
        if domain == "ns1.example.org":
            await asyncio.sleep(0.05)
            return ["2.2.2.2"]
        if domain == "ns2.example.org":
            await asyncio.sleep(0.05)
            return ["3.3.3.3"]
        return []

    async def query_ns_records(self, domain: str):
        return ["ns1.example.org", "ns2.example.org"]


class DummyGeoIPServiceMixed:
    def get_location_info(self, ip: str):
        if ip == "1.1.1.1":
            return {
                "ip": ip,
                "country_code": "CN",
                "country_name": "China",
                "is_china": True,
            }
        if ip == "2.2.2.2":
            return {
                "ip": ip,
                "country_code": "CN",
                "country_name": "China",
                "is_china": True,
            }
        return {
            "ip": ip,
            "country_code": "US",
            "country_name": "United States",
            "is_china": False,
        }


class DummyDNSServiceNSConsensus:
    doh_servers = {
        "resolver-1": "https://resolver-1.example/dns-query",
        "resolver-2": "https://resolver-2.example/dns-query",
    }

    async def query_a_record(self, domain: str, use_edns_china: bool = True):
        if domain == "example.com":
            return ["8.8.8.8"]
        return []

    async def query_a_record_evidence(
        self, domain: str, use_edns_china: bool = True
    ):
        addresses = {
            "ns1.example.net": "1.1.1.1",
            "ns2.example.net": "2.2.2.2",
            "ns3.example.net": "8.8.8.8",
        }
        ip = addresses.get(domain)
        if not ip:
            return {}
        return {
            "resolver-1": [ip],
            "resolver-2": [ip],
        }

    async def query_ns_records(self, domain: str):
        return [
            "ns1.example.net",
            "ns2.example.net",
            "ns3.example.net",
        ]


class DummyGeoIPServiceStrictConsensus(DummyGeoIPServiceNoChina):
    def get_location_info(self, ip: str):
        if ip in {"1.1.1.1", "2.2.2.2"}:
            return {
                "ip": ip,
                "country_code": "CN",
                "country_name": "China",
                "is_china": True,
            }
        return super().get_location_info(ip)

    def is_strict_china_ip(self, ip: str):
        return ip in {"1.1.1.1", "2.2.2.2"}


class DummyDNSServiceOathLike(DummyDNSServiceNSConsensus):
    async def query_a_record_evidence(
        self, domain: str, use_edns_china: bool = True
    ):
        index = int(domain[2]) if domain.startswith("ns") else 0
        ip = "202.165.97.53" if index == 5 else f"203.0.113.{index}"
        return {
            "resolver-1": [ip],
            "resolver-2": [ip],
        }

    async def query_ns_records(self, domain: str):
        return [f"ns{index}.example.net" for index in range(1, 9)]


class DummyGeoIPServiceOathLike(DummyGeoIPServiceNoChina):
    def get_location_info(self, ip: str):
        if ip == "202.165.97.53":
            return {
                "ip": ip,
                "country_code": "CN",
                "country_name": "China",
                "is_china": True,
            }
        return super().get_location_info(ip)

    def is_strict_china_ip(self, ip: str):
        return False


class DummyDNSServiceUnavailable:
    async def query_a_record(self, domain: str, use_edns_china: bool = True):
        return []

    async def query_ns_records(self, domain: str):
        return []


class DummyDNSServiceNXDOMAIN(DummyDNSServiceUnavailable):
    async def classify_domain_resolution(self, domain: str):
        return "nxdomain"


class DummyDNSServiceEmpty(DummyDNSServiceUnavailable):
    async def classify_domain_resolution(self, domain: str):
        return "empty"


class DummyDNSServiceConcurrentPrimary:
    async def query_a_record(self, domain: str, use_edns_china: bool = True):
        await asyncio.sleep(0.05)
        return ["1.1.1.1"]

    async def query_ns_records(self, domain: str):
        await asyncio.sleep(0.05)
        return []


class TestDomainChecker(unittest.IsolatedAsyncioTestCase):
    async def test_check_domain_comprehensive_china_ip(self):
        checker = DomainChecker(DummyDNSService(), DummyGeoIPService())
        result = await checker.check_domain_comprehensive("www.example.com")

        self.assertEqual(result["second_level_domain"], "example.com")
        self.assertTrue(result["domain_china_status"] or result["second_level_china_status"])
        self.assertFalse(result["ns_china_status"])
        self.assertTrue(checker.should_add_directly(result))
        self.assertEqual(checker.get_target_domain_to_add(result), "example.com")

    async def test_check_domain_comprehensive_reject(self):
        checker = DomainChecker(DummyDNSServiceNoChina(), DummyGeoIPServiceNoChina())
        result = await checker.check_domain_comprehensive("example.net")

        self.assertTrue(checker.should_reject(result))
        self.assertIsNone(checker.get_target_domain_to_add(result))
        self.assertIn("example.net", result["recommendation"])
        self.assertIn("可注册域名", result["recommendation"])
        self.assertNotIn("二级域名", result["recommendation"])
        self.assertNotIn("None", result["recommendation"])

    async def test_ns_ip_queries_run_concurrently(self):
        checker = DomainChecker(DummyDNSServiceConcurrentNS(), DummyGeoIPServiceMixed())

        start = time.perf_counter()
        result = await checker.check_domain_comprehensive("example.org")
        duration = time.perf_counter() - start

        self.assertFalse(result["ns_china_status"])
        self.assertIn("NS 严格判定: 1/2 个主机在中国大陆", result["details"])
        self.assertLess(duration, 0.09)

    async def test_ns_cluster_quorum_can_authorize_foreign_domain_ip(self):
        checker = DomainChecker(
            DummyDNSServiceNSConsensus(),
            DummyGeoIPServiceStrictConsensus(),
        )

        result = await checker.check_domain_comprehensive("example.com")

        self.assertFalse(result["domain_china_status"])
        self.assertTrue(result["ns_china_status"])
        self.assertEqual(result["ns_china_hosts"], 2)
        self.assertEqual(result["ns_resolved_hosts"], 3)
        self.assertTrue(checker.should_add_directly(result))
        self.assertFalse(checker.should_reject(result))
        self.assertEqual(checker.get_target_domain_to_add(result), "example.com")

    async def test_single_conflicting_ns_ip_cannot_authorize_domain(self):
        checker = DomainChecker(
            DummyDNSServiceOathLike(),
            DummyGeoIPServiceOathLike(),
        )

        result = await checker.check_domain_comprehensive("example.com")

        self.assertFalse(result["domain_china_status"])
        self.assertFalse(result["ns_china_status"])
        self.assertEqual(result["ns_china_hosts"], 0)
        self.assertEqual(result["ns_conflict_hosts"], 1)
        self.assertTrue(checker.should_reject(result))
        self.assertIsNone(checker.get_target_domain_to_add(result))
        self.assertIn("NS 归属数据冲突: 1 个主机", result["details"])

    async def test_empty_dns_result_is_unknown_not_foreign(self):
        checker = DomainChecker(DummyDNSServiceUnavailable(), DummyGeoIPServiceNoChina())
        result = await checker.check_domain_comprehensive("example.com")

        self.assertEqual(result["lookup_status"], "unknown")
        self.assertIn("error", result)
        self.assertFalse(checker.should_reject(result))

    async def test_confirmed_nxdomain_is_terminal_without_foreign_verdict(self):
        checker = DomainChecker(DummyDNSServiceNXDOMAIN(), DummyGeoIPServiceNoChina())
        result = await checker.check_domain_comprehensive("does-not-exist.example")

        self.assertEqual(result["lookup_status"], "nxdomain")
        self.assertEqual(result["error_code"], "nxdomain")
        self.assertFalse(checker.should_reject(result))

    async def test_confirmed_empty_dns_answer_is_terminal_policy_rejection(self):
        checker = DomainChecker(DummyDNSServiceEmpty(), DummyGeoIPServiceNoChina())
        result = await checker.check_domain_comprehensive("empty.example")

        self.assertEqual(result["lookup_status"], "empty")
        self.assertEqual(result["error_code"], "empty_dns")
        self.assertFalse(checker.should_reject(result))

    async def test_primary_a_and_ns_queries_run_concurrently(self):
        checker = DomainChecker(DummyDNSServiceConcurrentPrimary(), DummyGeoIPService())

        start = time.perf_counter()
        result = await checker.check_domain_comprehensive("example.com")
        duration = time.perf_counter() - start

        self.assertEqual(result["lookup_status"], "ok")
        self.assertLess(duration, 0.09)


class TestDNSResolutionClassification(unittest.IsolatedAsyncioTestCase):
    async def test_a_record_evidence_collects_and_caches_all_resolvers(self):
        service = DNSService(
            {
                "resolver-1": "https://resolver-1.example/dns-query",
                "resolver-2": "https://resolver-2.example/dns-query",
                "resolver-3": "https://resolver-3.example/dns-query",
            }
        )
        service.start = AsyncMock()
        service._build_dns_query = lambda *_args, **_kwargs: b"query"
        service._perform_doh_query = AsyncMock(
            side_effect=[
                ["1.1.1.1"],
                ["2.2.2.2"],
                RuntimeError("resolver unavailable"),
            ]
        )

        first = await service.query_a_record_evidence("example.com")
        second = await service.query_a_record_evidence("example.com")

        self.assertEqual(
            first,
            {
                "resolver-1": ["1.1.1.1"],
                "resolver-2": ["2.2.2.2"],
            },
        )
        self.assertEqual(second, first)
        self.assertEqual(service._perform_doh_query.await_count, 3)

    async def classify(self, doh_statuses, system_status):
        service = DNSService(
            {f"resolver-{index}": f"https://resolver-{index}.example/dns-query"
             for index in range(len(doh_statuses))}
        )
        service.start = AsyncMock()
        service._build_dns_query = lambda *_args, **_kwargs: b"query"
        service._perform_doh_rcode_query = AsyncMock(side_effect=doh_statuses)
        with patch.object(
            service,
            "_query_system_dns_status_sync",
            return_value=system_status,
        ):
            return await service.classify_domain_resolution("example.com")

    async def test_two_independent_nxdomain_answers_are_terminal(self):
        status = await self.classify(
            [(dns.rcode.NXDOMAIN, 0), (dns.rcode.NXDOMAIN, 0)], None
        )
        self.assertEqual(status, "nxdomain")

    async def test_nonempty_noerror_answer_wins_over_negative_answers(self):
        status = await self.classify(
            [(dns.rcode.NXDOMAIN, 0), (dns.rcode.NOERROR, 1)],
            (dns.rcode.NXDOMAIN, 0),
        )
        self.assertEqual(status, "exists")

    async def test_two_independent_empty_answers_are_terminal(self):
        status = await self.classify(
            [(dns.rcode.NOERROR, 0), (dns.rcode.NOERROR, 0)], None
        )
        self.assertEqual(status, "empty")

    async def test_one_negative_answer_is_not_enough_for_terminal_result(self):
        status = await self.classify([(dns.rcode.NXDOMAIN, 0), None], None)
        self.assertEqual(status, "unknown")


if __name__ == '__main__':
    unittest.main()
