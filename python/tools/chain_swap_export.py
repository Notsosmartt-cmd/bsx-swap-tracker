"""Nightly immutable copy of the valuable chain-scan tables as Parquet (redundancy and modelling input; added 2026-11-16).

  python3 python/tools/chain_swap_export.py            (also run by the nightly job)

Reads <data_dir>/chainscan.sqlite READ-ONLY and writes, atomically (temp file then rename), whole-table snapshots:
  data/parquet/chain_swap_events/all.parquet   every row of swap_events, all tiers (the recognised swap events; JSON columns kept as text)
  data/parquet/chain_swap_blocks/all.parquet   every row of blocks (which heights were scanned, hash, time, tx count): proves coverage
  data/parquet/chain_swap_events/_last_export.json   counts and time of the last export
script_spends (the big raw table, re-derivable from the chains inside their windows) and p2sh_outputs (a rolling lookup index) are NOT exported.
Chunked (50,000 rows), a few hundred MB at most. Nothing is deleted from the database (deep history, no age cap).
"""
import json
import os
import sqlite3
import sys
import time

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(ROOT) if os.path.basename(ROOT) == "python" else ROOT   # public layout: <repo>/python/collectors/...
DB = os.path.join(ROOT, "data", "analysis", "chainscan", "chainscan.sqlite")
OUT = os.path.join(ROOT, "data", "parquet")
TYPES = {"INTEGER": pa.int64(), "REAL": pa.float64(), "TEXT": pa.string(), "BLOB": pa.binary()}


def export(db, table, out_dir):
    cols = [(r[1], (r[2] or "TEXT").upper()) for r in db.execute("PRAGMA table_info(%s)" % table)]
    schema = pa.schema([(n, TYPES.get(t, pa.string())) for n, t in cols])
    os.makedirs(out_dir, exist_ok=True)
    dest = os.path.join(out_dir, "all.parquet")
    tmp = dest + ".tmp"
    n = 0
    cur = db.execute("SELECT %s FROM %s" % (", ".join(c for c, _ in cols), table))
    with pq.ParquetWriter(tmp, schema, compression="zstd") as w:
        while True:
            rows = cur.fetchmany(50000)
            if not rows:
                break
            w.write_table(pa.table({name: pa.array([r[i] for r in rows], type=schema.field(i).type) for i, (name, _) in enumerate(cols)}, schema=schema))
            n += len(rows)
    os.replace(tmp, dest)
    return n, os.path.getsize(dest)


def main():
    if not os.path.exists(DB):
        print("no chain-scan database yet")
        return 0
    db = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=60)
    t0 = time.time()
    info = {}
    for table, name in (("swap_events", "chain_swap_events"), ("blocks", "chain_swap_blocks")):
        n, size = export(db, table, os.path.join(OUT, name))
        info[table] = {"rows": n, "bytes": size}
        print("%s: %d rows, %.1f MB" % (name, n, size / 1e6))
    db.close()
    info["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    info["seconds"] = round(time.time() - t0, 1)
    with open(os.path.join(OUT, "chain_swap_events", "_last_export.json"), "w") as f:
        json.dump(info, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
