"""Smoke test: fetch ERC-20 Transfer logs for WETH over a small block range."""
import os
from dotenv import load_dotenv
import cryo

load_dotenv()

WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
TRANSFER = "Transfer(address indexed from, address indexed to, uint256 value)"

df = cryo.collect(
    "logs",
    blocks=["22400000:22400050"],
    contract=[WETH],
    event_signature=TRANSFER,
    rpc=os.environ["ETH_RPC_URL"],
    output_format="polars",
    hex=True,
    no_verbose=True,
)

print(f"rows: {len(df)}")
print(f"columns: {df.columns}")
print(df.head(5))
