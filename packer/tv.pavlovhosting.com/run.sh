#!/bin/bash

set -e

certbot certonly --standalone --preferred-challenges http -d tv.pavlovhosting.com --email tv.pavlovhosting.com.letsencrypt@lucy.sh --agree-tos -n
rsync /etc/letsencrypt/live/tv.pavlovhosting.com/ /root/secrets/ -a --copy-links -v
docker login rg.fr-par.scw.cloud/tv.pavlovhosting.com-containers -u nologin -p $SCW_SECRET_KEY
docker run --env-file /root/environment.txt -v /root/secrets:/secrets -p 443:443 rg.fr-par.scw.cloud/tv.pavlovhosting.com-containers/frontend:latest