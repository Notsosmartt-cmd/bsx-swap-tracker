"""Import path for the scripts in the sub-folders of python/collectors/ (layout of 2026-11-18, see README.md).

The live collectors and the libraries they share stay directly in python/collectors/ because the systemd units and scheduled jobs point at those
paths.  Everything run by hand lives in a themed sub-folder.  The scripts still import each other by plain module name; a moved script puts
python/collectors/ on sys.path and imports this module, which adds every sub-folder.  Module names are unique across the folders.
"""
import os
import sys

COLLECTORS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(COLLECTORS)
SUBDIRS = ("studies", "economics", "offers", "chain", "fresh_maker", "synthetic", "utils", "tests", "futures")
for _d in [COLLECTORS] + [os.path.join(COLLECTORS, _s) for _s in SUBDIRS]:
    if _d not in sys.path:
        sys.path.insert(1, _d)          # after the folder of the script that is running
