#!/bin/sh
# Oversized uploads through the Next.js proxy must be refused with 413, every time.
#
# Run from a container on the Compose network, against a frontend built in proxy
# mode (API_PROXY_TARGET set):
#     sh scripts/upload_limit_probe.sh http://frontend:3000
#
# The body limit runs before authentication, so no account is needed: oversized
# requests must get 413, and a small one must reach the API (401 or 422 from the
# API itself, not a proxy error).
# Regression check for two causes of intermittent 500s: the API closing on a body
# declared past its drain bound, and Next forwarding Expect: 100-continue (curl
# sends it for any large body) so the API refused without reading while Next wrote.
set -u
BASE=${1:?usage: upload_limit_probe.sh <frontend base url>}
TRIES=${TRIES:-5}
URL="$BASE/api/v1/organizations/00000000-0000-0000-0000-000000000000/documents"
DIR=$(mktemp -d)
trap 'rm -rf "$DIR"' EXIT
MiB=1048576
failures=0

status() {  # status <file> [curl args...]
  file=$1; shift
  curl -s -o /dev/null -w '%{http_code}' --max-time 120 "$@" \
    -F "file=@$file;type=text/plain" "$URL"
}

for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$URL")
  case $code in 401|422) break ;; esac
  sleep 2
done
echo "control (empty POST, no session): $code (want 401 or 422 from the API)"
case $code in 401|422) ;; *) failures=$((failures + 1)) ;; esac

head -c 1024 /dev/zero > "$DIR/small.txt"
code=$(status "$DIR/small.txt")
echo "control (1 KiB upload, no session): $code (want 401 or 422 from the API)"
case $code in 401|422) ;; *) failures=$((failures + 1)) ;; esac

for size in 26 40 100; do
  head -c $((size * MiB)) /dev/zero > "$DIR/big.txt"
  for mode in expect no-expect chunked; do
    case $mode in
      expect) args="" ;;  # curl's default for large bodies: Expect: 100-continue
      no-expect) args="-H Expect:" ;;  # what browsers send
      chunked) args="-H Expect: -H Transfer-Encoding:chunked" ;;
    esac
    codes=""
    for i in $(seq 1 "$TRIES"); do
      # shellcheck disable=SC2086
      code=$(status "$DIR/big.txt" $args)
      codes="$codes $code"
      [ "$code" = 413 ] || failures=$((failures + 1))
    done
    echo "${size} MiB ${mode}:${codes} (want 413)"
  done
done

if [ "$failures" -ne 0 ]; then
  echo "FAILED: $failures response(s) were not as expected"
  exit 1
fi
echo "OK: every oversized upload got 413"
