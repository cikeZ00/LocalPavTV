# tv.pavlovhosting.com

This is a system that allows you to record and play back Pavlov VR competitive games at a later time.

To make this work you override your computers DNS records so that tv.pavlov-vr.com points towards the recorder server (see packer/mitm.tv.pavlovhosting.com)

You can then take a .pavlovtv file from the system frontend (see packer/tv.pavlovhosting.com and containers/frontend) and re-upload it to the server

When you do so, the system puts the file in an S3 bucket, marks your IP address as wanting to replay that file and then when you open Pavlov TV the system will intercept the connection (as you've pointed DNS towards us) and serve just that file you uploaded

Files are encrypted before being given to users, this is so that users don't have to worry about file reputation as long as they trust this service.

Demo: https://tv.pavlovhosting.com/docs
