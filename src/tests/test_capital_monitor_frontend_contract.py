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
    watcher = "watch(selectedChartMode, () => {\n  void fetchHistory()\n})"
    assert watcher in CAPITAL_MONITOR_SOURCE
    assert 'selectedWindow.value = window\n  void fetchHistory()' in CAPITAL_MONITOR_SOURCE
    assert 'selectedExchange' not in CAPITAL_MONITOR_SOURCE


def test_gate_summary_prioritizes_current_mmr_outside_exchange_card():
    assert '<span>重点摘要</span>' not in CAPITAL_MONITOR_SOURCE
    assert 'Gate 风险重点' not in CAPITAL_MONITOR_SOURCE
    assert '<span>当前全仓MMR</span>' in CAPITAL_MONITOR_SOURCE
    current_mmr = CAPITAL_MONITOR_SOURCE.index('<span>当前全仓MMR</span>')
    minimum_mmr = CAPITAL_MONITOR_SOURCE.index('近7天最低MMR')
    assert current_mmr < minimum_mmr

    gate_card_start = CAPITAL_MONITOR_SOURCE.index('<div v-else class="gate-summary-risk">')
    gate_card_end = CAPITAL_MONITOR_SOURCE.index(
        '<div v-if="showSummaryDetails" class="metric-row">',
        gate_card_start,
    )
    gate_card = CAPITAL_MONITOR_SOURCE[gate_card_start:gate_card_end]
    assert '维持保证金' in gate_card
    assert '全仓MMR' not in gate_card


def test_bnb_balance_lives_in_toolbar_and_top_cards_share_available_separator():
    toolbar_start = CAPITAL_MONITOR_SOURCE.index('<div class="toolbar">')
    toolbar_end = CAPITAL_MONITOR_SOURCE.index('</div>\n\n    <div class="summary-grid">', toolbar_start)
    toolbar = CAPITAL_MONITOR_SOURCE[toolbar_start:toolbar_end]

    assert 'class="toolbar-bnb"' in toolbar
    assert 'latestByExchange.binance?.bnb_available_usdt' in toolbar
    assert 'latestByExchange.binance?.bnb_available,' not in toolbar
    assert '买BNB' in toolbar
    assert 'class="metric-row usage-row"' in CAPITAL_MONITOR_SOURCE
    assert '.usage-row {' in CAPITAL_MONITOR_SOURCE


def test_annualized_return_defaults_to_seven_days_and_supports_all_periods():
    assert 'selectedAnnualizedPeriod = ref<AnnualizedPeriod>(7)' in CAPITAL_MONITOR_SOURCE
    assert '/api/trading/capital/annualized-return?days=${period}' in CAPITAL_MONITOR_SOURCE
    for label in ('1天', '3天', '7天', '1个月', '3个月', '半年', '1年'):
        assert f"label: '{label}'" in CAPITAL_MONITOR_SOURCE
    assert '已有 ${summary.available_days} / ${summary.period_days} 天有效数据' in CAPITAL_MONITOR_SOURCE
    assert '当日已实现' in CAPITAL_MONITOR_SOURCE
    assert '策略表现' in CAPITAL_MONITOR_SOURCE
    assert '今日开仓/平仓' in CAPITAL_MONITOR_SOURCE
    assert 'formatOpenCloseCount(annualizedReturn?.today_opened_count, annualizedReturn?.today_closed_count)' in CAPITAL_MONITOR_SOURCE
    assert 'class="insight-metric-grid performance-metric-grid"' in CAPITAL_MONITOR_SOURCE


def test_mmr_and_gate_risk_share_card_beside_annualized_card():
    grid_start = CAPITAL_MONITOR_SOURCE.index('<div class="gate-risk-review-grid">')
    grid_end = CAPITAL_MONITOR_SOURCE.index(
        '<div v-if="gateRiskPanelError"',
        grid_start,
    )
    summary_grid = CAPITAL_MONITOR_SOURCE[grid_start:grid_end]

    current_mmr = summary_grid.index('当前全仓MMR')
    annualized = summary_grid.index('策略表现')
    minimum_mmr = summary_grid.index('近7天最低MMR')
    priority_asset = summary_grid.index('首平候选')
    assert current_mmr < minimum_mmr < priority_asset < annualized
    assert 'grid-template-columns: repeat(2, minmax(0, 1fr));' in CAPITAL_MONITOR_SOURCE
    assert 'class="insight-card gate-risk-overview"' in summary_grid
    assert 'class="insight-card annualized-summary"' in summary_grid
    assert 'class="insight-metric-grid"' in summary_grid
    assert summary_grid.count('class="insight-metric"') >= 6
    assert 'annualized-period-select' in summary_grid
    assert 'gatePriorityAsset' in summary_grid
    assert 'gateProfitReleaseAsset' in summary_grid
    assert 'gateProfitReleaseCandidate.floating_pnl_usdt' not in summary_grid
    assert '.priority-candidate-list {' in CAPITAL_MONITOR_SOURCE
    assert 'class="priority-candidate-item priority-risk"' in summary_grid
    assert 'class="priority-candidate-item priority-profit"' in summary_grid
    assert 'MMR ≤ 300%' in summary_grid
    assert 'MMR ≤ 350%' in summary_grid
    assert '.priority-risk {' in CAPITAL_MONITOR_SOURCE
    assert '.priority-profit {' in CAPITAL_MONITOR_SOURCE
    assert '.priority-candidate-main {' in CAPITAL_MONITOR_SOURCE


def test_mmr_summary_values_are_rendered_without_decimal_places():
    assert 'function formatWholePercent' in CAPITAL_MONITOR_SOURCE
    assert '{{ formatWholePercent(gateCrossRisk.account_mmr_pct) }}' in CAPITAL_MONITOR_SOURCE
    assert '{{ formatWholePercent(recentMinimumGateRisk?.account_mmr_pct) }}' in CAPITAL_MONITOR_SOURCE


def test_capital_trend_uses_total_only_and_removes_deprecated_chart_tabs():
    chart_start = CAPITAL_MONITOR_SOURCE.index('<div ref="chartPanelRef" class="chart-panel">')
    chart_end = CAPITAL_MONITOR_SOURCE.index('<el-dialog', chart_start)
    chart_panel = CAPITAL_MONITOR_SOURCE[chart_start:chart_end]

    assert 'chart-window-select' in chart_panel
    assert chart_panel.index('class="chart-window-select"') < chart_panel.index('class="metric-selector"')
    assert "label: '1小时'" not in CAPITAL_MONITOR_SOURCE
    assert "label: '3小时'" not in CAPITAL_MONITOR_SOURCE
    assert "label: '12小时'" not in CAPITAL_MONITOR_SOURCE
    assert "label: '3天'" in CAPITAL_MONITOR_SOURCE
    assert 'exchange-selector' not in chart_panel
    assert "selectedChartMode.value === 'gate_cross_risk' ? 'gate' : 'total'" in CAPITAL_MONITOR_SOURCE
    assert "{ key: 'unrealized_pnl_usdt', label: '未实现盈亏' }" not in CAPITAL_MONITOR_SOURCE
    assert "{ key: 'gross_total_pnl_usdt', label: '总盈亏' }" not in CAPITAL_MONITOR_SOURCE
    assert "const gateCrossRiskMetrics: ChartMetric[] = [\n  'gate_cross_mmr_pct',\n]" in CAPITAL_MONITOR_SOURCE


def test_gate_mmr_help_describes_auto_funding_and_tiered_close_rules():
    help_start = CAPITAL_MONITOR_SOURCE.index('<div class="mmr-help">')
    help_end = CAPITAL_MONITOR_SOURCE.index('</el-popover>', help_start)
    help_content = CAPITAL_MONITOR_SOURCE[help_start:help_end]

    for threshold in ('500%', '350%', '300%', '200%', '100%'):
        assert threshold in help_content
    for rule in (
        'Gate维持保证金 × 7 - Gate账户权益',
        'Binance Forward 可用资金的 70%',
        'max(总资产 × 2%, 50 USDT)',
        'max(100 USDT, Gate维持保证金 × 50%, 交易所最低额)',
        '一次只退出一个本地完整套利仓位',
        '关闭正向开仓只停止新自动任务',
        '最近一次自动划转评估明确为“可划资金不足”',
        '只生成一张一次性许可',
        '领取期间人工/自动划转均被阻止',
        '拒单或异常会释放预约但不立即重算',
        '在得到新的“仍然不足”结论前不会继续释放',
    ):
        assert rule in help_content
    assert 'max-height: min(560px, calc(100vh - 96px));' in CAPITAL_MONITOR_SOURCE
    assert "boundary: 'viewport'" in CAPITAL_MONITOR_SOURCE
    assert 'overflow-y: auto;' in CAPITAL_MONITOR_SOURCE
