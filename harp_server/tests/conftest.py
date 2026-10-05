import os
import sys

os.environ.setdefault("HARP_SERVER_NO_DEFAULT_APP", "1")
HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "job_runner")))
