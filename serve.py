"""
Production launcher. Run this instead of app.py for day-to-day use.
Flask's built-in server (used by `python app.py`) prints a warning that
it isn't meant for production; this uses waitress instead, which handles
several people connecting at once comfortably on a Raspberry Pi.
"""
from waitress import serve
from app import app

if __name__ == "__main__":
    serve(app, host="0.0.0.0", port=5000, threads=8)
