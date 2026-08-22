#!/bin/bash
set -a; . /opt/laplace2/.env.crypto; set +a
cd /opt/laplace2
/usr/bin/python3 /opt/laplace2/crypto_payment_poller.py >> /opt/laplace2/crypto_poller.log 2>&1
