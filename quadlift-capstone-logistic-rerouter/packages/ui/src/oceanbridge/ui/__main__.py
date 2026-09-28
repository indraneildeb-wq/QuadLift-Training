"""`python -m oceanbridge.ui [streamlit options]` runs the dashboard, e.g. `python -m oceanbridge.ui --server.port 8502`."""

import sys
from pathlib import Path

from streamlit.web import cli as stcli


def main() -> None:
    sys.argv = ["streamlit", "run", str(Path(__file__).with_name("app.py")), *sys.argv[1:]]
    sys.exit(stcli.main())


if __name__ == "__main__":
    main()
