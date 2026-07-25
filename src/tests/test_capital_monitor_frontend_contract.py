from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
CAPITAL_MONITOR_SOURCE = (
    REPO_ROOT / 'frontend' / 'src' / 'views' / 'CapitalMonitor.vue'
).read_text(encoding='utf-8')


def test_chart_is_loaded_only_after_it_approaches_the_viewport():
    assert 'new IntersectionObserver' in CAPITAL_MONITOR_SOURCE
    assert "import('../utils/capitalChart')" in CAPITAL_MONITOR_SOURCE
    assert 'await observeChartVisibility()' in CAPITAL_MONITOR_SOURCE


def test_frontend_delegates_sampling_to_backend():
    assert 'selectedInterval' not in CAPITAL_MONITOR_SOURCE
    assert 'interval-selector' not in CAPITAL_MONITOR_SOURCE
    assert "params.set('metric', selectedChartMode.value)" in CAPITAL_MONITOR_SOURCE
    assert "params.set('interval'" not in CAPITAL_MONITOR_SOURCE


def test_history_requests_are_cancelled_and_cached_by_query():
    assert 'historyAbortController?.abort()' in CAPITAL_MONITOR_SOURCE
    assert 'requestId !== historyRequestId' in CAPITAL_MONITOR_SOURCE
    assert 'historyCache.get(cacheKey)' in CAPITAL_MONITOR_SOURCE
    assert 'HISTORY_CACHE_TTL_MS = 60_000' in CAPITAL_MONITOR_SOURCE
    request_version = CAPITAL_MONITOR_SOURCE.index('const requestId = ++historyRequestId')
    cache_lookup = CAPITAL_MONITOR_SOURCE.index('const cached = historyCache.get(cacheKey)')
    assert request_version < cache_lookup


def test_chart_selection_changes_do_not_reload_capital_overview():
    watcher = "watch([selectedExchange, selectedChartMode], () => {\n  void fetchHistory()\n})"
    assert watcher in CAPITAL_MONITOR_SOURCE
    assert 'selectedWindow.value = window\n  void fetchHistory()' in CAPITAL_MONITOR_SOURCE
