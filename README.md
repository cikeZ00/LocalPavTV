## ⚠️ **ARCHIVED:** This project has been replaced by [PavlovReplayToolbox](https://github.com/cikeZ00/PavlovReplayToolbox). ⚠️
No further updates will be made to this repository.

# LocalPavTV

This is a system that allows you to record and play back Pavlov VR competitive games at a later time.

## Deployment steps:

You need to setup docker.

### Automatic setup

Clone the repositoery.

Run: ``docker compose -f docker-compose-full.yml up``
You'll need to have the following ports unused: 80, 443, 3000, 4000


### Manual setup
Clone this repository and run ``sudo docker compose up -d``, this will build the frontend and mitm images and start the containers.

You can access the frontend api on port ``3000``.


Generate SSL Certs
```
openssl genrsa -out fake-root.key 2048
openssl req -x509 -new -nodes -key fake-root.key -sha256 -days 1024 -out fake-root.crt -subj "/CN=Fake Root CA"

openssl genrsa -out pav.key 2048
openssl req -new -key pav.key -out pav.csr -subj "/CN=tv.vankrupt.net"

openssl x509 -req -in pav.csr -CA fake-root.crt -CAkey fake-root.key -CAcreateserial -out pav.crt -days 1024 -sha256
```

You're going to need nginx configured to reverse proxy the mitm service to ``tv.vankrupt.net`` (replace ``mitm_ip`` with the IP address of the machine where mitm is running):
```
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 80;
    server_name tv.vankrupt.net;

    # These shouldn't need to be changed
    return 301 https://$server_name$request_uri;
    
    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

}

server {
    server_name tv.vankrupt.net;

    # Ensure these lines point to your SSL certificate and key
    ssl_certificate path_to_fake_cert;
    ssl_certificate_key path_to_fake_key;


    # These shouldn't need to be changed
    listen 443;
    proxy_set_header Referer $http_referer;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Real-Port $remote_port;
    proxy_set_header X-Forwarded-Host $host:$remote_port;
    proxy_set_header X-Forwarded-Server $host;
    proxy_set_header X-Forwarded-Port $remote_port;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Ssl on;
    
    # Change mitm_ip
    location / {
        proxy_pass http://mitm_ip:4000;
    }
}
```

Replace ``path_to_fake_cert`` and ``path_to_fake_key`` with the path to wherever you placed the fake certificate and key we generated earlier.


### PavlovTV setup (Windows)

Use the api on port ``3000`` to list replays and download them.

Download a proxy server, I used Charles.

Import the fake root certificate into the proxy.

Enable DNS Spoofing and spoof ``tv.vankrupt.net`` to point to your mitm servers IP.

Open up PavlovTV, it should now purely make requests to our local mitm server.


