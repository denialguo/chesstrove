#!/bin/sh
# Zip the python-chess package uv installed (the uv.lock pin) for the in-browser indexer: web/public/py.
set -eu
cd "$(dirname "$0")/.."
version="$(uv run python -c 'import chess; print(chess.__version__)')"
site="$(uv run python -c 'import chess, pathlib; print(pathlib.Path(chess.__file__).parent.parent)')"
out="$PWD/web/public/py/python-chess-$version.zip"
rm -f web/public/py/python-chess-*.zip
(cd "$site" && find chess -name '*.py' -o -name 'py.typed' | sort | TZ=UTC zip -q -X -D "$out" -@)
cp "$site/chess-$version.dist-info/licenses/LICENSE.txt" web/public/py/python-chess-COPYING.txt
echo "$out"
