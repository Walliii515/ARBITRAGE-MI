from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MOBILE_SOURCE = (
    REPO_ROOT / 'frontend' / 'src' / 'views' / 'MobileCapitalMonitor.vue'
).read_text(encoding='utf-8')
ROUTER_SOURCE = (
    REPO_ROOT / 'frontend' / 'src' / 'router' / 'index.ts'
).read_text(encoding='utf-8')
APP_SOURCE = (REPO_ROOT / 'frontend' / 'src' / 'App.vue').read_text(encoding='utf-8')


def test_mobile_capital_route_is_standalone_and_not_added_to_menu():
    assert "path: '/mobile/capital'" in ROUTER_SOURCE
    assert "meta: { standalone: true }" in ROUTER_SOURCE
    assert 'isStandalonePage' in APP_SOURCE
    assert '<router-view v-if="isShelllessPage" />' in APP_SOURCE
    assert 'index="/mobile/capital"' not in APP_SOURCE


def test_mobile_capital_only_requests_required_monitoring_data():
    for endpoint in (
        '/api/trading/capital/latest',
        '/api/trading/capital/gate-cross-risk/live',
        '/api/trading/capital/annualized-return?days=7',
        '&exchange=total&metric=equity_usdt',
        '&exchange=total&metric=daily_return',
    ):
        assert endpoint in MOBILE_SOURCE
    for label in ('总计', '总资产', '今日已实现', '可用资金', 'BNB 可用', '全仓 MMR', '已实现年化', '总资产曲线', '每日收益'):
        assert label in MOBILE_SOURCE


def test_mobile_capital_layout_places_total_first_and_gate_mmr_inside_gate_card():
    total_card = MOBILE_SOURCE.index('<section class="total-card"')
    exchange_grid = MOBILE_SOURCE.index('<section class="exchange-grid"')
    assert total_card < exchange_grid
    assert 'v-if="exchange === \'gate\'" class="secondary-metric gate-mmr-metric"' in MOBILE_SOURCE
    assert 'class="highlight-grid"' not in MOBILE_SOURCE


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
