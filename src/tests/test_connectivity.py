# coding: utf-8
"""
交易所 API 连通性测试脚本

验证 .env 中配置的 Testnet API Key 是否有效、签名是否正确、网络是否可达。
无需启动 FastAPI 服务，直接调用 RealExecutor.test_connectivity()。

使用方式：
    cd src && python -m tests.test_connectivity

输出：
    ✓ Binance Testnet: 账户正常, 3 个有余额的资产
    ✓ Gate Testnet: 账户正常, 可用余额=10000
    或
    ✗ Binance Testnet: HTTP 401: {"code":-2014,"msg":"API-key format invalid."}
"""
import os
import sys

# 确保能找到项目模块
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from dotenv import load_dotenv
load_dotenv()

from calc.real_executor import RealExecutor, ExchangeConfig
from common.strategy_accounts import get_binance_credentials, get_gate_futures_credentials


def main():
    """运行连通性测试"""
    env = os.getenv('EXCHANGE_ENV', 'testnet').lower()
    print(f"\n{'='*60}")
    print(f"  交易所 API 连通性测试 (环境: {env})")
    print(f"{'='*60}\n")

    # 构建配置
    if env == 'mainnet':
        strategy = os.getenv('STRATEGY_ACCOUNT', 'forward').lower()
        binance_creds = get_binance_credentials(strategy, mainnet=True)
        gate_creds = get_gate_futures_credentials(strategy, mainnet=True)
        config = ExchangeConfig(
            binance_base_url='https://api.binance.com',
            binance_api_key=binance_creds.api_key,
            binance_api_secret=binance_creds.api_secret,
            gate_base_url='https://api.gateio.ws',
            gate_api_key=gate_creds.api_key,
            gate_api_secret=gate_creds.api_secret,
            env='mainnet',
        )
    else:
        config = ExchangeConfig(
            binance_base_url='https://testnet.binance.vision',
            binance_api_key=os.getenv('BINANCE_TESTNET_API_KEY', ''),
            binance_api_secret=os.getenv('BINANCE_TESTNET_API_SECRET', ''),
            gate_base_url='https://fx-api-testnet.gateio.ws',
            gate_api_key=os.getenv('GATE_FUTURES_TESTNET_API_KEY', ''),
            gate_api_secret=os.getenv('GATE_FUTURES_TESTNET_API_SECRET', ''),
            env='testnet',
        )

    # 打印配置摘要（Key 脱敏）
    print(f"  Binance URL:  {config.binance_base_url}")
    print(f"  Binance Key:  {config.binance_api_key[:8]}...{config.binance_api_key[-4:]}")
    print(f"  Gate URL:     {config.gate_base_url}")
    print(f"  Gate Key:     {config.gate_api_key[:8]}...{config.gate_api_key[-4:]}")
    print()

    # 执行测试
    executor = RealExecutor(config)
    result = executor.test_connectivity()

    # 输出结果
    all_ok = True

    # Binance
    b = result['binance']
    status = '✓' if b['ok'] else '✗'
    if not b['ok']:
        all_ok = False
    print(f"  {status} Binance ({env}): {b['msg']}")

    # Gate
    g = result['gate']
    status = '✓' if g['ok'] else '✗'
    if not g['ok']:
        all_ok = False
    print(f"  {status} Gate ({env}): {g['msg']}")

    print(f"\n{'='*60}")
    if all_ok:
        print("  ✅ 所有交易所 API 连通正常！可以启动 RealExecutor 服务。")
    else:
        print("  ❌ 存在连接问题，请检查 API Key 和网络。")
    print(f"{'='*60}\n")

    return 0 if all_ok else 1


if __name__ == '__main__':
    sys.exit(main())
