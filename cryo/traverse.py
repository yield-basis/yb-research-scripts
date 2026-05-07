"""Print every YB market reachable from the Factory."""
from yb import all_markets, factory_address

print(f"Factory: {factory_address()}")

markets = all_markets()
print(f"\n{len(markets)} market(s):")
for m in markets:
    print(f"\n  [{m.idx}]")
    print(f"    asset_token   {m.asset_token}")
    print(f"    cryptopool    {m.cryptopool}")
    print(f"    amm           {m.amm}")
    print(f"    lt            {m.lt}    # 'Controller'")
    print(f"    staker        {m.staker}    # LiquidityGauge")
    print(f"    price_oracle  {m.price_oracle}")
    print(f"    virtual_pool  {m.virtual_pool}")
