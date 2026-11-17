"""Serve one independently deployed result-verification plugin."""
import argparse
from http.server import HTTPServer

from artifact_verification import Handler, PLUGINS


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", choices=PLUGINS, required=True)
    parser.add_argument("--root", default="/artifacts")
    parser.add_argument("--port", type=int, default=8095)
    args = parser.parse_args()
    if args.plugin == "image-check":
        from image_check import inspect
    else:
        from digest_check import inspect
    with HTTPServer(("0.0.0.0", args.port), Handler) as server:
        server.plugin, server.root, server.inspect = args.plugin, args.root, inspect
        server.serve_forever()
