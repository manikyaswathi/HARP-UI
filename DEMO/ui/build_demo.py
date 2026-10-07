#!/usr/bin/env python3
"""
Build the offline demo of the HARP profiling pages.

Copies ../../profiling.html into this folder with
demo_mock.js inlined, so they open straight from disk (no server, no TAPIS)
and link to each other. Re-run after changing either page:

    python3 DEMO/ui/build_demo.py
"""

import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

BANNER = '''<div class="demo-bar" role="note">
  <b>Demo</b> &middot; Sample data, not connected to TAPIS. Log in with any username and password.
</div>
<div class="toast" id="demoToast" role="status"></div>
'''
STYLE = '''<style>
.demo-bar{background:var(--orange-50);color:var(--orange-700);font-size:13px;text-align:center;
  padding:8px 16px;border-bottom:1px solid #F1DCC2}
.demo-bar b{font-family:"JetBrains Mono",monospace;font-size:11px;letter-spacing:.06em;
  text-transform:uppercase;margin-right:2px}
</style>
'''


def build(name):
    page = open(os.path.join(ROOT, name)).read()
    mock = open(os.path.join(HERE, "demo_mock.js")).read()
    # the TAPIS systems the demo shows: an export of real system definitions (replace the file to update them)
    with open(os.path.join(HERE, "systems.json")) as f:
        systems = f.read()
    mock = "window.__HARP_SYSTEMS__ = " + systems.strip() + ";\n" + mock
    assert page.count("<body>") == 1 and page.count("</head>") == 1
    page = page.replace("</head>", STYLE + "<script>\n" + mock + "</script>\n</head>", 1)
    page = page.replace("<body>", "<body>\n" + BANNER, 1)
    page = page.replace("<title>", "<title>Demo: ", 1)
    out = os.path.join(HERE, name)
    with open(out, "w") as f:
        f.write(page)
    print("wrote", os.path.relpath(out, ROOT))


if __name__ == "__main__":
    build("profiling.html")
