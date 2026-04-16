"""
tests/test_tools.py
───────────────────
Quick smoke-tests for every Hannah data source.
Run before recording your demo to confirm all APIs are live.

Usage:
    python tests/test_tools.py
    python tests/test_tools.py pubmed          # test one source only
    python tests/test_tools.py biorxiv arxiv   # test multiple
"""

import asyncio
import sys
import os

# Allow running from repo root without installing the package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hannah.tools import (
    get_arxiv_papers,
    get_biorxiv_preprints,
    get_biotech_news,
    get_ema_updates,
    get_fda_updates,
    get_pubmed_papers,
    get_topic_summary,
)

PASS = "✅"
FAIL = "❌"
SKIP = "⏭ "
SEP  = "─" * 60


async def run_test(name: str, coro) -> bool:
    print(f"\n{SEP}")
    print(f"Testing: {name}")
    print(SEP)
    try:
        result = await coro
        if result and len(result) > 20:
            # Print first 500 chars so output stays readable
            preview = result[:500] + ("…" if len(result) > 500 else "")
            print(preview)
            print(f"\n{PASS} PASSED – got {len(result)} chars")
            return True
        else:
            print(f"Result was empty or too short: {result!r}")
            print(f"\n{FAIL} FAILED – unexpectedly empty response")
            return False
    except Exception as exc:
        print(f"Exception: {exc}")
        print(f"\n{FAIL} FAILED – {type(exc).__name__}: {exc}")
        return False


TESTS = {
    "pubmed": lambda: get_pubmed_papers(
        query="AI drug discovery machine learning", max_results=3
    ),
    "biorxiv": lambda: get_biorxiv_preprints(
        query="single cell RNA sequencing", server="biorxiv", max_results=3
    ),
    "arxiv": lambda: get_arxiv_papers(
        query="deep learning protein structure", category="q-bio", max_results=3
    ),
    "fda_news": lambda: get_fda_updates(search_type="news", max_results=3),
    "fda_approvals": lambda: get_fda_updates(
        search_type="drug_approvals", max_results=3
    ),
    "ema": lambda: get_ema_updates(max_results=3),
    "biotech_news": lambda: get_biotech_news(source="all", max_results=5),
    "topic_summary": lambda: get_topic_summary(
        topic="CRISPR gene editing", include_papers=True,
        include_preprints=True, include_news=True
    ),
}


async def main(filter_names: list[str]) -> None:
    print("\n🧪 Hannah Tool Tests")
    print("=" * 60)

    to_run = {
        k: v for k, v in TESTS.items()
        if not filter_names or k in filter_names
    }

    if not to_run:
        print(f"No matching tests for: {filter_names}")
        print(f"Available: {', '.join(TESTS.keys())}")
        return

    results: dict[str, bool] = {}
    for name, factory in to_run.items():
        results[name] = await run_test(name, factory())

    # Summary
    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print("=" * 60)
    for name, passed in results.items():
        icon = PASS if passed else FAIL
        print(f"  {icon}  {name}")

    passed_count = sum(results.values())
    total = len(results)
    print(f"\n{passed_count}/{total} tests passed.")

    if passed_count < total:
        print("\nFailed tests may indicate:")
        print("  • RSS feed temporarily down (try again later)")
        print("  • FDA API key not set (drug_approvals uses openFDA)")
        print("  • Network connectivity issue")
        sys.exit(1)


if __name__ == "__main__":
    filter_names = [a.lower() for a in sys.argv[1:]]
    asyncio.run(main(filter_names))
