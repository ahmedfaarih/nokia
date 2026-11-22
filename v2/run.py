"""Command-line entry point - this is what a weekly scheduled job would call.

    python run.py                       # all .xlsx in ./data, results in ./output
    python run.py --input my.xlsx       # one or more specific files
    python run.py --no-llm              # skip the optional Claude layer
"""
import argparse
import glob
import time
from pathlib import Path

from engine.pipeline import kpis, run, save

ap = argparse.ArgumentParser()
ap.add_argument("--input", nargs="*", default=None)
ap.add_argument("--out", default="output")
ap.add_argument("--no-llm", action="store_true")
a = ap.parse_args()

here = Path(__file__).parent
paths = a.input or sorted(glob.glob(str(here / "data" / "*.xlsx")))
t0 = time.time()
res = run(paths, use_llm=not a.no_llm)
save(res, here / a.out)
print(f"Processed {len(paths)} file(s) in {time.time() - t0:.1f}s")
for k, v in kpis(res["items"]).items():
    print(f"  {k:38} {v:,}" if isinstance(v, (int, float)) else f"  {k:38} {v}")
print(f"Results: {here / a.out / 'Reconciliation_Results.xlsx'}")
