# src/famly_fetch/gui/bootstrap.py
import sys
from pathlib import Path

def main():
    try:
        from streamlit.web import bootstrap as st_bootstrap
    except Exception:
        from streamlit import bootstrap as st_bootstrap
    here = Path(__file__).resolve().parent
    app_script = str(here / "app.py")
    return st_bootstrap.run(app_script, "", [], {})

if __name__ == "__main__":
    sys.exit(main())
