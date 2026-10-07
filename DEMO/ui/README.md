# Offline demo of the HARP profiling page

Open `profiling.html` in a browser. No server or TAPIS is needed: log in with any username and password.
It runs on sample apps, systems, sweeps and data (`demo_mock.js`).

It is a copy of `../../profiling.html` with the sample data built in. After changing the real page, rebuild it:

```bash
python3 DEMO/ui/build_demo.py
```

The real page is served by the HARP server at `/profiling`.
