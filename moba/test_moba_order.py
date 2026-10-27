import argparse, asyncio, logging, os, sys, uuid
from dotenv import load_dotenv
from moba_order import MobaOrderer, OrderRequest

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--product-id", type=int, required=True)
    p.add_argument("--qty", type=int, default=1)
    p.add_argument("--price", type=int, required=True)
    p.add_argument("--live", action="store_true")
    p.add_argument("--request-id", type=str, default=None)
    p.add_argument("--comment", type=str, default="")
    a = p.parse_args()
    load_dotenv()
    login, password = os.getenv("MOBA_LOGIN"), os.getenv("MOBA_PASSWORD")
    if not login or not password:
        print("Не заданы MOBA_LOGIN / MOBA_PASSWORD в .env", file=sys.stderr)
        return 2
    req = OrderRequest(request_id=a.request_id or f"test-{uuid.uuid4().hex[:12]}",
                       product_id=a.product_id, quantity=a.qty, expected_price=a.price,
                       title_hint=f"product {a.product_id}", comment=a.comment)
    o = MobaOrderer(login=login, password=password, account_name="test", dry_run=not a.live)
    try:
        await o.start()
        r = await o.place_order(req)
    finally:
        await o.close()
    print("=== OrderResult ===")
    for k in ("status", "order_number", "total", "message", "screenshot_path"):
        print(f"{k:16} {getattr(r, k)}")
    return 0 if r.status in ("placed", "dry_run_ok") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
