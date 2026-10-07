# Offline demo of the HARP profiling pages

Open `profiling.html` or `results.html` in a browser — no server or TAPIS needed.
Log in with any username and password. The pages link to each other.

They are copies of `../../profiling.html` and `../../results.html` with sample data
(`demo_mock.js`) built in. After changing either real page, rebuild them:

```bash
python3 DEMO/ui/build_demo.py
```

The real pages are served by the HARP server at `/profiling` and `/results`.
