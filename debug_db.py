import asyncio
import aiosqlite

async def check():
    db = await aiosqlite.connect("data/app.db")
    db.row_factory = aiosqlite.Row

    cur = await db.execute("SELECT COUNT(*) as c FROM items")
    row = await cur.fetchone()
    print(f"Total items: {row['c']}")

    print("\nTop 15 cheapest (in_stock=1):")
    cur = await db.execute(
        "SELECT nm_id, name, price_rub, stock_qty FROM items "
        "WHERE in_stock=1 ORDER BY price_rub ASC LIMIT 15"
    )
    rows = await cur.fetchall()
    for r in rows:
        print(f"  {r['price_rub']:>10.0f} | qty={str(r['stock_qty']):>5s} | {r['nm_id']} | {r['name'][:80]}")

    print("\nTop 10 cheapest (price >= 9000):")
    cur = await db.execute(
        "SELECT nm_id, name, price_rub, stock_qty FROM items "
        "WHERE in_stock=1 AND price_rub >= 9000 ORDER BY price_rub ASC LIMIT 10"
    )
    rows = await cur.fetchall()
    for r in rows:
        print(f"  {r['price_rub']:>10.0f} | qty={str(r['stock_qty']):>5s} | {r['nm_id']} | {r['name'][:80]}")

    await db.close()

asyncio.run(check())
