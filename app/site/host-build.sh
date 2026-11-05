#!/usr/bin/env bash
# The build the host runs from main, and the check runs the same way.
#
# Builds the site for SITE_URL, the address the host sets (the manual check
# reads it from the repository variable DOCS_SITE_URL; a local run without
# it builds for http://localhost/). Checks every link, asset, anchor and
# search target, then lays the result out under the URL's path in
# app/site/deploy/, with a redirect from the root when there is a path: the
# site may be served at a path of a shared domain that forwards the path to
# the host unchanged, and the homepage and the 404 page use absolute paths
# under the site URL, so the host serves the site under the same path.
#
# usage: host-build.sh            output in app/site/deploy/
set -euo pipefail
cd "$(dirname "$0")/../.."
export SITE_URL="${SITE_URL:-http://localhost/}"
python3 -m pip install --quiet -r app/site/requirements.txt
python3 app/site/build.py
python3 app/site/check.py app/site/dist --site-url "$SITE_URL"
path="$(python3 -c 'import os; from urllib.parse import urlsplit; print(urlsplit(os.environ["SITE_URL"]).path.strip("/"))')"
rm -rf app/site/deploy
mkdir -p "app/site/deploy/$path"
cp -r app/site/dist/. "app/site/deploy/$path/"
if [ -n "$path" ]; then printf '/ /%s/ 302\n' "$path" > app/site/deploy/_redirects; fi
echo "Laid out in app/site/deploy/${path:+$path/}"
