variable scw_access_key {
  type = string
  default = env("SCW_ACCESS_KEY")
}

variable scw_secret_key {
  type = string
  default = env("SCW_SECRET_KEY")
}

variable scw_project_id {
  type = string
  default = env("SCW_PROJECT_ID")
}

variable scw_zone {
  type = string
  default = "nl-ams-1"
}

variable image_name {
  type = string
  default = env("IMAGE_NAME")
}

variable private_key {
  type = string
  default = env("PRIVATE_KEY")
}

variable bucket_region {
  type = string
  default = env("BUCKET_REGION")
}

variable ip_state_bucket_name {
  type = string
  default = env("IP_STATE_BUCKET_NAME")
}

variable replay_files_bucket_name {
  type = string
  default = env("REPLAY_FILES_BUCKET_NAME")
}

variable files_for_download_bucket_name {
  type = string
  default = env("FILES_FOR_DOWNLOAD_BUCKET_NAME")
}

source "scaleway" "debian" {
  access_key = "${var.scw_access_key}"
  secret_key = "${var.scw_secret_key}"
  project_id = "${var.scw_project_id}"
  zone = "${var.scw_zone}"
  image = "debian_bullseye"
  commercial_type = "DEV1-S"
  ssh_username = "root"
  image_name = "${var.image_name}"
}

build {

  sources = ["source.scaleway.debian"]

  provisioner "shell" {
    inline = [
      "apt update && apt upgrade -y",
      "apt install certbot rsync -y"
    ]
  }


  provisioner "shell" {
    script = "./install-docker.sh"
  }

  provisioner "file" {
    source = "./environment.txt"
    destination = "/tmp/environment.txt"
  }

  provisioner "shell" {
    environment_vars = [
      "SCW_ACCESS_KEY=${var.scw_access_key}",
      "SCW_SECRET_KEY=${var.scw_secret_key}",
      "PRIVATE_KEY=${var.private_key}",
      "BUCKET_REGION=${var.bucket_region}",
      "IP_STATE_BUCKET_NAME=${var.ip_state_bucket_name}",
      "REPLAY_FILES_BUCKET_NAME=${var.replay_files_bucket_name}",
      "FILES_FOR_DOWNLOAD_BUCKET_NAME=${var.files_for_download_bucket_name}"
    ]
    inline = ["envsubst < /tmp/environment.txt > /root/environment.txt"]
  }

  provisioner "file" {
    source = "./frontend.service"
    destination = "/etc/systemd/system/frontend.service"
  }

  provisioner "file" {
    source = "./run.sh"
    destination = "/root/run.sh"
  }

  provisioner "shell" {
    inline = ["chmod +x /root/run.sh"]
  }

  provisioner "shell" {
    inline = ["systemctl enable frontend && systemctl start frontend"]
  }

}
