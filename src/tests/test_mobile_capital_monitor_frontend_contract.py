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
        '/api/trading/capital/annualized-return?days=',
        '&exchange=total&metric=equity_usdt',
        '&exchange=total&metric=daily_return',
    ):
        assert endpoint in MOBILE_SOURCE
    for label in ('总资产', '可用资金', 'BNB 可用', 'Gate 全仓 MMR', '已实现年化', '总资产曲线', '每日收益'):
        assert label in MOBILE_SOURCE


def test_mobile_capital_uses_iphone_safe_areas_and_touch_targets():
    assert 'env(safe-area-inset-top)' in MOBILE_SOURCE
    assert 'env(safe-area-inset-bottom)' in MOBILE_SOURCE
    assert 'width: 44px' in MOBILE_SOURCE
    assert 'height: 44px' in MOBILE_SOURCE
    assert 'min-height: 44px' in MOBILE_SOURCE
    assert 'touch-action: manipulation' in MOBILE_SOURCE
