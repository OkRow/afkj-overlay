import os, sys
if getattr(sys, "frozen", False):
    ROOT = os.path.dirname(sys.executable)
else:
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
USER = os.path.join(ROOT, "userdata")
CONFIG = os.path.join(ROOT, "config.json")
os.makedirs(USER, exist_ok=True)
