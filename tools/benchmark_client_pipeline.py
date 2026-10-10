#!/usr/bin/env python3
"""Offline Rule-Bot benchmark. All HTTP/DNS and GitHub operations are fixtures."""

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import resource
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
import dns.message
import dns.rdatatype
import dns.rrset
from aiohttp import web
from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data_manager import DataManager
from src.handlers.handler_manager import HandlerManager
from src.services.deferred_domain_service import DeferredDomainService
from src.services.dns_service import DNSService
from src.services.domain_checker import DomainChecker
from src.services.github_service import GitHubService
from src.services.rule_bot_client_api import ListenerConfig, RuleBotClientAPIServer
from src.services.rule_bot_client_token_service import RuleBotClientTokenService

logger.remove()
logging.disable(logging.CRITICAL)


class RepositoryFixture:
    def __init__(self, delay):
        self.content = "# 以下域名待提交 PR\n" + "\n".join(f"DOMAIN-SUFFIX,known-{i}.com" for i in range(4096)) + "\n"
        self.sha = "blob-0"
        self.reads = self.writes = 0
        self.delay = delay

    def get_contents(self, *args, **kwargs):
        self.reads += 1
        content, sha = self.content, self.sha
        time.sleep(self.delay)
        return SimpleNamespace(content=base64.b64encode(content.encode()).decode(), sha=sha)

    def update_file(self, path, message, content, sha, **kwargs):
        assert sha == self.sha, "fixture write lost revision ordering"
        time.sleep(self.delay)
        self.writes += 1
        self.content, self.sha = content, f"blob-{self.writes}"
        return {"commit": SimpleNamespace(sha=f"commit-{self.writes}"), "content": SimpleNamespace(sha=self.sha)}


async def bounded_batch(count, concurrency, operation):
    semaphore = asyncio.Semaphore(concurrency)
    latencies, results = [], []

    async def one(index):
        async with semaphore:
            start = time.perf_counter()
            result = await operation(index)
            latencies.append((time.perf_counter() - start) * 1000)
            results.append(result)

    start = time.perf_counter()
    cpu = time.process_time()
    await asyncio.gather(*(one(i) for i in range(count)))
    elapsed = time.perf_counter() - start
    ordered = sorted(latencies)
    return results, {
        "requests": count, "concurrency": concurrency, "seconds": elapsed,
        "requests_per_second": count / elapsed, "cpu_seconds": time.process_time() - cpu,
        "p50_ms": ordered[len(ordered)//2], "p95_ms": ordered[min(len(ordered)-1, int(len(ordered)*.95))],
        "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }


async def close_github(service):
    if hasattr(service, "aclose"):
        await service.aclose()
    else:
        service.close()


async def run(args):
    with tempfile.TemporaryDirectory(prefix="codex-rulebot-benchmark-") as directory:
        config = SimpleNamespace(
            GITHUB_TOKEN="fixture", GITHUB_REPO="fixture/repo", GITHUB_BRANCH="main",
            DIRECT_RULE_FILE="rules.list", GITHUB_COMMIT_NAME="Fixture", GITHUB_COMMIT_EMAIL="fixture@example.com",
            GITHUB_FILE_CACHE_SIZE=4, GITHUB_FILE_CACHE_TTL=60,
            RULE_BOT_CLIENT_PRIVATE_API_RATE_LIMIT_PER_HOUR=1000,
            RULE_BOT_CLIENT_COMMUNITY_API_RATE_LIMIT_PER_HOUR=50,
            DATA_DIR=directory, GEOSITE_CACHE_SIZE=2048, GEOSITE_CACHE_TTL=3600,
        )
        with patch.object(GitHubService, "_initialize_repo"):
            github = GitHubService(config)
        repository = github.repo = RepositoryFixture(args.upstream_ms/1000)
        deferred = DeferredDomainService(Path(directory)/"deferred.sqlite3", None)
        counts = Counter()
        original_connect = deferred._connect

        def queue_connection():
            counts["queue_connections"] += 1
            return original_connect()

        deferred._connect = queue_connection
        try:
            if args.case == "github_reads":
                async def read(index):
                    result = await github.check_domain_in_rules(f"known-{index % 4096}.com")
                    assert result["exists"]
                    return "exists_rules"
                values, result = await bounded_batch(args.requests, args.concurrency, read)
            elif args.case == "github_writes":
                async def write(index):
                    result = await github.add_domain_to_rules(f"new-{index}.com", "Fixture")
                    assert result["success"]
                    return "added"
                values, result = await bounded_batch(args.requests, args.concurrency, write)
                assert repository.writes == args.requests
                for index in range(args.requests):
                    assert repository.content.count(f"DOMAIN-SUFFIX,new-{index}.com\n") == 1
            elif args.case == "deferred_miss":
                async def miss(index):
                    assert not await deferred.contains(f"new-{index}.com", "rule_bot_client_community")
                    return "not_deferred"
                values, result = await bounded_batch(args.requests, args.concurrency, miss)
            else:
                values, result = await api_case(args, config, github, deferred, directory, counts)
            result.update({"case": args.case, "statuses": dict(Counter(values)),
                           "github_reads": repository.reads, "github_writes": repository.writes,
                           "queue_connections": counts["queue_connections"], "dns_requests": counts["dns_requests"],
                           "distinct_input_domains": 1 if args.case == "community_duplicate" else args.requests,
                           "semantic_digest": hashlib.sha256(json.dumps(dict(sorted(Counter(values).items()))).encode()).hexdigest(),
                           "fixture_upstream_ms": args.upstream_ms})
            return result
        finally:
            await close_github(github)


async def api_case(args, config, github, deferred, directory, counts):
    async def dns_fixture(request):
        counts["dns_requests"] += 1
        encoded = request.query["dns"]
        query = dns.message.from_wire(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        response = dns.message.make_response(query)
        question = query.question[0]
        if question.rdtype == dns.rdatatype.NS:
            response.answer.append(dns.rrset.from_text(question.name, 60, "IN", "NS", "ns1.provider.invalid.", "ns2.provider.invalid."))
        else:
            response.answer.append(dns.rrset.from_text(question.name, 60, "IN", "A", "198.51.100.17"))
        await asyncio.sleep(args.upstream_ms/1000)
        return web.Response(body=response.to_wire(), content_type="application/dns-message")

    dns_app = web.Application()
    dns_app.router.add_get("/dns-query", dns_fixture)
    dns_runner = web.AppRunner(dns_app, access_log=None)
    await dns_runner.setup()
    resolvers = {}
    for index in range(3):
        site = web.TCPSite(dns_runner, "127.0.0.1", 0)
        await site.start()
        resolvers[str(index)] = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/dns-query"
    dns_service = DNSService(resolvers, resolvers)
    await dns_service.start()
    data = DataManager(config)
    data.geosite_domains = {f"geosite-{i}.cn" for i in range(10000)}
    tokens = RuleBotClientTokenService(Path(directory)/"tokens.sqlite3", "fixture-signing-key", 30)
    token_values = []
    for user_id in range(min(args.concurrency, 128)):
        await tokens.consent(user_id)
        token_values.append((await tokens.issue(user_id))["token"])
    geoip = SimpleNamespace(get_location_info=lambda ip:{"is_china":False, "country_name":"Fixture", "country_code":"US"},
                            is_strict_china_ip=lambda ip:False)
    manager = HandlerManager.__new__(HandlerManager)
    manager.config, manager.github_service, manager.data_manager = config, github, data
    manager.domain_checker, manager.deferred_domains = DomainChecker(dns_service, geoip), deferred
    manager.rule_bot_client_token_service = tokens
    manager.user_add_history, manager._last_history_cleanup = defaultdict(list), 0
    manager.MAX_ADDS_PER_HOUR = 50
    api = RuleBotClientAPIServer(config, manager)
    listener = ListenerConfig("community", "127.0.0.1", 0, "/fixture-submit", "rule_bot_client_community")
    runner = web.AppRunner(api._build_app(listener), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/fixture-submit"
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=args.concurrency)) as client:
            async def request(index):
                if args.case == "community_existing":
                    domain = f"known-{index % 4096}.com"
                elif args.case == "community_duplicate":
                    domain = "foreign-fixture.com"
                else:
                    domain = f"foreign-{index}.com"
                async with client.post(url, json={"version":1,"domain":domain}, headers={"Authorization":"Bearer "+token_values[index % len(token_values)]}) as response:
                    body = await response.json()
                    assert response.status == 200, (response.status, body)
                    return body["status"]
            values, result = await bounded_batch(args.requests, args.concurrency, request)
        assert manager.github_service.repo.writes == 0
        return values, result
    finally:
        await runner.cleanup()
        await dns_service.close()
        await dns_runner.cleanup()
        await data.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=["github_reads", "github_writes", "deferred_miss", "community_existing", "community_unique", "community_duplicate"], required=True)
    parser.add_argument("--requests", type=int, default=512)
    parser.add_argument("--concurrency", type=int, default=64)
    parser.add_argument("--upstream-ms", type=float, default=10)
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 128 or not 1 <= args.requests <= 4096:
        parser.error("bounded harness requires concurrency 1..128 and requests 1..4096")
    if args.case.startswith("community") and (args.requests + args.concurrency - 1) // args.concurrency > 50:
        parser.error("community workload must stay within the existing 50 requests per user/hour")
    print(json.dumps(asyncio.run(run(args)), sort_keys=True))


if __name__ == "__main__":
    main()
