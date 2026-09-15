#!/bin/sh
# certbot --deploy-hook script: certbot runs this ONLY when a certificate
# was actually renewed, never on a no-op check - so this doesn't reload
# nginx on every timer firing, only on the (rare, ~every ~60 days) days a
# renewal genuinely happened. See docs/aws-deployment.md "HTTPS status" for
# the full renewal design and why a deploy-hook (not a fixed post-hook) was
# chosen.
#
# nginx caches the certificate file contents in memory at startup/reload -
# it does not reread /etc/letsencrypt on every TLS handshake - so a reload
# is required for a renewed cert to actually take effect. `nginx -s reload`
# is a graceful reload (existing connections finish on the old worker
# process; new connections get the new cert), not a restart - no dropped
# connections, no downtime.
set -e
docker exec retriva-nginx-1 nginx -s reload
echo "$(date -u +%FT%TZ) certbot deploy-hook: nginx reloaded after cert renewal" >> /var/log/retriva-certbot-renew.log
