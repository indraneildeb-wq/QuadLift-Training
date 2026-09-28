"""`python -m oceanbridge.api [--port 8000] [--host 127.0.0.1] [--reload]` runs the FastAPI backend."""

import argparse

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser(description="OceanBridge REST API")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args()
    uvicorn.run("oceanbridge.api.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
