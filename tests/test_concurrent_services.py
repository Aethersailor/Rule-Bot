import asyncio
import base64
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from github import GithubException

from src.services.github_service import GitHubService
from src.services.dns_service import DNSService


class FakeRepository:
    def __init__(self):
        self.content = "# 以下域名待提交 PR\nDOMAIN-SUFFIX,existing.com\n"
        self.sha = "blob-0"
        self.reads = self.writes = 0

    def get_contents(self, *args, **kwargs):
        self.reads += 1
        content, sha = self.content, self.sha
        time.sleep(.01)
        return SimpleNamespace(content=base64.b64encode(content.encode()).decode(), sha=sha)

    def update_file(self, path, message, content, sha, **kwargs):
        if sha != self.sha:
            raise GithubException(409, {"message": "conflict"}, {})
        self.writes += 1
        self.content = content
        self.sha = f"blob-{self.writes}"
        return {"commit": SimpleNamespace(sha=f"commit-{self.writes}"), "content": SimpleNamespace(sha=self.sha)}


def github_service():
    config = SimpleNamespace(GITHUB_TOKEN="test", GITHUB_REPO="example/repo", GITHUB_BRANCH="main",
                             DIRECT_RULE_FILE="rules.list", GITHUB_COMMIT_NAME="Bot", GITHUB_COMMIT_EMAIL="bot@example.com",
                             GITHUB_FILE_CACHE_SIZE=4, GITHUB_FILE_CACHE_TTL=60)
    with patch.object(GitHubService, "_initialize_repo"):
        service = GitHubService(config)
    service.repo = FakeRepository()
    return service


class TestConcurrentGitHub(unittest.IsolatedAsyncioTestCase):
    async def test_large_contents_response_reads_exact_blob_once(self):
        service = github_service()
        content = "DOMAIN-SUFFIX,existing.com\n" + "# padding\n" * 120000
        self.assertGreater(len(content), 1024*1024)
        service.repo.get_contents = MagicMock(return_value=SimpleNamespace(content="", encoding="none", sha="large-blob", size=len(content)))
        service.repo.get_git_blob = MagicMock(return_value=SimpleNamespace(content=base64.b64encode(content.encode()).decode(), encoding="base64"))
        try:
            values = await asyncio.gather(*(service.get_rule_file_data("rules.list") for _ in range(64)))
            self.assertTrue(all(value == {"content":content, "sha":"large-blob"} for value in values))
            service.repo.get_git_blob.assert_called_once_with("large-blob")
        finally:
            await service.aclose()

    async def test_omitted_nonempty_contents_never_becomes_an_empty_file_write(self):
        service = github_service()
        service.repo.get_contents = MagicMock(return_value=SimpleNamespace(content="", encoding="base64", sha="blob", size=100))
        try:
            self.assertIsNone(await service.get_rule_file_data("rules.list"))
            self.assertFalse((await service.add_domain_to_rules("added.com", "User"))["success"])
            self.assertEqual(service.repo.writes, 0)
        finally:
            await service.aclose()

    async def test_cold_different_domain_checks_share_read_and_analysis(self):
        service = github_service()
        try:
            with patch.object(service, "_analyze_rule_content", wraps=service._analyze_rule_content) as analyze:
                results = await asyncio.gather(*(service.check_domain_in_rules(f"d{i}.existing.com") for i in range(200)))
            self.assertTrue(all(result["exists"] for result in results))
            self.assertEqual(service.repo.reads, 1)
            self.assertEqual(analyze.call_count, 1)
        finally:
            await service.aclose()

    async def test_confirmed_writes_publish_real_blob_snapshot(self):
        service = github_service()
        try:
            results = await asyncio.gather(*(service.add_domain_to_rules(f"site-{i}.com", "User") for i in range(10)))
            self.assertTrue(all(result["success"] for result in results))
            self.assertEqual(service.repo.writes, 10)
            self.assertEqual(service.repo.reads, 1)
            for i in range(10):
                self.assertTrue((await service.check_domain_in_rules(f"site-{i}.com"))["exists"])
            self.assertEqual(service._file_cache.get("main:rules.list")["sha"], "blob-10")
        finally:
            await service.aclose()

    async def test_external_conflict_refreshes_and_preserves_other_rules(self):
        service = github_service()
        try:
            await service.get_rule_file_data("rules.list")
            service.repo.content += "DOMAIN-SUFFIX,external.com\n"
            service.repo.sha = "external-blob"
            result = await service.add_domain_to_rules("added.com", "User")
            self.assertTrue(result["success"])
            self.assertIn("DOMAIN-SUFFIX,external.com", service.repo.content)
            self.assertEqual(service.repo.reads, 2)
        finally:
            await service.aclose()

    async def test_slow_old_read_cannot_overwrite_confirmed_write(self):
        service = github_service()
        entered, release = threading.Event(), threading.Event()

        def slow_read(*args, **kwargs):
            entered.set()
            release.wait(2)
            return SimpleNamespace(content=base64.b64encode(b"old").decode(), sha="old-blob")

        service.repo.get_contents = slow_read
        task = asyncio.create_task(service.get_rule_file_data("rules.list"))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            service._publish_file("rules.list", "new", {"content": SimpleNamespace(sha="new-blob")})
            release.set()
            self.assertEqual(await task, {"content": "new", "sha": "new-blob"})
            self.assertEqual(service._file_cache.get("main:rules.list")["sha"], "new-blob")
        finally:
            release.set()
            await service.aclose()


class TestConcurrentDNS(unittest.IsolatedAsyncioTestCase):
    async def test_ns_and_evidence_lookups_coalesce_independently(self):
        service = DNSService({"one":"http://one", "two":"http://two", "three":"http://three"})
        await service.start()

        async def answer(*args):
            await asyncio.sleep(.01)
            return ["192.0.2.1"]
        try:
            with patch.object(service, "_perform_doh_query", side_effect=answer) as upstream:
                await asyncio.gather(*(service.query_ns_records("example.com") for _ in range(50)))
                self.assertEqual(upstream.call_count, 3)
                evidence = await asyncio.gather(*(service.query_a_record_evidence("example.com") for _ in range(50)))
                self.assertEqual(upstream.call_count, 6)
                self.assertEqual(set(evidence[0]), {"one", "two", "three"})
        finally:
            await service.close()

    async def test_resolution_classification_keeps_resolver_consensus(self):
        service = DNSService({"one":"http://one", "two":"http://two"})
        await service.start()

        async def nxdomain(*args):
            await asyncio.sleep(.01)
            return (3, 0)
        try:
            with patch.object(service, "_perform_doh_rcode_query", side_effect=nxdomain) as upstream, patch.object(service, "_query_system_dns_status_sync", return_value=None):
                statuses = await asyncio.gather(*(service.classify_domain_resolution("example.com") for _ in range(50)))
                self.assertEqual(statuses, ["nxdomain"] * 50)
                self.assertEqual(upstream.call_count, 2)
        finally:
            await service.close()

    async def test_queries_share_upstream_work_and_do_not_share_mutable_lists(self):
        service = DNSService({"one":"http://one", "two":"http://two", "three":"http://three"})
        await service.start()

        async def answer(*args):
            await asyncio.sleep(.02)
            return ["192.0.2.1"]

        try:
            with patch.object(service, "_perform_doh_query", side_effect=answer) as upstream:
                values = await asyncio.gather(*(service.query_a_record("example.com") for _ in range(100)))
                self.assertEqual(upstream.call_count, 3)
                values[0].append("192.0.2.2")
                self.assertEqual(await service.query_a_record("example.com"), ["192.0.2.1"])
                await service.query_a_record("example.com", False)
                self.assertEqual(upstream.call_count, 6)
        finally:
            await service.close()

    async def test_cancelled_last_caller_cancels_resolver_children(self):
        service = DNSService({"one":"http://one", "two":"http://two", "three":"http://three"})
        await service.start()
        active = 0
        entered = asyncio.Event()

        async def blocked(*args):
            nonlocal active
            active += 1
            if active == 3:
                entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                active -= 1

        try:
            with patch.object(service, "_perform_doh_query", side_effect=blocked):
                task = asyncio.create_task(service.query_a_record("example.com"))
                await entered.wait()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await service._queries.close()
                self.assertEqual(active, 0)
        finally:
            await service.close()
