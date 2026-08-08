from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MOBILE_SOURCE = (
    REPO_ROOT / 'frontend' / 'src' / 'views' / 'MobileCapitalMonitor.vue'
).read_text(encoding='utf-8')
ROUTER_SOURCE = (
    REPO_ROOT / 'frontend' / 'src' / 'router' / 'index.ts'
).read_text(encoding='utf-8')
APP_SOURCE = (REPO_ROOT / 'frontend' / 'src' / 'App.vue').read_text(encoding='utf-8')


def test_mobile_capital_route_is_standalone_and_linked_below_capital_menu():
    assert "path: '/mobile/capital'" in ROUTER_SOURCE
    assert "meta: { standalone: true }" in ROUTER_SOURCE
    assert 'isStandalonePage' in APP_SOURCE
    assert '<router-view v-if="isShelllessPage" />' in APP_SOURCE
    capital_menu = APP_SOURCE.index('index="/capital"')
    mobile_menu = APP_SOURCE.index('index="/mobile/capital"')
    reconciliation_menu = APP_SOURCE.index('index="/reconciliation"')
    assert capital_menu < mobile_menu < reconciliation_menu
    assert '<template #title>移动端</template>' in APP_SOURCE


def test_mobile_capital_only_requests_required_monitoring_data():
    for endpoint in (
        '/api/trading/capital/latest',
        '/api/trading/capital/gate-cross-risk/live',
        '/api/trading/capital/annualized-return?days=7',
        '/api/trading/reconciliation/latest',
        '&exchange=total&metric=equity_usdt',
        '&exchange=total&metric=daily_return',
    ):
        assert endpoint in MOBILE_SOURCE
    for label in ('总计', '总资产', '今日已实现', '可用资金', 'BNB 可用', '全仓 MMR', '已实现年化', '对账情况', '总资产曲线', '每日收益'):
        assert label in MOBILE_SOURCE


def test_mobile_capital_layout_places_total_first_and_gate_mmr_inside_gate_card():
    total_card = MOBILE_SOURCE.index('<section class="total-card"')
    exchange_grid = MOBILE_SOURCE.index('<section class="exchange-grid"')
    assert total_card < exchange_grid
    assert 'v-if="exchange === \'gate\'" class="secondary-metric gate-mmr-metric"' in MOBILE_SOURCE
    assert 'class="highlight-grid"' not in MOBILE_SOURCE


def test_mobile_capital_repeats_gate_mmr_beside_total_equity():
    total_grid_start = MOBILE_SOURCE.index('<div class="total-primary-grid">')
    total_grid_end = MOBILE_SOURCE.index('<div class="total-secondary-grid">', total_grid_start)
    total_grid = MOBILE_SOURCE[total_grid_start:total_grid_end]
    assert total_grid.index('<span>总资产</span>') < total_grid.index('<span>Gate 全仓 MMR</span>')
    assert 'formatPercent(displayedMmr, 1)' in total_grid
    assert MOBILE_SOURCE.count('formatPercent(displayedMmr, 1)') == 2


def test_mobile_capital_chart_supports_one_three_seven_thirty_and_ninety_days():
    for days in (1, 3, 7, 30, 90):
        assert f"value: {days}, label: '{days}天'" in MOBILE_SOURCE
    assert 'grid-template-columns: repeat(5, minmax(0, 1fr));' in MOBILE_SOURCE


def test_mobile_capital_uses_iphone_safe_areas_and_touch_targets():
    assert 'env(safe-area-inset-top)' in MOBILE_SOURCE
    assert 'env(safe-area-inset-bottom)' in MOBILE_SOURCE
    assert 'width: 44px' in MOBILE_SOURCE
    assert 'height: 44px' in MOBILE_SOURCE
    assert 'min-height: 44px' in MOBILE_SOURCE
    assert 'touch-action: manipulation' in MOBILE_SOURCE


def test_mobile_capital_has_persistent_notification_bell_and_mobile_sheet():
    assert 'aria-label="查看推送消息"' in MOBILE_SOURCE
    assert 'notificationUnreadCount > 99' in MOBILE_SOURCE
    assert "readStatus: 'all'" in MOBILE_SOURCE
    assert 'syncRecent' in MOBILE_SOURCE
    assert 'markMobileNotificationRead(item)' in MOBILE_SOURCE
    assert 'markAllMobileNotificationsRead' in MOBILE_SOURCE
    assert 'class="notification-sheet"' in MOBILE_SOURCE
    assert 'env(safe-area-inset-bottom)' in MOBILE_SOURCE


def test_mobile_capital_exposes_guarded_fund_transfer_and_bnb_actions():
    assert 'aria-label="资金操作"' in MOBILE_SOURCE
    assert '@click="openMobileFundTransfer"' in MOBILE_SOURCE
    assert '@click="buyMobileBnb"' in MOBILE_SOURCE
    assert '/api/trading/capital/fund-transfer/limits' in MOBILE_SOURCE
    assert '/api/trading/capital/fund-transfer/preflight?amount=' in MOBILE_SOURCE
    assert "post('/api/trading/capital/fund-transfer'" in MOBILE_SOURCE
    assert "post('/api/trading/capital/binance-bnb/buy'" in MOBILE_SOURCE

    preflight = MOBILE_SOURCE.index('const preview = await preflightMobileFundTransfer(amount)')
    transfer_post = MOBILE_SOURCE.index("post('/api/trading/capital/fund-transfer'")
    assert preflight < transfer_post
    assert '请输入当前登录密码' in MOBILE_SOURCE
    assert 'number < 5' in MOBILE_SOURCE
    assert 'number > 200' in MOBILE_SOURCE
    assert 'number > available' in MOBILE_SOURCE
    assert "activeFundTransfer ? `划转中 #${activeFundTransfer.id}`" in MOBILE_SOURCE


def test_mobile_capital_places_actions_beside_title_and_summarizes_reconciliation():
    title_row_start = MOBILE_SOURCE.index('<div class="mobile-title-row">')
    title_row_end = MOBILE_SOURCE.index('</div>', MOBILE_SOURCE.index('class="mobile-title-actions"'))
    title_row = MOBILE_SOURCE[title_row_start:title_row_end]
    assert title_row.index('<h1>资金监控</h1>') < title_row.index('aria-label="资金操作"')
    assert '<section class="mobile-fund-actions"' not in MOBILE_SOURCE

    annualized_metric = MOBILE_SOURCE.index('<span>已实现年化</span>')
    reconciliation_metric = MOBILE_SOURCE.index('<span>对账情况</span>')
    assert annualized_metric < reconciliation_metric
    assert "matched: '一致'" in MOBILE_SOURCE
    assert "mismatched: '不一致'" in MOBILE_SOURCE
    assert "reconciliationRows.value.every" in MOBILE_SOURCE
    assert 'grid-template-columns: repeat(3, minmax(0, 1fr));' in MOBILE_SOURCE
