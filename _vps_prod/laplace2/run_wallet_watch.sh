#!/bin/sh
# wallet reconciliation watcher (2026-08-06): poll bafather.uk to collect
# registered-wallet USDT transfers. Auth reuses the crypto poller secret.
. /opt/laplace2/.env.crypto
curl -s -m 60 -H "x-payments-secret: $PAYMENTS_SECRET" https://www.bafather.uk/api/cron/wallet-watch
echo ""
