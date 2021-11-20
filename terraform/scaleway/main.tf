variable "bucket" {
  default = ""
}
terraform {
  required_providers {
    scaleway = {
      source = "scaleway/scaleway"
    }
    cloudflare = {
      source = "cloudflare/cloudflare"
      version = "~> 3.0"
    }
  }
  required_version = ">= 0.13"

  backend "s3" {
    bucket                      = var.bucket
    key                         = "infrastructure/scaleway.tfstate"
    region                      = "nl-ams"
    endpoint                    = "https://s3.nl-ams.scw.cloud"
    skip_credentials_validation = true
    skip_region_validation      = true
  }
}

resource "scaleway_object_bucket" "ip_state" {
  name = "tv.pavlovhosting.com-ip-state"
  acl = "private"
}

resource "scaleway_object_bucket" "replay_files" {
  name = "tv.pavlovhosting.com-replay-files"
  acl = "public-read"
}